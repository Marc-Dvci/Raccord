from raccord.config import Settings


def test_current_ga_gemini_model_is_the_default(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path)
    assert settings.gemini_model == "gemini-3.7-flash"
    assert settings.gemini_location == "global"
