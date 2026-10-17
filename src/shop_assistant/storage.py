import json
import sqlite3
import time
from dataclasses import dataclass
from contextlib import contextmanager


@dataclass(frozen=True)
class Outgoing:
    id: int
    chat_id: int
    text: str
    buttons: tuple[tuple[str, str], ...]
    attempts: int
    photo: str | None = None
    ticket: int | None = None
    thread_id: int | None = None


class Storage:
    """Persistent dialog state and an at-least-once delivery queue.

    enqueue participates in its caller's transaction. Delivery acknowledgements
    commit independently; never call them inside an incoming-update transaction.
    """

    @contextmanager
    def incoming_scope(self, chat_id: int, thread_id: int | None):
        with self.connection:
            before = self.connection.execute('SELECT coalesce(max(id), 0) FROM outbox').fetchone()[0]
            yield
            if thread_id is not None:
                self.connection.execute(
                    'UPDATE outbox SET thread_id = ? WHERE id > ? AND chat_id = ? AND ticket IS NULL AND thread_id IS NULL',
                    (thread_id, before, chat_id))

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        with connection:
            connection.execute("BEGIN")
            connection.execute("CREATE TABLE IF NOT EXISTS dialog_schema (version INTEGER NOT NULL)")
            row = connection.execute("SELECT version FROM dialog_schema").fetchone()
            if row and row[0] not in (1, 2, 3):
                raise ValueError("Непідтримувана версія схеми діалогів.")
            if row:
                if row[0] == 1:
                    connection.execute("ALTER TABLE outbox ADD COLUMN photo TEXT")
                if row[0] < 3:
                    self._support_schema()
                    connection.execute("UPDATE dialog_schema SET version = 3")
                return
            for sql in (
                "CREATE TABLE dialogs (chat_id INTEGER PRIMARY KEY, mode TEXT NOT NULL DEFAULT 'BOT', ticket INTEGER, context TEXT NOT NULL DEFAULT '')",
                "CREATE TABLE tickets (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1)",
                "CREATE TABLE incoming (update_id INTEGER PRIMARY KEY, received_at REAL NOT NULL)",
                "CREATE TABLE request_times (chat_id INTEGER NOT NULL, received_at REAL NOT NULL)",
                "CREATE INDEX request_times_chat ON request_times(chat_id, received_at)",
                "CREATE TABLE choices (token TEXT PRIMARY KEY, chat_id INTEGER NOT NULL, sku TEXT NOT NULL)",
                "CREATE TABLE outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, text TEXT NOT NULL, buttons TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0, delivered INTEGER NOT NULL DEFAULT 0, photo TEXT)",
            ):
                connection.execute(sql)
            self._support_schema()
            connection.execute("INSERT INTO dialog_schema VALUES (3)")

    def _support_schema(self) -> None:
        for sql in (
            "ALTER TABLE outbox ADD COLUMN ticket INTEGER",
            "ALTER TABLE outbox ADD COLUMN message_id INTEGER",
            "ALTER TABLE outbox ADD COLUMN thread_id INTEGER",
            "CREATE TABLE support_context (ticket INTEGER PRIMARY KEY, text TEXT NOT NULL)",
            "CREATE TABLE support_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, ticket INTEGER NOT NULL, role TEXT NOT NULL, text TEXT NOT NULL, created_at REAL NOT NULL, outbox_id INTEGER)",
            "CREATE INDEX support_messages_ticket ON support_messages(ticket, id)",
            "CREATE TABLE support_sessions (chat_id INTEGER PRIMARY KEY, ticket INTEGER NOT NULL)",
            "CREATE TABLE support_topics (chat_id INTEGER NOT NULL, ticket INTEGER NOT NULL, thread_id INTEGER NOT NULL, PRIMARY KEY(chat_id, ticket), UNIQUE(chat_id, thread_id))",
        ):
            self.connection.execute(sql)
        if self.connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'dialogs'").fetchone():
            self.connection.execute('INSERT OR IGNORE INTO support_context SELECT ticket, context FROM dialogs WHERE ticket IS NOT NULL')

    def enqueue(self, chat_id: int, text: str, buttons=(), *, photo: str | None = None,
                ticket: int | None = None, thread_id: int | None = None) -> int:
        if photo is not None and len(text.encode('utf-16-le')) // 2 > 1024:
            raise ValueError("Підпис фото перевищує 1024 символи Telegram.")
        # Telegram measures text in UTF-16 code units. Splitting also protects
        # manager forwards when a configured input limit is larger than Telegram's.
        chunks = []
        chunk, units = "", 0
        for char in text:
            size = 2 if ord(char) > 0xFFFF else 1
            if units + size > 4000:
                chunks.append(chunk)
                chunk, units = "", 0
            chunk += char
            units += size
        chunks.append(chunk)
        for index, part in enumerate(chunks):
            cursor = self.connection.execute(
                "INSERT INTO outbox (chat_id, text, buttons, photo, ticket, thread_id) VALUES (?, ?, ?, ?, ?, ?)",
                (chat_id, part, json.dumps(buttons if index == len(chunks) - 1 else ()), photo, ticket, thread_id),
            )
        return cursor.lastrowid

    def pending(self, limit: int = 100, now: float | None = None) -> list[Outgoing]:
        rows = self.connection.execute(
            "SELECT o.* FROM outbox o WHERE o.delivered = 0 AND o.next_attempt <= ? "
            "AND NOT EXISTS (SELECT 1 FROM outbox earlier WHERE earlier.chat_id = o.chat_id "
            "AND earlier.delivered = 0 AND earlier.id < o.id) ORDER BY o.id LIMIT ?",
            (time.time() if now is None else now, limit),
        )
        return [Outgoing(r["id"], r["chat_id"], r["text"],
                         tuple(tuple(b) for b in json.loads(r["buttons"])), r["attempts"], r["photo"],
                         r['ticket'], r['thread_id']) for r in rows]

    def sent(self, message_id: int, telegram_message_id: int | None = None) -> None:
        with self.connection:
            self.connection.execute("UPDATE outbox SET delivered = 1, message_id = ? WHERE id = ?",
                                    (telegram_message_id, message_id))

    def topic_ticket(self, chat_id: int, thread_id: int):
        row = self.connection.execute(
            'SELECT ticket FROM support_topics WHERE chat_id = ? AND thread_id = ?',
            (chat_id, thread_id)).fetchone()
        return row['ticket'] if row else None

    def failed(self, message_id: int, retry_after: float = 5, now: float | None = None) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE outbox SET attempts = attempts + 1, next_attempt = ? WHERE id = ? AND delivered = 0",
                ((time.time() if now is None else now) + max(0, retry_after), message_id),
            )

    def dialog(self, chat_id: int):
        return self.connection.execute("SELECT * FROM dialogs WHERE chat_id = ?", (chat_id,)).fetchone()
