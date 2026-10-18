from decimal import Decimal

import pytest

from shop_assistant.config import Config

BASE = {"TELEGRAM_BOT_TOKEN": "123456:synthetic_test_token", "MANAGER_TELEGRAM_ID": "123"}


def test_default_is_unpaid_and_secrets_are_not_in_repr():
    config = Config.from_env(BASE)
    assert not config.llm_enabled
    assert config.manager_id == 123
    assert config.daily_budget == 0
    assert "synthetic_test_token" not in repr(config)


@pytest.mark.parametrize("name,value", [
    ("MANAGER_TELEGRAM_ID", "0"), ("MANAGER_TELEGRAM_ID", "NaN"),
    ("MAX_MESSAGE_LENGTH", "99999"), ("CLIENT_REQUESTS_PER_WINDOW", "-1"),
    ("LLM_MAX_CALLS_PER_MESSAGE", "3"), ("LLM_ENABLED", "yes"),
    ("TELEGRAM_BOT_TOKEN", "sensitive-invalid-value"),
])
def test_invalid_settings_do_not_echo_values(name, value):
    with pytest.raises(ValueError) as error:
        Config.from_env({**BASE, name: value})
    assert "synthetic_test_token" not in str(error.value)
    assert "sensitive-invalid-value" not in str(error.value)


def paid_env():
    return {**BASE, "LLM_ENABLED": "true", "ANTHROPIC_API_KEY": "synthetic_api_key",
            "ANTHROPIC_MODEL": "configured-model", "LLM_DAILY_BUDGET_USD": "1.00",
            "LLM_INPUT_USD_PER_MILLION": "1", "LLM_OUTPUT_USD_PER_MILLION": "2",
            "LLM_CACHE_WRITE_USD_PER_MILLION": "3", "LLM_CACHE_READ_USD_PER_MILLION": "0.1"}


def test_paid_mode_requires_explicit_key_model_and_all_tariffs():
    env = paid_env()
    assert Config.from_env(env).daily_budget == Decimal("1.00")
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_MODEL", "LLM_INPUT_USD_PER_MILLION",
                 "LLM_OUTPUT_USD_PER_MILLION", "LLM_CACHE_WRITE_USD_PER_MILLION",
                 "LLM_CACHE_READ_USD_PER_MILLION"):
        with pytest.raises(ValueError):
            Config.from_env({**env, name: ""})
    for value in ("NaN", "Infinity", "0", "-1"):
        with pytest.raises(ValueError):
            Config.from_env({**env, "LLM_DAILY_BUDGET_USD": value})


def test_bootstrap_only_imports_explicit_catalog_into_empty_db(tmp_path):
    from test_catalog import DEMO

    from shop_assistant.__main__ import bootstrap_catalog
    from shop_assistant.catalog import Catalog

    path = tmp_path / "catalog.sqlite3"
    with Catalog(path) as catalog:
        bootstrap_catalog(catalog, None)
        assert not catalog.search(sku="00123")
        bootstrap_catalog(catalog, DEMO)
        assert catalog.search(sku="00123")
    with Catalog(path) as catalog:
        # Existing catalog must be kept even if a later bootstrap path is invalid.
        bootstrap_catalog(catalog, tmp_path / "missing.csv")
        assert catalog.search(sku="00123")
