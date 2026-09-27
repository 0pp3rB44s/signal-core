from __future__ import annotations

from app.config import Settings


def test_secret_settings_are_typed_and_redacted_from_repr():
    settings = Settings(
        _env_file=None,
        BITGET_API_KEY="private-api-key",
        BITGET_API_SECRET="private-api-secret",
        BITGET_API_PASSPHRASE="private-passphrase",
        DASHBOARD_PASSWORD="private-dashboard-password",
        DASHBOARD_SECRET_KEY="private-dashboard-key",
    )

    rendered = repr(settings)
    for secret in (
        "private-api-key",
        "private-api-secret",
        "private-passphrase",
        "private-dashboard-password",
        "private-dashboard-key",
    ):
        assert secret not in rendered

    assert settings.bitget_api_key.get_secret_value() == "private-api-key"


def test_legacy_environment_names_still_populate_typed_settings(monkeypatch):
    monkeypatch.setenv("BREAKOUT_CONTEXT_MIN_EXPANSION_PROB", "72.5")
    monkeypatch.setenv("MOMENTUM_FUNNEL_AUDIT", "false")
    monkeypatch.setenv("STRATEGY_DEBUG_SYMBOLS", "BTCUSDT,ETHUSDT")

    settings = Settings(_env_file=None)

    assert settings.breakout_context_min_expansion_prob == 72.5
    assert settings.momentum_funnel_audit is False
    assert settings.strategy_debug_symbol_set == {"BTCUSDT", "ETHUSDT"}
