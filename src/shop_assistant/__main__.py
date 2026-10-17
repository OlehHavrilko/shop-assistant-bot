import argparse
import sqlite3
from pathlib import Path
from zipfile import BadZipFile
from xml.etree.ElementTree import ParseError
import csv
import asyncio
import sys

from .catalog import Catalog, format_product
from .importer import import_catalog


def main() -> int:
    parser = argparse.ArgumentParser(description="Демонстраційний каталог магазину тканин")
    parser.add_argument("--db", type=Path, default=Path("data/catalog.sqlite3"))
    commands = parser.add_subparsers(dest="command", required=True)
    importer = commands.add_parser("import", help="Повністю замінити каталог з CSV/XLSX")
    importer.add_argument("file", type=Path)
    search = commands.add_parser("search", help="Пошук у каталозі")
    search.add_argument("--sku")
    search.add_argument("--name")
    search.add_argument("--shade")
    commands.add_parser("run", help="Запустити Telegram-бота (налаштування через оточення)")
    args = parser.parse_args()
    if args.command == "search" and not any((args.sku, args.name, args.shade)):
        parser.error("вкажіть --sku, --name або --shade")
    try:
        if args.command == "run":
            from .config import Config
            from .telegram import run_bot

            config = Config.from_env()
            config.database_path.parent.mkdir(parents=True, exist_ok=True)
            with Catalog(config.database_path) as catalog:
                bootstrap_catalog(catalog, config.demo_catalog_path)
                asyncio.run(run_bot(config, catalog))
            return 0
        args.db.parent.mkdir(parents=True, exist_ok=True)
        with Catalog(args.db) as catalog:
            if args.command == "import":
                report = import_catalog(catalog, args.file)
                print(f"Завантажено записів: {report.count}. Час: {report.loaded_at.isoformat()}")
            else:
                products = catalog.search(sku=args.sku, name=args.name, shade=args.shade)
                print("\n\n".join(map(format_product, products)) if products else "Товар не знайдено.")
    except (ValueError, OSError, sqlite3.Error, BadZipFile, ParseError, csv.Error) as error:
        if isinstance(error, ValueError):
            parser.exit(1, f"Помилка: {error}\n")
        parser.exit(1, "Не вдалося прочитати каталог або базу даних. Перевірте файл та права доступу.\n")
    return 0


def bootstrap_catalog(catalog: Catalog, path: Path | None) -> None:
    if path is not None and not catalog.connection.execute("SELECT 1 FROM products LIMIT 1").fetchone():
        import_catalog(catalog, path)


def entrypoint() -> None:
    # Keep Ukrainian output readable in redirected Windows terminals as well.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0)
    except Exception as error:
        # SDK errors may contain connection URLs; never echo exception strings.
        raise SystemExit(f"Запуск зупинено: {type(error).__name__}. Перевірте налаштування та доступність сервісів.") from None


if __name__ == "__main__":
    entrypoint()
