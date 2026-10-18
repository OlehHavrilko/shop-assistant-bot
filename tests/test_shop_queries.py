from pathlib import Path

import pytest

from shop_assistant.catalog import Catalog
from shop_assistant.importer import import_catalog
from shop_assistant.storefront import Storefront

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('query,equivalent', [
    ('Скільки коштує льон Siena?', 'льон Siena'),
    ('ціна на льон ivory', 'льон ivory'),
    ('подскажите, есть хлопок синий в наличии?', 'хлопок синий'),
    ('Підкажіть, чи є лляна тканина?', 'льон'),
    ('Сколько стоит льняная ткань?', 'льон'),
])
def test_common_shopping_phrases_keep_product_filters(tmp_path, query, equivalent):
    with Catalog(tmp_path / 'shop.sqlite3') as catalog:
        import_catalog(catalog, ROOT / 'examples/store_catalog.csv')
        shop = Storefront(catalog, ROOT / 'examples/store.json')
        expected = [product.sku for product in shop.search(equivalent)]
        assert expected
        assert [product.sku for product in shop.search(query)] == expected
        assert shop.search(query + ' неіснуючийматеріал') == []
