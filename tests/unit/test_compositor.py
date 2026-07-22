"""Unit tests — video compositor and plotter (no ffmpeg)."""

import numpy as np
from PIL import Image

from src.video.compositor import VideoCompositor


class TestVideoCompositor:
    def test_resolution(self, test_config):
        c = VideoCompositor(test_config)
        assert c.width == 1080
        assert c.height == 1920

    def test_intro_hold_shape(self, test_config):
        c = VideoCompositor(test_config)
        frames = c.render_intro_hold("Test", poster_path=None, duration=0.5)
        assert len(frames) == 15  # 0.5s × 30fps
        assert frames[0].shape == (1920, 1080, 3)

    def test_intro_transition_shape(self, test_config):
        c = VideoCompositor(test_config)
        poster_area = Image.new("RGB", (1080, 640), (0, 0, 0))
        frames = c.render_intro_transition(
            poster_path=None, poster_area=poster_area, plotter_frames=[], duration=0.5
        )
        assert len(frames) == 15
        assert frames[0].shape == (1920, 1080, 3)

    def test_verdict_shape(self, test_config):
        c = VideoCompositor(test_config)
        summary = {
            "total_hard": 42,
            "total_soft": 100,
            "total_f_bombs": 200,
            "peak_minute": 30,
            "peak_score": 15,
            "rating": "🚨 TOXIC AF",
        }
        poster_area = Image.new("RGB", (1080, 640), (0, 0, 0))
        frames = c.render_verdict("Test", summary, poster_area, duration=0.5)
        assert len(frames) == 15
        assert frames[0].shape == (1920, 1080, 3)

    def test_graph_empty(self, test_config):
        c = VideoCompositor(test_config)
        poster_area = Image.new("RGB", (1080, 640), (0, 0, 0))
        frames = c.render_graph_segment([], poster_area, duration=0.5)
        assert len(frames) == 15
        for f in frames:
            assert f.shape == (1920, 1080, 3)

    def test_short_public_helpers_wrap_iterators(self, test_config, monkeypatch):
        c = VideoCompositor(test_config)
        expected = np.zeros((2, 2, 3), dtype=np.uint8)
        monkeypatch.setattr(
            c,
            "iter_intro_hold",
            lambda *_args, **_kwargs: iter((expected,)),
        )

        assert c.render_intro_hold("Test", None) == [expected]

    def test_render_all_streams_iterators_without_calling_list_helpers(
        self, test_config, tmp_path, monkeypatch
    ):
        c = VideoCompositor(test_config)
        c.width = 4
        c.height = 3
        frame = np.zeros((3, 4, 3), dtype=np.uint8)
        poster = Image.new("RGB", (4, 640), (0, 0, 0))
        monkeypatch.setattr(c, "render_poster_area", lambda *_args, **_kwargs: poster)
        for name in (
            "render_intro_hold",
            "render_intro_transition",
            "render_graph_segment",
            "render_verdict",
        ):
            monkeypatch.setattr(
                c,
                name,
                lambda *_args, _name=name, **_kwargs: (_ for _ in ()).throw(
                    AssertionError(f"list helper called: {_name}")
                ),
            )
        monkeypatch.setattr(
            c, "iter_intro_hold", lambda *_args, **_kwargs: iter((frame, frame))
        )
        monkeypatch.setattr(
            c, "iter_intro_transition", lambda *_args, **_kwargs: iter((frame,))
        )
        monkeypatch.setattr(
            c, "iter_graph_segment", lambda *_args, **_kwargs: iter((frame, frame))
        )
        monkeypatch.setattr(c, "iter_verdict", lambda *_args, **_kwargs: iter((frame,)))
        progress = []

        result = c.render_all(
            tmp_path / "staging", progress_cb=lambda *args: progress.append(args)
        )

        assert result["total_frames"] == 6
        assert all(not isinstance(value, list) for value in result["segments"].values())
        assert len(list((tmp_path / "staging" / "concat").glob("*.png"))) == 6
        assert progress[-1] == ("verdict", 1, 1)


class TestRagePlotter:
    def test_blank_frames_shape(self, test_config, tmp_path):
        from src.video.plotter import RagePlotter

        p = RagePlotter(test_config)
        frames = p.generate_frames([], tmp_path, n_frames=3)
        assert len(frames) == 3
        for fp in frames:
            assert fp.exists()
