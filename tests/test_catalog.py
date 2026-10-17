import csv
import sqlite3
import subprocess
import sys
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

import pytest
from openpyxl import Workbook

from shop_assistant.catalog import Catalog, format_product
from shop_assistant.importer import ImportError, REQUIRED, import_catalog


DEMO = Path(__file__).resolve().parents[1] / "examples" / "catalog.csv"


@pytest.fixture
def catalog(tmp_path):
    with Catalog(tmp_path / "catalog.sqlite3") as database:
        import_catalog(database, DEMO)
        yield database


def write_catalog(tmp_path, rows, extension="csv", header=REQUIRED):
    path = tmp_path / f"input.{extension}"
    if extension == "csv":
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(header)
            writer.writerows(rows)
    else:
        workbook = Workbook()
        workbook.active.append(list(header))
        for row in rows:
            workbook.active.append(list(row))
        workbook.save(path)
        workbook.close()
    return path


VALID = ("00123", "Льон Siena", "ivory", "345.50", "12.75", "2026-10-15T12:00:00+03:00")


@pytest.mark.parametrize("extension", ["csv", "xlsx"])
def test_csv_and_xlsx_replace_and_preserve_sku(catalog, tmp_path, extension):
    path = write_catalog(tmp_path, [VALID, ("123", *VALID[1:])], extension)
    original = path.read_bytes()
    report = import_catalog(catalog, path)
    assert report.count == 2 and report.loaded_at.utcoffset().total_seconds() == 0
    product = catalog.search(sku="00123")[0]
    assert product.price_uah_per_m == Decimal("345.50")
    assert product.stock_m == Decimal("12.75")
    assert product.updated_at.isoformat() == "2026-10-15T09:00:00+00:00"
    assert catalog.search(sku="123")[0].sku == "123"
    assert catalog.search(sku="COT-010") == []
    assert path.read_bytes() == original


@pytest.mark.parametrize("field,value", [
    ("sku", ""), ("name", ""), ("shade", ""),
    ("price_uah_per_m", "-1"), ("price_uah_per_m", "1.001"),
    ("price_uah_per_m", "NaN"), ("price_uah_per_m", "Infinity"),
    ("price_uah_per_m", "9" * 5000), ("stock_m", "9" * 5000),
    ("price_uah_per_m", ""), ("stock_m", "-0.01"), ("stock_m", "1.001"),
    ("updated_at", "2026-10-15T12:00:00"), ("updated_at", "2026-02-30T12:00:00Z"),
    ("name", "=SUM(A1:A2)"),
])
def test_invalid_import_preserves_catalog_and_timestamp(catalog, tmp_path, field, value):
    row = list(VALID)
    row[0] = "NEW-001"
    row[REQUIRED.index(field)] = value
    before = catalog.connection.execute("SELECT * FROM catalog_import").fetchone()[:]
    path = write_catalog(tmp_path, [VALID, row])
    with pytest.raises(ImportError, match="Рядок 3"):
        import_catalog(catalog, path)
    assert len(catalog.search(name="Льон")) == 3
    assert catalog.connection.execute("SELECT * FROM catalog_import").fetchone()[:] == before


@pytest.mark.parametrize("extension", ["csv", "xlsx"])
def test_duplicate_sku_is_rejected(catalog, tmp_path, extension):
    with pytest.raises(ImportError, match="Рядок 3: повтор"):
        import_catalog(catalog, write_catalog(tmp_path, [VALID, VALID], extension))
    assert catalog.search(sku="COT-010")


@pytest.mark.parametrize("value", [123, "=A1"])
def test_excel_sku_must_be_text_and_formulas_rejected(catalog, tmp_path, value):
    with pytest.raises(ImportError, match="Рядок 2"):
        import_catalog(catalog, write_catalog(tmp_path, [(value, *VALID[1:])], "xlsx"))
    assert catalog.search(sku="00123")


@pytest.mark.parametrize("rows,header", [
    ([], REQUIRED), ([VALID], REQUIRED[:-1]),
    ([VALID], (*REQUIRED[:-1], "sku")), ([VALID[:-1]], REQUIRED),
])
def test_empty_or_malformed_export_does_not_clear_catalog(catalog, tmp_path, rows, header):
    with pytest.raises(ImportError):
        import_catalog(catalog, write_catalog(tmp_path, rows, header=header))
    assert catalog.search(sku="COT-010")


def test_search_normalization_exact_sku_and_ambiguity(catalog):
    assert len(catalog.search(name="  лЬоН   siena  ", shade=" IVORY ")) == 2
    assert len(catalog.search(sku="00123", name="Бавовна")) == 1
    assert catalog.search(sku="0012") == []
    assert catalog.search(name="невідомий товар") == []
    assert catalog.search(sku="' OR 1=1 --") == []
    assert len(catalog.search(name="Льон", limit=1)) == 1
    with pytest.raises(ValueError):
        catalog.search(name="Льон", limit=21)


def test_public_product_has_no_private_fields_and_distinguishes_stock(catalog):
    product = catalog.search(sku="00123")[0]
    assert set(asdict(product)) == set(REQUIRED)
    card = format_product(product)
    assert "345.50 грн/м" in card and "12.75 м" in card
    assert "230.00" not in card and "постачальник" not in card
    assert "немає в наявності" in format_product(catalog.search(sku="123")[0])
    assert "немає даних" in format_product(catalog.search(sku="COT-010")[0])


def test_restart_preserves_catalog(tmp_path):
    path = tmp_path / "persistent.sqlite3"
    with Catalog(path) as database:
        import_catalog(database, DEMO)
    with Catalog(path) as database:
        assert database.search(sku="00123")[0].stock_m == Decimal("12.75")
        assert database.connection.execute("PRAGMA user_version").fetchone()[0] == 1


def test_failed_database_write_rolls_back_whole_import(catalog, tmp_path):
    catalog.connection.execute("""CREATE TRIGGER reject_product BEFORE INSERT ON products
        WHEN NEW.sku = 'FAIL' BEGIN SELECT RAISE(ABORT, 'test failure'); END""")
    path = write_catalog(tmp_path, [VALID, ("FAIL", *VALID[1:])])
    with pytest.raises(sqlite3.IntegrityError):
        import_catalog(catalog, path)
    assert catalog.search(sku="COT-010")


def test_unknown_schema_is_rejected_without_changes(tmp_path):
    path = tmp_path / "future.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 99")
    with pytest.raises(ValueError, match="версія"):
        Catalog(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 99


def test_cli_import_and_search_without_tokens(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    command = [sys.executable, "-m", "shop_assistant", "--db", str(tmp_path / "cli.sqlite3")]
    imported = subprocess.run([*command, "import", str(DEMO)], capture_output=True)
    assert imported.returncode == 0, imported.stderr
    found = subprocess.run([*command, "search", "--sku", "00123"], capture_output=True)
    assert found.returncode == 0, found.stderr
    assert b"345.50" in found.stdout and b"00123" in found.stdout
    missing = subprocess.run([*command, "import", str(tmp_path / "missing.csv")], capture_output=True)
    assert missing.returncode == 1 and b"Traceback" not in missing.stderr
