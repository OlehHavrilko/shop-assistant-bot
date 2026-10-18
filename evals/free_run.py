"""Run real local demo search and card checks. No model or network client is imported."""

import argparse
import csv
import hashlib
import json
import tempfile
from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from shop_assistant.catalog import Catalog
from shop_assistant.importer import import_catalog
from shop_assistant.storefront import Storefront

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = ("purchase_price_uah_per_m", "supplier")


def load_cases(path):
    cases = json.loads(Path(path).read_text(encoding="utf-8"))["cases"]
    if Counter(case["category"] for case in cases) != {"unique": 20, "ambiguous": 10, "unknown": 10}:
        raise ValueError("Expected 20 unique, 10 ambiguous and 10 unknown/FAQ cases.")
    if len({case["id"] for case in cases}) != 40:
        raise ValueError("Expected 40 distinct case IDs.")
    for case in cases:
        skus = case["expected_skus"]
        if not isinstance(case["query"], str) or not case["query"].strip():
            raise ValueError("Every case needs a query.")
        if not isinstance(skus, list) or any(type(sku) is not str for sku in skus) or len(set(skus)) != len(skus):
            raise ValueError("Invalid expected SKUs.")
        category, action = case["category"], case["expected_action"]
        if not ((category == "unique" and action == "product" and len(skus) == 1)
                or (category == "ambiguous" and action == "clarify" and 2 <= len(skus) <= 10)
                or (category == "unknown" and not skus and action in
                    ("not_found", "delivery", "payment", "about", "manager", "catalog"))):
            raise ValueError("Invalid category/action combination.")
    return cases


def check_card(store, product, source):
    card = store.card(product)
    expected_date = datetime.fromisoformat(source["updated_at"]).astimezone(UTC)
    expected_stock = Decimal(source["stock_m"]) if source["stock_m"] else None
    facts_ok = (product.name == source["name"] and product.shade == source["shade"]
                and product.price_uah_per_m == Decimal(source["price_uah_per_m"])
                and product.stock_m == expected_stock and product.updated_at == expected_date
                and product.sku in card and source["name"] in card and source["shade"] in card
                and f"{Decimal(source['price_uah_per_m']):.2f} грн/м" in card
                and expected_date.isoformat() in card)
    stock_text = ("уточнює менеджер" if expected_stock is None else
                  "немає в наявності" if expected_stock == 0 else f"{expected_stock:.2f} м")
    facts_ok = facts_ok and stock_text in card
    public = asdict(product)
    privacy_ok = not any(field in public or field in card for field in PRIVATE)
    # Injected private sentinels prove the display does not leak closed values,
    # even when their values would otherwise accidentally equal a public price.
    privacy_ok = privacy_ok and "PRIVATE_SUPPLIER_SENTINEL" not in card and "987654321.23" not in card
    photo = store.photo(product)
    photo_ok = (photo is not None and Path(photo).is_file()
                and Path(photo).read_bytes().startswith(b"\xff\xd8")
                and "Фото ілюстративне" in card)
    caption_ok = len(card.encode("utf-16-le")) // 2 <= 1024
    return {"sku": product.sku, "facts_pass": facts_ok, "privacy_pass": privacy_ok,
            "photo_pass": photo_ok, "caption_pass": caption_ok,
            "passed": bool(facts_ok and privacy_ok and photo_ok and caption_ok)}


def score(case, store):
    intent = store.intent(case["query"])
    products = [] if intent else store.search(case["query"])
    action = intent or ("not_found" if not products else "product" if len(products) == 1 else "clarify")
    actual_skus = sorted(product.sku for product in products)
    expected_skus = sorted(case["expected_skus"])
    info_ok = True
    if action in ("delivery", "payment", "about"):
        info_ok = store.shop[action] in store.info(action)
    if action == "catalog":
        info_ok = len(store.categories()) == 4
    passed = action == case["expected_action"] and actual_skus == expected_skus and info_ok
    return {**case, "actual_action": action, "actual_skus": actual_skus,
            "info_pass": info_ok, "passed": passed}


def run(cases_path, catalog_path, store_path):
    cases = load_cases(cases_path)
    with Path(catalog_path).open(encoding="utf-8-sig", newline="") as stream:
        source_rows = {row["sku"]: row for row in csv.DictReader(stream)}
    with tempfile.TemporaryDirectory(prefix="shop-free-eval-") as directory:
        with Catalog(Path(directory) / "catalog.sqlite3") as catalog:
            imported = import_catalog(catalog, catalog_path)
            # Test privacy against non-public data in the actual database.
            with catalog.connection:
                catalog.connection.execute("UPDATE products SET supplier = ?, purchase_price_uah_per_m = ?",
                                           ("PRIVATE_SUPPLIER_SENTINEL", "987654321.23"))
            store = Storefront(catalog, Path(store_path))
            products = [catalog.search(sku=sku)[0] for sku in source_rows]
            assets_ok = (imported.count == 20 and set(store.metadata) == set(source_rows)
                         and {sku for case in cases if case["category"] == "unique"
                              for sku in case["expected_skus"]} == set(source_rows))
            cards = [check_card(store, product, source_rows[product.sku]) for product in products]
            results = [score(case, store) for case in cases]
    return {"mode": "deterministic-free-demo", "model_calls": 0,
            "scope": "Actual local search, intent routing, catalog facts, card privacy and local photos; no generative model or real Telegram delivery evaluated.",
            "created_at": datetime.now(UTC).isoformat(),
            "inputs_sha256": {name: hashlib.sha256(Path(path).read_bytes()).hexdigest()
                              for name, path in (("cases", cases_path), ("catalog", catalog_path), ("store", store_path))},
            "assets_pass": assets_ok, "cards": cards, "cases": results,
            "passed_cases": sum(result["passed"] for result in results),
            "passed": bool(assets_ok and all(card["passed"] for card in cards)
                           and all(result["passed"] for result in results))}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=ROOT / "evals/free_cases.json")
    parser.add_argument("--catalog", type=Path, default=ROOT / "examples/store_catalog.csv")
    parser.add_argument("--store", type=Path, default=ROOT / "examples/store.json")
    parser.add_argument("--output", type=Path, help="New JSON report file; existing files are never replaced.")
    args = parser.parse_args(argv)
    if args.output is not None and args.output.exists():
        parser.error("Output already exists; choose a new path.")
    try:
        report = run(args.cases, args.catalog, args.store)
        if args.output is not None:
            with args.output.open("x", encoding="utf-8") as stream:
                json.dump(report, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
    except (ValueError, KeyError, OSError) as exc:
        parser.error(str(exc))
    print(f"Free deterministic demo: {report['passed_cases']}/40 queries; "
          f"{sum(card['passed'] for card in report['cards'])}/20 cards. No model or network calls.")
    for result in report["cases"]:
        if not result["passed"]:
            print(f"FAIL {result['id']} {result['query']!r}: "
                  f"expected {result['expected_action']} {result['expected_skus']}; "
                  f"actual {result['actual_action']} {result['actual_skus']}")
    for card in report["cards"]:
        if not card["passed"]:
            print(f"FAIL card {card['sku']}: {card}")
    if not report["assets_pass"]:
        print("FAIL assets: expected 20 matched catalog/metadata SKUs and full unique-case coverage.")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
