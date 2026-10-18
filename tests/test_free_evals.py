import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("free_eval_runner", Path(__file__).parents[1] / "evals/free_run.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def test_actual_free_demo_runs_all_queries_and_card_invariants(tmp_path, capsys):
    output = tmp_path / "free-report.json"
    assert runner.main(["--output", str(output)]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["passed"] and report["assets_pass"]
    assert report["passed_cases"] == 40 and len(report["cases"]) == 40
    assert report["model_calls"] == 0
    assert len(report["cards"]) == 20
    assert all(card["facts_pass"] and card["privacy_pass"] and card["photo_pass"]
               and card["caption_pass"] for card in report["cards"])
    assert "40/40 queries; 20/20 cards" in capsys.readouterr().out


def test_wrong_expectation_is_a_failed_run_with_case_diagnostics(tmp_path, capsys):
    data = json.loads((runner.ROOT / "evals/free_cases.json").read_text(encoding="utf-8"))
    # Keep the same 20 unique SKU coverage but attach the wrong two expectations.
    data["cases"][0]["expected_skus"], data["cases"][1]["expected_skus"] = (
        data["cases"][1]["expected_skus"], data["cases"][0]["expected_skus"])
    cases = tmp_path / "wrong.json"
    cases.write_text(json.dumps(data), encoding="utf-8")
    assert runner.main(["--cases", str(cases)]) == 1
    output = capsys.readouterr().out
    assert "38/40" in output and "FAIL U01" in output and "FAIL U02" in output
    assert "expected product" in output and "actual product" in output


def test_existing_report_is_preserved(tmp_path):
    output = tmp_path / "report.json"
    output.write_text("preserve existing report", encoding="utf-8")
    with pytest.raises(SystemExit) as error:
        runner.main(["--output", str(output)])
    assert error.value.code == 2
    assert output.read_text(encoding="utf-8") == "preserve existing report"


def test_missing_product_photo_fails_card_validation(tmp_path):
    import csv

    from shop_assistant.catalog import Catalog
    from shop_assistant.importer import import_catalog
    from shop_assistant.storefront import Storefront

    store_path = tmp_path / "store.json"
    store_path.write_text((runner.ROOT / "examples/store.json").read_text(encoding="utf-8"), encoding="utf-8")
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        import_catalog(catalog, runner.ROOT / "examples/store_catalog.csv")
        store = Storefront(catalog, store_path)
        with (runner.ROOT / "examples/store_catalog.csv").open(encoding="utf-8-sig") as stream:
            source = next(csv.DictReader(stream))
        result = runner.check_card(store, catalog.search(sku=source["sku"])[0], source)
        assert result["facts_pass"] and result["privacy_pass"]
        assert not result["photo_pass"] and not result["passed"]
