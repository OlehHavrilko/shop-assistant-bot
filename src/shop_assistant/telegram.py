import asyncio
import logging
import signal
from contextlib import AsyncExitStack, suppress
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update


logger = logging.getLogger(__name__)


async def handle_update(dialogs, bot, update: Update) -> None:
    message = update.message
    callback = update.callback_query
    if callback is not None:
        message = callback.message
        if (not isinstance(message, Message) or message.chat.type != "private"
                or callback.from_user.id != message.chat.id):
            return
        await dialogs.handle(update.update_id, message.chat.id, callback_data=callback.data)
        # Acknowledgement is cosmetic; its failure must not roll back the choice.
        with suppress(TelegramAPIError):
            await bot.answer_callback_query(callback.id)
    elif message is not None and message.chat.type == "private":
        if message.from_user is None or message.from_user.id != message.chat.id:
            return
        await dialogs.handle(update.update_id, message.chat.id, text=message.text, private=True)


async def deliver_pending(storage, bot, *, limit: int = 100, stop: asyncio.Event | None = None) -> int:
    delivered = 0
    for outgoing in storage.pending(limit=limit):
        if stop is not None and stop.is_set():
            break
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=data)]
            for label, data in outgoing.buttons
        ]) if outgoing.buttons else None
        try:
            photo = getattr(outgoing, 'photo', None)
            if photo and Path(photo).is_file():
                try:
                    await bot.send_photo(chat_id=outgoing.chat_id, photo=FSInputFile(photo),
                                         caption=outgoing.text, reply_markup=markup,
                                         parse_mode=None, request_timeout=15)
                except TelegramBadRequest:
                    await bot.send_message(chat_id=outgoing.chat_id, text=outgoing.text,
                                           reply_markup=markup, parse_mode=None, request_timeout=10)
            else:
                await bot.send_message(chat_id=outgoing.chat_id, text=outgoing.text,
                                       reply_markup=markup, parse_mode=None, request_timeout=10)
        except TelegramRetryAfter as error:
            storage.failed(outgoing.id, retry_after=max(error.retry_after, 1))
        except (TelegramAPIError, OSError, asyncio.TimeoutError) as error:
            # Exception strings may contain URLs, tokens or message contents.
            logger.warning("Outgoing delivery delayed: %s", type(error).__name__)
            storage.failed(outgoing.id, retry_after=min(3600, 5 * 2 ** min(outgoing.attempts, 9)))
        else:
            storage.sent(outgoing.id)
            delivered += 1
    return delivered


async def _pause(stop: asyncio.Event, seconds: float) -> None:
    with suppress(asyncio.TimeoutError):
        await asyncio.wait_for(stop.wait(), seconds)


async def _outbox_worker(storage, bot, stop: asyncio.Event) -> None:
    while not stop.is_set():
        await deliver_pending(storage, bot, stop=stop)
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
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous = signal.getsignal(signum)
            signal.signal(signum, lambda *_: loop.call_soon_threadsafe(stop.set))
            resources.callback(signal.signal, signum, previous)
        worker = asyncio.create_task(_outbox_worker(dialogs.storage, bot, stop))
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
