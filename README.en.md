# Shop Assistant Bot

[![Tests](https://github.com/OlehHavrilko/shop-assistant-bot/actions/workflows/tests.yml/badge.svg)](https://github.com/OlehHavrilko/shop-assistant-bot/actions/workflows/tests.yml)

**A Telegram bot for a shop: a photo catalog, product search by keywords and a hand-off to a human manager.** It runs without paid AI, on a single VPS, with data imported from Excel/CSV.

[▶ Open the demo in Telegram](https://t.me/shopasisstanthvrlko_bot) · [Українська версія](README.md)

The demo is a fictional Ukrainian fabric store: 20 products, four categories and licensed illustrative photos. The bot interface is in Ukrainian. Prices and stock are demo data. The bot does not take orders or reserve stock.

## Features

- **Customers:** a category catalog with photo cards, search in Ukrainian, Russian and English with typo correction, shop and delivery info, and "Ask a manager" from any product card.
- **Manager:** a ticket desk with active tickets, history and archive. The manager replies with a plain message or a native Telegram Reply. Each ticket gets its own Telegram topic and keeps the product context.
- **Owner:** the catalog is replaced by one atomic CSV/XLSX import. Prices and stock come only from the database. Purchase prices and suppliers never reach customers.

## Engineering

- `update_id` deduplication, a persistent outbox with retries (at-least-once delivery) and graceful SIGTERM shutdown.
- SQLite with `Decimal` prices and full import validation that reports row numbers.
- Deterministic search: exact SKU first, then synonyms and conservative typo correction. Unknown words never return a random product.
- An optional Anthropic integration with a daily budget, reserved in SQLite before each call.
- A non-root Docker image with no inbound ports and a named volume.
- CI runs `ruff` and `pytest` on Python 3.11/3.12, with and without the AI SDK.
- 187 tests plus a 40-query search evaluation.

## Quick start

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
python -m shop_assistant import examples/store_catalog.csv
python -m shop_assistant search --sku 00123
pytest -q
```

To run the Telegram bot, set the environment variables `TELEGRAM_BOT_TOKEN`, `MANAGER_TELEGRAM_ID` and `STORE_DATA_PATH=examples/store.json`, then run `python -m shop_assistant run`. To run it in Docker, use `docker compose --env-file .env up -d --build`. See [.env.example](.env.example) and [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

Code: [MIT](LICENSE). Photos: Pexels License, see [docs/MEDIA_SOURCES.md](docs/MEDIA_SOURCES.md).
