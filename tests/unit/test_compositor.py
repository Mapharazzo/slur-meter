"""Unit tests — video compositor and plotter (no ffmpeg)."""

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
        frames = c.render_intro_transition(poster_path=None, poster_area=poster_area, plotter_frames=[], duration=0.5)
        assert len(frames) == 15
        assert frames[0].shape == (1920, 1080, 3)

    def test_verdict_shape(self, test_config):
        c = VideoCompositor(test_config)
        summary = {
            "total_hard": 42, "total_soft": 100,
            "total_f_bombs": 200, "peak_minute": 30,
            "peak_score": 15, "rating": "🚨 TOXIC AF",
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


class TestRagePlotter:
    def test_blank_frames_shape(self, test_config, tmp_path):
        from src.video.plotter import RagePlotter
        p = RagePlotter(test_config)
        frames = p.generate_frames([], tmp_path, n_frames=3)
        assert len(frames) == 3
        for fp in frames:
            assert fp.exists()
