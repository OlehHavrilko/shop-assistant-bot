import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path


def _integer(env: Mapping[str, str], name: str, default: int, minimum=1, maximum=10**15) -> int:
    try:
        result = int(env.get(name, str(default)))
        if not minimum <= result <= maximum:
            raise ValueError
        return result
    except ValueError:
        raise ValueError(f"{name}: потрібне ціле число від {minimum} до {maximum}.") from None


def _money(env: Mapping[str, str], name: str, default: str = "") -> Decimal:
    try:
        result = Decimal(env.get(name, default))
        if not result.is_finite() or result <= 0:
            raise ValueError
        return result
    except (ValueError, InvalidOperation):
        raise ValueError(f"{name}: вкажіть додатне скінченне число.") from None


@dataclass(frozen=True)
class Config:
    token: str = field(repr=False)
    manager_id: int
    database_path: Path
    demo_catalog_path: Path | None
    message_length: int
    request_rate: int
    rate_window: int
    context_limit: int
    llm_enabled: bool
    api_key: str = field(repr=False)
    model: str
    daily_budget: Decimal
    input_price: Decimal
    output_price: Decimal
    cache_write_price: Decimal
    cache_read_price: Decimal
    max_input_tokens: int
    max_output_tokens: int
    max_calls: int
    llm_timeout: int
    store_data_path: Path | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Config":
        env = os.environ if env is None else env
        token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
        # Fail with the setting name only: never echo an invalid token.
        prefix, separator, secret = token.partition(":")
        if not separator or not prefix.isdigit() or not secret or any(c.isspace() for c in token):
            raise ValueError("TELEGRAM_BOT_TOKEN: вкажіть токен тестового бота.")
        raw_enabled = env.get("LLM_ENABLED", "false").casefold()
        if raw_enabled not in ("true", "false"):
            raise ValueError("LLM_ENABLED: вкажіть true або false.")
        enabled = raw_enabled == "true"
        key = env.get("ANTHROPIC_API_KEY", "").strip()
        model = env.get("ANTHROPIC_MODEL", "").strip()
        if enabled and (not key or not model):
            raise ValueError("Платний режим потребує ANTHROPIC_API_KEY та ANTHROPIC_MODEL.")
        prices = {}
        for name, setting in (
            ("daily_budget", "LLM_DAILY_BUDGET_USD"),
            ("input_price", "LLM_INPUT_USD_PER_MILLION"),
            ("output_price", "LLM_OUTPUT_USD_PER_MILLION"),
            ("cache_write_price", "LLM_CACHE_WRITE_USD_PER_MILLION"),
            ("cache_read_price", "LLM_CACHE_READ_USD_PER_MILLION"),
        ):
            prices[name] = _money(env, setting) if enabled else Decimal("0")
        database = env.get("DATABASE_PATH", "data/catalog.sqlite3").strip()
        if not database:
            raise ValueError("DATABASE_PATH: вкажіть шлях до бази даних.")
        demo = env.get("DEMO_CATALOG_PATH", "").strip()
        return cls(
            token=token, manager_id=_integer(env, "MANAGER_TELEGRAM_ID", 0),
            database_path=Path(database), demo_catalog_path=Path(demo) if demo else None,
            message_length=_integer(env, "MAX_MESSAGE_LENGTH", 2000, maximum=3000),
            request_rate=_integer(env, "CLIENT_REQUESTS_PER_WINDOW", 20, maximum=1000),
            rate_window=_integer(env, "CLIENT_RATE_WINDOW_SECONDS", 60, maximum=86400),
            context_limit=_integer(env, "MANAGER_CONTEXT_LIMIT", 3000, maximum=3000),
            llm_enabled=enabled, api_key=key, model=model,
            max_input_tokens=_integer(env, "LLM_MAX_INPUT_TOKENS", 16000, maximum=32000),
            max_output_tokens=_integer(env, "LLM_MAX_OUTPUT_TOKENS", 400, maximum=2000),
            max_calls=_integer(env, "LLM_MAX_CALLS_PER_MESSAGE", 2, maximum=2),
            llm_timeout=_integer(env, "LLM_TIMEOUT_SECONDS", 15, maximum=30),
            store_data_path=Path(env.get("STORE_DATA_PATH", "examples/store.json")),
            **prices,
        )
