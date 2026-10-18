"""Manager-only tools: statistics, catalog import from Telegram, reminders, search demand."""

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .catalog import normalize
from .importer import ImportError, parse_catalog

IMPORT_LIMIT_BYTES = 5 * 1024 * 1024
IMPORT_SUFFIXES = ('.xlsx', '.csv')
_AWAITING = ('active = 1 AND last_customer_at IS NOT NULL '
             'AND (last_manager_at IS NULL OR last_manager_at < last_customer_at)')


class ManagerTools:
    def __init__(self, dialogs):
        self.dialogs = dialogs
        self.storage = dialogs.storage
        self.db = dialogs.catalog.connection
        self.manager = dialogs.manager_id

    # Search demand -----------------------------------------------------

    def record_miss(self, text):
        query = normalize(text)[:80]
        if query:
            self.db.execute('INSERT INTO search_misses VALUES (?, 1, ?) '
                            'ON CONFLICT(query) DO UPDATE SET count = count + 1, last_at = excluded.last_at',
                            (query, self.dialogs.clock()))

    # Statistics --------------------------------------------------------

    def stats(self):
        now = self.dialogs.clock()
        day, week = now - 86400, now - 7 * 86400
        one = lambda sql, *args: self.db.execute(sql, args).fetchone()[0]  # noqa: E731
        created_day = one('SELECT count(*) FROM tickets WHERE created_at >= ?', day)
        created_week = one('SELECT count(*) FROM tickets WHERE created_at >= ?', week)
        active = one('SELECT count(*) FROM tickets WHERE active = 1')
        waiting = one(f'SELECT count(*) FROM tickets WHERE {_AWAITING}')
        response = one('SELECT avg(first_reply_at - created_at) FROM tickets '
                       'WHERE created_at >= ? AND first_reply_at IS NOT NULL', week)
        rating, rated = self.db.execute('SELECT avg(rating), count(rating) FROM tickets '
                                        'WHERE closed_at >= ? AND rating IS NOT NULL', (week,)).fetchone()
        leads_day = one('SELECT count(*) FROM leads WHERE created_at >= ?', day)
        leads_week = one('SELECT count(*) FROM leads WHERE created_at >= ?', week)
        misses = self.db.execute('SELECT query, count FROM search_misses WHERE last_at >= ? '
                                 'ORDER BY count DESC, last_at DESC LIMIT 5', (now - 30 * 86400,)).fetchall()
        lines = ['📊 Статистика',
                 '',
                 f'Звернення: {created_day} за добу · {created_week} за 7 днів',
                 f'Відкриті зараз: {active} · чекають відповіді: {waiting}',
                 f'Перша відповідь (7 днів): {_duration(response) if response is not None else "немає даних"}',
                 f'Оцінка (7 днів): {f"{rating:.1f} ★ з {rated}" if rated else "немає оцінок"}',
                 f'Заявки: {leads_day} за добу · {leads_week} за 7 днів']
        if misses:
            lines += ['', 'Що шукали й не знайшли (30 днів):']
            lines += [f'• {row["query"]} — {row["count"]}' for row in misses]
        self.storage.enqueue(self.manager, '\n'.join(lines),
                             [('Оновити статистику', 'shop:stats'), ('До списку звернень', 'shop:tickets')])

    # Reminders ---------------------------------------------------------

    def remind(self, minutes):
        now = self.dialogs.clock()
        if minutes <= 0 or not self.dialogs.storefront.is_open(now):
            return 0
        rows = self.db.execute(
            f'SELECT * FROM tickets WHERE {_AWAITING} AND last_customer_at <= ? '
            'AND (reminded_at IS NULL OR reminded_at < last_customer_at)', (now - minutes * 60,)).fetchall()
        for row in rows:
            waited = _duration(now - row['last_customer_at'])
            self.storage.enqueue(self.manager,
                                 f"⏰ Звернення #{row['id']} чекає відповіді {waited}\n"
                                 f"{self.dialogs.support.customer_label(row['chat_id'])}",
                                 [('Відкрити діалог', f"ticket:open:{row['id']}")], ticket=row['id'])
            self.db.execute('UPDATE tickets SET reminded_at = ? WHERE id = ?', (now, row['id']))
        return len(rows)

    # Catalog import ----------------------------------------------------

    def _form(self):
        row = self.db.execute("SELECT * FROM forms WHERE chat_id = ? AND kind = 'import'", (self.manager,)).fetchone()
        return (row['step'], json.loads(row['data'])) if row else None

    def keeps(self, path):
        form = self._form()
        return form is not None and form[1].get('path') == str(path)

    def waiting_for_file(self):
        form = self._form()
        return form is not None and form[0] == 'file'

    def start_import(self):
        self._discard()
        self.db.execute("INSERT OR REPLACE INTO forms VALUES (?, 'import', 'file', '{}', ?)",
                        (self.manager, self.dialogs.clock()))
        self.storage.enqueue(self.manager,
                             '📦 Оновлення каталогу\n\nНадішліть файл .xlsx або .csv до 5 МБ. Обов’язкові колонки: '
                             'sku, name, shade, price_uah_per_m, stock_m, updated_at.\n'
                             'Спершу покажу, що зміниться; каталог замінюється лише після підтвердження.',
                             [('Скасувати імпорт', 'import:cancel')])

    def _discard(self):
        form = self._form()
        if form and form[1].get('path'):
            Path(form[1]['path']).unlink(missing_ok=True)
        self.db.execute("DELETE FROM forms WHERE chat_id = ? AND kind = 'import'", (self.manager,))

    def upload(self, path, media):
        """Handle a manager file. Returns False when no import is awaited."""
        if not self.waiting_for_file():
            if path is not None:
                Path(path).unlink(missing_ok=True)
            return False
        if path is None:
            self.storage.enqueue(self.manager, 'Потрібен файл .xlsx або .csv до 5 МБ.' if media
                                 else 'Очікую файл каталогу.', [('Скасувати імпорт', 'import:cancel')])
            return True
        try:
            rows = parse_catalog(path)
        except ImportError as error:
            Path(path).unlink(missing_ok=True)
            shown = error.errors[:10]
            more = f'\n…і ще {len(error.errors) - 10}' if len(error.errors) > 10 else ''
            self.storage.enqueue(self.manager, 'Файл не прийнято, каталог не змінено:\n' + '\n'.join(shown) + more
                                 + '\n\nВиправте файл і надішліть ще раз.', [('Скасувати імпорт', 'import:cancel')])
            return True
        except (ValueError, OSError, KeyError):
            Path(path).unlink(missing_ok=True)
            self.storage.enqueue(self.manager, 'Не вдалося прочитати файл. Перевірте формат і надішліть ще раз.',
                                 [('Скасувати імпорт', 'import:cancel')])
            return True
        self.db.execute("UPDATE forms SET step = 'confirm', data = ? WHERE chat_id = ? AND kind = 'import'",
                        (json.dumps({'path': str(path)}), self.manager))
        self.storage.enqueue(self.manager, self._preview(rows),
                             [('Застосувати', 'import:apply'), ('Скасувати імпорт', 'import:cancel')])
        return True

    def _preview(self, rows):
        current = {row['sku']: row for row in self.db.execute('SELECT * FROM products')}
        new = {row['sku']: row for row in rows}
        added = [sku for sku in new if sku not in current]
        removed = [sku for sku in current if sku not in new]
        prices = [sku for sku in new if sku in current and new[sku]['price_uah_per_m'] != current[sku]['price_uah_per_m']]
        stock = [sku for sku in new if sku in current and new[sku]['stock_m'] != current[sku]['stock_m']]
        sample = lambda skus: (': ' + ', '.join(skus[:5]) + ('…' if len(skus) > 5 else '')) if skus else ''  # noqa: E731
        return ('Перевірте зміни каталогу:\n\n'
                f'Товарів у файлі: {len(new)} (зараз {len(current)})\n'
                f'Нові: {len(added)}{sample(added)}\n'
                f'Зникнуть: {len(removed)}{sample(removed)}\n'
                f'Змінено ціну: {len(prices)}{sample(prices)}\n'
                f'Змінено залишок: {len(stock)}{sample(stock)}\n\n'
                'Перед заміною буде створено резервну копію бази.')

    def callback(self, data):
        if data == 'import:cancel':
            self._discard()
            self.storage.enqueue(self.manager, 'Імпорт скасовано. Каталог не змінено.',
                                 [('До списку звернень', 'shop:tickets')])
            return
        form = self._form()
        if data != 'import:apply' or not form or form[0] != 'confirm':
            self.storage.enqueue(self.manager, 'Немає файлу для застосування. Почніть імпорт ще раз.',
                                 [('Оновити каталог', 'shop:import')])
            return
        path = Path(form[1]['path'])
        try:
            rows = parse_catalog(path)
        except (ValueError, OSError, KeyError):
            self._discard()
            self.storage.enqueue(self.manager, 'Файл більше недоступний. Надішліть його ще раз.',
                                 [('Оновити каталог', 'shop:import')])
            return
        backup = self._backup()
        report = self.dialogs.catalog.replace_in_transaction(rows)
        self._discard()
        note = f'\nРезервна копія: {backup}' if backup else ''
        self.storage.enqueue(self.manager, f'✅ Каталог оновлено: {report.count} товарів.{note}',
                             [('До списку звернень', 'shop:tickets')])

    def _backup(self):
        database = self.dialogs.database_file
        if database is None:
            return None
        folder = database.parent / 'backups'
        folder.mkdir(exist_ok=True)
        target = folder / f"before-import-{datetime.now(UTC):%Y%m%dT%H%M%S%fZ}.sqlite3"
        # A second connection copies the last committed state: the catalog before this import.
        source, destination = sqlite3.connect(database), sqlite3.connect(target)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        target.chmod(0o600)
        return f'backups/{target.name}'


def _duration(seconds):
    minutes = max(0, round(seconds / 60))
    if minutes < 60:
        return f'{minutes} хв'
    hours, minutes = divmod(minutes, 60)
    return f'{hours} год {minutes} хв' if hours < 48 else f'{hours // 24} дн'
