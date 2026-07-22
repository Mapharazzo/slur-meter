"""Safe ffmpeg frame-sequence encoder with atomic final promotion."""

from __future__ import annotations

import asyncio
import os
import queue
import re
import shutil
import subprocess
import threading
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

    def __init__(
        self,
        *,
        ffmpeg: str = "ffmpeg",
        ffprobe: str = "ffprobe",
        fps: int = 30,
        preset: str = "medium",
        stderr_limit: int = 8192,
        popen: Callable[..., Any] = subprocess.Popen,
        which: Callable[[str], str | None] = shutil.which,
        probe_duration: Callable[[Path], float | None] | None = None,
        sanitize: Callable[[str], str] | None = None,
    ) -> None:
        if fps < 1:
            raise ValueError("Encoder FPS must be positive")
        if stderr_limit < 1:
            raise ValueError("stderr limit must be positive")
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.fps = int(fps)
        self.preset = str(preset)
        self.stderr_limit = int(stderr_limit)
        self._popen = popen
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
        # Keep a bounded raw window large enough for redaction patterns to retain
        # their identifying prefix. The public diagnostic is sanitized first and
        # only then truncated to ``stderr_limit``.
        stderr_tail = _BoundedTail(max(64 * 1024, self.stderr_limit * 16))
        stdout_thread = threading.Thread(
            target=_read_lines,
            args=(process.stdout, stdout_queue),
            name="ffmpeg-progress-reader",
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_read_tail,
            args=(process.stderr, stderr_tail),
            name="ffmpeg-stderr-reader",
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        stdout_done = False
        cancelled = False
        last_frame = -1
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
            safe_tail = self._safe_tail(stderr_tail.value)
            if returncode != 0:
                raise EncodingError(
                    f"ffmpeg exited with status {returncode}",
                    stderr_tail=safe_tail,
                )
            self._validate_output(partial)
            os.replace(partial, output_path)
            return output_path
        except BaseException:
            if process.poll() is None:
                self._stop(process)
            partial.unlink(missing_ok=True)
            raise

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

    def _validate_output(self, path: Path) -> None:
        if not path.is_file() or path.stat().st_size <= 0:
            raise EncodingError("ffmpeg produced an empty output")
        duration: float | None = None
        if self._probe_duration is not None:
            try:
                value = self._probe_duration(path)
                duration = None if value is None else float(value)
            except (OSError, TypeError, ValueError) as exc:
                raise EncodingError(
                    f"Encoded output validation failed ({type(exc).__name__})"
                ) from exc
        else:
            probe = self._which(self.ffprobe)
            if probe is not None:
                try:
                    result = subprocess.run(
                        [
                            probe,
                            "-v",
                            "error",
                            "-show_entries",
                            "format=duration",
                            "-of",
                            "default=noprint_wrappers=1:nokey=1",
                            str(path),
                        ],
                        check=True,
                        capture_output=True,
                        text=True,
                        shell=False,
                    )
                    duration = float(result.stdout.strip())
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    raise EncodingError(
                        f"Encoded output validation failed ({type(exc).__name__})"
                    ) from exc
        if duration is not None and duration <= 0:
            raise EncodingError("Encoded output has no positive duration")

    def _safe_tail(self, value: str) -> str:
        return self._sanitize(value)[-self.stderr_limit :]

    @staticmethod
    def _stop(process: Any) -> None:
        try:
            process.terminate()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
            process.wait(timeout=2)


class _BoundedTail:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.value = ""
        self._lock = threading.Lock()

    def append(self, chunk: str) -> None:
        with self._lock:
            self.value = (self.value + chunk)[-self.limit :]


def _read_lines(stream: Any, destination: queue.Queue[str | None]) -> None:
    try:
        if stream is not None:
            for line in stream:
                destination.put(line)
    finally:
        destination.put(None)


def _read_tail(stream: Any, destination: _BoundedTail) -> None:
    if stream is None:
        return
    while True:
        chunk = stream.read(4096)
        if not chunk:
            return
        destination.append(chunk)
