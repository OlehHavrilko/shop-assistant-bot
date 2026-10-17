import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendPhoto
from aiogram.types import FSInputFile

from shop_assistant.catalog import Catalog
from shop_assistant.storage import Storage
from shop_assistant.telegram import deliver_pending


def test_v1_outbox_migrates_without_losing_delivered_or_pending_messages(tmp_path):
    path = tmp_path / 'legacy.sqlite3'
    with Catalog(path) as catalog:
        catalog.connection.executescript('''
            CREATE TABLE dialog_schema (version INTEGER NOT NULL);
            INSERT INTO dialog_schema VALUES (1);
            CREATE TABLE outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
                text TEXT NOT NULL, buttons TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL DEFAULT 0, delivered INTEGER NOT NULL DEFAULT 0);
            INSERT INTO outbox (chat_id,text,buttons,delivered) VALUES (10,'earlier','[]',1);
            INSERT INTO outbox (chat_id,text,buttons,attempts) VALUES (10,'pending','[]',2);
        ''')
        storage = Storage(catalog.connection)
        assert catalog.connection.execute('SELECT version FROM dialog_schema').fetchone()[0] == 3
        pending = storage.pending()
        assert len(pending) == 1 and pending[0].text == 'pending' and pending[0].attempts == 2
        assert pending[0].photo is None
        assert catalog.connection.execute('SELECT COUNT(*) FROM outbox WHERE delivered=1').fetchone()[0] == 1
        # Reopening the domain schema does not repeat ALTER or clear existing data.
        Storage(catalog.connection)
        assert len(storage.pending()) == 1


def test_photo_and_buttons_persist_across_restart(tmp_path):
    path = tmp_path / 'photo.sqlite3'
    with Catalog(path) as catalog:
        storage = Storage(catalog.connection)
        with catalog.connection:
            storage.enqueue(10, 'Льон Siena — ivory', [('Каталог', 'shop:catalog')], photo='examples/media/linen.jpg')
    with Catalog(path) as catalog:
        pending = Storage(catalog.connection).pending()[0]
        assert pending.photo == 'examples/media/linen.jpg'
        assert pending.buttons == (('Каталог', 'shop:catalog'),)


def test_photo_caption_limit_uses_telegram_utf16_units(tmp_path):
    with Catalog(tmp_path / 'length.sqlite3') as catalog:
        storage = Storage(catalog.connection)
        with catalog.connection, pytest.raises(ValueError, match='1024'):
            storage.enqueue(10, '🧵' * 513, photo='photo.jpg')
        assert not storage.pending()


@pytest.mark.parametrize('photo_available,telegram_rejects', [(True, False), (False, False), (True, True)])
def test_photo_delivery_or_readable_text_fallback(tmp_path, photo_available, telegram_rejects):
    async def scenario():
        photo = tmp_path / 'photo.jpg'
        if photo_available:
            photo.write_bytes(b'file-content-read-by-fake-transport')
        with Catalog(tmp_path / 'send.sqlite3') as catalog:
            storage = Storage(catalog.connection)
            with catalog.connection:
                storage.enqueue(10, 'Картка з перевіреною ціною', [('Каталог', 'shop:catalog')], photo=str(photo))
            photo_error = TelegramBadRequest(method=SendPhoto(chat_id=10, photo='synthetic'), message='synthetic')
            bot = SimpleNamespace(send_photo=AsyncMock(side_effect=photo_error if telegram_rejects else None),
                                  send_message=AsyncMock())
            assert await deliver_pending(storage, bot) == 1
            assert not storage.pending()
            if photo_available:
                assert isinstance(bot.send_photo.call_args.kwargs['photo'], FSInputFile)
                assert bot.send_photo.call_args.kwargs['caption'] == 'Картка з перевіреною ціною'
            else:
                bot.send_photo.assert_not_awaited()
            if not photo_available or telegram_rejects:
                bot.send_message.assert_awaited_once()
                assert bot.send_message.call_args.kwargs['text'] == 'Картка з перевіреною ціною'
            else:
                bot.send_message.assert_not_awaited()
    asyncio.run(scenario())
