from src.publishing.metadata import generate_metadata


class TestMetadataGeneration:
    def test_clean_movie_metadata(self):
        summary = {
            "total_hard": 0,
            "total_soft": 0,
            "total_f_bombs": 0,
            "rating": "⭐ G-RATED ANGEL"
        }
        meta = generate_metadata("Toy Story", summary)
        assert meta["tier"] == "clean"
        assert "CLEAN" in meta["verdict"]
        assert "#Clean" in meta["hashtags"]

    def test_toxic_movie_metadata(self):
        summary = {
            "total_hard": 150,
            "total_f_bombs": 300,
            "rating": "🚨 TOXIC AF"
        }
        meta = generate_metadata("Wolf of Wall Street", summary)
        assert meta["tier"] == "toxic"
        assert "TOXIC" in meta["verdict"]
        assert "#TOXIC" in meta["hashtags"]

    def test_hazmat_movie_metadata(self):
        summary = {
            "total_hard": 250,
            "total_f_bombs": 500,
            "rating": "💀 CALL THE HAZMAT TEAM"
        }
        meta = generate_metadata("Extreme Profanity", summary)
        assert meta["tier"] == "hazmat"
        assert "HAZMAT" in meta["verdict"]

    def test_metadata_fields_present(self):
        summary = {"total_hard": 5, "total_f_bombs": 10, "rating": "😬 EDGY"}
        meta = generate_metadata("Test Movie", summary)
        expected_fields = ["video_title", "description", "tags", "hashtags", "tier", "verdict"]
        for field in expected_fields:
            assert field in meta
            assert meta[field]
