import asyncio
import logging
import signal
from contextlib import AsyncExitStack, suppress
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update


logger = logging.getLogger(__name__)


def _keyboard(buttons):
    labels = {
        'Каталог тканин': '🧵 Каталог тканин',
        'Підібрати тканину': '🔎 Знайти тканину',
        'Доставка й оплата': '🚚 Доставка й оплата',
        'Про магазин': 'ℹ️ Про магазин',
        'Оплата': '💳 Оплата',
        'Написати менеджеру': '💬 Написати менеджеру',
        'Запитати менеджера': '💬 Запитати менеджера',
        'Кабінет менеджера': '🗂 Кабінет менеджера',
        'Головне меню': '🏠 Головне меню',
        'Переглянути магазин': '🧵 До магазину',
        'Усі категорії': '🧵 Усі категорії',
        'Ще тканини': 'Ще тканини →',
        'Попередні тканини': '← Попередні тканини',
        'Відкрити діалог': '💬 Відкрити діалог',
        'Моє звернення': '💬 Моє звернення',
        'Завершити розмову': '✓ Завершити розмову',
        'Завершити звернення': '✓ Завершити звернення',
        'Архів': '🗃 Архів',
        'Активні звернення': '📥 Активні звернення',
        'Оновити список': '🔄 Оновити список',
        'До списку звернень': '📥 До списку звернень',
        'Усі звернення': '📥 Усі звернення',
        'Назад': '← Назад',
        'Далі': 'Далі →',
    }
    rows = []
    for label, data in buttons:
        button = InlineKeyboardButton(text=labels.get(label, label), callback_data=data)
        previous = rows[-1][0].callback_data if rows and len(rows[-1]) == 1 else None
        pair = (previous is not None and
                ((data.startswith('shop:category:') and previous.startswith('shop:category:'))
                 or (previous == 'shop:info:delivery' and data == 'shop:info:about')))
        if pair:
            rows[-1].append(button)
        else:
            rows.append([button])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def handle_update(dialogs, bot, update: Update) -> None:
    message = update.message
    callback = update.callback_query
    if callback is not None:
        message = callback.message
        if (not isinstance(message, Message) or message.chat.type != "private"
                or callback.from_user.id != message.chat.id):
            return
        routing = {'thread_id': message.message_thread_id} if message.message_thread_id is not None else {}
        await dialogs.handle(update.update_id, message.chat.id, callback_data=callback.data, **routing)
        # Acknowledgement is cosmetic; its failure must not roll back the choice.
        with suppress(TelegramAPIError):
            await bot.answer_callback_query(callback.id)
    elif message is not None and message.chat.type == "private":
        if message.from_user is None or message.from_user.id != message.chat.id:
            return
        if message.forum_topic_created is not None:
            return
        routing = {'thread_id': message.message_thread_id} if message.message_thread_id is not None else {}
        if message.reply_to_message and message.reply_to_message.forum_topic_created is None:
            routing['reply_to_message_id'] = message.reply_to_message.message_id
        await dialogs.handle(update.update_id, message.chat.id, text=message.text, private=True, **routing)


async def deliver_pending(storage, bot, *, limit: int = 100, stop: asyncio.Event | None = None,
                          topics_enabled: bool = False) -> int:
    delivered = 0
    for outgoing in storage.pending(limit=limit):
        if stop is not None and stop.is_set():
            break
        markup = _keyboard(outgoing.buttons)
        try:
            thread_id = outgoing.thread_id
            if topics_enabled and outgoing.ticket is not None and thread_id is None:
                row = storage.connection.execute(
                    'SELECT thread_id FROM support_topics WHERE chat_id = ? AND ticket = ?',
                    (outgoing.chat_id, outgoing.ticket)).fetchone()
                if row:
                    thread_id = row['thread_id']
                else:
                    topic = await bot.create_forum_topic(chat_id=outgoing.chat_id,
                                                        name=f'Звернення #{outgoing.ticket}', request_timeout=10)
                    thread_id = topic.message_thread_id
                    with storage.connection:
                        storage.connection.execute('INSERT INTO support_topics VALUES (?, ?, ?)',
                                                   (outgoing.chat_id, outgoing.ticket, thread_id))
            routing = {'message_thread_id': thread_id} if thread_id is not None else {}
            photo = getattr(outgoing, 'photo', None)
            if photo and Path(photo).is_file():
                try:
                    result = await bot.send_photo(chat_id=outgoing.chat_id, photo=FSInputFile(photo),
                                         caption=outgoing.text, reply_markup=markup,
                                         parse_mode=None, request_timeout=15, **routing)
                except TelegramBadRequest:
                    result = await bot.send_message(chat_id=outgoing.chat_id, text=outgoing.text,
                                           reply_markup=markup, parse_mode=None, request_timeout=10, **routing)
            else:
                result = await bot.send_message(chat_id=outgoing.chat_id, text=outgoing.text,
                                       reply_markup=markup, parse_mode=None, request_timeout=10, **routing)
        except TelegramRetryAfter as error:
            storage.failed(outgoing.id, retry_after=max(error.retry_after, 1))
        except TelegramBadRequest as error:
            # A deleted topic must not strand every future message for this chat.
            if (topics_enabled and outgoing.ticket is not None and outgoing.thread_id is None
                    and 'message thread not found' in error.message.lower()):
                with storage.connection:
                    storage.connection.execute('DELETE FROM support_topics WHERE chat_id = ? AND ticket = ?',
                                               (outgoing.chat_id, outgoing.ticket))
            logger.warning('Outgoing delivery delayed: TelegramBadRequest')
            storage.failed(outgoing.id, retry_after=min(3600, 5 * 2 ** min(outgoing.attempts, 9)))
        except (TelegramAPIError, OSError, asyncio.TimeoutError) as error:
            # Exception strings may contain URLs, tokens or message contents.
            logger.warning("Outgoing delivery delayed: %s", type(error).__name__)
            storage.failed(outgoing.id, retry_after=min(3600, 5 * 2 ** min(outgoing.attempts, 9)))
        else:
            telegram_id = getattr(result, 'message_id', None)
            storage.sent(outgoing.id, telegram_id if isinstance(telegram_id, int) else None)
            delivered += 1
    return delivered


async def _pause(stop: asyncio.Event, seconds: float) -> None:
    with suppress(asyncio.TimeoutError):
        await asyncio.wait_for(stop.wait(), seconds)


async def _outbox_worker(storage, bot, stop: asyncio.Event, *, topics_enabled: bool = False) -> None:
    while not stop.is_set():
        await deliver_pending(storage, bot, stop=stop, topics_enabled=topics_enabled)
        await _pause(stop, 1)


async def poll_updates(dialogs, bot, stop: asyncio.Event) -> None:
    offset = None
    retry_delay = 1
    while not stop.is_set():
        try:
            updates = await bot.get_updates(offset=offset, timeout=20, request_timeout=25,
                                            allowed_updates=["message", "callback_query"])
        except TelegramRetryAfter as error:
            await _pause(stop, max(error.retry_after, 1))
            continue
        except (TelegramNetworkError, OSError, asyncio.TimeoutError) as error:
            logger.warning("Polling delayed: %s", type(error).__name__)
            await _pause(stop, retry_delay)
            retry_delay = min(retry_delay * 2, 30)
            continue
        retry_delay = 1
        for update in updates:
            if stop.is_set():
                return
            # Advance offset only after durable processing. Unexpected storage
            # errors stop polling, leaving this update available after restart.
            await handle_update(dialogs, bot, update)
            offset = update.update_id + 1


async def run_bot(config, catalog) -> None:
    from .dialogs import Dialogs
    from .storefront import Storefront

    async with AsyncExitStack() as resources:
        llm = None
        if config.llm_enabled:
            from .limits import Budget, Tariffs
            from .llm import LLM

            budget = Budget(config.database_path, daily_usd=config.daily_budget, tariffs=Tariffs(
                input_per_million=config.input_price,
                output_per_million=config.output_price,
                cache_write_per_million=config.cache_write_price,
                cache_read_per_million=config.cache_read_price,
            ))
            resources.callback(budget.close)
            llm = LLM(catalog, model=config.model, budget=budget, api_key=config.api_key,
                      max_input_tokens=config.max_input_tokens, max_output_tokens=config.max_output_tokens,
                      max_calls=config.max_calls, timeout=config.llm_timeout)
            resources.push_async_callback(llm.close)
        dialogs = Dialogs(catalog, manager_id=config.manager_id, llm=llm,
                          message_length=config.message_length, request_rate=config.request_rate,
                          rate_window=config.rate_window, context_limit=config.context_limit,
                          storefront=Storefront(catalog, config.store_data_path))
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        bot = Bot(config.token)
        resources.push_async_callback(bot.session.close)
        identity = await bot.get_me()
        dialogs.topics_enabled = bool(identity.has_topics_enabled)
        logger.info('Private support topics enabled: %s', dialogs.topics_enabled)
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous = signal.getsignal(signum)
            signal.signal(signum, lambda *_: loop.call_soon_threadsafe(stop.set))
            resources.callback(signal.signal, signum, previous)
        worker = asyncio.create_task(_outbox_worker(dialogs.storage, bot, stop,
                                                   topics_enabled=dialogs.topics_enabled))
        polling = asyncio.create_task(poll_updates(dialogs, bot, stop))
        try:
            # Worker failures must not leave a bot running without delivery.
            done, _ = await asyncio.wait((worker, polling), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            stop.set()
            # Finish the active operation, leaving the queued backlog for restart.
            results = []
            try:
                results = await asyncio.wait_for(
                    asyncio.gather(worker, polling, return_exceptions=True), timeout=80,
                )
            except asyncio.TimeoutError:
                logger.warning("Shutdown deadline reached; pending delivery remains in SQLite.")
            for result in results:
                if isinstance(result, BaseException):
                    raise result
