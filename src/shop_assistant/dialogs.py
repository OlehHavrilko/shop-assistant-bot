import asyncio
import logging
import re
import secrets
import time
from pathlib import Path

from .catalog import Catalog
from .leads import Leads
from .manager_tools import ManagerTools
from .storage import Storage
from .storefront import Storefront
from .support import Support

logger = logging.getLogger(__name__)


class Dialogs:
    def __init__(self, catalog: Catalog, manager_id: int, llm=None, *,
                 message_length: int = 2000, request_rate: int = 20,
                 rate_window: float = 60, context_limit: int = 3000, storefront=None,
                 reminder_minutes: int = 15, clock=time.time):
        if min(message_length, request_rate, rate_window, context_limit) <= 0:
            raise ValueError("Ліміти мають бути додатними.")
        self.catalog, self.manager_id, self.llm = catalog, manager_id, llm
        self.storefront = storefront if storefront is not None else Storefront(catalog)
        self.storage = Storage(catalog.connection)
        self.message_length, self.request_rate = message_length, request_rate
        self.rate_window, self.context_limit = rate_window, context_limit
        self.topics_enabled = False
        self.reminder_minutes, self.clock = reminder_minutes, clock
        self.support = Support(self)
        self.leads = Leads(self)
        self.tools = ManagerTools(self)
        # One serial update pipeline prevents overlapping transactions and update
        # dedup races on the shared SQLite connection, including during LLM awaits.
        self._lock = asyncio.Lock()

    @property
    def database_file(self) -> Path | None:
        row = self.catalog.connection.execute('PRAGMA database_list').fetchone()
        return Path(row[2]) if row and row[2] else None

    @property
    def upload_dir(self) -> Path:
        database = self.database_file
        return (database.parent if database else Path('.')) / 'uploads'

    def wants_upload(self, chat_id: int) -> bool:
        return chat_id == self.manager_id and self.tools.waiting_for_file()

    async def remind(self) -> int:
        async with self._lock:
            with self.catalog.connection:
                return self.tools.remind(self.reminder_minutes)

    async def handle(self, update_id: int, chat_id: int, text: str | None = None,
                     callback_data: str | None = None, private: bool = True,
                     reply_to_message_id: int | None = None, thread_id: int | None = None, *,
                     profile: dict | None = None, media: str | None = None, message_id: int | None = None,
                     upload: Path | None = None) -> None:
        if not private:
            return
        try:
            await self._handle(update_id, chat_id, text, callback_data, reply_to_message_id, thread_id,
                               profile, media, message_id, upload)
        finally:
            # An upload not adopted by a pending import (duplicate, rate limit, error) is removed.
            if upload is not None and not self.tools.keeps(upload):
                Path(upload).unlink(missing_ok=True)

    async def _handle(self, update_id, chat_id, text, callback_data, reply_to_message_id, thread_id,
                      profile, media, message_id, upload) -> None:
        async with self._lock:
            db = self.catalog.connection
            if db.execute("SELECT 1 FROM incoming WHERE update_id = ?", (update_id,)).fetchone():
                return
            now = self.clock()
            # No transaction remains open while contacting the model. Incoming is
            # committed together with state/outbox only after the result is ready.
            decision = None
            state = self.storage.dialog(chat_id)
            allowed = db.execute(
                "SELECT count(*) FROM request_times WHERE chat_id = ? AND received_at > ?",
                (chat_id, now - self.rate_window),
            ).fetchone()[0] < self.request_rate
            clean = (text or "").strip()
            # A caption under a photo or file is not a catalog query.
            query = '' if media else clean
            in_form = bool(query) and not callback_data and self.leads.form(chat_id) is not None
            exact = self.catalog.search(sku=query) if query and not in_form else []
            intent = self.storefront.intent(query) if query and not in_form else None
            matches = (self.storefront.search(query) if query and not query.startswith('/') and not in_form
                       and len(query) <= self.message_length and not callback_data else [])
            if (allowed and len(query) <= self.message_length and not in_form
                    and query and not query.startswith('/') and not callback_data
                    and (not state or state['mode'] == 'BOT') and not exact
                    and not matches and not intent
                    and chat_id != self.manager_id and thread_id is None
                    and self.llm is not None and not self._looks_like_sku(query)):
                try:
                    decision = await self.llm.resolve(query)
                except Exception as error:
                    # The free storefront answers instead; only the type is logged, never the text.
                    logger.warning("LLM resolve failed: %s", type(error).__name__)
                    decision = None
            storefront_thread = (thread_id if thread_id is not None
                                 and self.storage.topic_ticket(chat_id, thread_id) is None else None)
            with self.storage.incoming_scope(chat_id, storefront_thread):
                inserted = db.execute("INSERT OR IGNORE INTO incoming VALUES (?, ?)", (update_id, now))
                if not inserted.rowcount:
                    return
                db.execute("INSERT OR IGNORE INTO dialogs (chat_id) VALUES (?)", (chat_id,))
                if profile and chat_id != self.manager_id:
                    db.execute('INSERT OR REPLACE INTO customers VALUES (?, ?, ?, ?, ?)',
                               (chat_id, *(str(profile.get(key) or '')[:64] or None
                                           for key in ('first_name', 'last_name', 'username')), now))
                db.execute("DELETE FROM request_times WHERE received_at <= ?", (now - self.rate_window,))
                if chat_id == self.manager_id and len(clean) > self.message_length:
                    self.storage.enqueue(chat_id, f"Повідомлення задовге. Надішліть до {self.message_length} символів.")
                    return
                if chat_id == self.manager_id and self.support.manager_input(
                        clean, callback_data, reply_to_message_id, thread_id,
                        media=media, message_id=message_id, upload=upload):
                    return
                if not allowed:
                    self.storage.enqueue(chat_id, "Забагато запитів. Спробуйте трохи пізніше.")
                    return
                db.execute("INSERT INTO request_times VALUES (?, ?)", (chat_id, now))
                if len(clean) > self.message_length:
                    self.storage.enqueue(chat_id, f"Повідомлення задовге. Надішліть до {self.message_length} символів.")
                    return
                state = self.storage.dialog(chat_id)
                if clean in ('/start', '/help', '/manager'):
                    self.leads.cancel(chat_id)
                if callback_data and callback_data.startswith('lead:'):
                    self.leads.callback(chat_id, callback_data)
                    return
                if not callback_data and self.leads.form(chat_id) is not None:
                    if media or not clean:
                        self.storage.enqueue(chat_id, 'Для заявки надішліть текст. Скасувати: /start.')
                    else:
                        self.leads.text(chat_id, clean)
                    return
                if chat_id != self.manager_id and self.support.customer_input(chat_id, clean, callback_data, thread_id):
                    return
                if clean:
                    context = (state['context'] + '\nКлієнт: ' + clean)[-self.context_limit:]
                    db.execute("UPDATE dialogs SET context = ? WHERE chat_id = ?", (context, chat_id))
                if (state['mode'] == 'HUMAN' and not callback_data
                        and (not self.topics_enabled or clean == '/manager'
                             or (thread_id is not None and
                                 self.storage.topic_ticket(chat_id, thread_id) == state['ticket']))):
                    if clean in ('/start', '/help', '/manager', '/support'):
                        self.support.show(state['ticket'], chat_id)
                        return
                    number = state['ticket']
                    label = self.support.customer_label(chat_id)
                    if media and message_id is not None:
                        self.support.record(number, 'customer', f'[{media}] {clean}'.strip())
                        self.storage.enqueue(self.manager_id, f"Звернення #{number} · {label}\nКлієнт надіслав: {media}",
                                             ticket=number)
                        self.storage.enqueue(self.manager_id, '', self.support.buttons(number), ticket=number,
                                             copy=(chat_id, message_id))
                        self.off_hours_notice(chat_id, number)
                    elif clean:
                        self.support.record(number, 'customer', clean)
                        self.storage.enqueue(self.manager_id, f"Звернення #{number} · {label}\nКлієнт: {clean}",
                                             self.support.buttons(number), ticket=number)
                        self.off_hours_notice(chat_id, number)
                    else:
                        self.storage.enqueue(chat_id, 'Надішліть менеджеру текст, фото або файл.',
                                             self.support.customer_buttons(state['ticket']), ticket=state['ticket'])
                    return
                if clean == '/manager':
                    self._handoff(chat_id)
                elif clean in ('/start', '/help'):
                    self._welcome(chat_id)
                elif callback_data:
                    if callback_data.startswith('shop:'):
                        self._shop(chat_id, callback_data)
                    else:
                        self._choice(chat_id, callback_data)
                elif media and state['ticket']:
                    self.storage.enqueue(chat_id, f"Надішліть файл у темі «Звернення #{state['ticket']}» — "
                                                  'там його отримає менеджер.', self._menu(chat_id))
                elif media:
                    self.storage.enqueue(chat_id, 'Щоб показати фото чи файл менеджеру, натисніть «Написати менеджеру» '
                                                  'і надішліть його ще раз.', self._menu(chat_id))
                elif not clean:
                    self.storage.enqueue(chat_id, "Надішліть текст або артикул. Для допомоги: /manager.")
                elif exact:
                    self._card(chat_id, exact[0])
                elif intent:
                    self._intent(chat_id, intent)
                elif matches:
                    self._products(chat_id, matches)
                elif self._looks_like_sku(clean):
                    self.storage.enqueue(chat_id, "Артикул не знайдено. Перевірте його або напишіть /manager.")
                elif decision is None:
                    if chat_id != self.manager_id:
                        self.tools.record_miss(clean)
                    self.storage.enqueue(chat_id, "Не знайшли тканину за цим запитом. Спробуйте назву, колір або артикул — наприклад, «льон ivory». Або перегляньте каталог.", self._menu(chat_id))
                else:
                    self._decision(chat_id, decision)

    @staticmethod
    def _looks_like_sku(text: str) -> bool:
        return bool(re.fullmatch(r'[\w.-]*\d[\w.-]*', text))

    def _menu(self, chat_id: int):
        buttons = [
            ('Каталог тканин', 'shop:catalog'),
            ('Підібрати тканину', 'shop:search'),
            ('Доставка й оплата', 'shop:info:delivery'),
            ('Про магазин', 'shop:info:about'),
            ('Написати менеджеру', 'shop:manager'),
        ]
        if chat_id == self.manager_id:
            buttons.append(('Кабінет менеджера', 'shop:tickets'))
        elif (state := self.storage.dialog(chat_id)) and state['ticket']:
            buttons.append(('Моє звернення', f"client:status:{state['ticket']}"))
        return buttons

    def _welcome(self, chat_id: int) -> None:
        self.storage.enqueue(chat_id, self.storefront.welcome(), self._menu(chat_id))

    def _card(self, chat_id: int, product, ticket: int | None = None) -> int:
        # Imported SKU may be longer than Telegram's callback limit. Exact search
        # remains available, while a persisted choice binds the action to a chat.
        buttons = []
        for prefix, label in (('ask:', 'Запитати менеджера'), ('order:', 'Оформити заявку')):
            if prefix == 'ask:' and ticket is not None:
                continue
            token = prefix + secrets.token_urlsafe(12)
            self.catalog.connection.execute('INSERT INTO choices VALUES (?, ?, ?)', (token, chat_id, product.sku))
            buttons.append((label, token))
        buttons.append(('Каталог тканин', 'shop:catalog'))
        return self.storage.enqueue(chat_id, self.storefront.card(product), buttons,
                                    photo=self.storefront.photo(product), ticket=ticket)

    def _products(self, chat_id: int, products) -> None:
        if len(products) == 1:
            self._card(chat_id, products[0])
            return
        db = self.catalog.connection
        db.execute('DELETE FROM choices WHERE chat_id = ?', (chat_id,))
        buttons = []
        for product in products:
            token = 'pick:' + secrets.token_urlsafe(12)
            db.execute('INSERT INTO choices VALUES (?, ?, ?)', (token, chat_id, product.sku))
            label = f'{product.sku} · {product.name} · {product.shade}'
            buttons.append((label[:100], token))
        buttons.append(('Каталог тканин', 'shop:catalog'))
        self.storage.enqueue(chat_id, 'Знайшли кілька тканин. Оберіть відтінок:', buttons)

    def _intent(self, chat_id: int, intent: str) -> None:
        if intent == 'manager':
            self._handoff(chat_id)
        elif intent == 'catalog':
            self._shop(chat_id, 'shop:catalog')
        else:
            self.storage.enqueue(chat_id, self.storefront.info(intent),
                                 [('Оплата', 'shop:info:payment'), ('Головне меню', 'shop:home'),
                                  ('Написати менеджеру', 'shop:manager')])

    def _shop(self, chat_id: int, data: str) -> None:
        if data == 'shop:home':
            self.storage.enqueue(chat_id, self.storefront.welcome(), self._menu(chat_id))
        elif data == 'shop:catalog':
            buttons = [(label, f'shop:category:{category}:0')
                       for category, label in self.storefront.categories()]
            buttons.append(('Головне меню', 'shop:home'))
            self.storage.enqueue(chat_id, '🧵 Знайдемо тканину для вашого задуму\n\nОберіть категорію нижче — покажемо фото, характеристики й ціни.', buttons)
        elif data == 'shop:search':
            self.storage.enqueue(chat_id, '🔎 Що шукаємо?\n\nНапишіть матеріал, колір або призначення:\n• льон айворі\n• бавовна синя\n• тканина для сукні\n\nЗнаєте артикул? Просто надішліть його — відкриємо картку.',
                                 [('Каталог тканин', 'shop:catalog'), ('Головне меню', 'shop:home')])
        elif data == 'shop:manager':
            self._handoff(chat_id)
        elif data in ('shop:info:about', 'shop:info:delivery', 'shop:info:payment'):
            self._intent(chat_id, data.rsplit(':', 1)[1])
        elif data.startswith('shop:category:'):
            parts = data.split(':')
            categories = dict(self.storefront.categories())
            if (len(parts) != 4 or parts[2] not in categories
                    or not re.fullmatch(r'(?:0|[1-9][0-9]{0,4})', parts[3])
                    or not 0 <= int(parts[3]) <= 10000 or int(parts[3]) % 4):
                self.storage.enqueue(chat_id, 'Ця категорія недоступна. Відкрийте каталог ще раз.', self._menu(chat_id))
                return
            category, offset = parts[2], int(parts[3])
            products, more = self.storefront.category_products(category, offset=offset, limit=4)
            for product in products:
                self._card(chat_id, product)
            buttons = []
            if offset:
                buttons.append(('Попередні тканини', f'shop:category:{category}:{offset - 4}'))
            if more:
                buttons.append(('Ще тканини', f'shop:category:{category}:{offset + 4}'))
            buttons.append(('Усі категорії', 'shop:catalog'))
            self.storage.enqueue(chat_id, categories[category] if products else 'У цій категорії поки немає тканин.', buttons)
        elif data.startswith('shop:ask:'):
            products = self.catalog.search(sku=data.removeprefix('shop:ask:'))
            if products:
                self._ask_product(chat_id, products[0])
            else:
                self.storage.enqueue(chat_id, 'Тканина більше недоступна. Відкрийте каталог ще раз.', self._menu(chat_id))
        else:
            self.storage.enqueue(chat_id, 'Цей вибір більше недоступний. Відкрийте головне меню.', self._menu(chat_id))

    def _ask_product(self, chat_id: int, product) -> None:
        db = self.catalog.connection
        context = (self.storage.dialog(chat_id)['context'] +
                   f'\nОбрана тканина: {product.name}, {product.shade}, артикул {product.sku}')[-self.context_limit:]
        db.execute('UPDATE dialogs SET context = ? WHERE chat_id = ?', (context, chat_id))
        self._handoff(chat_id)

    def _handoff(self, chat_id: int) -> None:
        if chat_id == self.manager_id:
            self.storage.enqueue(chat_id, 'Ви менеджер магазину. Відкрийте кабінет звернень.', self._menu(chat_id))
            return
        state = self.storage.dialog(chat_id)
        if state['ticket']:
            self.support.show(state['ticket'], chat_id)
            return
        ticket = self.create_ticket(chat_id)
        self.storage.enqueue(chat_id, f"Звернення #{ticket} передано менеджеру. Напишіть тут своє запитання, "
                                      "можна з фото чи файлом. Відповідь надійде сюди.",
                             self.support.customer_buttons(ticket), ticket=ticket)
        context = self.storage.dialog(chat_id)['context']
        self.storage.enqueue(self.manager_id, f"Нове звернення #{ticket} · {self.support.customer_label(chat_id)}\n"
                                              f"{context}\nВідкрийте діалог або скористайтеся Telegram «Відповісти».",
                             self.support.buttons(ticket), ticket=ticket)
        self.off_hours_notice(chat_id, ticket)

    def create_ticket(self, chat_id: int) -> int:
        db = self.catalog.connection
        now = self.clock()
        ticket = db.execute("INSERT INTO tickets (chat_id, created_at, last_customer_at) VALUES (?, ?, ?)",
                            (chat_id, now, now)).lastrowid
        db.execute("UPDATE dialogs SET mode = 'HUMAN', ticket = ? WHERE chat_id = ?", (ticket, chat_id))
        db.execute("DELETE FROM choices WHERE chat_id = ?", (chat_id,))
        db.execute('INSERT INTO support_context VALUES (?, ?)', (ticket, self.storage.dialog(chat_id)['context']))
        return ticket

    def off_hours_notice(self, chat_id: int, ticket: int) -> None:
        now = self.clock()
        if self.storefront.is_open(now):
            return
        row = self.catalog.connection.execute('SELECT off_hours_at FROM tickets WHERE id = ?', (ticket,)).fetchone()
        if row and row['off_hours_at'] and now - row['off_hours_at'] < 12 * 3600:
            return
        self.catalog.connection.execute('UPDATE tickets SET off_hours_at = ? WHERE id = ?', (now, ticket))
        self.storage.enqueue(chat_id, f"🌙 Зараз неробочий час. Менеджер відповість у робочі години: "
                                      f"{self.storefront.hours['label']}.", ticket=ticket)

    def _choice(self, chat_id: int, data: str) -> None:
        db = self.catalog.connection
        row = db.execute("SELECT sku FROM choices WHERE token = ? AND chat_id = ?", (data, chat_id)).fetchone()
        products = self.catalog.search(sku=row['sku']) if row else []
        if products:
            db.execute("DELETE FROM choices WHERE chat_id = ?", (chat_id,))
            if data.startswith('ask:'):
                self._ask_product(chat_id, products[0])
            elif data.startswith('order:'):
                self.leads.start(chat_id, products[0])
            else:
                self._card(chat_id, products[0])
        else:
            self.storage.enqueue(chat_id, "Цей вибір більше недоступний. Надішліть новий запит.")

    def _decision(self, chat_id: int, decision) -> None:
        action = getattr(decision, 'action', None)
        if action == 'handoff':
            self._handoff(chat_id)
            return
        skus = getattr(decision, 'skus', ())
        if not isinstance(skus, (list, tuple)) or len(skus) > 10 or any(not isinstance(s, str) for s in skus):
            skus = ()
        products = []
        for sku in dict.fromkeys(skus):
            found = self.catalog.search(sku=sku)
            if not found:
                products = []
                break
            products.extend(found)
        if action == 'products' and len(products) == 1:
            self._card(chat_id, products[0])
        elif action in ('products', 'clarify') and products:
            self._products(chat_id, products)
        elif action == 'clarify' and not skus:
            self.storage.enqueue(chat_id, "Уточніть назву, відтінок або артикул тканини. Допомога: /manager.")
        else:
            self.storage.enqueue(chat_id, "Товар не знайдено. Уточніть артикул або напишіть /manager.")
