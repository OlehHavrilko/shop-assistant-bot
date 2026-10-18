import asyncio
import logging
import re
import secrets
import time

from .catalog import Catalog
from .storage import Storage
from .storefront import Storefront
from .support import Support

logger = logging.getLogger(__name__)


class Dialogs:
    def __init__(self, catalog: Catalog, manager_id: int, llm=None, *,
                 message_length: int = 2000, request_rate: int = 20,
                 rate_window: float = 60, context_limit: int = 3000, storefront=None):
        if min(message_length, request_rate, rate_window, context_limit) <= 0:
            raise ValueError("Ліміти мають бути додатними.")
        self.catalog, self.manager_id, self.llm = catalog, manager_id, llm
        self.storefront = storefront if storefront is not None else Storefront(catalog)
        self.storage = Storage(catalog.connection)
        self.message_length, self.request_rate = message_length, request_rate
        self.rate_window, self.context_limit = rate_window, context_limit
        self.topics_enabled = False
        self.support = Support(self)
        # One serial update pipeline prevents overlapping transactions and update
        # dedup races on the shared SQLite connection, including during LLM awaits.
        self._lock = asyncio.Lock()

    async def handle(self, update_id: int, chat_id: int, text: str | None = None,
                     callback_data: str | None = None, private: bool = True,
                     reply_to_message_id: int | None = None, thread_id: int | None = None) -> None:
        if not private:
            return
        async with self._lock:
            db = self.catalog.connection
            if db.execute("SELECT 1 FROM incoming WHERE update_id = ?", (update_id,)).fetchone():
                return
            now = time.time()
            # No transaction remains open while contacting the model. Incoming is
            # committed together with state/outbox only after the result is ready.
            decision = None
            state = self.storage.dialog(chat_id)
            allowed = db.execute(
                "SELECT count(*) FROM request_times WHERE chat_id = ? AND received_at > ?",
                (chat_id, now - self.rate_window),
            ).fetchone()[0] < self.request_rate
            clean = (text or "").strip()
            exact = self.catalog.search(sku=clean) if clean else []
            intent = self.storefront.intent(clean) if clean else None
            matches = (self.storefront.search(clean) if clean and not clean.startswith('/')
                       and len(clean) <= self.message_length and not callback_data else [])
            if (allowed and len(clean) <= self.message_length
                    and clean and not clean.startswith('/') and not callback_data
                    and (not state or state['mode'] == 'BOT') and not exact
                    and not matches and not intent
                    and chat_id != self.manager_id and thread_id is None
                    and self.llm is not None and not self._looks_like_sku(clean)):
                try:
                    decision = await self.llm.resolve(clean)
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
                db.execute("DELETE FROM request_times WHERE received_at <= ?", (now - self.rate_window,))
                if chat_id == self.manager_id and len(clean) > self.message_length:
                    self.storage.enqueue(chat_id, f"Повідомлення задовге. Надішліть до {self.message_length} символів.")
                    return
                if chat_id == self.manager_id and self.support.manager_input(
                        clean, callback_data, reply_to_message_id, thread_id):
                    return
                if not allowed:
                    self.storage.enqueue(chat_id, "Забагато запитів. Спробуйте трохи пізніше.")
                    return
                db.execute("INSERT INTO request_times VALUES (?, ?)", (chat_id, now))
                if len(clean) > self.message_length:
                    self.storage.enqueue(chat_id, f"Повідомлення задовге. Надішліть до {self.message_length} символів.")
                    return
                state = self.storage.dialog(chat_id)
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
                    if clean:
                        self.support.record(state['ticket'], 'customer', clean)
                        self.storage.enqueue(self.manager_id, f"Звернення #{state['ticket']}\nКлієнт: {clean}",
                                             self.support.buttons(state['ticket']), ticket=state['ticket'])
                    else:
                        self.storage.enqueue(chat_id, 'Надішліть текстове повідомлення менеджеру.',
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

    def _card(self, chat_id: int, product) -> None:
        # Imported SKU may be longer than Telegram's callback limit. Exact search
        # remains available, while a persisted choice binds the action to a chat.
        token = 'ask:' + secrets.token_urlsafe(12)
        self.catalog.connection.execute('INSERT INTO choices VALUES (?, ?, ?)',
                                        (token, chat_id, product.sku))
        buttons = [('Запитати менеджера', token), ('Каталог тканин', 'shop:catalog')]
        self.storage.enqueue(chat_id, self.storefront.card(product), buttons,
                             photo=self.storefront.photo(product))

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
        db = self.catalog.connection
        state = self.storage.dialog(chat_id)
        if state['ticket']:
            self.support.show(state['ticket'], chat_id)
            return
        ticket = db.execute("INSERT INTO tickets (chat_id) VALUES (?)", (chat_id,)).lastrowid
        db.execute("UPDATE dialogs SET mode = 'HUMAN', ticket = ? WHERE chat_id = ?", (ticket, chat_id))
        db.execute("DELETE FROM choices WHERE chat_id = ?", (chat_id,))
        self.storage.enqueue(chat_id, f"Звернення #{ticket} передано менеджеру. Напишіть тут своє запитання. Відповідь надійде сюди.",
                             self.support.customer_buttons(ticket), ticket=ticket)
        context = self.storage.dialog(chat_id)['context']
        db.execute('INSERT INTO support_context VALUES (?, ?)', (ticket, context))
        self.storage.enqueue(self.manager_id, f"Нове звернення #{ticket}\n{context}\nВідкрийте діалог або скористайтеся Telegram «Відповісти».",
                             self.support.buttons(ticket), ticket=ticket)

    def _choice(self, chat_id: int, data: str) -> None:
        db = self.catalog.connection
        row = db.execute("SELECT sku FROM choices WHERE token = ? AND chat_id = ?", (data, chat_id)).fetchone()
        products = self.catalog.search(sku=row['sku']) if row else []
        if products:
            db.execute("DELETE FROM choices WHERE chat_id = ?", (chat_id,))
            if data.startswith('ask:'):
                self._ask_product(chat_id, products[0])
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
