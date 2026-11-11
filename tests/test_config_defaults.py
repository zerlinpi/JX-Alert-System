from jx_alert.config import Settings


def test_default_ad_attribution_refresh_covers_longest_mature_window():
    settings = Settings(_env_file=None)
    assert settings.ad_attribution_refresh_days == 22
