from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from shop_assistant.catalog import Catalog
from shop_assistant.limits import Budget, Tariffs


def tariffs():
    return Tariffs(Decimal("1"), Decimal("5"), Decimal("2"), Decimal("0.1"))


def test_atomic_parallel_cap_and_restart(tmp_path):
    path = tmp_path / "budget.db"
    budget = Budget(path, Decimal("0.1"), tariffs())
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(lambda _: budget.reserve(1000, 1000), range(40)))
    assert sum(i is not None for i in ids) == 12
    assert budget.spent() == Decimal("0.084")
    assert Budget(path, Decimal("0.1"), tariffs()).reserve(1000, 1000) is None


def test_cache_usage_reconciles_once_and_utc_rollover(tmp_path, monkeypatch):
    budget = Budget(tmp_path / "b.db", Decimal("1"), tariffs())
    monkeypatch.setattr(budget, "_day", lambda: "2026-10-17")
    reservation = budget.reserve(1000, 1000)
    budget.reconcile(reservation, input_tokens=100, output_tokens=200,
                     cache_write_tokens=50, cache_read_tokens=100)
    assert budget.spent() == Decimal("0.00121")
    budget.reconcile(reservation, input_tokens=0, output_tokens=0)
    assert budget.spent() == Decimal("0.00121")
    monkeypatch.setattr(budget, "_day", lambda: "2026-10-18")
    assert budget.spent() == 0
    assert budget.spent("2026-10-17") == Decimal("0.00121")


def test_independent_schema_compatible_with_catalog(tmp_path):
    path = tmp_path / "both.db"
    Budget(path, Decimal("1"), tariffs())
    with Catalog(path) as catalog:
        assert catalog.connection.execute("PRAGMA user_version").fetchone()[0] == 1
    Budget(path, Decimal("1"), tariffs())


@pytest.mark.parametrize("value", [Decimal("0"), Decimal("-1"), Decimal("NaN"), Decimal("Infinity")])
def test_unknown_or_nonpositive_tariffs_rejected(value):
    with pytest.raises(ValueError):
        Tariffs(value, Decimal("1"), Decimal("1"), Decimal("1"))


def test_invalid_usage_does_not_release_reservation(tmp_path):
    budget = Budget(tmp_path / "b.db", Decimal("1"), tariffs())
    reservation = budget.reserve(1000, 1000)
    with pytest.raises(ValueError):
        budget.reconcile(reservation, input_tokens=-1, output_tokens=0)
    assert budget.spent() == Decimal("0.007")
