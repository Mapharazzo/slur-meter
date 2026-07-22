"""Audio mixer — combines AudioLayers into a single mixed track via ffmpeg.

Builds an ffmpeg filtergraph that:
  1. Loads each layer's audio file as an input
  2. Applies per-layer: volume, fade-in/out, trim, loop
  3. Applies ducking (lower music volume while TTS plays)
  4. Mixes all streams down to one stereo output
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .layers import AudioLayer, AudioTimeline


class AudioMixer:
    """Stateless mixer — give it a timeline, get a mixed file."""

    def __init__(
        self,
        sample_rate: int = 44100,
        *,
        popen: Callable[..., Any] = subprocess.Popen,
        timeout: float = 120.0,
    ):
        if timeout <= 0:
            raise ValueError("Audio mixer timeout must be positive")
        self.sample_rate = sample_rate
        self._popen = popen
        self._timeout = float(timeout)

    def mix(
        self,
        timeline: AudioTimeline,
        output_path: Path,
        *,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> Path:
        """Render *timeline* to a single audio file at *output_path*."""
        self._raise_if_cancelled(cancel_requested)
        layers = [
            layer for layer in timeline.layers if layer.file and layer.file.exists()
        ]
        if not layers:
            # No audio layers — produce silence for the video duration
            return self._make_silence(
                timeline.total_duration,
                output_path,
                cancel_requested=cancel_requested,
            )

        if len(layers) == 1:
            return self._mix_single(
                layers[0],
                timeline.total_duration,
                output_path,
                cancel_requested=cancel_requested,
            )

        return self._mix_multi(
            layers,
            timeline.total_duration,
            output_path,
            cancel_requested=cancel_requested,
        )

    # ── Single-layer shortcut ───────────────────

    def _mix_single(
        self,
        layer: AudioLayer,
        total_dur: float,
        output_path: Path,
        *,
        cancel_requested: Callable[[], bool] | None,
    ) -> Path:
        """Optimised path when there is only one audio layer."""
        cmd = ["ffmpeg", "-y"]
        cmd += ["-i", str(layer.file)]

        filters = self._layer_filters(layer, idx=0, total_dur=total_dur)
        pad = (
            f"[0:a]{';'.join(filters)}apad,atrim=0:{total_dur}"
            if filters
            else f"[0:a]apad,atrim=0:{total_dur}"
        )

        cmd += ["-af", pad]
        cmd += ["-c:a", "aac", "-b:a", "128k", "-ar", str(self.sample_rate)]
        partial = self._partial(output_path)
        cmd += [str(partial)]

        output_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._run(cmd, cancel_requested)
            self._raise_if_cancelled(cancel_requested)
            return self._promote(partial, output_path)
        finally:
            partial.unlink(missing_ok=True)

    # ── Multi-layer complex mix ─────────────────

    def _mix_multi(
        self,
        layers: list[AudioLayer],
        total_dur: float,
        output_path: Path,
        *,
        cancel_requested: Callable[[], bool] | None,
    ) -> Path:
        cmd = ["ffmpeg", "-y"]

        # Add inputs
        for layer in layers:
            cmd += ["-i", str(layer.file)]

        # Build filtergraph
        filter_parts: list[str] = []
        mix_inputs: list[str] = []

        for idx, layer in enumerate(layers):
            chain = self._build_layer_chain(layer, idx, total_dur)
            out_label = f"[a{idx}]"
            filter_parts.append(f"{chain}{out_label}")
            mix_inputs.append(out_label)

        # Ducking: find TTS layers that duck others
        duckers = [layer for layer in layers if layer.duck_others]
        if duckers:
            filter_parts, mix_inputs = self._apply_ducking(
                filter_parts, mix_inputs, layers, duckers, total_dur
            )

        # Final amix
        n = len(mix_inputs)
        inputs_str = "".join(mix_inputs)
        filter_parts.append(
            f"{inputs_str}amix=inputs={n}:duration=longest:dropout_transition=2,"
            f"atrim=0:{total_dur},aformat=sample_rates={self.sample_rate}[out]"
        )

        filtergraph = ";".join(filter_parts)
        cmd += ["-filter_complex", filtergraph]
        cmd += ["-map", "[out]"]
        cmd += ["-c:a", "aac", "-b:a", "128k"]
        partial = self._partial(output_path)
        cmd += [str(partial)]

        output_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._run(cmd, cancel_requested)
            self._raise_if_cancelled(cancel_requested)
            return self._promote(partial, output_path)
        finally:
            partial.unlink(missing_ok=True)

    # ── Per-layer filter chain builder ──────────

    def _build_layer_chain(self, layer: AudioLayer, idx: int, total_dur: float) -> str:
        """Return a filtergraph chain string for one layer (without output label)."""
        parts: list[str] = []

        # Loop if needed
        if layer.loop and layer.end:
            loop_dur = (layer.end or total_dur) - layer.start
            parts.append(f"aloop=loop=-1:size={int(loop_dur * self.sample_rate)}")

        # Volume
        if layer.volume != 1.0:
            parts.append(f"volume={layer.volume:.2f}")

        # Fade in
        if layer.fade_in > 0:
            parts.append(f"afade=t=in:st=0:d={layer.fade_in:.2f}")

        # Fade out
        if layer.fade_out > 0:
            end_time = (layer.end or total_dur) - layer.start
            fade_start = max(0, end_time - layer.fade_out)
            parts.append(f"afade=t=out:st={fade_start:.2f}:d={layer.fade_out:.2f}")

        # Delay to start position
        if layer.start > 0:
            delay_ms = int(layer.start * 1000)
            parts.append(f"adelay={delay_ms}|{delay_ms}")

        # Pad to total duration so amix doesn't cut short
        parts.append(f"apad,atrim=0:{total_dur}")

        chain = ",".join(parts)
        return f"[{idx}:a]{chain}"

    def _layer_filters(
        self, layer: AudioLayer, idx: int, total_dur: float
    ) -> list[str]:
        """Simple filter list for single-layer mode."""
        parts: list[str] = []
        if layer.volume != 1.0:
            parts.append(f"volume={layer.volume:.2f}")
        if layer.fade_in > 0:
            parts.append(f"afade=t=in:st=0:d={layer.fade_in:.2f}")
        if layer.fade_out > 0:
            end_time = (layer.end or total_dur) - layer.start
            fade_start = max(0, end_time - layer.fade_out)
            parts.append(f"afade=t=out:st={fade_start:.2f}:d={layer.fade_out:.2f}")
        if layer.start > 0:
            delay_ms = int(layer.start * 1000)
            parts.append(f"adelay={delay_ms}|{delay_ms}")
        return parts

    # ── Ducking via sidechaincompress ───────────

    def _apply_ducking(
        self,
        filter_parts: list[str],
        mix_inputs: list[str],
        layers: list[AudioLayer],
        duckers: list[AudioLayer],
        total_dur: float,
    ) -> tuple[list[str], list[str]]:
        """Replace duckable layer outputs with sidechained versions.

        For each music/sfx layer, if any TTS layer overlaps it and has
        duck_others=True, we apply a volume envelope that dips the music
        while the TTS plays.  Uses ffmpeg's volume filter with enable
        expressions for simplicity (no sidechain needed).
        """
        new_parts = list(filter_parts)
        new_inputs = list(mix_inputs)

        for layer_idx, layer in enumerate(layers):
            if layer.duck_others:
                continue  # don't duck the ducker

            # Collect all overlapping duck windows for this layer
            windows: list[tuple[float, float, float]] = []  # (start, end, vol)
            for ducker in duckers:
                d_start = ducker.start
                d_end = ducker.end or (d_start + 10.0)
                l_start = layer.start
                l_end = layer.end or total_dur
                if d_start < l_end and d_end > l_start:
                    windows.append((d_start, d_end, ducker.duck_amount))

            if not windows:
                continue

            # Chain filters so each step consumes the previous output label
            current_label = f"[a{layer_idx}]"
            for win_idx, (d_start, d_end, duck_vol) in enumerate(windows):
                is_last = win_idx == len(windows) - 1
                next_label = (
                    f"[a{layer_idx}d]" if is_last else f"[a{layer_idx}d{win_idx}]"
                )
                new_parts.append(
                    f"{current_label}"
                    f"volume=volume={duck_vol:.2f}"
                    f":enable='between(t,{d_start:.2f},{d_end:.2f})'"
                    f"{next_label}"
                )
                current_label = next_label

            # Swap in the final ducked label in mix_inputs
            base_label = f"[a{layer_idx}]"
            for j, inp in enumerate(new_inputs):
                if inp == base_label:
                    new_inputs[j] = current_label

        return new_parts, new_inputs

    # ── Silence fallback ────────────────────────

    def _make_silence(
        self,
        duration: float,
        output_path: Path,
        *,
        cancel_requested: Callable[[], bool] | None,
    ) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        partial = self._partial(output_path)
        try:
            self._run(
                [
                    "ffmpeg",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    f"anullsrc=r={self.sample_rate}:cl=stereo",
                    "-t",
                    str(duration),
                    "-c:a",
                    "aac",
                    "-b:a",
                    "128k",
                    str(partial),
                ],
                cancel_requested,
            )
            self._raise_if_cancelled(cancel_requested)
            return self._promote(partial, output_path)
        finally:
            partial.unlink(missing_ok=True)

    @staticmethod
    def _partial(output_path: Path) -> Path:
        return output_path.with_name(
            f".{output_path.stem}.{uuid.uuid4().hex}.partial{output_path.suffix}"
        )

    @staticmethod
    def _promote(partial: Path, output_path: Path) -> Path:
        if not partial.is_file() or partial.stat().st_size <= 0:
            raise RuntimeError("ffmpeg produced empty audio output")
        os.replace(partial, output_path)
        return output_path

    def _run(
        self,
        command: list[str],
        cancel_requested: Callable[[], bool] | None,
    ) -> None:
        process = self._popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
        )
        deadline = time.monotonic() + self._timeout
        while process.poll() is None:
            if cancel_requested is not None and cancel_requested():
                self._stop(process)
                raise asyncio.CancelledError("Audio mixing was cancelled")
            if time.monotonic() >= deadline:
                self._stop(process)
                raise subprocess.TimeoutExpired(command, self._timeout)
            time.sleep(0.02)
        returncode = process.wait()
        if returncode != 0:
            raise subprocess.CalledProcessError(returncode, command)

    @staticmethod
    def _raise_if_cancelled(
        cancel_requested: Callable[[], bool] | None,
    ) -> None:
        if cancel_requested is not None and cancel_requested():
            raise asyncio.CancelledError("Audio mixing was cancelled")

    @staticmethod
    def _stop(process: Any) -> None:
        try:
            process.terminate()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
            process.wait(timeout=2)
