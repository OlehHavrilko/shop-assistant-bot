"""Offline validation by default; real Anthropic evaluation requires explicit consent."""

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from shop_assistant.catalog import Catalog, format_product
from shop_assistant.importer import import_catalog
from shop_assistant.limits import Budget, Tariffs
from shop_assistant.llm import DECISION_TOOL, SEARCH_TOOL, SYSTEM, LLM

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = ("purchase_price_uah_per_m", "supplier")


def load_cases(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    cases = data["cases"]
    counts = {name: sum(c["category"] == name for c in cases)
              for name in ("unique", "ambiguous", "unknown")}
    if len(cases) != 40 or counts != {"unique": 20, "ambiguous": 10, "unknown": 10}:
        raise ValueError("Expected 20 unique, 10 ambiguous and 10 unknown cases.")
    if len({c["id"] for c in cases}) != 40:
        raise ValueError("Case IDs must be unique.")
    for case in cases:
        action, skus = case["expected_action"], case["expected_skus"]
        if not isinstance(case["query"], str) or not case["query"].strip():
            raise ValueError("Every case needs a query.")
        if not isinstance(skus, list) or any(type(s) is not str for s in skus) or len(set(skus)) != len(skus):
            raise ValueError("Invalid expected SKUs.")
        category = case["category"]
        if not ((category == "unique" and action == "product" and len(skus) == 1)
                or (category == "ambiguous" and action == "clarify" and len(skus) > 1)
                or (category == "unknown" and action in ("not_found", "handoff") and not skus)):
            raise ValueError("Expected action does not match category.")
    return cases


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def paid_settings(env):
    if env.get("LLM_ENABLED", "").casefold() != "true":
        raise ValueError("Real evaluation also requires LLM_ENABLED=true.")
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_MODEL"):
        if not env.get(name, "").strip():
            raise ValueError(f"Set {name} explicitly.")
    values = []
    for name in ("LLM_DAILY_BUDGET_USD", "LLM_INPUT_USD_PER_MILLION",
                 "LLM_OUTPUT_USD_PER_MILLION", "LLM_CACHE_WRITE_USD_PER_MILLION",
                 "LLM_CACHE_READ_USD_PER_MILLION"):
        try:
            value = Decimal(env.get(name, ""))
            if not value.is_finite() or value <= 0:
                raise ValueError
        except (ValueError, InvalidOperation):
            raise ValueError(f"Set a positive finite {name} explicitly.") from None
        values.append(value)
    settings = {}
    for key, name, default, maximum in (
        ("max_input_tokens", "LLM_MAX_INPUT_TOKENS", 16000, 32000),
        ("max_output_tokens", "LLM_MAX_OUTPUT_TOKENS", 400, 2000),
        ("max_calls", "LLM_MAX_CALLS_PER_MESSAGE", 2, 2),
        ("timeout", "LLM_TIMEOUT_SECONDS", 15, 30),
    ):
        try:
            settings[key] = int(env.get(name, str(default)))
            if not 1 <= settings[key] <= maximum:
                raise ValueError
        except ValueError:
            raise ValueError(f"Invalid {name}.") from None
    return values[0], Tariffs(*values[1:]), settings


class ObservedClient:
    """Observe usage and privacy without saving requests or provider error text."""

    def __init__(self, client):
        self.client = client
        self.messages = self
        self.calls = []

    async def create(self, **payload):
        call = {"privacy_pass": not any(p in json.dumps(payload, ensure_ascii=False) for p in PRIVATE),
                "usage": None, "error": None}
        self.calls.append(call)
        try:
            response = await self.client.messages.create(**payload)
            usage = response.usage
            call["usage"] = {name: getattr(usage, name, 0) or 0 for name in (
                "input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")}
            return response
        except BaseException as error:
            call["error"] = type(error).__name__
            raise

    async def close(self):
        await self.client.close()


def ledger_total(path):
    with sqlite3.connect(path) as db:
        rows = db.execute("SELECT id, units, settled FROM budget_calls").fetchall()
    return {row[0]: {"cost_usd": str(Decimal(row[1]) / 1_000_000_000),
                     "settled": bool(row[2])} for row in rows}


def score(case, decision, catalog):
    skus = list(decision.skus)
    products = [catalog.search(sku=s) for s in skus]
    # Match the server's delivery behavior: multiple products produce selection buttons.
    action = "clarify" if decision.action == "products" and len(skus) > 1 else decision.action
    if action == "products":
        action = "product"
    cards = [format_product(found[0]) for found in products if len(found) == 1]
    return {"actual_action": action, "actual_skus": skus,
            "action_pass": action == case["expected_action"],
            "sku_pass": set(skus) == set(case["expected_skus"]),
            "facts_pass": all(len(found) == 1 for found in products),
            "server_cards": cards,
            "privacy_pass": not any(p in "\n".join(cards) for p in PRIVATE)}


async def evaluate(cases, catalog_path, output, env):
    daily, tariffs, settings = paid_settings(env)
    output.mkdir(parents=True, exist_ok=False)
    metadata = {"run_id": output.name, "started_at_utc": datetime.now(timezone.utc).isoformat(),
                "model": env["ANTHROPIC_MODEL"], "catalog_sha256": sha(catalog_path),
                "cases_sha256": sha(env["_EVAL_CASES_PATH"]),
                "prompt_sha256": hashlib.sha256(json.dumps(
                    [SYSTEM, SEARCH_TOOL, DECISION_TOOL], sort_keys=True).encode()).hexdigest(),
                "source_sha256": {p.name: sha(p) for p in sorted((ROOT / "src/shop_assistant").glob("*.py"))},
                "dependency_versions": {name: importlib.metadata.version(name)
                                        for name in ("anthropic", "aiogram", "openpyxl")},
                "settings": settings, "daily_budget_usd": str(daily),
                "tariffs_usd_per_million": {k: str(v) for k, v in vars(tariffs).items()},
                "scope": "Real LLM resolve and server cards; no Telegram delivery. Facts use catalog templates.",
                "status": "running"}
    meta_path = output / "metadata.json"
    meta_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    results = []
    with tempfile.TemporaryDirectory(prefix="shop-eval-") as temporary:
        with Catalog(Path(temporary) / "catalog.sqlite3") as catalog:
            import_catalog(catalog, catalog_path)
            # Keep only the cost ledger in the report, never the catalog's private fields.
            ledger_path = output / "budget.sqlite3"
            budget = Budget(ledger_path, daily, tariffs)
            llm = LLM(catalog, model=env["ANTHROPIC_MODEL"], budget=budget,
                      api_key=env["ANTHROPIC_API_KEY"], **settings)
            observer = ObservedClient(llm.client)
            llm.client = observer
            try:
                with (output / "cases.jsonl").open("x", encoding="utf-8") as report:
                    for case in cases:
                        before = ledger_total(ledger_path)
                        first_call = len(observer.calls)
                        start = time.monotonic()
                        error = None
                        try:
                            decision = await llm.resolve(case["query"])
                            result = score(case, decision, catalog)
                        except Exception as exception:
                            error = type(exception).__name__
                            result = {"actual_action": None, "actual_skus": [],
                                      "action_pass": False, "sku_pass": False,
                                      "facts_pass": False, "privacy_pass": False, "server_cards": []}
                        calls = observer.calls[first_call:]
                        ledger = {key: value for key, value in ledger_total(ledger_path).items() if key not in before}
                        if any(c["error"] or c["usage"] is None for c in calls):
                            error = error or "ProviderCallFailedOrUnknownUsage"
                        if not calls:
                            error = error or "NoProviderCallBudgetOrContextLimit"
                        if any(not c["settled"] for c in ledger.values()):
                            error = error or "UnsettledReservation"
                        usage = {name: sum(c["usage"][name] for c in calls if c["usage"] is not None)
                                 for name in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")}
                        result.update(case_id=case["id"], category=case["category"],
                                      usage=usage, usage_complete=all(c["usage"] is not None for c in calls),
                                      call_count=len(calls), cost_usd=str(sum(
                                          (Decimal(c["cost_usd"]) for c in ledger.values()), Decimal(0))),
                                      cost_ledger=ledger, latency_ms=round((time.monotonic()-start)*1000), error=error)
                        result["privacy_pass"] &= all(c["privacy_pass"] for c in calls)
                        result["pass"] = error is None and all(result[k] for k in (
                            "action_pass", "sku_pass", "facts_pass", "privacy_pass"))
                        report.write(json.dumps(result, ensure_ascii=False) + "\n")
                        report.flush()
                        results.append(result)
            finally:
                await llm.close()
                metadata.update(finished_at_utc=datetime.now(timezone.utc).isoformat(),
                                total_cost_usd=str(sum((Decimal(r["cost_usd"]) for r in ledger_total(ledger_path).values()), Decimal(0))),
                                status="completed" if len(results) == 40 else "incomplete")
                meta_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    counts = {name: sum(r["pass"] for r in results if r["category"] == name)
              for name in ("unique", "ambiguous", "unknown")}
    passed = counts["unique"] >= 18 and counts["ambiguous"] == 10 and counts["unknown"] == 10
    passed &= all(r["facts_pass"] and r["privacy_pass"] for r in results)
    (output / "summary.md").write_text(
        f"# Real model evaluation\n\nUnique: {counts['unique']}/20; ambiguous: {counts['ambiguous']}/10; "
        f"unknown: {counts['unknown']}/10.\n\nThreshold passed: {bool(passed)}. "
        f"Cost including retained reservations: {metadata['total_cost_usd']} USD.\n\n"
        "Checks cover real LLM decisions, public tool payloads and server-generated catalog cards. "
        "They do not verify Telegram delivery or independent rendering of factual fields.\n", encoding="utf-8")
    return 0 if passed else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=ROOT / "evals/cases.json")
    parser.add_argument("--catalog", type=Path, default=ROOT / "examples/catalog.csv")
    parser.add_argument("--allow-paid", action="store_true", help="Explicitly authorize API calls for this run")
    parser.add_argument("--output", type=Path, help="New exclusive report directory; required for paid run")
    args = parser.parse_args(argv)
    try:
        cases = load_cases(args.cases)
        with tempfile.TemporaryDirectory(prefix="shop-eval-validation-") as temporary:
            with Catalog(Path(temporary) / "catalog.sqlite3") as catalog:
                import_catalog(catalog, args.catalog)
                if any(not catalog.search(sku=sku) for case in cases for sku in case["expected_skus"]):
                    raise ValueError("Expected SKU is absent from catalog.")
        if not args.allow_paid:
            print("Validated 40 cases: 20 unique / 10 ambiguous / 10 unknown. No API calls.")
            return 0
        if args.output is None:
            raise ValueError("Paid run requires --output pointing to a new directory.")
        env = dict(os.environ, _EVAL_CASES_PATH=str(args.cases))
        return asyncio.run(evaluate(cases, args.catalog, args.output, env))
    except (ValueError, OSError, KeyError, TypeError) as error:
        parser.exit(2, f"Evaluation failed: {type(error).__name__}. Check arguments and explicit environment settings.\n")


if __name__ == "__main__":
    raise SystemExit(main())
