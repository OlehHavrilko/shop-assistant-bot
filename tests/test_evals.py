import asyncio
import importlib.util
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from shop_assistant.catalog import Catalog
from shop_assistant.importer import import_catalog
from shop_assistant.llm import SearchDecision

spec = importlib.util.spec_from_file_location("eval_runner", Path(__file__).parents[1] / "evals/run.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def test_default_validation_never_constructs_llm(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("Offline validation must not construct an LLM client")
    monkeypatch.setattr(runner, "LLM", forbidden)
    assert runner.main([]) == 0
    assert "No API calls" in capsys.readouterr().out


def test_paid_run_requires_enabled_env_and_does_not_create_report(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_ENABLED", raising=False)
    output = tmp_path / "report"
    with pytest.raises(SystemExit) as error:
        runner.main(["--allow-paid", "--output", str(output)])
    assert error.value.code == 2
    assert not output.exists()


def test_score_follows_server_candidate_selection_and_unknown_stock(tmp_path):
    cases = runner.load_cases(runner.ROOT / "evals/cases.json")
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        import_catalog(catalog, runner.ROOT / "examples/catalog.csv")
        ambiguous = next(case for case in cases if case["id"] == "A06")
        result = runner.score(ambiguous, SearchDecision("products", ("00123", "LIN-002")), catalog)
        assert result["actual_action"] == "clarify" and result["action_pass"] and result["sku_pass"]
        cotton = next(case for case in cases if case["id"] == "U20")
        result = runner.score(cotton, SearchDecision("products", ("COT-010",)), catalog)
        assert result["facts_pass"] and result["privacy_pass"]
        assert result["server_cards"] == [runner.format_product(catalog.search(sku="COT-010")[0])]


def test_paid_run_never_overwrites_existing_output(tmp_path, monkeypatch):
    env = {"LLM_ENABLED": "true", "ANTHROPIC_API_KEY": "offline-test-key",
           "ANTHROPIC_MODEL": "offline-test-model"}
    env.update({key: "1" for key in ("LLM_DAILY_BUDGET_USD", "LLM_INPUT_USD_PER_MILLION",
               "LLM_OUTPUT_USD_PER_MILLION", "LLM_CACHE_WRITE_USD_PER_MILLION",
               "LLM_CACHE_READ_USD_PER_MILLION")})
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    output = tmp_path / "existing"
    output.mkdir()
    sentinel = output / "metadata.json"
    sentinel.write_text("preserve", encoding="utf-8")
    def forbidden(*args, **kwargs):
        pytest.fail("Existing output must be rejected before LLM construction")
    monkeypatch.setattr(runner, "LLM", forbidden)
    with pytest.raises(SystemExit) as error:
        runner.main(["--allow-paid", "--output", str(output)])
    assert error.value.code == 2
    assert sentinel.read_text(encoding="utf-8") == "preserve"


@pytest.mark.skipif(importlib.util.find_spec("anthropic") is None, reason="needs the optional .[llm] extra")
def test_report_pipeline_with_real_adapter_and_offline_provider(tmp_path, monkeypatch):
    """Verify reporting and budget plumbing, not real-model accuracy."""
    cases = runner.load_cases(runner.ROOT / "evals/cases.json")
    by_query = {case["query"]: case for case in cases}

    class OfflineProvider:
        messages = None

        def __init__(self):
            self.messages = self

        async def create(self, **payload):
            case = by_query[payload["messages"][0]["content"]]
            if len(payload["messages"]) == 1:
                if case["expected_action"] == "handoff":
                    name, params = "choose_products", {"action": "handoff", "skus": []}
                else:
                    name = "search_catalog"
                    if case["category"] == "ambiguous":
                        params = {"name": "Siena"}
                        if len(case["expected_skus"]) == 2:
                            params["shade"] = "ivory"
                    else:
                        params = {"sku": next(iter(case["expected_skus"]), "missing")}
            else:
                name, params = "choose_products", {"action": "products", "skus": case["expected_skus"]}
            return NS(stop_reason="tool_use", usage=NS(input_tokens=100, output_tokens=50),
                      content=[NS(type="tool_use", id="offline", name=name, input=params)])

        async def close(self):
            pass

    actual_llm = runner.LLM

    def offline_llm(catalog, **settings):
        settings.pop("api_key")
        return actual_llm(catalog, client=OfflineProvider(), **settings)

    monkeypatch.setattr(runner, "LLM", offline_llm)
    env = {"LLM_ENABLED": "true", "ANTHROPIC_API_KEY": "offline-test-key",
           "ANTHROPIC_MODEL": "offline-test-model",
           "_EVAL_CASES_PATH": str(runner.ROOT / "evals/cases.json")}
    env.update({key: "1" for key in ("LLM_DAILY_BUDGET_USD", "LLM_INPUT_USD_PER_MILLION",
               "LLM_OUTPUT_USD_PER_MILLION", "LLM_CACHE_WRITE_USD_PER_MILLION",
               "LLM_CACHE_READ_USD_PER_MILLION")})
    output = tmp_path / "offline-report"
    assert asyncio.run(runner.evaluate(cases, runner.ROOT / "examples/catalog.csv", output, env)) == 0
    metadata = json.loads((output / "metadata.json").read_text(encoding="utf-8"))
    results = [json.loads(line) for line in (output / "cases.jsonl").read_text(encoding="utf-8").splitlines()]
    assert metadata["status"] == "completed" and Decimal(metadata["total_cost_usd"]) == Decimal("0.0105")
    assert len(results) == 40 and all(result["pass"] for result in results)
    assert sum(result["call_count"] for result in results) == 70
    assert all(result["usage_complete"] for result in results)
    assert "offline-test-key" not in (output / "metadata.json").read_text(encoding="utf-8")
