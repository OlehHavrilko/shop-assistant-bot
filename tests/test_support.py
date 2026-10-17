import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import SendMessage

from shop_assistant.catalog import Catalog
from shop_assistant.dialogs import Dialogs
from shop_assistant.storage import Storage
from shop_assistant.importer import import_catalog
from shop_assistant.telegram import deliver_pending, handle_update
from test_dialogs import messages, run
from test_telegram import update


@pytest.fixture
def dialogs(tmp_path):
    with Catalog(tmp_path / 'support.sqlite3') as catalog:
        yield Dialogs(catalog, 999)


def test_inbox_open_reply_and_back_do_not_mix_two_customers(dialogs):
    run(dialogs, 1, 10, '/manager')
    run(dialogs, 2, 20, '/manager')
    run(dialogs, 3, 10, 'Питання про льон')
    run(dialogs, 4, 20, 'Питання про бавовну')
    run(dialogs, 5, 999, '/tickets')
    inbox = messages(dialogs, 999)[-1]
    assert 'Кабінет менеджера' in inbox.text
    assert any(button[1] == 'ticket:open:1' for button in inbox.buttons)
    run(dialogs, 6, 999, callback_data='ticket:open:2')
    assert any('Питання про бавовну' in m.text for m in messages(dialogs, 999))
    run(dialogs, 7, 999, 'Відповідь про бавовну')
    assert messages(dialogs, 20)[-1].text == 'Менеджер: Відповідь про бавовну'
    assert not any('Відповідь про бавовну' in m.text for m in messages(dialogs, 10))
    run(dialogs, 8, 999, callback_data='shop:tickets')
    before = len(messages(dialogs, 20))
    run(dialogs, 9, 999, 'Вже не відповідь')
    assert len(messages(dialogs, 20)) == before


@pytest.mark.parametrize('command', ['/start', '/help'])
def test_manager_home_preserves_storefront_and_leaves_reply_mode(dialogs, command):
    run(dialogs, 1, 10, '/manager')
    run(dialogs, 2, 999, callback_data='ticket:open:1')
    run(dialogs, 3, 999, command)
    home = messages(dialogs, 999)[-1]
    assert 'Shop Assistant' in home.text
    assert ['Каталог тканин', 'shop:catalog'] in home.buttons
    assert ['Кабінет менеджера', 'shop:tickets'] in home.buttons
    assert not dialogs.catalog.connection.execute('SELECT * FROM support_sessions').fetchall()
    before = len(messages(dialogs, 10))
    run(dialogs, 4, 999, 'Запит до каталогу')
    assert len(messages(dialogs, 10)) == before
    run(dialogs, 5, 10, '/start')
    assert not any(b[1] == 'shop:tickets' for m in messages(dialogs, 10) for b in m.buttons)


def test_native_reply_uses_actual_sent_message_even_when_other_ticket_is_selected(dialogs):
    async def scenario():
        await dialogs.handle(1, 10, '/manager')
        await dialogs.handle(2, 20, '/manager')
        bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=501)))
        await deliver_pending(dialogs.storage, bot)
        await dialogs.handle(3, 999, callback_data='ticket:open:2')
        incoming = update(4, 999, text='Відповідь через Reply')
        original = incoming.message.model_copy(update={
            'message_id': 501, 'from_user': incoming.message.from_user.model_copy(update={'is_bot': True})})
        incoming = incoming.model_copy(update={'message': incoming.message.model_copy(update={'reply_to_message': original})})
        await handle_update(dialogs, bot, incoming)
        assert messages(dialogs, 10)[-1].text == 'Менеджер: Відповідь через Reply'
        assert not any('Відповідь через Reply' in m.text for m in messages(dialogs, 20))
    asyncio.run(scenario())


def test_closed_or_unknown_reply_never_falls_back_to_selected_customer(dialogs):
    run(dialogs, 1, 10, '/manager')
    run(dialogs, 2, 20, '/manager')
    outgoing = next(m for m in dialogs.storage.pending() if m.chat_id == 999)
    dialogs.storage.sent(outgoing.id, 44)
    run(dialogs, 3, 10, callback_data='client:close:1')
    run(dialogs, 4, 999, callback_data='ticket:open:2')
    run(dialogs, 5, 999, 'Не надсилати іншому', reply_to_message_id=44)
    run(dialogs, 6, 999, 'Теж не надсилати', reply_to_message_id=99999)
    assert not any('Не надсилати' in m.text or 'Теж не надсилати' in m.text for m in messages(dialogs, 20))


@pytest.mark.parametrize('data', ['ticket:open:1', 'ticket:history:1:0', 'ticket:close:1',
                                  'ticket:confirm:1', 'ticket:list:1:0', 'shop:tickets'])
def test_customer_cannot_read_or_close_someone_elses_ticket(dialogs, data):
    run(dialogs, 1, 10, '/manager')
    run(dialogs, 2, 10, 'Приватне питання')
    run(dialogs, 3, 20, callback_data=data)
    assert not any('Приватне питання' in m.text for m in messages(dialogs, 20))
    assert dialogs.support.ticket(1)['active'] == 1


def test_customer_status_close_and_archive_preserve_conversation(dialogs):
    run(dialogs, 1, 10, '/manager')
    run(dialogs, 2, 10, 'Питання клієнта')
    run(dialogs, 3, 999, '/reply 1 Відповідь менеджера')
    run(dialogs, 4, 10, callback_data='client:status:1')
    assert any('Питання клієнта' in m.text for m in messages(dialogs, 10))
    run(dialogs, 5, 10, callback_data='client:confirm:1')
    assert dialogs.support.ticket(1)['active'] == 1
    run(dialogs, 6, 10, callback_data='client:close:1')
    assert dialogs.storage.dialog(10)['mode'] == 'BOT'
    run(dialogs, 7, 999, callback_data='ticket:list:0:0')
    assert any(b[1] == 'ticket:open:1' for b in messages(dialogs, 999)[-1].buttons)
    run(dialogs, 8, 999, callback_data='ticket:open:1')
    assert any('Відповідь менеджера' in m.text for m in messages(dialogs, 999))
    assert not dialogs.catalog.connection.execute('SELECT * FROM support_sessions').fetchall()


def test_reply_delivery_status_is_pending_until_telegram_accepts(dialogs):
    run(dialogs, 1, 10, '/manager')
    run(dialogs, 2, 999, '/reply 1 Тест доставки')
    run(dialogs, 3, 999, callback_data='ticket:open:1')
    assert any('очікує доставки' in m.text for m in messages(dialogs, 999))
    row = dialogs.catalog.connection.execute('SELECT outbox_id FROM support_messages WHERE role = ?', ('manager',)).fetchone()
    dialogs.storage.sent(row['outbox_id'], 40)
    run(dialogs, 4, 999, callback_data='ticket:open:1')
    assert any('доставлено в Telegram' in m.text for m in messages(dialogs, 999))


def test_history_and_manager_selection_survive_restart(tmp_path):
    path = tmp_path / 'restart.sqlite3'
    with Catalog(path) as catalog:
        dialogs = Dialogs(catalog, 999)
        run(dialogs, 1, 10, '/manager')
        run(dialogs, 2, 10, 'Збережене питання')
        run(dialogs, 3, 999, callback_data='ticket:open:1')
    with Catalog(path) as catalog:
        dialogs = Dialogs(catalog, 999)
        run(dialogs, 4, 999, 'Відповідь після перезапуску')
        assert messages(dialogs, 10)[-1].text == 'Менеджер: Відповідь після перезапуску'
        run(dialogs, 5, 999, callback_data='ticket:open:1')
        assert any('Збережене питання' in m.text for m in messages(dialogs, 999))


def test_separate_private_topics_route_both_directions_and_survive_delivery_retry(dialogs):
    async def scenario():
        dialogs.topics_enabled = True
        await dialogs.handle(1, 10, '/manager')
        await dialogs.handle(2, 20, '/manager')
        next_topic = 100
        next_message = 500
        async def create(**kwargs):
            nonlocal next_topic
            next_topic += 1
            return SimpleNamespace(message_thread_id=next_topic)
        async def send(**kwargs):
            nonlocal next_message
            assert kwargs['message_thread_id'] > 100
            next_message += 1
            return SimpleNamespace(message_id=next_message)
        bot = SimpleNamespace(create_forum_topic=AsyncMock(side_effect=create), send_message=AsyncMock(side_effect=send))
        # First delivery creates customer 1, customer 2 and manager 1 topics.
        await deliver_pending(dialogs.storage, bot, topics_enabled=True)
        await deliver_pending(dialogs.storage, bot, topics_enabled=True)
        assert bot.create_forum_topic.await_count == 4
        manager_topic = dialogs.catalog.connection.execute(
            'SELECT thread_id FROM support_topics WHERE chat_id = 999 AND ticket = 2').fetchone()[0]
        await dialogs.handle(3, 999, 'Відповідь у темі 2', thread_id=manager_topic)
        assert messages(dialogs, 20)[-1].text == 'Менеджер: Відповідь у темі 2'
        assert not any('Відповідь у темі 2' in m.text for m in messages(dialogs, 10))
        customer_topic = dialogs.catalog.connection.execute(
            'SELECT thread_id FROM support_topics WHERE chat_id = 20').fetchone()[0]
        await dialogs.handle(4, 20, 'Наступне питання', thread_id=customer_topic)
        assert messages(dialogs, 999)[-1].text.endswith('Клієнт: Наступне питання')
        bot.send_message.side_effect = TelegramNetworkError(method=SendMessage(chat_id=20, text='test'), message='synthetic')
        await deliver_pending(dialogs.storage, bot, topics_enabled=True)
        assert bot.create_forum_topic.await_count == 4
        assert dialogs.storage.pending(now=10**12)
    asyncio.run(scenario())


def test_unknown_topic_cannot_use_other_selected_ticket(dialogs):
    run(dialogs, 1, 10, '/manager')
    run(dialogs, 2, 999, callback_data='ticket:open:1')
    run(dialogs, 3, 999, 'Не надсилати', thread_id=777)
    assert not any('Не надсилати' in m.text for m in messages(dialogs, 10))
    run(dialogs, 4, 20, 'Чуже повідомлення', thread_id=777)
    assert not any('Чуже повідомлення' in m.text for m in messages(dialogs, 999))


@pytest.mark.parametrize('actor', [10, 999])
def test_user_created_topic_search_stays_in_topic_and_never_becomes_support_reply(dialogs, actor):
    import_catalog(dialogs.catalog, Path(__file__).resolve().parents[1] / 'examples' / 'catalog.csv')
    dialogs.topics_enabled = True
    run(dialogs, 1, 10, '/manager')
    run(dialogs, 2, 999, callback_data='ticket:open:1')
    run(dialogs, 3, actor, '00123', thread_id=777)
    response = dialogs.catalog.connection.execute('SELECT * FROM outbox WHERE chat_id = ? ORDER BY id DESC LIMIT 1', (actor,)).fetchone()
    assert '345.50 грн/м' in response['text']
    assert response['thread_id'] == 777 and response['ticket'] is None
    assert not dialogs.catalog.connection.execute('SELECT * FROM support_messages').fetchall()
    run(dialogs, 4, actor, callback_data='shop:catalog', thread_id=777)
    response = dialogs.catalog.connection.execute('SELECT * FROM outbox WHERE chat_id = ? ORDER BY id DESC LIMIT 1', (actor,)).fetchone()
    assert response['thread_id'] == 777 and 'категорію' in response['text']


def test_telegram_implicit_topic_service_reply_does_not_become_manager_reply(dialogs):
    from aiogram.types import Update
    import_catalog(dialogs.catalog, Path(__file__).resolve().parents[1] / 'examples' / 'catalog.csv')
    dialogs.topics_enabled = True
    incoming = update(1, 999, text='00123').model_dump(exclude_none=True)
    incoming['message']['message_thread_id'] = 777
    incoming['message']['is_topic_message'] = True
    incoming['message']['reply_to_message'] = {
        'message_id': 700, 'date': 1700000000, 'chat': {'id': 999, 'type': 'private'},
        'from': {'id': 999, 'is_bot': False, 'first_name': 'Synthetic'},
        'forum_topic_created': {'name': 'Пошук', 'icon_color': 7322096},
    }
    asyncio.run(handle_update(dialogs, SimpleNamespace(), Update.model_validate(incoming)))
    response = dialogs.catalog.connection.execute('SELECT * FROM outbox ORDER BY id DESC LIMIT 1').fetchone()
    assert '345.50 грн/м' in response['text'] and response['thread_id'] == 777
    assert not dialogs.catalog.connection.execute('SELECT * FROM support_messages').fetchall()


def test_reply_inside_topic_to_own_message_uses_topic_but_mismatched_quote_is_rejected(dialogs):
    dialogs.topics_enabled = True
    run(dialogs, 1, 10, '/manager')
    run(dialogs, 2, 20, '/manager')
    with dialogs.catalog.connection:
        dialogs.catalog.connection.execute('INSERT INTO support_topics VALUES (999, 1, 71)')
        dialogs.catalog.connection.execute('INSERT INTO support_topics VALUES (999, 2, 72)')
    run(dialogs, 3, 999, 'Відповідаю у темі', thread_id=71, reply_to_message_id=300)
    assert messages(dialogs, 10)[-1].text == 'Менеджер: Відповідаю у темі'
    outgoing = next(m for m in dialogs.storage.pending() if m.chat_id == 999)
    dialogs.storage.sent(outgoing.id, 301)
    run(dialogs, 4, 999, 'Конфлікт теми', thread_id=72, reply_to_message_id=301)
    assert not any('Конфлікт теми' in m.text for chat in (10, 20) for m in messages(dialogs, chat))


def test_deleted_topic_is_recreated_without_losing_pending_reply(dialogs):
    async def scenario():
        with dialogs.catalog.connection:
            dialogs.storage.enqueue(10, 'Відповідь', ticket=1)
            dialogs.catalog.connection.execute('INSERT INTO support_topics VALUES (10, 1, 50)')
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=TelegramBadRequest(
            method=SendMessage(chat_id=10, text='test'), message='Bad Request: message thread not found')),
            create_forum_topic=AsyncMock(return_value=SimpleNamespace(message_thread_id=51)))
        assert await deliver_pending(dialogs.storage, bot, topics_enabled=True) == 0
        assert not dialogs.catalog.connection.execute('SELECT * FROM support_topics').fetchall()
        with dialogs.catalog.connection:
            dialogs.catalog.connection.execute('UPDATE outbox SET next_attempt = 0')
        bot.send_message.side_effect = None
        bot.send_message.return_value = SimpleNamespace(message_id=601)
        assert await deliver_pending(dialogs.storage, bot, topics_enabled=True) == 1
        assert bot.send_message.call_args.kwargs['message_thread_id'] == 51
        assert not dialogs.storage.pending()
    asyncio.run(scenario())


def test_topics_leave_main_catalog_separate_and_reject_old_closed_topic(dialogs):
    dialogs.topics_enabled = True
    run(dialogs, 1, 10, '/manager')
    with dialogs.catalog.connection:
        dialogs.catalog.connection.execute('INSERT INTO support_topics VALUES (10, 1, 61)')
    run(dialogs, 2, 10, 'Пошук у головному чаті')
    assert not dialogs.catalog.connection.execute('SELECT * FROM support_messages').fetchall()
    incoming = update(3, 10, text='Питання менеджеру')
    incoming = incoming.model_copy(update={'message': incoming.message.model_copy(update={'message_thread_id': 61})})
    asyncio.run(handle_update(dialogs, SimpleNamespace(), incoming))
    assert messages(dialogs, 999)[-1].text.endswith('Питання менеджеру')
    run(dialogs, 4, 10, callback_data='client:close:1', thread_id=61)
    count = dialogs.catalog.connection.execute('SELECT count(*) FROM support_messages').fetchone()[0]
    run(dialogs, 5, 10, 'У старій темі', thread_id=61)
    assert dialogs.catalog.connection.execute('SELECT count(*) FROM support_messages').fetchone()[0] == count
    run(dialogs, 6, 10, callback_data='shop:home', thread_id=61)
    assert 'Shop Assistant' in messages(dialogs, 10)[-1].text


def test_stale_customer_buttons_do_not_close_new_ticket(dialogs):
    run(dialogs, 1, 10, '/manager')
    run(dialogs, 2, 10, callback_data='client:close:1')
    run(dialogs, 3, 10, '/manager')
    run(dialogs, 4, 10, callback_data='client:close:1')
    assert dialogs.storage.dialog(10)['ticket'] == 2
    assert dialogs.support.ticket(2)['active'] == 1
    run(dialogs, 5, 20, callback_data='client:close:2')
    run(dialogs, 6, 20, callback_data='client:status:2')
    assert dialogs.support.ticket(2)['active'] == 1
    assert not any('Звернення #2 ·' in m.text for m in messages(dialogs, 20))


def test_history_and_inbox_pagination_keep_all_items_accessible(dialogs):
    for number in range(1, 11):
        run(dialogs, number, number + 10, '/manager')
    run(dialogs, 11, 999, '/tickets')
    assert any(b[1] == 'ticket:list:1:8' for b in messages(dialogs, 999)[-1].buttons)
    run(dialogs, 12, 999, callback_data='ticket:list:1:8')
    assert any(b[1] == 'ticket:open:1' for b in messages(dialogs, 999)[-1].buttons)
    for number in range(13, 21):
        run(dialogs, number, 11, f'Питання {number}')
    run(dialogs, 21, 999, callback_data='ticket:open:1')
    assert any(b[1] == 'ticket:history:1:6' for b in messages(dialogs, 999)[-1].buttons)
    run(dialogs, 22, 999, callback_data='ticket:history:1:6')
    assert any('Питання 13' in m.text for m in messages(dialogs, 999))


def test_v2_upgrade_keeps_existing_open_ticket_context(tmp_path):
    with Catalog(tmp_path / 'upgrade.sqlite3') as catalog:
        catalog.connection.executescript('''
            CREATE TABLE dialog_schema(version INTEGER NOT NULL);
            INSERT INTO dialog_schema VALUES (2);
            CREATE TABLE dialogs(chat_id INTEGER PRIMARY KEY, mode TEXT, ticket INTEGER, context TEXT);
            INSERT INTO dialogs VALUES (10, 'HUMAN', 4, 'Обрана тканина: Siena');
            CREATE TABLE outbox(id INTEGER PRIMARY KEY, chat_id INTEGER, text TEXT, buttons TEXT,
                attempts INTEGER DEFAULT 0, next_attempt REAL DEFAULT 0, delivered INTEGER DEFAULT 0, photo TEXT);
            INSERT INTO outbox(id, chat_id, text, buttons) VALUES (1, 10, 'Збережене повідомлення', '[]');
        ''')
        storage = Storage(catalog.connection)
        assert storage.pending()[0].text == 'Збережене повідомлення'
        assert catalog.connection.execute('SELECT text FROM support_context WHERE ticket = 4').fetchone()[0] == 'Обрана тканина: Siena'
        Storage(catalog.connection)
        assert storage.dialog(10)['mode'] == 'HUMAN'
