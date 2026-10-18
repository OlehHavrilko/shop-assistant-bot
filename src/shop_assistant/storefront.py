"""Public shop presentation and deterministic search; prices always come from SQLite."""

import difflib
import json
import re
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from shop_assistant.catalog import Catalog, Product, format_product

_ALIASES = {
    "linen": "льон", "лен": "льон", "лён": "льон", "льону": "льон",
    "лляна": "льон", "лляний": "льон", "льняной": "льон",
    "льняная": "льон", "лляну": "льон", "лляні": "льон",
    "cotton": "бавовна", "хлопок": "бавовна", "хлопка": "бавовна",
    "бавовни": "бавовна", "бавовняна": "бавовна",
    "viscose": "віскоза", "вискоза": "віскоза", "віскози": "віскоза",
    "muslin": "муслін", "муслин": "муслін", "мусліну": "муслін",
    "ivory": "айворі", "айвори": "айворі", "молочний": "айворі",
    "молочна": "айворі", "молочный": "айворі",
    "blue": "синій", "синий": "синій", "синяя": "синій", "синя": "синій",
    "white": "білий", "белый": "білий", "біла": "білий", "белая": "білий",
    "black": "чорний", "черный": "чорний", "чёрный": "чорний", "чорна": "чорний",
    "green": "зелений", "зеленый": "зелений", "зелена": "зелений",
    "pink": "рожевий", "розовый": "рожевий", "рожева": "рожевий",
    "beige": "бежевий", "бежевый": "бежевий", "бежева": "бежевий",
    "grey": "сірий", "gray": "сірий", "серый": "сірий", "сіра": "сірий",
    "dress": "сукня", "сукні": "сукня", "суконь": "сукня", "платье": "сукня",
    "платья": "сукня", "платьев": "сукня",
    "сорочки": "сорочка", "рубашка": "сорочка", "рубашки": "сорочка",
    "штор": "штори", "шторы": "штори",
    "спідниці": "спідниця", "юбка": "спідниця", "юбки": "спідниця",
    "дитячого": "дитячий", "дитячі": "дитячий", "детский": "дитячий",
    "пелюшки": "пелюшка", "пелюшок": "пелюшка", "пеленки": "пелюшка",
}
_FILLER = {
    "чи", "є", "у", "вас", "мене", "мені", "мне", "есть", "ли", "в", "на", "для",
    "будь", "ласка", "пожалуйста", "покажи", "покажіть", "покажите", "потрібна",
    "потрібен", "потрібно", "нужна", "нужен", "нужно", "шукаю", "ищу", "хочу",
    "тканина", "тканини", "тканину", "ткань", "ткани", "кольору", "колір", "цвет",
    "цвета", "артикул", "sku", "знайди", "знайдіть", "найди", "please", "show",
    "скільки", "коштує", "коштують", "ціна", "ціну", "цена", "цену", "стоит",
    "стоят", "сколько", "стоимость", "підкажіть", "подскажите", "наявності", "наличии",
}


def _tokens(text: str, *, query: bool = False) -> set[str]:
    words = re.findall(r"[^\W_]+", text.casefold(), flags=re.UNICODE)
    return {_ALIASES.get(word, word) for word in words if not query or word not in _FILLER}


def _text(value, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError("Некоректний текст у файлі магазину.")
    if any(ord(char) < 32 and char not in "\n\t" for char in value):
        raise ValueError("Недопустимий символ у файлі магазину.")
    return value.strip()


def _short(text: str) -> str:
    if len(text.encode("utf-16-le")) <= 160:
        return text
    return text.encode("utf-16-le")[:156].decode("utf-16-le", errors="ignore") + "…"


class Storefront:
    def __init__(self, catalog: Catalog, path: Path | None = None):
        self.catalog = catalog
        self.path = Path(path) if path is not None else Path("examples/store.json")
        self.shop: dict = {}
        self.metadata: dict[str, dict] = {}
        self._categories: list[tuple[str, str]] = []
        if not self.path.exists():
            return
        if self.path.stat().st_size > 500_000:
            raise ValueError("Файл магазину завеликий.")
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("Очікується об'єкт магазину.")
            self.shop = {key: _text(raw[key], limit) for key, limit in (
                ("name", 80), ("tagline", 200), ("about", 1500),
                ("delivery", 1500), ("payment", 1500),
            )}
            categories = raw["categories"]
            if not isinstance(categories, list) or not 1 <= len(categories) <= 20:
                raise ValueError("Некоректні категорії магазину.")
            for category in categories:
                category_id = _text(category["id"], 30)
                if not re.fullmatch(r"[a-z][a-z0-9_]*", category_id):
                    raise ValueError("Некоректний ідентифікатор категорії.")
                if category_id in dict(self._categories):
                    raise ValueError("Повтор категорії магазину.")
                self._categories.append((category_id, _text(category["name"], 60)))
            products = raw["products"]
            if not isinstance(products, list) or len(products) > 1000:
                raise ValueError("Некоректний перелік товарів магазину.")
            for item in products:
                metadata = {key: _text(item[key], limit) for key, limit in (
                    ("sku", 200), ("category", 30), ("composition", 120),
                    ("description", 250), ("photo", 120),
                )}
                if metadata["sku"] in self.metadata or metadata["category"] not in dict(self._categories):
                    raise ValueError("Повтор товару або невідома категорія.")
                for key in ("width_cm", "density_g_m2"):
                    value = item[key]
                    if type(value) is not int or not 1 <= value <= 2000:
                        raise ValueError("Некоректні характеристики тканини.")
                    metadata[key] = value
                for key in ("uses", "tags"):
                    values = item[key]
                    if not isinstance(values, list) or len(values) > 15:
                        raise ValueError("Некоректні теги тканини.")
                    metadata[key] = [_text(value, 60) for value in values]
                self._photo_path(metadata["photo"])
                self.metadata[metadata["sku"]] = metadata
        except (KeyError, TypeError, json.JSONDecodeError, UnicodeError) as exc:
            raise ValueError("Некоректний файл магазину.") from exc

    def _photo_path(self, filename: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.(?:jpg|jpeg|png|webp)", filename):
            raise ValueError("Фото має бути ім'ям локального зображення.")
        media = self.path.parent.resolve() / "media"
        photo = (media / filename).resolve()
        if photo.parent != media:
            raise ValueError("Фото поза каталогом media.")
        return photo

    def _products(self) -> list[Product]:
        rows = self.catalog.connection.execute(
            "SELECT sku, name, shade, price_uah_per_m, stock_m, updated_at "
            "FROM products ORDER BY sku"
        )
        return [Product(row["sku"], row["name"], row["shade"],
                        Decimal(row["price_uah_per_m"]),
                        Decimal(row["stock_m"]) if row["stock_m"] is not None else None,
                        datetime.fromisoformat(row["updated_at"])) for row in rows]

    def categories(self) -> list[tuple[str, str]]:
        return list(self._categories) if self._categories else [("all", "Усі тканини")]

    def category_products(self, category: str, offset: int = 0,
                          limit: int = 4) -> tuple[list[Product], bool]:
        if type(offset) is not int or offset < 0 or not 1 <= limit <= 20:
            raise ValueError("Некоректна сторінка каталогу.")
        if category not in dict(self.categories()):
            return [], False
        products = [product for product in self._products()
                    if category == "all" or self.metadata.get(product.sku, {}).get("category") == category]
        return products[offset:offset + limit], len(products) > offset + limit

    def search(self, text: str, limit: int = 10) -> list[Product]:
        if not 1 <= limit <= 20:
            raise ValueError("Ліміт результатів має бути від 1 до 20.")
        if not isinstance(text, str) or len(text) > 4000:
            return []
        direct = self.catalog.search(sku=text.strip())
        if direct:
            return direct
        query = _tokens(text, query=True)
        if not query:
            return []
        indexed = []
        for product in self._products():
            metadata = self.metadata.get(product.sku, {})
            words = [product.name, product.shade, product.sku]
            if metadata:
                words.extend([metadata["category"], dict(self._categories)[metadata["category"]],
                              metadata["composition"], metadata["description"],
                              *metadata["uses"], *metadata["tags"]])
            indexed.append((product, _tokens(" ".join(words))))
        found = [product for product, words in indexed if query <= words]
        if not found:
            # Typo tolerance: only unknown words of 4+ letters are replaced, and only
            # by a close catalog word with the same first letter.
            vocabulary = set().union(*(words for _, words in indexed))
            vocabulary |= {alias for alias, word in _ALIASES.items() if word in vocabulary}
            corrected = set()
            for word in query:
                # Same first letter: «вовна» (wool) must not become «бавовна» (cotton).
                candidates = [known for known in vocabulary if known[0] == word[0]]
                close = (difflib.get_close_matches(word, candidates, n=1, cutoff=0.8)
                         if word not in vocabulary and len(word) >= 4 else [])
                corrected.add(_ALIASES.get(close[0], close[0]) if close else word)
            if corrected != query:
                found = [product for product, words in indexed if corrected <= words]
        return found[:limit]

    def card(self, product: Product) -> str:
        metadata = self.metadata.get(product.sku)
        basic = format_product(replace(product, name=_short(product.name), shade=_short(product.shade)))
        if metadata is None:
            return basic
        basic += "Фото ілюстративне: точний відтінок може відрізнятися."
        if product.stock_m is None:
            stock = "уточнює менеджер"
        elif product.stock_m == 0:
            stock = "немає в наявності"
        else:
            stock = f"{product.stock_m:.2f} м"
        text = (
            f"{product.name} · {product.shade}\nАртикул: {product.sku}\n"
            f"{dict(self._categories)[metadata['category']]} · {metadata['composition']}\n"
            f"Ширина {metadata['width_cm']} см · щільність {metadata['density_g_m2']} г/м²\n\n"
            f"{metadata['description']}\n"
            f"Для: {', '.join(metadata['uses'])}\n\n"
            f"{product.price_uah_per_m:.2f} грн/м · Залишок: {stock}\n"
            f"Оновлено: {product.updated_at.isoformat()}\n"
            "Демонстраційний товар. Дані довідкові; резервування немає.\n"
            "Фото ілюстративне: точний відтінок може відрізнятися."
        )
        # Telegram captions count UTF-16 code units. Keep the factual footer intact.
        if len(text.encode("utf-16-le")) // 2 > 1024:
            return basic
        return text

    def photo(self, product: Product) -> str | None:
        metadata = self.metadata.get(product.sku)
        if metadata is None:
            return None
        path = self._photo_path(metadata["photo"])
        return str(path) if path.is_file() else None

    def welcome(self) -> str:
        if not self.shop:
            return "Каталог тканин\nШукайте за назвою, кольором або артикулом. Це демонстраційний магазин."
        return (f"🧵 {self.shop['name']} · магазин тканин\n{self.shop['tagline']}\n\n"
                "Вітаємо! Зазирніть у каталог або напишіть, яку тканину шукаєте.\n"
                "Наприклад: «льон айворі» чи «тканина для сукні».\n\n"
                "💬 Маєте запитання? Натисніть «Написати менеджеру».\n\n"
                "Демо-магазин: товари й дані вигадані. Замовлення та оплата тут не здійснюються.")

    def info(self, kind: str) -> str:
        if kind not in ("about", "delivery", "payment"):
            raise ValueError("Невідомий розділ магазину.")
        if not self.shop:
            return "Це демонстраційний каталог тканин. Деталі уточнюйте у менеджера: /manager."
        headings = {'about': 'ℹ️ Про магазин', 'delivery': '🚚 Доставка й оплата', 'payment': '💳 Оплата'}
        return f"{headings[kind]}\n\n{self.shop[kind]}\n\nВигаданий магазин для демонстрації. Замовлення не оформлюються."

    @staticmethod
    def intent(text: str) -> str | None:
        if len(text) > 4000:
            return None
        words = _tokens(text)
        for intent, terms in (
            ("manager", {"менеджер", "менеджера", "оператор", "людина", "людину"}),
            ("delivery", {"доставка", "доставки", "доставку", "доставляєте", "доставляете"}),
            ("payment", {"оплата", "оплату", "оплати", "оплатити", "оплатить"}),
            ("catalog", {"каталог", "асортимент", "ассортимент"}),
        ):
            if words & terms:
                return intent
        if words in ({"про", "магазин"}, {"о", "магазине"}, {"about"}):
            return "about"
        return None
