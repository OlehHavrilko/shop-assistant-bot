import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import SendMessage
from aiogram.types import Update

from shop_assistant.catalog import Catalog
from shop_assistant.dialogs import Dialogs
from shop_assistant.telegram import deliver_pending, handle_update, poll_updates


def update(update_id, chat_id=100, *, private=True, text="/start", callback=None):
    message = {"message_id": 1, "date": 1700000000,
               "chat": {"id": chat_id, "type": "private" if private else "group"},
               "from": {"id": chat_id, "is_bot": False, "first_name": "Synthetic"}, "text": text}
    value = {"update_id": update_id}
    if callback:
        value["callback_query"] = {"id": "synthetic", "from": message["from"],
                                    "chat_instance": "synthetic", "data": callback, "message": message}
    else:
        value["message"] = message
    return Update.model_validate(value)


def test_adapter_ignores_groups_and_passes_update_id_and_callbacks():
    async def scenario():
        dialogs = SimpleNamespace(handle=AsyncMock())
        bot = SimpleNamespace(answer_callback_query=AsyncMock())
        await handle_update(dialogs, bot, update(1, private=False))
        dialogs.handle.assert_not_awaited()
        await handle_update(dialogs, bot, update(2, text="00123"))
        dialogs.handle.assert_awaited_once_with(2, 100, text="00123", private=True)
        await handle_update(dialogs, bot, update(3, callback="pick:synthetic"))
        dialogs.handle.assert_awaited_with(3, 100, callback_data="pick:synthetic")
        bot.answer_callback_query.assert_awaited_once_with("synthetic")
    asyncio.run(scenario())


def test_failed_delivery_persists_and_other_recipient_receives(tmp_path):
    async def scenario():
        path = tmp_path / "outbox.sqlite3"
        with Catalog(path) as catalog:
            dialogs = Dialogs(catalog, manager_id=999)
            await dialogs.handle(1, 100, "/manager")
            bot = SimpleNamespace(send_message=AsyncMock(side_effect=[
                TelegramForbiddenError(method=SendMessage(chat_id=100, text="test"), message="synthetic"), None,
            ]))
            assert await deliver_pending(dialogs.storage, bot) == 1
            assert dialogs.storage.dialog(100)["mode"] == "HUMAN"
        with Catalog(path) as catalog:
            dialogs = Dialogs(catalog, manager_id=999)
            pending = dialogs.storage.pending(now=10**12)
            assert len(pending) == 1 and pending[0].chat_id == 100 and pending[0].attempts == 1
    asyncio.run(scenario())


def test_retry_after_keeps_message_and_does_not_send_early(tmp_path):
    async def scenario():
        with Catalog(tmp_path / "retry.sqlite3") as catalog:
            dialogs = Dialogs(catalog, manager_id=999)
            await dialogs.handle(1, 100, "/start")
            bot = SimpleNamespace(send_message=AsyncMock(side_effect=TelegramRetryAfter(
                method=SendMessage(chat_id=100, text="test"), message="synthetic", retry_after=30)))
            assert await deliver_pending(dialogs.storage, bot) == 0
            assert not dialogs.storage.pending()
            assert dialogs.storage.pending(now=10**12)[0].attempts == 1
    asyncio.run(scenario())


def test_polling_acknowledges_only_after_durable_processing():
    async def scenario():
        stop = asyncio.Event()
        calls = []
        class DialogsFake:
            async def handle(self, update_id, *args, **kwargs):
                calls.append(update_id)
        async def fetch(**kwargs):
            if kwargs["offset"] is None:
                return [update(10), update(11)]
            assert calls == [10, 11] and kwargs["offset"] == 12
            stop.set()
            return []
        await poll_updates(DialogsFake(), SimpleNamespace(get_updates=fetch), stop)
    asyncio.run(scenario())


def test_storage_failure_stops_polling_without_ack():
    async def scenario():
        async def fail(*args, **kwargs):
            raise RuntimeError("synthetic database failure")
        bot = SimpleNamespace(get_updates=AsyncMock(return_value=[update(10)]))
        with pytest.raises(RuntimeError, match="database"):
            await poll_updates(SimpleNamespace(handle=fail), bot, asyncio.Event())
        assert bot.get_updates.await_count == 1
    asyncio.run(scenario())


def test_stop_during_delivery_leaves_backlog_for_restart(tmp_path):
    async def scenario():
        with Catalog(tmp_path / "stop.sqlite3") as catalog:
            dialogs = Dialogs(catalog, manager_id=999)
            await dialogs.handle(1, 100, "/start")
            await dialogs.handle(2, 200, "/start")
            stop = asyncio.Event()
            async def send(**kwargs):
                assert kwargs["request_timeout"] == 10
                stop.set()
            bot = SimpleNamespace(send_message=AsyncMock(side_effect=send))
            assert await deliver_pending(dialogs.storage, bot, stop=stop) == 1
            assert len(dialogs.storage.pending()) == 1
            assert bot.send_message.await_count == 1
    asyncio.run(scenario())


def test_initialization_failure_closes_acquired_llm(tmp_path, monkeypatch):
    from dataclasses import replace
    from shop_assistant.config import Config
    from shop_assistant.telegram import run_bot
    from test_config import paid_env
    import shop_assistant.dialogs
    import shop_assistant.llm

    close = AsyncMock()
    monkeypatch.setattr(shop_assistant.llm, "LLM", lambda *args, **kwargs: SimpleNamespace(close=close))
    def fail(*args, **kwargs):
        raise ValueError("synthetic migration failure")
    monkeypatch.setattr(shop_assistant.dialogs, "Dialogs", fail)
    path = tmp_path / "init.sqlite3"
    config = replace(Config.from_env(paid_env()), database_path=path)
    with Catalog(path) as catalog, pytest.raises(ValueError, match="migration"):
        asyncio.run(run_bot(config, catalog))
    close.assert_awaited_once()
