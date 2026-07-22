import asyncio
import io
import json
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


class FakeProbeProcess:
    def __init__(self, payload, *, returncode=0, hanging=False):
        self.stdout = io.StringIO(json.dumps(payload))
        self.returncode = None if hanging else returncode
        self.terminated = False
        self.killed = False

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


def test_failed_encode_never_retains_raw_long_token_suffix(tmp_path):
    output = tmp_path / "staged.mp4"
    secret = "Bearer " + ("s" * 70000)
    encoder = FFmpegEncoder(
        ffmpeg="ffmpeg",
        popen=lambda args, **_kwargs: FakeProcess(
            args, stderr=secret, returncode=1, output=b"failed-partial"
        ),
        which=lambda _name: "/fake/ffmpeg",
        stderr_limit=128,
        sanitize=lambda text: text.replace(secret, "[TOKEN]"),
    )

    with pytest.raises(EncodingError) as captured:
        encoder.encode(
            _frames(tmp_path),
            None,
            output,
            lambda _frame: None,
            lambda: False,
        )

    assert captured.value.stderr_tail == ""
    assert "s" * 32 not in captured.value.stderr_tail


def test_truncated_success_is_rejected_against_expected_frame_duration(tmp_path):
    output = tmp_path / "staged.mp4"
    output.write_bytes(b"last-good")
    encoder = FFmpegEncoder(
        ffmpeg="ffmpeg",
        fps=10,
        popen=lambda args, **_kwargs: FakeProcess(args, output=b"truncated"),
        which=lambda _name: "/fake/ffmpeg",
        probe_duration=lambda _path: 0.2,
    )

    with pytest.raises(EncodingError, match="duration"):
        encoder.encode(
            _frames(tmp_path, count=10),
            None,
            output,
            lambda _frame: None,
            lambda: False,
        )

    assert output.read_bytes() == b"last-good"


def test_configured_probe_returning_no_duration_fails_closed(tmp_path):
    output = tmp_path / "staged.mp4"
    output.write_bytes(b"last-good")
    encoder = FFmpegEncoder(
        ffmpeg="ffmpeg",
        popen=lambda args, **_kwargs: FakeProcess(args, output=b"new-video"),
        which=lambda _name: "/fake/ffmpeg",
        probe_duration=lambda _path: None,
    )

    with pytest.raises(EncodingError, match="validation.*facts"):
        encoder.encode(
            _frames(tmp_path),
            None,
            output,
            lambda _frame: None,
            lambda: False,
        )

    assert output.read_bytes() == b"last-good"


def test_late_cancellation_after_probe_does_not_make_output_promotable(tmp_path):
    output = tmp_path / "staged.mp4"
    output.write_bytes(b"last-good")
    cancelled = False

    def probe(_path):
        nonlocal cancelled
        cancelled = True
        return 0.1

    encoder = FFmpegEncoder(
        ffmpeg="ffmpeg",
        popen=lambda args, **_kwargs: FakeProcess(args, output=b"new-video"),
        which=lambda _name: "/fake/ffmpeg",
        probe_duration=probe,
    )

    with pytest.raises(asyncio.CancelledError):
        encoder.encode(
            _frames(tmp_path),
            None,
            output,
            lambda _frame: None,
            lambda: cancelled,
        )

    assert output.read_bytes() == b"last-good"


def test_ffprobe_frame_count_mismatch_fails_closed(tmp_path):
    output = tmp_path / "staged.mp4"
    output.write_bytes(b"last-good")
    probe = FakeProbeProcess(
        {
            "streams": [{"duration": "0.3", "nb_read_frames": "2"}],
            "format": {"duration": "0.3"},
        }
    )
    encoder = FFmpegEncoder(
        ffmpeg="ffmpeg",
        fps=10,
        popen=lambda args, **_kwargs: FakeProcess(args, output=b"new-video"),
        probe_popen=lambda _args, **_kwargs: probe,
        which=lambda name: f"/fake/{name}",
    )

    with pytest.raises(EncodingError, match="frame count"):
        encoder.encode(
            _frames(tmp_path, count=3),
            None,
            output,
            lambda _frame: None,
            lambda: False,
        )

    assert output.read_bytes() == b"last-good"


def test_hung_ffprobe_is_terminated_when_cancellation_arrives(tmp_path):
    output = tmp_path / "staged.mp4"
    output.write_bytes(b"last-good")
    probe = None

    def probe_popen(_args, **_kwargs):
        nonlocal probe
        probe = FakeProbeProcess({}, hanging=True)
        return probe

    encoder = FFmpegEncoder(
        ffmpeg="ffmpeg",
        popen=lambda args, **_kwargs: FakeProcess(args, output=b"new-video"),
        probe_popen=probe_popen,
        which=lambda name: f"/fake/{name}",
    )

    with pytest.raises(asyncio.CancelledError):
        encoder.encode(
            _frames(tmp_path),
            None,
            output,
            lambda _frame: None,
            lambda: probe is not None,
        )

    assert probe is not None and probe.terminated
    assert output.read_bytes() == b"last-good"


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
