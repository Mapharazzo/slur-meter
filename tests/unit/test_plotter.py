import pytest
from pathlib import Path
from src.video.plotter import RagePlotter

class TestRagePlotter:
    def test_generate_frames_returns_paths(self, test_config, tmp_path):
        p = RagePlotter(test_config)
        # Mock data: 2 minutes of activity
        binned = [
            {"minute": 0, "hard": 1, "soft": 2, "f_bombs": 3},
            {"minute": 1, "hard": 0, "soft": 1, "f_bombs": 5}
        ]
        frames = p.generate_frames(binned, tmp_path, n_frames=5)
        assert len(frames) == 5
        for fp in frames:
            assert fp.exists()
            assert fp.suffix == ".png"

    def test_generate_specific_frames(self, test_config, tmp_path):
        p = RagePlotter(test_config)
        binned = [{"minute": 0, "hard": 1, "soft": 0, "f_bombs": 0}]
        indices = [0, 10, 20]
        frames = p.generate_specific_frames(binned, tmp_path, frame_indices=indices, total_frames=100)
        assert len(frames) == 3
        for fp in frames:
            assert fp.exists()

    def test_empty_binned_data(self, test_config, tmp_path):
        p = RagePlotter(test_config)
        # Should generate blank frames or handle gracefully
        frames = p.generate_frames([], tmp_path, n_frames=2)
        assert len(frames) == 2
        for fp in frames:
            assert fp.exists()
