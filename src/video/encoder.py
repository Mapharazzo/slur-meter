"""Safe ffmpeg frame-sequence encoder with atomic final promotion."""

from __future__ import annotations

import asyncio
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any


class EncodingError(RuntimeError):
    """A deterministic encoder failure with a bounded safe diagnostic tail."""

    def __init__(self, message: str, *, stderr_tail: str = "") -> None:
        super().__init__(message)
        self.stderr_tail = stderr_tail


class FFmpegEncoder:
    """Encode validated PNG frames while reporting ffmpeg's real frame counter."""

    _FRAME_RE = re.compile(r"^frame=(\d+)\s*$")
    _INTEGER_PROGRESS_KEYS = {
        "frame",
        "total_size",
        "out_time_us",
        "dup_frames",
        "drop_frames",
    }
    _INTEGER_PROGRESS_RE = re.compile(r"\d{1,20}\Z")
    _FPS_RE = re.compile(r"\d{1,6}(?:\.\d{1,6})?\Z")
    _BITRATE_RE = re.compile(r"(?:N/A|\d{1,12}(?:\.\d{1,6})?kbits/s)\Z")
    _SPEED_RE = re.compile(r"(?:N/A|\d{1,6}(?:\.\d{1,6})?x)\Z")
    _DIAGNOSTIC_PRIORITY = (
        "frame",
        "progress",
        "fps",
        "out_time_us",
        "total_size",
        "dup_frames",
        "drop_frames",
        "bitrate",
        "speed",
    )

    def __init__(
        self,
        *,
        ffmpeg: str = "ffmpeg",
        ffprobe: str = "ffprobe",
        fps: int = 30,
        preset: str = "medium",
        stderr_limit: int = 8192,
        popen: Callable[..., Any] = subprocess.Popen,
        probe_popen: Callable[..., Any] = subprocess.Popen,
        probe_timeout: float = 15.0,
        which: Callable[[str], str | None] = shutil.which,
        probe_duration: Callable[[Path], float | None] | None = None,
        sanitize: Callable[[str], str] | None = None,
    ) -> None:
        if fps < 1:
            raise ValueError("Encoder FPS must be positive")
        if stderr_limit < 1:
            raise ValueError("stderr limit must be positive")
        if probe_timeout <= 0:
            raise ValueError("Probe timeout must be positive")
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.fps = int(fps)
        self.preset = str(preset)
        self.stderr_limit = int(stderr_limit)
        self._popen = popen
        self._probe_popen = probe_popen
        self._probe_timeout = float(probe_timeout)
        self._which = which
        self._probe_duration = probe_duration
        self._sanitize = sanitize or (lambda text: text)

    def encode(
        self,
        frames: str | Path | Sequence[str | Path],
        audio: str | Path | None,
        output: str | Path,
        on_progress: Callable[[int], None] | None,
        cancel_requested: Callable[[], bool] | None,
    ) -> Path:
        """Encode to a unique partial and replace ``output`` only after validation."""
        executable = self._which(self.ffmpeg)
        if executable is None:
            raise EncodingError("ffmpeg executable was not found")
        pattern, total_frames = self._frame_input(frames)
        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        partial = output_path.with_name(
            f".{output_path.stem}.{uuid.uuid4().hex}.partial{output_path.suffix or '.mp4'}"
        )
        args = [
            executable,
            "-y",
            "-nostats",
            "-progress",
            "pipe:1",
            "-framerate",
            str(self.fps),
            "-i",
            pattern,
        ]
        if audio is not None:
            args.extend(["-i", str(audio)])
        args.extend(
            [
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-preset",
                self.preset,
            ]
        )
        if audio is not None:
            args.extend(["-c:a", "aac", "-shortest"])
        args.append(str(partial))

        if cancel_requested is not None and cancel_requested():
            raise asyncio.CancelledError("Encoding was cancelled")

        process = self._popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            shell=False,
        )
        stdout_queue: queue.Queue[str | None] = queue.Queue()
        stdout_thread = threading.Thread(
            target=_read_lines,
            args=(process.stdout, stdout_queue),
            name="ffmpeg-progress-reader",
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_drain_stream,
            args=(process.stderr,),
            name="ffmpeg-stderr-reader",
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        stdout_done = False
        cancelled = False
        last_frame = -1
        diagnostics: dict[str, int | float | str] = {}
        try:
            while not stdout_done:
                if cancel_requested is not None and cancel_requested():
                    cancelled = True
                    self._stop(process)
                    break
                try:
                    line = stdout_queue.get(timeout=0.05)
                except queue.Empty:
                    continue
                if line is None:
                    stdout_done = True
                    continue
                self._capture_progress_diagnostic(line.strip(), diagnostics)
                match = self._FRAME_RE.fullmatch(line.strip())
                if match is None:
                    continue
                frame = min(int(match.group(1)), total_frames)
                if frame != last_frame:
                    last_frame = frame
                    if on_progress is not None:
                        on_progress(frame)
            returncode = process.wait()
            stdout_thread.join(timeout=1)
            stderr_thread.join(timeout=1)
            if cancelled:
                raise asyncio.CancelledError("Encoding was cancelled")
            if returncode != 0:
                raise EncodingError(
                    f"ffmpeg exited with status {returncode}",
                    stderr_tail=self._render_progress_diagnostics(diagnostics),
                )
            if cancel_requested is not None and cancel_requested():
                raise asyncio.CancelledError("Encoding was cancelled")
            self._validate_output(
                partial,
                expected_frames=total_frames,
                cancel_requested=cancel_requested,
            )
            if cancel_requested is not None and cancel_requested():
                raise asyncio.CancelledError("Encoding was cancelled")
            os.replace(partial, output_path)
            return output_path
        except BaseException:
            if process.poll() is None:
                self._stop(process)
            partial.unlink(missing_ok=True)
            raise

    @classmethod
    def _capture_progress_diagnostic(
        cls,
        line: str,
        diagnostics: dict[str, int | float | str],
    ) -> None:
        key, separator, value = line.partition("=")
        if not separator:
            return
        if key in cls._INTEGER_PROGRESS_KEYS:
            if cls._INTEGER_PROGRESS_RE.fullmatch(value):
                diagnostics[key] = int(value)
            return
        if key == "fps":
            if cls._FPS_RE.fullmatch(value):
                diagnostics[key] = float(value)
            return
        if key == "bitrate":
            if cls._BITRATE_RE.fullmatch(value):
                diagnostics[key] = value
            return
        if key == "speed":
            if cls._SPEED_RE.fullmatch(value):
                diagnostics[key] = value
            return
        if key == "progress" and value in {"continue", "end"}:
            diagnostics[key] = value

    def _render_progress_diagnostics(
        self, diagnostics: dict[str, int | float | str]
    ) -> str:
        retained: dict[str, int | float | str] = {}
        for key in self._DIAGNOSTIC_PRIORITY:
            if key not in diagnostics:
                continue
            candidate = {**retained, key: diagnostics[key]}
            encoded = json.dumps(candidate, separators=(",", ":"), sort_keys=True)
            if len(encoded) <= self.stderr_limit:
                retained = candidate
        encoded = json.dumps(retained, separators=(",", ":"), sort_keys=True)
        return encoded if retained and len(encoded) <= self.stderr_limit else ""

    def _frame_input(
        self, frames: str | Path | Sequence[str | Path]
    ) -> tuple[str, int]:
        if isinstance(frames, str | Path):
            path = Path(frames)
            if path.is_dir():
                names = sorted(item.name for item in path.glob("*.png"))
                expected = [f"{index:05d}.png" for index in range(len(names))]
                if not names or names != expected:
                    raise EncodingError(
                        "Frame directory must contain a non-empty sequential PNG sequence"
                    )
                return str(path / "%05d.png"), len(names)
            raise EncodingError("Frames must be supplied as a validated directory")
        paths = [Path(item) for item in frames]
        if not paths:
            raise EncodingError("Frame sequence is empty")
        parents = {path.parent.resolve() for path in paths}
        expected = [f"{index:05d}.png" for index in range(len(paths))]
        if len(parents) != 1 or [path.name for path in paths] != expected:
            raise EncodingError("Frame sequence must use sequential generated names")
        return str(paths[0].parent / "%05d.png"), len(paths)

    def _validate_output(
        self,
        path: Path,
        *,
        expected_frames: int,
        cancel_requested: Callable[[], bool] | None,
    ) -> None:
        if not path.is_file() or path.stat().st_size <= 0:
            raise EncodingError("ffmpeg produced an empty output")
        expected_duration = expected_frames / self.fps
        duration: float | None = None
        frame_count: int | None = None
        if self._probe_duration is not None:
            try:
                value = self._probe_duration(path)
                duration = None if value is None else float(value)
            except (OSError, TypeError, ValueError) as exc:
                raise EncodingError(
                    f"Encoded output validation failed ({type(exc).__name__})"
                ) from exc
            if duration is None:
                raise EncodingError("Encoded output validation returned no media facts")
        else:
            probe = self._which(self.ffprobe)
            if probe is not None:
                try:
                    process = self._probe_popen(
                        [
                            probe,
                            "-v",
                            "error",
                            "-count_frames",
                            "-select_streams",
                            "v:0",
                            "-show_entries",
                            "stream=duration,nb_read_frames:format=duration",
                            "-of",
                            "json",
                            str(path),
                        ],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        shell=False,
                    )
                    deadline = time.monotonic() + self._probe_timeout
                    while process.poll() is None:
                        if cancel_requested is not None and cancel_requested():
                            self._stop(process)
                            raise asyncio.CancelledError("Encoding was cancelled")
                        if time.monotonic() >= deadline:
                            self._stop(process)
                            raise EncodingError("Encoded output validation timed out")
                        time.sleep(0.02)
                    if cancel_requested is not None and cancel_requested():
                        raise asyncio.CancelledError("Encoding was cancelled")
                    if process.wait() != 0:
                        raise EncodingError("Encoded output validation failed")
                    payload = json.loads(process.stdout.read())
                    streams = payload.get("streams") or []
                    stream = streams[0] if streams else {}
                    raw_duration = stream.get("duration") or (
                        payload.get("format") or {}
                    ).get("duration")
                    if raw_duration not in (None, "N/A"):
                        duration = float(raw_duration)
                    raw_frames = stream.get("nb_read_frames")
                    if raw_frames not in (None, "N/A"):
                        frame_count = int(raw_frames)
                    if duration is None and frame_count is None:
                        raise EncodingError(
                            "Encoded output validation returned no media facts"
                        )
                except asyncio.CancelledError:
                    raise
                except EncodingError:
                    raise
                except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise EncodingError(
                        f"Encoded output validation failed ({type(exc).__name__})"
                    ) from exc
        if frame_count is not None and frame_count != expected_frames:
            raise EncodingError("Encoded output frame count does not match its input")
        if duration is not None and duration <= 0:
            raise EncodingError("Encoded output has no positive duration")
        if duration is not None and abs(duration - expected_duration) > (1 / self.fps):
            raise EncodingError(
                "Encoded output duration does not match its frame count"
            )

    @staticmethod
    def _stop(process: Any) -> None:
        try:
            process.terminate()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
            process.wait(timeout=2)


def _read_lines(stream: Any, destination: queue.Queue[str | None]) -> None:
    try:
        if stream is not None:
            for line in stream:
                destination.put(line)
    finally:
        destination.put(None)


def _drain_stream(stream: Any) -> None:
    if stream is None:
        return
    while True:
        chunk = stream.read(4096)
        if not chunk:
            return
