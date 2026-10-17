import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from shop_assistant.catalog import Catalog, Product
from shop_assistant.storefront import Storefront


@pytest.fixture
def storefront(tmp_path):
    database = Catalog(tmp_path / "catalog.sqlite3")
    database.replace([{
        "sku": sku, "name": name, "shade": shade,
        "price_uah_per_m": "345.50", "stock_m": stock,
        "updated_at": "2026-10-17T09:00:00+00:00",
        "purchase_price_uah_per_m": "123.45", "supplier": "PRIVATE SUPPLIER",
    } for sku, name, shade, stock in (
        ("00123", "Льон Siena", "ivory", "12.75"),
        ("123", "Льон Siena", "білий", "0"),
        ("COT-010", "Бавовна Porto", "синій", None),
    )])
    store = {
        "name": "Полотно", "tagline": "Тканини для власних ідей",
        "about": "Вигаданий магазин тканин.", "delivery": "Демо: доставка за погодженням.",
        "payment": "Демо: платежі не приймаємо.",
        "categories": [{"id": "linen", "name": "Льон"}, {"id": "cotton", "name": "Бавовна"}],
        "products": [{
            "sku": sku, "category": category, "composition": "100% натуральне волокно",
            "width_cm": 140, "density_g_m2": 180, "description": "Тканина для щоденного одягу.",
            "uses": ["сукні", "сорочки"], "tags": [], "photo": "fabric.jpg",
        } for sku, category in (("00123", "linen"), ("123", "linen"), ("COT-010", "cotton"))],
    }
    path = tmp_path / "store.json"
    path.write_text(json.dumps(store, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "media").mkdir()
    (tmp_path / "media" / "fabric.jpg").write_bytes(b"photo-fixture")
    yield Storefront(database, path)
    database.close()


@pytest.mark.parametrize("text,skus", [
    ("00123", ["00123"]), ("123", ["123"]),
    ("Чи є у вас льон айворі?", ["00123"]),
    ("хлопок синий", ["COT-010"]),
    ("  LINEN, IVORY!", ["00123"]),
    ("тканина для сукні", ["00123", "123", "COT-010"]),
    ("льон", ["00123", "123"]),
    ("льон неіснуючий", []), ("шовк", []), ("покажи тканину", []),
    ("", []), ("?", []),
])
def test_free_search_and_ambiguity(storefront, text, skus):
    assert [product.sku for product in storefront.search(text)] == skus


def test_categories_pagination_and_removed_products(storefront):
    assert storefront.categories() == [("linen", "Льон"), ("cotton", "Бавовна")]
    first, more = storefront.category_products("linen", limit=1)
    assert [product.sku for product in first] == ["00123"] and more
    last, more = storefront.category_products("linen", offset=1, limit=1)
    assert [product.sku for product in last] == ["123"] and not more
    assert storefront.category_products("unknown") == ([], False)
    storefront.catalog.connection.execute("DELETE FROM products WHERE sku = '00123'")
    assert storefront.search("льон айворі") == []
    assert [p.sku for p in storefront.category_products("linen")[0]] == ["123"]


def test_card_prices_are_current_public_sqlite_facts(storefront):
    storefront.catalog.connection.execute(
        "UPDATE products SET price_uah_per_m = '222.22', stock_m = '1.50' WHERE sku = '00123'"
    )
    product = storefront.search("00123")[0]
    caption = storefront.card(product)
    for text in ("222.22 грн/м", "1.50 м", "140 см", "180 г/м²", "00123", "Демонстраційний"):
        assert text in caption
    assert "123.45" not in caption and "PRIVATE" not in caption
    assert len(caption.encode("utf-16-le")) // 2 <= 1024
    assert "немає в наявності" in storefront.card(storefront.search("123")[0])
    assert "уточнює менеджер" in storefront.card(storefront.search("COT-010")[0])


def test_photo_and_shop_information(storefront):
    photo = storefront.photo(storefront.search("00123")[0])
    assert photo.endswith("fabric.jpg")
    assert "Полотно" in storefront.welcome()
    for section in ("about", "payment", "delivery"):
        assert "Вигаданий магазин" in storefront.info(section)
    (storefront.path.parent / "media" / "fabric.jpg").unlink()
    assert storefront.photo(storefront.search("00123")[0]) is None


@pytest.mark.parametrize("filename", ["../secret.jpg", "https://example.com/a.jpg", "C:\\secret.jpg", "file.txt"])
def test_media_path_traversal_is_rejected(storefront, filename):
    data = json.loads(storefront.path.read_text(encoding="utf-8"))
    data["products"][0]["photo"] = filename
    storefront.path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="локального"):
        Storefront(storefront.catalog, storefront.path)


def test_missing_metadata_preserves_legacy_catalog(storefront):
    fallback = Storefront(storefront.catalog, storefront.path.parent / "missing.json")
    assert fallback.categories() == [("all", "Усі тканини")]
    assert [p.sku for p in fallback.search("лен ivory")] == ["00123"]
    assert fallback.photo(fallback.search("00123")[0]) is None
    assert "Полотно" not in fallback.welcome()
    assert "345.50 грн/м" in fallback.card(fallback.search("00123")[0])


def test_caption_limit_handles_long_unicode_facts(storefront):
    product = Product("😀" * 200, "😀" * 200, "😀" * 200, Decimal("100.00"),
                      Decimal("1.00"), datetime.now(timezone.utc))
    assert len(storefront.card(product).encode("utf-16-le")) // 2 <= 1024


@pytest.mark.parametrize("text,intent", [("Як оплатити?", "payment"), ("доставка", "delivery"),
                                      ("Про магазин", "about"), ("менеджера", "manager"),
                                      ("Каталог", "catalog"), ("льон айворі", None)])
def test_shop_intents(storefront, text, intent):
    assert storefront.intent(text) == intent


def test_invalid_metadata_is_not_silently_accepted(storefront):
    data = json.loads(storefront.path.read_text(encoding="utf-8"))
    data["products"][0]["width_cm"] = True
    storefront.path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="характеристики"):
        Storefront(storefront.catalog, storefront.path)
