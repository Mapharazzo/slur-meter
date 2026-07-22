import asyncio
import io
from pathlib import Path

import pytest

from src.video.encoder import EncodingError, FFmpegEncoder


class FakeProcess:
    def __init__(self, args, *, stdout="", stderr="", returncode=0, output=b"video"):
        self.args = args
        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO(stderr)
        self.returncode = returncode
        self.terminated = False
        self.killed = False
        Path(args[-1]).write_bytes(output)

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9


def _frames(tmp_path, count=3):
    directory = tmp_path / "frames"
    directory.mkdir()
    for index in range(count):
        (directory / f"{index:05d}.png").write_bytes(b"png")
    return directory


def test_encode_uses_argument_array_and_reports_real_ffmpeg_frames(tmp_path):
    calls = []

    def popen(args, **kwargs):
        calls.append((args, kwargs))
        return FakeProcess(
            args,
            stdout="frame=1\nprogress=continue\nframe=3\nprogress=end\n",
        )

    progress = []
    output = tmp_path / "final.mp4"
    encoder = FFmpegEncoder(
        ffmpeg="ffmpeg",
        popen=popen,
        which=lambda _name: "/fake/ffmpeg",
        probe_duration=lambda _path: 0.1,
    )

    result = encoder.encode(
        _frames(tmp_path),
        tmp_path / "audio.m4a",
        output,
        progress.append,
        lambda: False,
    )

    args, kwargs = calls[0]
    assert isinstance(args, list)
    assert args[0] == "/fake/ffmpeg"
    assert args[args.index("-progress") + 1] == "pipe:1"
    assert kwargs["shell"] is False
    assert kwargs["encoding"] == "utf-8"
    assert kwargs["errors"] == "replace"
    assert progress == [1, 3]
    assert result == output
    assert output.read_bytes() == b"video"


def test_failed_encode_preserves_last_validated_output_and_bounds_sanitized_stderr(
    tmp_path,
):
    output = tmp_path / "final.mp4"
    output.write_bytes(b"last-good")
    private = str(tmp_path / "private.mov")
    secret = "Bearer " + ("s" * 100)
    stderr = ("x" * 200) + private + "\n" + secret

    encoder = FFmpegEncoder(
        ffmpeg="ffmpeg",
        popen=lambda args, **_kwargs: FakeProcess(
            args, stderr=stderr, returncode=1, output=b"failed-partial"
        ),
        which=lambda _name: "/fake/ffmpeg",
        stderr_limit=64,
        sanitize=lambda text: text.replace(str(tmp_path), "[WORKSPACE]").replace(
            secret, "[TOKEN]"
        ),
    )

    with pytest.raises(EncodingError) as captured:
        encoder.encode(
            _frames(tmp_path),
            tmp_path / "audio.m4a",
            output,
            lambda _frame: None,
            lambda: False,
        )

    assert output.read_bytes() == b"last-good"
    assert len(captured.value.stderr_tail) <= 64
    assert str(tmp_path) not in captured.value.stderr_tail
    assert secret[-32:] not in captured.value.stderr_tail
    assert not list(tmp_path.glob("*.partial*.mp4"))


def test_missing_ffmpeg_is_rejected_before_process_start(tmp_path):
    encoder = FFmpegEncoder(
        ffmpeg="ffmpeg",
        popen=lambda *_args, **_kwargs: pytest.fail("process should not start"),
        which=lambda _name: None,
    )

    with pytest.raises(EncodingError, match="ffmpeg.*not found"):
        encoder.encode(
            _frames(tmp_path),
            None,
            tmp_path / "final.mp4",
            lambda _frame: None,
            lambda: False,
        )


def test_zero_byte_success_is_not_promoted(tmp_path):
    output = tmp_path / "final.mp4"
    output.write_bytes(b"last-good")
    encoder = FFmpegEncoder(
        ffmpeg="ffmpeg",
        popen=lambda args, **_kwargs: FakeProcess(args, output=b""),
        which=lambda _name: "/fake/ffmpeg",
    )

    with pytest.raises(EncodingError, match="empty"):
        encoder.encode(
            _frames(tmp_path),
            None,
            output,
            lambda _frame: None,
            lambda: False,
        )

    assert output.read_bytes() == b"last-good"


def test_cancellation_terminates_ffmpeg_and_preserves_previous_output(tmp_path):
    output = tmp_path / "final.mp4"
    output.write_bytes(b"last-good")
    process = None

    def popen(args, **_kwargs):
        nonlocal process
        process = FakeProcess(
            args,
            stdout="frame=1\nprogress=continue\nframe=2\nprogress=end\n",
            output=b"partial",
        )
        return process

    cancelled = False

    def on_progress(_frame):
        nonlocal cancelled
        cancelled = True

    encoder = FFmpegEncoder(
        ffmpeg="ffmpeg",
        popen=popen,
        which=lambda _name: "/fake/ffmpeg",
    )

    with pytest.raises(asyncio.CancelledError):
        encoder.encode(
            _frames(tmp_path),
            None,
            output,
            on_progress,
            lambda: cancelled,
        )

    assert process is not None and process.terminated
    assert output.read_bytes() == b"last-good"
    assert not list(tmp_path.glob("*.partial*.mp4"))
