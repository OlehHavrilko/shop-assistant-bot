import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from shop_assistant.catalog import Catalog
from shop_assistant.dialogs import Dialogs
from shop_assistant.importer import import_catalog
from shop_assistant.storage import Storage
from shop_assistant.storefront import Storefront


DEMO = Path(__file__).resolve().parents[1] / 'examples' / 'catalog.csv'


@pytest.fixture
def catalog(tmp_path):
    with Catalog(tmp_path / 'dialogs.db') as catalog:
        import_catalog(catalog, DEMO)
        yield catalog


def run(dialogs, update, chat, text=None, **kwargs):
    asyncio.run(dialogs.handle(update, chat, text, **kwargs))


def messages(dialogs, chat):
    rows = dialogs.catalog.connection.execute(
        'SELECT * FROM outbox WHERE chat_id = ? AND delivered = 0 ORDER BY id', (chat,))
    return [SimpleNamespace(id=r['id'], text=r['text'], buttons=json.loads(r['buttons']), photo=r['photo']) for r in rows]


def test_sku_without_llm_and_duplicate_update(catalog):
    dialogs = Dialogs(catalog, 999)
    run(dialogs, 1, 10, '00123')
    run(dialogs, 1, 10, '00123')
    assert len(messages(dialogs, 10)) == 1
    assert '345.50 грн/м' in messages(dialogs, 10)[0].text
    run(dialogs, 2, 10, '99999')
    assert 'не знайдено' in messages(dialogs, 10)[-1].text
    run(dialogs, 3, 10, 'льон')
    assert 'Оберіть відтінок' in messages(dialogs, 10)[-1].text


def test_human_routing_authorization_and_bot_return(catalog):
    class NeverCalled:
        async def resolve(self, text):
            raise AssertionError('HUMAN must bypass LLM')

    dialogs = Dialogs(catalog, 999, NeverCalled())
    run(dialogs, 1, 10, '/manager')
    run(dialogs, 2, 20, '/manager')
    run(dialogs, 3, 10, 'Доставка')
    run(dialogs, 4, 999, '/reply 2 Відповідь другому')
    assert 'Відповідь другому' in messages(dialogs, 20)[-1].text
    assert not any('Відповідь другому' in m.text for m in messages(dialogs, 10))
    run(dialogs, 5, 30, '/bot 1')
    assert dialogs.storage.dialog(10)['mode'] == 'HUMAN'
    run(dialogs, 6, 999, '/bot 1')
    assert dialogs.storage.dialog(10)['mode'] == 'BOT'
    assert dialogs.storage.dialog(20)['mode'] == 'HUMAN'
    run(dialogs, 7, 999, '/reply 1 Закрите')
    assert not any('Закрите' in m.text for m in messages(dialogs, 10))


def test_restart_persists_state_outbox_dedup_and_rate(tmp_path):
    path = tmp_path / 'restart.db'
    with Catalog(path) as catalog:
        dialogs = Dialogs(catalog, 999, request_rate=1)
        run(dialogs, 1, 10, '/manager')
        first = dialogs.storage.pending()[0]
        dialogs.storage.failed(first.id, retry_after=60, now=100)
        assert first.id not in [m.id for m in dialogs.storage.pending(now=159)]
    with Catalog(path) as catalog:
        dialogs = Dialogs(catalog, 999, request_rate=1)
        assert dialogs.storage.dialog(10)['mode'] == 'HUMAN'
        assert dialogs.storage.pending(now=160)[0].attempts == 1
        before = len(dialogs.storage.pending())
        run(dialogs, 1, 10, '/manager')
        assert len(dialogs.storage.pending()) == before
        run(dialogs, 2, 10, 'Привіт')
        assert 'Забагато' in messages(dialogs, 10)[-1].text
        dialogs.storage.sent(first.id)
        assert first.id not in [m.id for m in dialogs.storage.pending()]


def test_choices_bound_to_customer_persist_and_reread_catalog(catalog):
    class Model:
        async def resolve(self, text):
            return SimpleNamespace(action='products', skus=('00123', '123'), text='Ціна 1 грн')

    dialogs = Dialogs(catalog, 999, Model())
    run(dialogs, 1, 10, 'порадьте невідому тканину')
    buttons = messages(dialogs, 10)[-1].buttons
    assert len(buttons) == 3
    assert all(len(token.encode()) <= 64 for _, token in buttons)
    token = buttons[0][1]
    dialogs = Dialogs(catalog, 999)
    run(dialogs, 2, 20, callback_data=token)
    assert 'недоступний' in messages(dialogs, 20)[-1].text
    with catalog.connection:
        catalog.connection.execute("UPDATE products SET price_uah_per_m = '500.00' WHERE sku = '00123'")
    run(dialogs, 3, 10, callback_data=token)
    assert '500.00 грн/м' in messages(dialogs, 10)[-1].text
    assert 'Ціна 1 грн' not in messages(dialogs, 10)[-1].text
    run(dialogs, 4, 10, callback_data=token)
    assert 'недоступний' in messages(dialogs, 10)[-1].text


def test_invalid_sku_from_model_rejects_entire_selection(catalog):
    class Model:
        async def resolve(self, text):
            return SimpleNamespace(action='products', skus=('00123', 'INVENTED'))

    dialogs = Dialogs(catalog, 999, Model())
    run(dialogs, 1, 10, 'порадьте невідому тканину')
    assert 'не знайдено' in messages(dialogs, 10)[0].text
    assert not messages(dialogs, 10)[0].buttons


def test_exact_sku_avoids_llm_and_model_failures_fallback(catalog):
    class Model:
        calls = 0

        async def resolve(self, text):
            self.calls += 1
            assert not catalog.connection.in_transaction
            raise TimeoutError('no private data in response')

    model = Model()
    dialogs = Dialogs(catalog, 999, model)
    run(dialogs, 1, 10, '00123')
    assert model.calls == 0
    run(dialogs, 2, 10, 'невідома тканина')
    assert model.calls == 1
    assert 'Не знайшли' in messages(dialogs, 10)[-1].text


def test_outbox_failure_cannot_rollback_handoff(catalog):
    dialogs = Dialogs(catalog, 999)
    run(dialogs, 1, 10, '/manager')
    manager_message = messages(dialogs, 999)[0]
    dialogs.storage.failed(manager_message.id)
    assert dialogs.storage.dialog(10)['mode'] == 'HUMAN'
    assert len(messages(dialogs, 10)) == 1
    assert catalog.connection.execute('SELECT count(*) FROM tickets').fetchone()[0] == 1


def test_concurrent_duplicate_updates_only_resolve_once(catalog):
    class Model:
        calls = 0

        async def resolve(self, text):
            self.calls += 1
            await asyncio.sleep(0)
            return SimpleNamespace(action='handoff', skus=())

    model = Model()
    dialogs = Dialogs(catalog, 999, model)

    async def concurrent():
        await asyncio.gather(dialogs.handle(1, 10, 'невідомий запит'), dialogs.handle(1, 10, 'невідомий запит'))

    asyncio.run(concurrent())
    assert model.calls == 1
    assert catalog.connection.execute('SELECT count(*) FROM tickets').fetchone()[0] == 1


def test_length_and_private_filter_before_model(catalog):
    dialogs = Dialogs(catalog, 999, message_length=5)
    run(dialogs, 1, 10, 'overlong')
    assert 'задовге' in messages(dialogs, 10)[0].text
    run(dialogs, 2, 20, '00123', private=False)
    assert not messages(dialogs, 20)


def test_oversized_messages_use_persistent_rate_limit_without_model_or_context(catalog):
    class Model:
        calls = 0

        async def resolve(self, text):
            self.calls += 1
            return SimpleNamespace(action='handoff', skus=())

    model = Model()
    dialogs = Dialogs(catalog, 999, model, message_length=5, request_rate=1)
    run(dialogs, 1, 10, 'overlong')
    assert 'задовге' in messages(dialogs, 10)[-1].text
    dialogs = Dialogs(catalog, 999, model, message_length=5, request_rate=1)
    run(dialogs, 2, 10, 'overlong again')
    assert 'Забагато запитів' in messages(dialogs, 10)[-1].text
    assert dialogs.storage.dialog(10)['context'] == ''
    assert model.calls == 0
    run(dialogs, 3, 999, 'oversized manager command')
    assert 'задовге' in messages(dialogs, 999)[-1].text
    run(dialogs, 4, 999, 'oversized manager command again')
    assert 'задовге' in messages(dialogs, 999)[-1].text


def test_incoming_and_outbox_roll_back_together(catalog, monkeypatch):
    dialogs = Dialogs(catalog, 999)
    enqueue = dialogs.storage.enqueue

    def fail(chat, text, buttons=(), **kwargs):
        enqueue(chat, text, buttons, **kwargs)
        raise RuntimeError('disk failure simulation')

    monkeypatch.setattr(dialogs.storage, 'enqueue', fail)
    with pytest.raises(RuntimeError, match='disk failure'):
        run(dialogs, 1, 10, '/manager')
    assert not catalog.connection.execute('SELECT * FROM incoming').fetchall()
    assert not catalog.connection.execute('SELECT * FROM tickets').fetchall()
    assert not dialogs.storage.pending()
    monkeypatch.setattr(dialogs.storage, 'enqueue', enqueue)
    run(dialogs, 1, 10, '/manager')
    assert dialogs.storage.dialog(10)['mode'] == 'HUMAN'


def test_failed_schema_migration_does_not_leave_partial_tables(catalog):
    with catalog.connection:
        catalog.connection.execute('CREATE TABLE tickets (existing_data TEXT)')
        catalog.connection.execute("INSERT INTO tickets VALUES ('preserve')")
    with pytest.raises(sqlite3.OperationalError, match='tickets already exists'):
        Storage(catalog.connection)
    tables = {row[0] for row in catalog.connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert not {'dialog_schema', 'dialogs', 'incoming', 'outbox'} & tables
    assert catalog.connection.execute('SELECT existing_data FROM tickets').fetchone()[0] == 'preserve'
    assert catalog.connection.execute('PRAGMA user_version').fetchone()[0] == 1


def test_outbox_preserves_chat_order_without_blocking_other_chats(catalog):
    storage = Storage(catalog.connection)
    with catalog.connection:
        storage.enqueue(10, 'first')
        storage.enqueue(10, 'second')
        storage.enqueue(20, 'other customer')
        storage.enqueue(999, 'manager')
    initial = storage.pending(now=100)
    assert [m.text for m in initial] == ['first', 'other customer', 'manager']
    storage.failed(initial[0].id, retry_after=60, now=100)
    assert [m.text for m in storage.pending(now=159)] == ['other customer', 'manager']
    assert [m.text for m in storage.pending(now=160)] == ['first', 'other customer', 'manager']
    storage.sent(initial[0].id)
    assert [m.text for m in storage.pending(now=160)] == ['second', 'other customer', 'manager']


@pytest.fixture
def shop(catalog, tmp_path):
    (tmp_path / 'media').mkdir()
    (tmp_path / 'media' / 'linen.jpg').write_bytes(b'test image transport fixture')
    metadata = {
        'sku': '00123', 'category': 'linen', 'composition': '100% льон',
        'description': 'Легка тканина для сорочки.', 'photo': 'linen.jpg',
        'width_cm': 145, 'density_g_m2': 150, 'uses': ['сорочка'], 'tags': [],
    }
    path = tmp_path / 'shop.json'
    path.write_text(json.dumps({
        'name': 'Полотно', 'tagline': 'Тканини для ваших задумів',
        'about': 'Вигаданий магазин Полотно.', 'delivery': 'Демо: доставка поштою.',
        'payment': 'Демо: оплата після узгодження.',
        'categories': [{'id': 'linen', 'name': 'Льон'}], 'products': [metadata],
    }), encoding='utf-8')
    return Storefront(catalog, path)


def test_storefront_navigation_queues_photo_and_factual_card(catalog, shop):
    dialogs = Dialogs(catalog, 999, storefront=shop)
    run(dialogs, 1, 10, '/start')
    welcome = messages(dialogs, 10)[-1]
    assert 'Полотно' in welcome.text
    assert ('Каталог тканин', 'shop:catalog') in map(tuple, welcome.buttons)
    run(dialogs, 2, 10, callback_data='shop:catalog')
    assert ['Льон', 'shop:category:linen:0'] in messages(dialogs, 10)[-1].buttons
    run(dialogs, 3, 10, callback_data='shop:category:linen:0')
    cards = [message for message in messages(dialogs, 10) if message.photo]
    assert len(cards) == 1
    assert Path(cards[0].photo).name == 'linen.jpg'
    assert '345.50 грн/м' in cards[0].text and '12.75 м' in cards[0].text
    assert len(cards[0].text.encode('utf-16-le')) // 2 <= 1024
    assert 'supplier' not in cards[0].text and '230.00' not in cards[0].text
    assert all(len(token.encode()) <= 64 for _, token in cards[0].buttons)


def test_free_search_faq_and_ambiguity_never_call_model(catalog, shop):
    class Model:
        async def resolve(self, text):
            raise AssertionError('Catalog and FAQ do not require a model')

    dialogs = Dialogs(catalog, 999, Model(), storefront=shop)
    run(dialogs, 1, 10, 'льон ivory')
    choices = messages(dialogs, 10)[-1]
    assert 'Оберіть відтінок' in choices.text
    assert len([button for button in choices.buttons if button[1].startswith('pick:')]) == 2
    run(dialogs, 2, 10, 'бавовна синя')
    assert 'COT-010' in messages(dialogs, 10)[-1].text
    run(dialogs, 3, 10, 'Яка доставка?')
    assert 'доставка поштою' in messages(dialogs, 10)[-1].text
    run(dialogs, 4, 10, callback_data='shop:info:payment')
    assert 'оплата після узгодження' in messages(dialogs, 10)[-1].text
    assert dialogs.storage.dialog(10)['mode'] == 'BOT'


def test_manager_can_browse_storefront_without_self_ticket(catalog, shop):
    dialogs = Dialogs(catalog, 999, storefront=shop)
    run(dialogs, 1, 999, '/start')
    assert 'Полотно' in messages(dialogs, 999)[-1].text
    assert ['Каталог тканин', 'shop:catalog'] in messages(dialogs, 999)[-1].buttons
    assert ['Кабінет менеджера', 'shop:tickets'] in messages(dialogs, 999)[-1].buttons
    run(dialogs, 2, 999, '00123')
    assert messages(dialogs, 999)[-1].photo
    ask = messages(dialogs, 999)[-1].buttons[0][1]
    run(dialogs, 3, 999, callback_data=ask)
    assert 'Ви менеджер' in messages(dialogs, 999)[-1].text
    run(dialogs, 4, 999, callback_data='shop:tickets')
    assert 'Активні звернення: немає' in messages(dialogs, 999)[-1].text
    assert not catalog.connection.execute('SELECT * FROM tickets').fetchall()
    assert dialogs.storage.dialog(999)['mode'] == 'BOT'


def test_card_question_binds_sku_to_customer_and_rereads_catalog(catalog, shop):
    dialogs = Dialogs(catalog, 999, storefront=shop)
    run(dialogs, 1, 10, '00123')
    ask = messages(dialogs, 10)[-1].buttons[0][1]
    run(dialogs, 2, 20, callback_data=ask)
    assert dialogs.storage.dialog(20)['mode'] == 'BOT'
    run(dialogs, 3, 10, callback_data=ask)
    assert dialogs.storage.dialog(10)['mode'] == 'HUMAN'
    assert 'артикул 00123' in messages(dialogs, 999)[-1].text
    run(dialogs, 4, 30, '123')
    deleted_ask = messages(dialogs, 30)[-1].buttons[0][1]
    with catalog.connection:
        catalog.connection.execute("DELETE FROM products WHERE sku = '123'")
    run(dialogs, 5, 30, callback_data=deleted_ask)
    assert 'недоступний' in messages(dialogs, 30)[-1].text
    assert dialogs.storage.dialog(30)['mode'] == 'BOT'


@pytest.mark.parametrize('data', [
    'shop:category:../../:0', 'shop:category:linen:-4', 'shop:category:linen:1',
    'shop:category:linen:10004', 'shop:category:linen:0:extra',
    'shop:category:linen:²',
    'shop:info:secret', 'shop:tickets', 'shop:ask:invented',
])
def test_invalid_storefront_callbacks_preserve_authorization(catalog, shop, data):
    dialogs = Dialogs(catalog, 999, storefront=shop)
    run(dialogs, 1, 10, callback_data=data)
    assert dialogs.storage.dialog(10)['mode'] == 'BOT'
    assert not catalog.connection.execute('SELECT * FROM tickets').fetchall()
    assert not any(message.photo for message in messages(dialogs, 10))
    assert 'Активні звернення' not in messages(dialogs, 10)[-1].text


def test_category_pagination_has_no_repeated_products(catalog, tmp_path):
    with catalog.connection:
        catalog.connection.execute("""
            INSERT INTO products SELECT 'EXTRA', name, shade, price_uah_per_m,
            stock_m, updated_at, purchase_price_uah_per_m, supplier
            FROM products WHERE sku = '00123'
        """)
    shop = Storefront(catalog, tmp_path / 'missing.json')
    dialogs = Dialogs(catalog, 999, storefront=shop)
    expected = [p.sku for p in shop.category_products('all', limit=20)[0]]
    run(dialogs, 1, 10, callback_data='shop:category:all:0')
    first = messages(dialogs, 10)
    assert len(first) == 5
    assert ['Ще тканини', 'shop:category:all:4'] in first[-1].buttons
    run(dialogs, 2, 10, callback_data='shop:category:all:4')
    second = messages(dialogs, 10)[len(first):]
    assert len(second) == 2
    cards = first[:-1] + second[:-1]
    assert all(any(f'Артикул: {sku}\n' in card.text for card in cards) for sku in expected)
    assert ['Попередні тканини', 'shop:category:all:0'] in second[-1].buttons
    assert not any(button[0] == 'Ще тканини' for button in second[-1].buttons)
