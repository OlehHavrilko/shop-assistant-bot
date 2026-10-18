"""Persistent, atomic reservations in USD; no network calls are made here."""

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from uuid import uuid4


def _positive(value: Decimal) -> bool:
    return isinstance(value, Decimal) and value.is_finite() and value > 0


@dataclass(frozen=True)
class Tariffs:
    input_per_million: Decimal
    output_per_million: Decimal
    cache_write_per_million: Decimal
    cache_read_per_million: Decimal

    def __post_init__(self):
        if not all(_positive(value) for value in vars(self).values()):
            raise ValueError("Усі тарифи моделі мають бути відомими додатними числами.")

    def cost(self, input_tokens: int, output_tokens: int,
             cache_write_tokens: int = 0, cache_read_tokens: int = 0) -> Decimal:
        counts = (input_tokens, output_tokens, cache_write_tokens, cache_read_tokens)
        if any(type(n) is not int or n < 0 for n in counts):
            raise ValueError("Некоректний usage.")
        return sum((n * rate for n, rate in zip(counts, vars(self).values(), strict=True)), Decimal(0)) / 1_000_000


class Budget:
    def __init__(self, path: str | Path, daily_usd: Decimal, tariffs: Tariffs,
                 cutoff: Decimal = Decimal("0.85")):
        if not _positive(daily_usd) or daily_usd > 1_000_000:
            raise ValueError("Некоректний денний бюджет.")
        if not isinstance(cutoff, Decimal) or not cutoff.is_finite() or not Decimal("0.8") <= cutoff <= Decimal("0.9"):
            raise ValueError("Запас бюджету має становити 10–20%.")
        self.path, self.daily_usd, self.tariffs, self.cutoff = str(path), daily_usd, tariffs, cutoff
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS budget_schema (id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL)")
            db.execute("INSERT OR IGNORE INTO budget_schema VALUES (1, 1)")
            if db.execute("SELECT version FROM budget_schema WHERE id=1").fetchone()[0] != 1:
                raise ValueError("Непідтримувана версія бюджету.")
            db.execute("""CREATE TABLE IF NOT EXISTS budget_calls (
                id TEXT PRIMARY KEY, day TEXT NOT NULL, units INTEGER NOT NULL,
                settled INTEGER NOT NULL DEFAULT 0)""")
            db.execute("CREATE INDEX IF NOT EXISTS budget_calls_day ON budget_calls(day)")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _units(value: Decimal) -> int:
        return int((value * 1_000_000_000).to_integral_value(rounding=ROUND_CEILING))

    @staticmethod
    def _day() -> str:
        return datetime.now(UTC).date().isoformat()

    def reserve(self, max_input_tokens: int, max_output_tokens: int) -> str | None:
        if any(type(n) is not int or not 1 <= n <= 1_000_000 for n in (max_input_tokens, max_output_tokens)):
            raise ValueError("Некоректні межі токенів.")
        # Price all possible input at the most expensive configured input tariff.
        rate = max(self.tariffs.input_per_million, self.tariffs.cache_write_per_million,
                   self.tariffs.cache_read_per_million)
        units = self._units((max_input_tokens * rate + max_output_tokens * self.tariffs.output_per_million) / 1_000_000)
        day, reservation = self._day(), uuid4().hex
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            total = db.execute("SELECT COALESCE(SUM(units), 0) FROM budget_calls WHERE day=?", (day,)).fetchone()[0]
            if total + units > self._units(self.daily_usd * self.cutoff):
                return None
            db.execute("INSERT INTO budget_calls(id, day, units) VALUES (?, ?, ?)", (reservation, day, units))
        return reservation

    def reconcile(self, reservation: str, *, input_tokens: int, output_tokens: int,
                  cache_write_tokens: int = 0, cache_read_tokens: int = 0) -> None:
        units = self._units(self.tariffs.cost(input_tokens, output_tokens, cache_write_tokens, cache_read_tokens))
        with self._connect() as db:
            db.execute("UPDATE budget_calls SET units=?, settled=1 WHERE id=? AND settled=0", (units, reservation))

    def spent(self, day: str | None = None) -> Decimal:
        with self._connect() as db:
            units = db.execute("SELECT COALESCE(SUM(units), 0) FROM budget_calls WHERE day=?", (day or self._day(),)).fetchone()[0]
        return Decimal(units) / 1_000_000_000

    def close(self) -> None:
        # Connections are scoped to each operation, so there is nothing to close.
        pass
