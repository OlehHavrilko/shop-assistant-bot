import asyncio
import itertools
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from aiogram.types import Update

from shop_assistant.catalog import Catalog
from shop_assistant.dialogs import Dialogs
from shop_assistant.importer import import_catalog
from shop_assistant.storefront import Storefront
from shop_assistant.telegram import deliver_pending, handle_update

ROOT = Path(__file__).resolve().parents[1]
MANAGER, CLIENT, OTHER = 999, 10, 20
PROFILE = {'first_name': 'Олена', 'last_name': 'Коваль', 'username': 'olena_k'}
KYIV = ZoneInfo('Europe/Kyiv')


class Clock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


@pytest.fixture
def shop(tmp_path):
    with Catalog(tmp_path / 'shop.sqlite3') as catalog:
        import_catalog(catalog, ROOT / 'examples' / 'store_catalog.csv')
        clock = Clock(datetime(2026, 10, 14, 12, 0, tzinfo=KYIV).timestamp())  # Wednesday noon
        dialogs = Dialogs(catalog, MANAGER, storefront=Storefront(catalog, ROOT / 'examples' / 'store.json'),
                          clock=clock)
        ids = itertools.count(1)

        def send(chat, text=None, **kwargs):
            if chat != MANAGER:
                kwargs.setdefault('profile', PROFILE if chat == CLIENT else {'first_name': 'Іван'})
            asyncio.run(dialogs.handle(next(ids), chat, text, **kwargs))
        yield SimpleNamespace(dialogs=dialogs, send=send, clock=clock, db=catalog.connection, path=tmp_path)


def outbox(shop, chat):
    rows = shop.db.execute('SELECT * FROM outbox WHERE chat_id = ? ORDER BY id', (chat,)).fetchall()
    return [SimpleNamespace(text=r['text'], buttons=json.loads(r['buttons']), copy=(r['copy_chat'], r['copy_message']),
                            photo=r['photo'], ticket=r['ticket']) for r in rows]


def button(message, label):
    return next(data for text, data in message.buttons if text == label)


def test_manager_sees_customer_name_in_notification_inbox_and_ticket(shop):
    shop.send(CLIENT, '/manager')
    assert 'Олена Коваль (@olena_k)' in outbox(shop, MANAGER)[-1].text
    shop.send(CLIENT, 'Чи є льон?')
    assert outbox(shop, MANAGER)[-1].text == 'Звернення #1 · Олена Коваль (@olena_k)\nКлієнт: Чи є льон?'
    shop.send(MANAGER, '/tickets')
    assert any('Олена Коваль' in text for text, _ in outbox(shop, MANAGER)[-1].buttons)
    shop.send(MANAGER, callback_data='ticket:open:1')
    assert any('Клієнт: Олена Коваль (@olena_k)' in m.text for m in outbox(shop, MANAGER))


def test_customer_photo_is_copied_to_manager_and_kept_in_history(shop):
    shop.send(CLIENT, '/manager')
    shop.send(CLIENT, 'Такий відтінок є?', media='фото', message_id=77)
    header, copy = outbox(shop, MANAGER)[-2:]
    assert header.text == 'Звернення #1 · Олена Коваль (@olena_k)\nКлієнт надіслав: фото'
    assert copy.copy == (CLIENT, 77) and copy.ticket == 1
    assert ['Відкрити діалог', 'ticket:open:1'] in copy.buttons
    history = shop.db.execute('SELECT text FROM support_messages WHERE ticket = 1').fetchall()
    assert history[-1]['text'] == '[фото] Такий відтінок є?'


def test_photo_outside_ticket_is_not_a_search_query(shop):
    shop.send(CLIENT, 'льон', media='фото', message_id=5)
    last = outbox(shop, CLIENT)[-1]
    assert 'Написати менеджеру' in last.text and last.photo is None
    assert not shop.db.execute('SELECT * FROM tickets').fetchall()


def test_manager_media_reply_is_copied_only_to_the_open_ticket(shop):
    shop.send(CLIENT, '/manager')
    shop.send(OTHER, '/manager')
    shop.send(MANAGER, callback_data='ticket:open:1')
    shop.send(MANAGER, 'Ось рулон', media='фото', message_id=300)
    assert outbox(shop, CLIENT)[-1].copy == (MANAGER, 300)
    assert all(m.copy == (None, None) for m in outbox(shop, OTHER))
    shop.send(MANAGER, '/tickets')
    shop.send(MANAGER, media='файл', message_id=301)
    assert outbox(shop, MANAGER)[-1].text == 'Щоб надіслати файл клієнту, відкрийте звернення.'


def test_copied_message_is_delivered_with_copy_message(shop):
    shop.send(CLIENT, '/manager')
    shop.send(CLIENT, media='голосове повідомлення', message_id=12)
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)),
                          copy_message=AsyncMock(return_value=SimpleNamespace(message_id=2)))
    for _ in range(4):  # one message per chat per pass keeps per-chat order
        asyncio.run(deliver_pending(shop.dialogs.storage, bot))
    bot.copy_message.assert_awaited_once()
    assert bot.copy_message.await_args.kwargs['from_chat_id'] == CLIENT
    assert bot.copy_message.await_args.kwargs['message_id'] == 12


def test_deleted_original_turns_into_a_notice_instead_of_blocking_the_chat(shop):
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import CopyMessage

    shop.send(CLIENT, '/manager')
    shop.send(CLIENT, media='фото', message_id=12)
    error = TelegramBadRequest(method=CopyMessage(chat_id=MANAGER, from_chat_id=CLIENT, message_id=12),
                               message='Bad Request: message to copy not found')
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)),
                          copy_message=AsyncMock(side_effect=error))
    for _ in range(4):
        asyncio.run(deliver_pending(shop.dialogs.storage, bot))
    texts = [call.kwargs['text'] for call in bot.send_message.await_args_list if call.kwargs['chat_id'] == MANAGER]
    assert texts[-1] == 'Файл більше недоступний: відправник його видалив.'
    assert not shop.dialogs.storage.pending(now=10**12)


def test_purchase_request_collects_meters_phone_delivery_and_reaches_manager(shop):
    shop.send(CLIENT, '00123')
    order = button(outbox(shop, CLIENT)[-1], 'Оформити заявку')
    shop.send(CLIENT, callback_data=order)
    assert 'Скільки метрів' in outbox(shop, CLIENT)[-1].text
    shop.send(CLIENT, 'багато')
    assert 'числом' in outbox(shop, CLIENT)[-1].text
    shop.send(CLIENT, '2,5 м')
    shop.send(CLIENT, '12')
    assert 'Не схоже на номер' in outbox(shop, CLIENT)[-1].text
    shop.send(CLIENT, '+38 (067) 123-45-67')
    shop.send(CLIENT, callback_data='lead:delivery:0')
    shop.send(CLIENT, callback_data='lead:skip')
    confirm = outbox(shop, CLIENT)[-1]
    assert 'Кількість: 2.5 м' in confirm.text and 'Орієнтовна сума: 863.75 грн' in confirm.text
    assert 'Телефон: +380671234567' in confirm.text and 'Доставка: Нова пошта' in confirm.text
    shop.send(CLIENT, callback_data='lead:submit')
    assert 'Заявку №1 передано менеджеру (звернення #1)' in outbox(shop, CLIENT)[-1].text
    lead = shop.db.execute('SELECT * FROM leads').fetchone()
    assert (lead['sku'], lead['meters'], lead['phone'], lead['ticket']) == ('00123', '2.5', '+380671234567', 1)
    notice = outbox(shop, MANAGER)[-1]
    assert notice.text.startswith('🧾 Нова заявка №1 · звернення #1\nОлена Коваль (@olena_k)')
    assert shop.dialogs.storage.dialog(CLIENT)['mode'] == 'HUMAN'
    assert not shop.db.execute('SELECT * FROM forms').fetchall()


def test_purchase_request_is_cancelled_by_start_and_does_not_search(shop):
    shop.send(CLIENT, '00123')
    shop.send(CLIENT, callback_data=button(outbox(shop, CLIENT)[-1], 'Оформити заявку'))
    shop.send(CLIENT, '/start')
    assert not shop.db.execute('SELECT * FROM forms').fetchall()
    shop.send(CLIENT, '00123')
    assert outbox(shop, CLIENT)[-1].photo


def test_rating_after_close_is_saved_once_and_shown_to_manager(shop):
    shop.send(CLIENT, '/manager')
    shop.send(MANAGER, '/bot 1')
    closed = outbox(shop, CLIENT)[-1]
    assert 'Оцініть' in closed.text and ['★★★★', 'client:rate:1:4'] in closed.buttons
    shop.send(CLIENT, callback_data='client:rate:1:4')
    assert outbox(shop, CLIENT)[-1].text == 'Дякуємо за оцінку!'
    assert outbox(shop, MANAGER)[-1].text == 'Клієнт оцінив звернення #1: ★★★★☆'
    shop.send(CLIENT, callback_data='client:rate:1:1')
    assert shop.db.execute('SELECT rating FROM tickets WHERE id = 1').fetchone()[0] == 4
    shop.send(OTHER, callback_data='client:rate:1:5')
    assert outbox(shop, OTHER)[-1].text == 'Звернення недоступне.'


def test_quick_reply_template_is_sent_to_the_customer(shop):
    shop.send(CLIENT, '/manager')
    shop.send(MANAGER, callback_data='ticket:open:1')
    assert ['Шаблони відповідей', 'ticket:tpl:1'] in outbox(shop, MANAGER)[-1].buttons
    shop.send(MANAGER, callback_data='ticket:tpl:1')
    shop.send(MANAGER, callback_data='ticket:tplsend:1:1')
    assert outbox(shop, CLIENT)[-1].text == 'Менеджер: Ця тканина є в наявності. Скільки метрів вам потрібно?'
    shop.send(MANAGER, callback_data='ticket:tplsend:1:9')
    assert outbox(shop, MANAGER)[-1].text == 'Звернення недоступне.'


def test_manager_sends_product_card_by_button_and_command(shop):
    shop.send(CLIENT, '/manager')
    shop.send(MANAGER, callback_data='ticket:card:1')
    shop.send(MANAGER, 'неіснуючий')
    assert 'Не знайшли один товар' in outbox(shop, MANAGER)[-1].text
    shop.send(MANAGER, 'LIN-002')
    card = outbox(shop, CLIENT)[-1]
    assert 'LIN-002' in card.text and card.photo and card.ticket == 1
    assert [label for label, _ in card.buttons] == ['Оформити заявку', 'Каталог тканин']
    shop.send(MANAGER, callback_data='ticket:open:1')
    shop.send(MANAGER, '/card 00123')
    assert '00123' in outbox(shop, CLIENT)[-1].text
    assert outbox(shop, MANAGER)[-1].text.startswith('Картку 00123 надіслано')


def test_unanswered_ticket_reminds_manager_once(shop):
    shop.send(CLIENT, '/manager')
    shop.send(CLIENT, 'Є відповідь?')
    shop.clock.value += 10 * 60
    assert asyncio.run(shop.dialogs.remind()) == 0
    shop.clock.value += 6 * 60
    assert asyncio.run(shop.dialogs.remind()) == 1
    assert outbox(shop, MANAGER)[-1].text.startswith('⏰ Звернення #1 чекає відповіді 16 хв')
    assert asyncio.run(shop.dialogs.remind()) == 0
    shop.send(MANAGER, '/reply 1 Відповідаю')
    shop.clock.value += 60 * 60
    assert asyncio.run(shop.dialogs.remind()) == 0


@pytest.mark.hours
def test_off_hours_notice_is_sent_once_and_reminders_wait(shop):
    shop.clock.value = datetime(2026, 10, 18, 21, 0, tzinfo=KYIV).timestamp()  # Sunday evening
    shop.send(CLIENT, '/manager')
    assert outbox(shop, CLIENT)[-1].text.startswith('🌙 Зараз неробочий час')
    assert 'Пн, Вт, Ср, Чт, Пт, Сб 09:00–19:00' in outbox(shop, CLIENT)[-1].text
    shop.send(CLIENT, 'Ще питання')
    assert sum(m.text.startswith('🌙') for m in outbox(shop, CLIENT)) == 1
    shop.clock.value += 60 * 60
    assert asyncio.run(shop.dialogs.remind()) == 0


def test_stats_show_tickets_response_time_rating_leads_and_misses(shop):
    shop.send(CLIENT, '/manager')
    shop.clock.value += 5 * 60
    shop.send(MANAGER, '/reply 1 Добрий день')
    shop.send(MANAGER, '/bot 1')
    shop.send(CLIENT, callback_data='client:rate:1:5')
    shop.send(OTHER, 'оксамит бордо')
    shop.send(OTHER, 'Оксамит   БОРДО')
    shop.send(MANAGER, callback_data='shop:stats')
    text = outbox(shop, MANAGER)[-1].text
    assert 'Звернення: 1 за добу · 1 за 7 днів' in text
    assert 'Перша відповідь (7 днів): 5 хв' in text
    assert 'Оцінка (7 днів): 5.0 ★ з 1' in text
    assert '• оксамит бордо — 2' in text


def test_catalog_import_from_telegram_previews_then_replaces_with_backup(shop):
    shop.send(MANAGER, '/tickets')
    shop.send(MANAGER, callback_data=button(outbox(shop, MANAGER)[-1], 'Оновити каталог'))
    assert shop.dialogs.wants_upload(MANAGER)
    bad = shop.path / 'uploads' / 'bad.csv'
    bad.parent.mkdir()
    bad.write_text('sku,name\n1,x\n', encoding='utf-8')
    shop.send(MANAGER, media='файл', message_id=1, upload=bad)
    assert 'Файл не прийнято' in outbox(shop, MANAGER)[-1].text and not bad.exists()
    good = shop.path / 'uploads' / 'good.csv'
    good.write_text('sku,name,shade,price_uah_per_m,stock_m,updated_at\n'
                    '00123,Льон Siena,ivory,399.00,12.75,2026-10-18T09:00:00+03:00\n'
                    'NEW-1,Шовк Amalfi,бордо,900.00,3,2026-10-18T09:00:00+03:00\n', encoding='utf-8')
    shop.send(MANAGER, media='файл', message_id=2, upload=good)
    preview = outbox(shop, MANAGER)[-1].text
    assert 'Товарів у файлі: 2 (зараз 20)' in preview and 'Нові: 1: NEW-1' in preview
    assert 'Зникнуть: 19' in preview and 'Змінено ціну: 1: 00123' in preview
    assert shop.db.execute('SELECT count(*) FROM products').fetchone()[0] == 20 and good.exists()
    shop.send(MANAGER, callback_data='import:apply')
    assert outbox(shop, MANAGER)[-1].text.startswith('✅ Каталог оновлено: 2 товарів.')
    assert shop.db.execute('SELECT count(*) FROM products').fetchone()[0] == 2 and not good.exists()
    backups = list((shop.path / 'backups').glob('before-import-*.sqlite3'))
    assert len(backups) == 1
    with Catalog(backups[0]) as backup:
        assert backup.connection.execute('SELECT count(*) FROM products').fetchone()[0] == 20


def test_import_cancel_and_customer_cannot_import(shop):
    shop.send(MANAGER, '/import')
    shop.send(MANAGER, callback_data='import:cancel')
    assert not shop.dialogs.wants_upload(MANAGER)
    shop.send(CLIENT, callback_data='shop:import')
    assert not shop.dialogs.wants_upload(CLIENT)
    assert 'недоступний' in outbox(shop, CLIENT)[-1].text


def test_v3_database_upgrades_to_v4_keeping_tickets(tmp_path):
    path = tmp_path / 'v3.sqlite3'
    with Catalog(path) as catalog:
        Dialogs(catalog, MANAGER)
        catalog.connection.execute('DROP TABLE customers')
        catalog.connection.execute("INSERT INTO tickets (chat_id) VALUES (10)")
        catalog.connection.execute('UPDATE dialog_schema SET version = 3')
        catalog.connection.commit()
    with Catalog(path) as catalog:
        Dialogs(catalog, MANAGER)
        assert catalog.connection.execute('SELECT version FROM dialog_schema').fetchone()[0] == 4
        assert catalog.connection.execute('SELECT chat_id FROM tickets').fetchone()[0] == 10


def _update(value):
    base = {'message_id': 40, 'date': 1700000000, 'chat': {'id': CLIENT, 'type': 'private'},
            'from': {'id': CLIENT, 'is_bot': False, 'first_name': 'Олена', 'username': 'olena_k'}}
    return Update.model_validate({'update_id': 5, 'message': {**base, **value}})


def test_adapter_passes_photo_caption_contact_and_downloads_awaited_catalog(tmp_path):
    async def scenario():
        dialogs = SimpleNamespace(handle=AsyncMock(), wants_upload=lambda chat: True, upload_dir=tmp_path)
        bot = SimpleNamespace(download=AsyncMock())
        photo = [{'file_id': 'f', 'file_unique_id': 'u', 'width': 1, 'height': 1}]
        await handle_update(dialogs, bot, _update({'photo': photo, 'caption': 'Такий є?'}))
        kwargs = dialogs.handle.await_args.kwargs
        assert kwargs['text'] == 'Такий є?' and kwargs['media'] == 'фото' and kwargs['message_id'] == 40
        assert kwargs['profile']['username'] == 'olena_k'
        await handle_update(dialogs, bot, _update({'contact': {'phone_number': '+380671234567',
                                                               'first_name': 'Олена', 'user_id': CLIENT}}))
        assert dialogs.handle.await_args.kwargs['text'] == '+380671234567'
        document = {'file_id': 'd', 'file_unique_id': 'du', 'file_name': 'Каталог.XLSX', 'file_size': 2048}
        await handle_update(dialogs, bot, _update({'document': document}))
        assert dialogs.handle.await_args.kwargs['upload'] == tmp_path / '5.xlsx'
        bot.download.assert_awaited_once()
        large = {**document, 'file_size': 6 * 1024 * 1024}
        await handle_update(dialogs, bot, _update({'document': large}))
        assert 'upload' not in dialogs.handle.await_args.kwargs
    asyncio.run(scenario())
