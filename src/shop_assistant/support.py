"""Ticket screens and routing; Telegram transport remains in the outbox worker."""

import json
from datetime import UTC, datetime


class Support:
    def __init__(self, dialogs):
        self.dialogs = dialogs
        self.storage = dialogs.storage
        self.db = dialogs.catalog.connection
        self.manager = dialogs.manager_id

    def customer_label(self, chat):
        row = self.db.execute('SELECT * FROM customers WHERE chat_id = ?', (chat,)).fetchone()
        if not row:
            return 'Клієнт без імені'
        name = ' '.join(part for part in (row['first_name'], row['last_name']) if part) or 'Клієнт без імені'
        return f"{name} (@{row['username']})" if row['username'] else name

    def ticket(self, number):
        return self.db.execute('SELECT * FROM tickets WHERE id = ?', (number,)).fetchone()

    def buttons(self, number):
        return [('Відкрити діалог', f'ticket:open:{number}'),
                ('Завершити звернення', f'ticket:confirm:{number}'),
                ('Усі звернення', 'shop:tickets')]

    def customer_buttons(self, number):
        return [('Моє звернення', f'client:status:{number}'), ('Завершити розмову', f'client:confirm:{number}'),
                ('Каталог тканин', 'shop:catalog')]

    def record(self, number, role, text, outbox_id=None):
        now = self.dialogs.clock()
        self.db.execute(
            'INSERT INTO support_messages (ticket, role, text, created_at, outbox_id) VALUES (?, ?, ?, ?, ?)',
            (number, role, text, now, outbox_id))
        if role == 'customer':
            self.db.execute('UPDATE tickets SET last_customer_at = ? WHERE id = ?', (now, number))
        else:
            self.db.execute('UPDATE tickets SET last_manager_at = ?, first_reply_at = coalesce(first_reply_at, ?) '
                            'WHERE id = ?', (now, now, number))

    def inbox(self, active=True, offset=0):
        self.db.execute('DELETE FROM support_sessions WHERE chat_id = ?', (self.manager,))
        rows = self.db.execute(
            'SELECT t.*, (SELECT text FROM support_messages WHERE ticket = t.id ORDER BY id DESC LIMIT 1) preview '
            'FROM tickets t WHERE active = ? ORDER BY id DESC LIMIT 9 OFFSET ?', (int(active), offset)).fetchall()
        buttons = [(f"#{r['id']} · {self.customer_label(r['chat_id'])[:30]} · {(r['preview'] or 'Нове звернення')[:40]}",
                    f"ticket:open:{r['id']}") for r in rows[:8]]
        flag = int(active)
        if offset:
            buttons.append(('Назад', f'ticket:list:{flag}:{max(0, offset - 8)}'))
        if len(rows) > 8:
            buttons.append(('Далі', f'ticket:list:{flag}:{offset + 8}'))
        buttons += [('Архів' if active else 'Активні звернення', f'ticket:list:{1 - flag}:0'),
                    ('Статистика', 'shop:stats'), ('Оновити каталог', 'shop:import'),
                    ('Переглянути магазин', 'shop:home')]
        title = 'Активні звернення' if active else 'Архів звернень'
        buttons.append(('Оновити список', f'ticket:list:{flag}:{offset}'))
        self.storage.enqueue(self.manager, f"🗂 Кабінет менеджера\n\n{title}\nОберіть звернення — відкриємо контекст і переписку." if rows
                             else f"🗂 Кабінет менеджера\n\n{title}: немає.\nТут з’являться нові звернення покупців.", buttons)

    def show(self, number, recipient, offset=0):
        row = self.ticket(number)
        if not row or (recipient != self.manager and recipient != row['chat_id']):
            self.storage.enqueue(recipient, 'Звернення недоступне.')
            return
        manager = recipient == self.manager
        if manager and row['active']:
            self.db.execute('INSERT OR REPLACE INTO support_sessions VALUES (?, ?)', (recipient, number))
        elif manager:
            self.db.execute('DELETE FROM support_sessions WHERE chat_id = ?', (recipient,))
        context = self.db.execute('SELECT text FROM support_context WHERE ticket = ?', (number,)).fetchone()
        status = 'Відкрите' if row['active'] else 'Завершене'
        header = f"Звернення #{number} · {status}"
        if manager:
            header += f"\nКлієнт: {self.customer_label(row['chat_id'])}"
            if row['rating']:
                header += f"\nОцінка клієнта: {'★' * row['rating']}{'☆' * (5 - row['rating'])}"
        if context and manager:
            header += '\n' + context['text']
        self.storage.enqueue(recipient, header, ticket=number)
        rows = self.db.execute(
            'SELECT m.*, o.delivered, o.attempts FROM support_messages m LEFT JOIN outbox o ON o.id = m.outbox_id '
            'WHERE m.ticket = ? ORDER BY m.id DESC LIMIT 7 OFFSET ?', (number, offset)).fetchall()
        for message in reversed(rows[:6]):
            role = 'Клієнт' if message['role'] == 'customer' else 'Менеджер'
            delivery = ''
            if manager and message['role'] == 'manager':
                delivery = ' · доставлено в Telegram' if message['delivered'] else ' · очікує доставки'
                if not message['delivered'] and message['attempts']:
                    delivery = ' · доставка затримується, повторюємо'
            stamp = datetime.fromtimestamp(message['created_at'], UTC).strftime('%d.%m %H:%M UTC')
            self.storage.enqueue(recipient, f"{role} · {stamp}{delivery}:\n{message['text']}", ticket=number)
        buttons = []
        if len(rows) > 6:
            buttons.append(('Раніші повідомлення', f'ticket:history:{number}:{offset + 6}'))
        if offset:
            buttons.append(('Останні повідомлення', f'ticket:history:{number}:0'))
        if manager:
            if row['active']:
                buttons += [('Шаблони відповідей', f'ticket:tpl:{number}'),
                            ('Надіслати товар', f'ticket:card:{number}'),
                            ('Завершити звернення', f'ticket:confirm:{number}')]
            buttons.append(('До списку звернень', 'shop:tickets'))
        else:
            buttons += self.customer_buttons(number) if row['active'] else [('Головне меню', 'shop:home')]
        prompt = (f'Ви відповідаєте у звернення #{number}. Напишіть звичайне повідомлення.'
                  if manager and row['active'] else 'Історія розмови.')
        self.storage.enqueue(recipient, prompt, buttons, ticket=number)

    def close(self, number, actor):
        row = self.ticket(number)
        if not row or (actor != self.manager and actor != row['chat_id']):
            self.storage.enqueue(actor, 'Звернення недоступне.')
            return
        if not row['active']:
            self.storage.enqueue(actor, 'Це звернення вже завершене.', ticket=number)
            return
        self.db.execute('UPDATE tickets SET active = 0, closed_at = ? WHERE id = ?', (self.dialogs.clock(), number))
        self.db.execute("UPDATE dialogs SET mode = 'BOT', ticket = NULL WHERE chat_id = ? AND ticket = ?",
                        (row['chat_id'], number))
        self.db.execute('DELETE FROM support_sessions WHERE ticket = ?', (number,))
        stars = [('★' * score, f'client:rate:{number}:{score}') for score in range(1, 6)]
        self.storage.enqueue(row['chat_id'], f'Звернення #{number} завершено. Можна знову шукати тканини.\n\n'
                                             'Оцініть, будь ласка, допомогу менеджера:',
                             stars + [('Головне меню', 'shop:home')], ticket=number)
        self.storage.enqueue(self.manager, f'Звернення #{number} повернуто боту.',
                             [('До списку звернень', 'shop:tickets')], ticket=number)

    def reply(self, number, text):
        row = self.ticket(number)
        state = self.storage.dialog(row['chat_id']) if row else None
        if not row or not row['active'] or not state or state['ticket'] != number or state['mode'] != 'HUMAN':
            self.storage.enqueue(self.manager, 'Активне звернення не знайдено. Відкрийте список звернень.',
                                 [('Звернення', 'shop:tickets')])
            return
        outbox = self.storage.enqueue(row['chat_id'], 'Менеджер: ' + text,
                                      self.customer_buttons(number), ticket=number)
        if not self.dialogs.topics_enabled:
            self.db.execute('INSERT OR REPLACE INTO support_sessions VALUES (?, ?)', (self.manager, number))
        self.record(number, 'manager', text, outbox)
        context = (state['context'] + '\nМенеджер: ' + text)[-self.dialogs.context_limit:]
        self.db.execute('UPDATE dialogs SET context = ? WHERE chat_id = ?', (context, row['chat_id']))
        self.storage.enqueue(self.manager, f'Відповідь #{number} у черзі. Статус доставки — в історії звернення.',
                             self.buttons(number), ticket=number)

    def send_card(self, number, product):
        row = self.ticket(number)
        state = self.storage.dialog(row['chat_id']) if row else None
        if not row or not row['active'] or not state or state['ticket'] != number:
            self.storage.enqueue(self.manager, 'Активне звернення не знайдено. Відкрийте список звернень.',
                                 [('Звернення', 'shop:tickets')])
            return
        outbox = self.dialogs._card(row['chat_id'], product, ticket=number)
        self.record(number, 'manager', f'Картка товару: {product.name} · {product.shade} ({product.sku})', outbox)
        self.storage.enqueue(self.manager, f'Картку {product.sku} надіслано у звернення #{number}.',
                             self.buttons(number), ticket=number)

    def relay_media(self, number, message_id, kind, caption):
        row = self.ticket(number)
        state = self.storage.dialog(row['chat_id']) if row else None
        if not row or not row['active'] or not state or state['ticket'] != number or state['mode'] != 'HUMAN':
            self.storage.enqueue(self.manager, 'Активне звернення не знайдено. Відкрийте список звернень.',
                                 [('Звернення', 'shop:tickets')])
            return
        outbox = self.storage.enqueue(row['chat_id'], '', self.customer_buttons(number), ticket=number,
                                      copy=(self.manager, message_id))
        self.record(number, 'manager', f'[{kind}] {caption}'.strip(), outbox)
        self.storage.enqueue(self.manager, f'Файл ({kind}) у черзі до звернення #{number}.',
                             self.buttons(number), ticket=number)

    def _templates(self, number):
        replies = self.dialogs.storefront.quick_replies
        buttons = [(reply[:60] + ('…' if len(reply) > 60 else ''), f'ticket:tplsend:{number}:{index}')
                   for index, reply in enumerate(replies)]
        text = '\n\n'.join(f'{index + 1}. {reply}' for index, reply in enumerate(replies))
        self.storage.enqueue(self.manager, f'Шаблони для звернення #{number}. Натисніть — клієнт отримає текст:\n\n{text}',
                             buttons + [('Відкрити діалог', f'ticket:open:{number}')], ticket=number)

    def _card_form(self, text):
        row = self.db.execute("SELECT data FROM forms WHERE chat_id = ? AND kind = 'card'", (self.manager,)).fetchone()
        if row is None:
            return False
        number = json.loads(row['data'])['ticket']
        product = self._find_product(text)
        if product is None:
            self.storage.enqueue(self.manager, 'Не знайшли один товар. Надішліть точний артикул.',
                                 [('Відкрити діалог', f'ticket:open:{number}')], ticket=number)
            return True
        self.db.execute("DELETE FROM forms WHERE chat_id = ? AND kind = 'card'", (self.manager,))
        self.send_card(number, product)
        return True

    def _find_product(self, text):
        exact = self.dialogs.catalog.search(sku=text) if text else []
        found = exact or (self.dialogs.storefront.search(text, limit=2) if text else [])
        return found[0] if len(found) == 1 else None

    def manager_input(self, text, data, reply_to=None, thread_id=None, media=None, message_id=None, upload=None):
        tools = self.dialogs.tools
        if text in ('/start', '/help'):
            self.db.execute('DELETE FROM support_sessions WHERE chat_id = ?', (self.manager,))
            self.db.execute("DELETE FROM forms WHERE chat_id = ? AND kind = 'card'", (self.manager,))
            tools._discard()
            self.dialogs._welcome(self.manager)
            return True
        if data == 'shop:tickets' or text == '/tickets':
            self.inbox()
            return True
        if data == 'shop:stats' or text == '/stats':
            tools.stats()
            return True
        if data == 'shop:import' or text == '/import':
            tools.start_import()
            return True
        if data and data.startswith('import:'):
            tools.callback(data)
            return True
        if (upload is not None or media) and tools.upload(upload, media):
            return True
        if data and data.startswith('ticket:'):
            parts = data.split(':')
            if len(parts) >= 3 and all(p.isdigit() and len(p) <= 9 for p in parts[2:]):
                action, number = parts[1], int(parts[2])
                if action == 'list' and len(parts) == 4 and number in (0, 1):
                    self.inbox(bool(number), int(parts[3]))
                    return True
                row = self.ticket(number)
                if row:
                    if action in ('open', 'history') and len(parts) in (3, 4):
                        self.show(number, self.manager, int(parts[3]) if len(parts) == 4 else 0)
                        return True
                    if action == 'confirm' and len(parts) == 3:
                        self.storage.enqueue(self.manager, f'Завершити звернення #{number}?',
                                             [('Так, завершити', f'ticket:close:{number}'),
                                              ('Продовжити розмову', f'ticket:open:{number}')], ticket=number)
                        return True
                    if action == 'close' and len(parts) == 3:
                        self.close(number, self.manager)
                        return True
                    if action == 'tpl' and len(parts) == 3 and row['active']:
                        self._templates(number)
                        return True
                    replies = self.dialogs.storefront.quick_replies
                    if action == 'tplsend' and len(parts) == 4 and int(parts[3]) < len(replies):
                        self.reply(number, replies[int(parts[3])])
                        return True
                    if action == 'card' and len(parts) == 3 and row['active']:
                        self.db.execute("INSERT OR REPLACE INTO forms VALUES (?, 'card', 'sku', ?, ?)",
                                        (self.manager, json.dumps({'ticket': number}), self.dialogs.clock()))
                        self.storage.enqueue(self.manager, f'Надішліть артикул — клієнт звернення #{number} '
                                                           'отримає картку з фото й кнопкою заявки.',
                                             [('Відкрити діалог', f'ticket:open:{number}')], ticket=number)
                        return True
            self.storage.enqueue(self.manager, 'Звернення недоступне.', [('Звернення', 'shop:tickets')])
            return True
        if text.startswith('/reply') or text.startswith('/bot'):
            parts = text.split(maxsplit=2)
            if len(parts) >= 2 and parts[1].isdigit() and len(parts[1]) <= 9:
                if parts[0] == '/reply' and len(parts) == 3:
                    self.reply(int(parts[1]), parts[2])
                    return True
                if parts[0] == '/bot' and len(parts) == 2:
                    self.close(int(parts[1]), self.manager)
                    return True
            self.storage.enqueue(self.manager, 'Відкрийте звернення й напишіть відповідь.', [('Звернення', 'shop:tickets')])
            return True
        if data:
            self.db.execute('DELETE FROM support_sessions WHERE chat_id = ?', (self.manager,))
            self.db.execute("DELETE FROM forms WHERE chat_id = ? AND kind = 'card'", (self.manager,))
            return False
        if text and not text.startswith('/') and not media and self._card_form(text):
            return True
        number = None
        if reply_to is not None:
            row = self.db.execute('SELECT ticket FROM outbox WHERE chat_id = ? AND message_id = ? AND ticket IS NOT NULL',
                                  (self.manager, reply_to)).fetchone()
            number = row['ticket'] if row else None
        if thread_id is not None:
            topic_number = self.storage.topic_ticket(self.manager, thread_id)
            number = topic_number if number is None or number == topic_number else None
        elif reply_to is None and not self.dialogs.topics_enabled:
            row = self.db.execute('SELECT ticket FROM support_sessions WHERE chat_id = ?', (self.manager,)).fetchone()
            number = row['ticket'] if row else None
        if text.startswith('/card'):
            product = self._find_product(text.removeprefix('/card').strip())
            if number is None or product is None:
                self.storage.enqueue(self.manager, 'Відкрийте звернення й надішліть /card АРТИКУЛ.',
                                     [('Звернення', 'shop:tickets')], thread_id=thread_id)
            else:
                self.send_card(number, product)
            return True
        if media and message_id is not None and number is not None:
            self.relay_media(number, message_id, media, text)
            return True
        if media:
            self.storage.enqueue(self.manager, 'Щоб надіслати файл клієнту, відкрийте звернення.',
                                 [('Звернення', 'shop:tickets')], thread_id=thread_id)
            return True
        if text and not text.startswith('/') and number is not None:
            self.reply(number, text)
            return True
        if reply_to is not None or (thread_id is not None and self.storage.topic_ticket(self.manager, thread_id) is not None):
            self.storage.enqueue(self.manager, 'Не вдалося визначити звернення. Відкрийте його зі списку.',
                                 [('Звернення', 'shop:tickets')], thread_id=thread_id)
            return True
        if text == '/manager':
            self.inbox()
            return True
        return False

    def _rate(self, row, score):
        if not 1 <= score <= 5 or row['active']:
            self.storage.enqueue(row['chat_id'], 'Оцінку можна поставити після завершення звернення.')
            return
        if row['rating']:
            self.storage.enqueue(row['chat_id'], 'Оцінку вже збережено. Дякуємо!', self.dialogs._menu(row['chat_id']))
            return
        self.db.execute('UPDATE tickets SET rating = ? WHERE id = ?', (score, row['id']))
        self.storage.enqueue(row['chat_id'], 'Дякуємо за оцінку!', self.dialogs._menu(row['chat_id']))
        self.storage.enqueue(self.manager, f"Клієнт оцінив звернення #{row['id']}: {'★' * score}{'☆' * (5 - score)}",
                             [('Відкрити діалог', f"ticket:open:{row['id']}")])

    def customer_input(self, chat, text, data, thread_id=None):
        state = self.storage.dialog(chat)
        if data and data.startswith('ticket:'):
            parts = data.split(':')
            if (len(parts) in (3, 4) and parts[1] in ('open', 'history')
                    and all(p.isdigit() and len(p) <= 9 for p in parts[2:])):
                self.show(int(parts[2]), chat, int(parts[3]) if len(parts) == 4 else 0)
            else:
                self.storage.enqueue(chat, 'Звернення недоступне.')
            return True
        if (data and data.startswith('client:')) or text in ('/support', '/cancel'):
            number = state['ticket']
            if data:
                parts = data.split(':')
                if (len(parts) not in (3, 4) or (len(parts) == 4) != (parts[1] == 'rate')
                        or not all(p.isdigit() and len(p) <= 9 for p in parts[2:])):
                    self.storage.enqueue(chat, 'Ця кнопка застаріла. Відкрийте головне меню.', self.dialogs._menu(chat))
                    return True
                number = int(parts[2])
                row = self.ticket(number)
                if not row or row['chat_id'] != chat:
                    self.storage.enqueue(chat, 'Звернення недоступне.')
                    return True
                action = parts[1]
            else:
                action = 'close' if text == '/cancel' else 'status'
            if not number:
                self.storage.enqueue(chat, 'Немає активного звернення.', self.dialogs._menu(chat))
            elif action == 'rate':
                self._rate(row, int(parts[3]))
            elif action == 'close':
                self.close(number, chat)
            elif action == 'confirm':
                self.storage.enqueue(chat, 'Завершити розмову з менеджером?',
                                     [('Так, завершити', f'client:close:{number}'),
                                      ('Продовжити', f'client:status:{number}')], ticket=number)
            else:
                self.show(number, chat)
            return True
        if thread_id is not None:
            if data and data.startswith('shop:'):
                return False
            number = self.storage.topic_ticket(chat, thread_id)
            if number is not None and number != state['ticket']:
                self.storage.enqueue(chat, 'Це звернення завершене або недоступне. Відкрийте головне меню.',
                                     [('Головне меню', 'shop:home')], thread_id=thread_id)
                return True
        return False
