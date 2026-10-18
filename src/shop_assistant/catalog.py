import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path


def normalize(value: str) -> str:
    return " ".join(value.split()).casefold()


@dataclass(frozen=True)
class Product:
    sku: str
    name: str
    shade: str
    price_uah_per_m: Decimal
    stock_m: Decimal | None
    updated_at: datetime


@dataclass(frozen=True)
class ImportReport:
    count: int
    loaded_at: datetime


class Catalog:
    def __init__(self, path: str | Path):
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        try:
            with self.connection:
                version = self.connection.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, 1):
                    raise ValueError("Непідтримувана версія бази даних.")
                if version == 0:
                    self.connection.execute("BEGIN")
                    self.connection.execute("""
                        CREATE TABLE products (
                            sku TEXT PRIMARY KEY, name TEXT NOT NULL,
                            shade TEXT NOT NULL, price_uah_per_m TEXT NOT NULL,
                            stock_m TEXT, updated_at TEXT NOT NULL,
                            purchase_price_uah_per_m TEXT, supplier TEXT
                        )
                    """)
                    self.connection.execute("""
                        CREATE TABLE catalog_import (
                            id INTEGER PRIMARY KEY CHECK (id = 1),
                            loaded_at TEXT NOT NULL, count INTEGER NOT NULL
                        )
                    """)
                    self.connection.execute("PRAGMA user_version = 1")
        except Exception:
            self.connection.close()
            raise

    def close(self) -> None:
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def replace(self, rows: list[dict]) -> ImportReport:
        loaded_at = datetime.now(UTC)
        with self.connection:
            self.connection.execute("DELETE FROM products")
            self.connection.executemany(
                """INSERT INTO products VALUES (
                    :sku, :name, :shade, :price_uah_per_m, :stock_m,
                    :updated_at, :purchase_price_uah_per_m, :supplier
                )""", rows,
            )
            self.connection.execute(
                "INSERT OR REPLACE INTO catalog_import VALUES (1, ?, ?)",
                (loaded_at.isoformat(), len(rows)),
            )
        return ImportReport(len(rows), loaded_at)

    def search(self, *, sku: str | None = None, name: str | None = None,
               shade: str | None = None, limit: int = 10) -> list[Product]:
        if not 1 <= limit <= 20:
            raise ValueError("Ліміт результатів має бути від 1 до 20.")
        # Projection is explicit: internal fields never enter the public model.
        columns = "sku, name, shade, price_uah_per_m, stock_m, updated_at"
        if sku is not None:
            rows = self.connection.execute(
                f"SELECT {columns} FROM products WHERE sku = ?", (sku.strip(),)
            )
        elif name or shade:
            self.connection.create_function("normalize", 1, normalize)
            rows = self.connection.execute(
                f"SELECT {columns} FROM products "
                "WHERE instr(normalize(name), ?) > 0 "
                "AND instr(normalize(shade), ?) > 0 ORDER BY sku LIMIT ?",
                (normalize(name or ""), normalize(shade or ""), limit),
            )
        else:
            return []
        return [Product(
            row["sku"], row["name"], row["shade"], Decimal(row["price_uah_per_m"]),
            Decimal(row["stock_m"]) if row["stock_m"] is not None else None,
            datetime.fromisoformat(row["updated_at"]),
        ) for row in rows]


def format_product(product: Product) -> str:
    if product.stock_m is None:
        stock = "немає даних"
    elif product.stock_m == 0:
        stock = "немає в наявності"
    else:
        stock = f"{product.stock_m:.2f} м"
    return (
        f"{product.name} — {product.shade}\nАртикул: {product.sku}\n"
        f"Ціна: {product.price_uah_per_m:.2f} грн/м\nЗалишок: {stock}\n"
        f"Оновлено: {product.updated_at.isoformat()}\n"
        "Демонстраційний каталог. Наявність довідкова, товар не зарезервовано."
    )
