"""Purchase request form: SKU, meters, phone, delivery, comment. No payment or reservation."""

import json
import re
from decimal import Decimal, InvalidOperation

from .storefront import DELIVERY_OPTIONS

CANCEL = ('Скасувати заявку', 'lead:cancel')


class Leads:
    def __init__(self, dialogs):
        self.dialogs = dialogs
        self.storage = dialogs.storage
        self.db = dialogs.catalog.connection

    def form(self, chat):
        row = self.db.execute("SELECT * FROM forms WHERE chat_id = ? AND kind = 'lead'", (chat,)).fetchone()
        return (row['step'], json.loads(row['data'])) if row else None

    def _save(self, chat, step, data):
        self.db.execute("INSERT OR REPLACE INTO forms VALUES (?, 'lead', ?, ?, ?)",
                        (chat, step, json.dumps(data, ensure_ascii=False), self.dialogs.clock()))

    def _product(self, data):
        found = self.dialogs.catalog.search(sku=data['sku'])
        return found[0] if found else None

    def start(self, chat, product):
        if chat == self.dialogs.manager_id:
            self.storage.enqueue(chat, 'Заявки оформлюють клієнти. Ви бачите їх у кабінеті менеджера.',
                                 self.dialogs._menu(chat))
            return
        self._save(chat, 'meters', {'sku': product.sku})
        self.storage.enqueue(chat, f'🧾 Заявка: {product.name} · {product.shade} ({product.sku})\n'
                                   f'Ціна: {product.price_uah_per_m:.2f} грн/м\n\n'
                                   'Скільки метрів потрібно? Наприклад: 2.5', [CANCEL])

    def cancel(self, chat):
        self.db.execute("DELETE FROM forms WHERE chat_id = ? AND kind = 'lead'", (chat,))

    def callback(self, chat, data):
        current = self.form(chat)
        if data == 'lead:cancel':
            self.cancel(chat)
            self.storage.enqueue(chat, 'Заявку скасовано.', self.dialogs._menu(chat))
            return
        if current is None:
            self.storage.enqueue(chat, 'Ця заявка вже неактивна. Відкрийте товар ще раз.', self.dialogs._menu(chat))
            return
        step, form = current
        if step == 'delivery' and re.fullmatch(r'lead:delivery:[0-9]', data) and int(data[-1]) < len(DELIVERY_OPTIONS):
            form['delivery'] = DELIVERY_OPTIONS[int(data[-1])]
            self._save(chat, 'comment', form)
            self.storage.enqueue(chat, 'Коментар до заявки: місто й відділення, побажання щодо відтінку. '
                                       'Або натисніть «Без коментаря».',
                                 [('Без коментаря', 'lead:skip'), CANCEL])
        elif step == 'comment' and data == 'lead:skip':
            form['comment'] = ''
            self._confirm(chat, form)
        elif step == 'confirm' and data == 'lead:submit':
            self._submit(chat, form)
        else:
            self._repeat(chat, step, form)

    def text(self, chat, text):
        step, form = self.form(chat)
        if step == 'meters':
            meters = _meters(text)
            if meters is None:
                self.storage.enqueue(chat, 'Вкажіть кількість метрів числом від 0.1 до 1000, наприклад 2.5.', [CANCEL])
                return
            form['meters'] = format(meters, 'f')
            self._save(chat, 'phone', form)
            self.storage.enqueue(chat, 'Ваш номер телефону для підтвердження? Наприклад: +380 67 123 45 67', [CANCEL])
        elif step == 'phone':
            phone = _phone(text)
            if phone is None:
                self.storage.enqueue(chat, 'Не схоже на номер телефону. Вкажіть 10–15 цифр, наприклад +380671234567.',
                                     [CANCEL])
                return
            form['phone'] = phone
            self._save(chat, 'delivery', form)
            self._repeat(chat, 'delivery', form)
        elif step == 'comment':
            form['comment'] = text[:500]
            self._confirm(chat, form)
        else:
            self._repeat(chat, step, form)

    def _repeat(self, chat, step, form):
        if step == 'delivery':
            buttons = [(option, f'lead:delivery:{index}') for index, option in enumerate(DELIVERY_OPTIONS)]
            self.storage.enqueue(chat, 'Як зручно отримати тканину?', buttons + [CANCEL])
        elif step == 'confirm':
            self._confirm(chat, form)
        else:
            self.storage.enqueue(chat, 'Продовжіть заявку або скасуйте її.', [CANCEL])

    def summary(self, form):
        product = self._product(form)
        meters = Decimal(form['meters'])
        lines = [f"Товар: {product.name} · {product.shade} ({product.sku})" if product else f"Товар: {form['sku']}",
                 f"Кількість: {meters} м"]
        if product:
            lines.append(f"Орієнтовна сума: {product.price_uah_per_m * meters:.2f} грн")
            if product.stock_m is not None and meters > product.stock_m:
                lines.append(f"Увага: на складі {product.stock_m:.2f} м — менеджер уточнить.")
        lines += [f"Телефон: {form['phone']}", f"Доставка: {form['delivery']}"]
        if form.get('comment'):
            lines.append(f"Коментар: {form['comment']}")
        return '\n'.join(lines)

    def _confirm(self, chat, form):
        if self._product(form) is None:
            self.cancel(chat)
            self.storage.enqueue(chat, 'Цей товар більше недоступний. Оберіть інший у каталозі.', self.dialogs._menu(chat))
            return
        self._save(chat, 'confirm', form)
        self.storage.enqueue(chat, f'Перевірте заявку:\n\n{self.summary(form)}\n\n'
                                   'Це не оплата й не резерв: менеджер підтвердить наявність і доставку.',
                             [('Надіслати заявку', 'lead:submit'), CANCEL])

    def _submit(self, chat, form):
        summary = self.summary(form)
        self.cancel(chat)
        state = self.storage.dialog(chat)
        number = state['ticket'] or self.dialogs.create_ticket(chat)
        now = self.dialogs.clock()
        lead = self.db.execute(
            'INSERT INTO leads (chat_id, ticket, sku, meters, phone, delivery, comment, created_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (chat, number, form['sku'], form['meters'], form['phone'], form['delivery'],
             form.get('comment') or None, now)).lastrowid
        support = self.dialogs.support
        support.record(number, 'customer', f'Заявка №{lead}\n{summary}')
        self.storage.enqueue(chat, f'✅ Заявку №{lead} передано менеджеру (звернення #{number}).\n'
                                   'Менеджер зв’яжеться, щоб підтвердити наявність, доставку й оплату. '
                                   'Бот не приймає оплату.',
                             support.customer_buttons(number), ticket=number)
        self.storage.enqueue(self.dialogs.manager_id,
                             f'🧾 Нова заявка №{lead} · звернення #{number}\n{support.customer_label(chat)}\n\n{summary}',
                             support.buttons(number), ticket=number)
        self.dialogs.off_hours_notice(chat, number)


def _meters(text):
    value = text.strip().lower().removesuffix('м').strip().replace(',', '.')
    if not re.fullmatch(r'[0-9]{1,4}(?:\.[0-9]{1,2})?', value):
        return None
    try:
        meters = Decimal(value)
    except InvalidOperation:
        return None
    return meters if Decimal('0.1') <= meters <= 1000 else None


def _phone(text):
    value = text.strip()
    digits = re.sub(r'[\s()\-.]', '', value)
    if not re.fullmatch(r'\+?[0-9]{10,15}', digits):
        return None
    # 0671234567 stays local; 380671234567 gets the international plus.
    if not digits.startswith('+') and len(digits) > 10:
        digits = '+' + digits
    return digits
