import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from aiohttp import web
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.constants import ChatMemberStatus
from telegram.error import BadRequest, Forbidden, TelegramError, RetryAfter, NetworkError, TimedOut
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "").strip()

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")
if not ADMIN_ID_RAW:
    raise RuntimeError("ADMIN_ID is missing")
try:
    ADMIN_ID = int(ADMIN_ID_RAW)
except ValueError as exc:
    raise RuntimeError("ADMIN_ID must be a numeric Telegram user ID") from exc

# Telegram does NOT support unlimited-speed sending.  The panel accepts
# very small values, but Telegram may return RetryAfter (429), which we handle.
DEFAULT_INTERVAL_SECONDS = 5.0
MIN_INTERVAL_SECONDS = 0.00001
MAX_INTERVAL_SECONDS = 86400.0
DEFAULT_START_NUMBER = 1

DATA_DIR = Path(os.getenv("DATA_DIR", "/var/data"))
STATE_FILE = DATA_DIR / "state.json"

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("counter-bot")

state_lock = asyncio.Lock()
state: Dict[str, Any] = {
    "channel": None,
    "next_number": DEFAULT_START_NUMBER,
    "running": False,
    "interval_seconds": DEFAULT_INTERVAL_SECONDS,
}

ADMIN_MODE = "admin_mode"
MODE_NONE = None
MODE_WAIT_CHANNEL = "wait_channel"
MODE_WAIT_START_NUMBER = "wait_start_number"
MODE_WAIT_INTERVAL = "wait_interval"

application_ref: Optional[Application] = None
counter_task: Optional[asyncio.Task] = None
watchdog_task: Optional[asyncio.Task] = None
polling_watchdog_task: Optional[asyncio.Task] = None


def ensure_data_dir() -> None:
    global DATA_DIR, STATE_FILE
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        DATA_DIR = Path("data")
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE = DATA_DIR / "state.json"


def load_state() -> None:
    global state
    ensure_data_dir()
    if not STATE_FILE.exists():
        return
    try:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            loaded = json.load(f)
        if not isinstance(loaded, dict):
            raise ValueError("state.json must contain an object")

        channel = loaded.get("channel")
        if channel is not None and (not isinstance(channel, dict) or "chat_id" not in channel):
            channel = None

        next_number = int(loaded.get("next_number", DEFAULT_START_NUMBER))
        if next_number < 0:
            next_number = DEFAULT_START_NUMBER

        try:
            interval = float(loaded.get("interval_seconds", DEFAULT_INTERVAL_SECONDS))
        except (TypeError, ValueError):
            interval = DEFAULT_INTERVAL_SECONDS
        interval = max(MIN_INTERVAL_SECONDS, min(MAX_INTERVAL_SECONDS, interval))

        state = {
            "channel": channel,
            "next_number": next_number,
            "running": bool(loaded.get("running", False)),
            "interval_seconds": interval,
        }
        logger.info("State loaded: %s", state)
    except Exception as exc:
        logger.error("Could not load state file: %s", exc)
        state = {
            "channel": None,
            "next_number": DEFAULT_START_NUMBER,
            "running": False,
            "interval_seconds": DEFAULT_INTERVAL_SECONDS,
        }


def save_state() -> None:
    ensure_data_dir()
    payload = json.dumps(state, ensure_ascii=False, indent=2)
    fd, temp_name = tempfile.mkstemp(prefix="state_", suffix=".tmp", dir=str(DATA_DIR))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_name, STATE_FILE)
    finally:
        try:
            if os.path.exists(temp_name):
                os.remove(temp_name)
        except OSError:
            pass


async def safe_state_save() -> None:
    async with state_lock:
        await asyncio.to_thread(save_state)


def is_admin(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id == ADMIN_ID)


def panel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["📢 تنظیم کانال", "✅ بررسی کانال"],
            ["▶️ شروع", "⏸ توقف"],
            ["🔢 عدد شروع", "⏱ فاصله ارسال"],
            ["📊 وضعیت", "🗑 حذف کانال"],
            ["❓ راهنما"],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def current_channel_text() -> str:
    channel = state.get("channel")
    if not channel:
        return "تنظیم نشده"
    title = channel.get("title") or "بدون عنوان"
    chat_id = channel.get("chat_id")
    username = channel.get("username")
    extra = f"@{username}" if username else str(chat_id)
    return f"{title} ({extra})"


def status_text() -> str:
    running = "🟢 در حال شمارش" if state.get("running") else "🔴 متوقف"
    interval = state.get("interval_seconds", DEFAULT_INTERVAL_SECONDS)
    return (
        "📊 وضعیت ربات\n\n"
        f"کانال: {current_channel_text()}\n"
        f"وضعیت: {running}\n"
        f"عدد بعدی: {state.get('next_number', DEFAULT_START_NUMBER)}\n"
        f"فاصله ارسال: {interval:g} ثانیه"
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    context.user_data[ADMIN_MODE] = MODE_NONE
    await update.message.reply_text(
        "👋 پنل مدیریت شمارنده آماده است.\n\n"
        "ابتدا کانال را تنظیم کن، سپس ربات را در همان کانال ادمین کن و بعد «▶️ شروع» را بزن.",
        reply_markup=panel_keyboard(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    await update.message.reply_text(
        "❓ راهنما\n\n"
        "1) ربات را در کانال Administrator کن و اجازه ارسال پیام بده.\n"
        "2) «📢 تنظیم کانال» را بزن.\n"
        "3) آیدی کانال مثل -1001234567890 یا یوزرنیم مثل @mychannel را بفرست.\n"
        "4) «✅ بررسی کانال» را بزن.\n"
        "5) با «⏱ فاصله ارسال» فاصله را بر حسب ثانیه تنظیم کن.\n"
        "6) «▶️ شروع» را بزن.\n\n"
        "نکته: اگر فاصله خیلی کوچک باشد Telegram ممکن است محدودیت 429 بدهد؛ کد خودش RetryAfter را مدیریت می‌کند.\n"
        "با «⏸ توقف» متوقف می‌شود و با شروع دوباره از همان عدد ادامه می‌دهد.",
        reply_markup=panel_keyboard(),
    )


async def set_channel_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    context.user_data[ADMIN_MODE] = MODE_WAIT_CHANNEL
    await update.message.reply_text(
        "📢 آیدی عددی کانال یا یوزرنیم کانال را بفرست.\n\n"
        "مثال آیدی عددی:\n-1001234567890\n\n"
        "مثال یوزرنیم:\n@mychannel\n\n"
        "برای لغو /cancel را بفرست.",
        reply_markup=ReplyKeyboardRemove(),
    )


async def set_start_number_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    context.user_data[ADMIN_MODE] = MODE_WAIT_START_NUMBER
    await update.message.reply_text(
        "🔢 عدد شروع را بفرست.\nمثلاً 1 یا 1000\n\nبرای لغو /cancel را بفرست.",
        reply_markup=ReplyKeyboardRemove(),
    )


async def set_interval_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    context.user_data[ADMIN_MODE] = MODE_WAIT_INTERVAL
    await update.message.reply_text(
        "⏱ فاصله ارسال را به ثانیه بفرست.\n\n"
        "مثال: 5 ، 1 ، 0.5 ، 0.1\n"
        "کمترین مقدار قابل ثبت: 0.00001 ثانیه\n\n"
        "⚠️ مقدار بسیار کم باعث محدودیت Telegram می‌شود؛ کد در صورت 429 خودش صبر می‌کند.\n\n"
        "برای لغو /cancel را بفرست.",
        reply_markup=ReplyKeyboardRemove(),
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    context.user_data[ADMIN_MODE] = MODE_NONE
    await update.message.reply_text("❌ لغو شد.", reply_markup=panel_keyboard())


async def validate_bot_admin(chat_id: Any, context: ContextTypes.DEFAULT_TYPE) -> tuple[bool, str, Any]:
    try:
        chat = await context.bot.get_chat(chat_id)
    except (BadRequest, Forbidden) as exc:
        return False, f"❌ کانال پیدا نشد یا ربات دسترسی ندارد.\n{exc}", None
    except TelegramError as exc:
        return False, f"❌ خطا هنگام دسترسی به کانال:\n{exc}", None

    try:
        me = await context.bot.get_me()
        member = await context.bot.get_chat_member(chat.id, me.id)
    except TelegramError as exc:
        return False, f"❌ نتوانستم وضعیت ادمین ربات را بررسی کنم:\n{exc}", chat

    if member.status != ChatMemberStatus.ADMINISTRATOR:
        return False, "❌ ربات ادمین این کانال نیست.", chat

    can_post = getattr(member, "can_post_messages", True)
    if can_post is False:
        return False, "❌ ربات اجازه ارسال پیام در کانال را ندارد.", chat

    return True, "✅ دسترسی ارسال پیام تأیید شد.", chat


async def process_channel_input(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    value = text.strip()
    if not value:
        await update.message.reply_text("❌ مقدار خالی است.")
        return

    chat_id: Any = value
    if value.lstrip("-").isdigit():
        chat_id = int(value)
    elif not value.startswith("@"):
        chat_id = "@" + value.lstrip("@")

    ok, message, chat = await validate_bot_admin(chat_id, context)
    if not ok:
        await update.message.reply_text(message)
        return

    state["channel"] = {
        "chat_id": chat.id,
        "title": getattr(chat, "title", None) or getattr(chat, "full_name", None) or "",
        "username": getattr(chat, "username", None),
    }
    await safe_state_save()
    context.user_data[ADMIN_MODE] = MODE_NONE
    await update.message.reply_text(
        "✅ کانال ثبت شد.\n\n"
        f"نام: {state['channel']['title'] or 'بدون عنوان'}\n"
        f"آیدی: {state['channel']['chat_id']}\n\n"
        "حالا می‌توانی «▶️ شروع» را بزنی.",
        reply_markup=panel_keyboard(),
    )


async def process_start_number(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    try:
        number = int(text.strip())
    except ValueError:
        await update.message.reply_text("❌ فقط عدد صحیح بفرست.")
        return
    if number < 0 or number > 10**300:
        await update.message.reply_text("❌ عدد نامعتبر است.")
        return
    state["next_number"] = number
    await safe_state_save()
    context.user_data[ADMIN_MODE] = MODE_NONE
    await update.message.reply_text(f"✅ عدد بعدی روی {number} تنظیم شد.", reply_markup=panel_keyboard())


async def process_interval(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    try:
        seconds = float(text.strip())
    except ValueError:
        await update.message.reply_text("❌ فقط عدد بفرست. مثال: 5 یا 0.5 یا 0.00001")
        return
    if not (MIN_INTERVAL_SECONDS <= seconds <= MAX_INTERVAL_SECONDS):
        await update.message.reply_text(
            f"❌ فاصله باید بین {MIN_INTERVAL_SECONDS} تا {MAX_INTERVAL_SECONDS} ثانیه باشد."
        )
        return
    state["interval_seconds"] = seconds
    await safe_state_save()
    context.user_data[ADMIN_MODE] = MODE_NONE
    await update.message.reply_text(
        f"✅ فاصله ارسال روی {seconds:g} ثانیه تنظیم شد.",
        reply_markup=panel_keyboard(),
    )


async def check_channel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    channel = state.get("channel")
    if not channel:
        await update.message.reply_text("❌ هنوز کانالی تنظیم نشده است.")
        return
    ok, message, _ = await validate_bot_admin(channel["chat_id"], context)
    if ok:
        await update.message.reply_text(
            f"✅ کانال سالم است و ربات اجازه ارسال دارد.\n\n{current_channel_text()}",
            reply_markup=panel_keyboard(),
        )
    else:
        await update.message.reply_text(message, reply_markup=panel_keyboard())


async def start_counter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    channel = state.get("channel")
    if not channel:
        await update.message.reply_text("❌ ابتدا «📢 تنظیم کانال» را انجام بده.")
        return
    ok, message, _ = await validate_bot_admin(channel["chat_id"], context)
    if not ok:
        await update.message.reply_text(message, reply_markup=panel_keyboard())
        return

    state["running"] = True
    await safe_state_save()
    await ensure_counter_task(context.application)
    interval = state.get("interval_seconds", DEFAULT_INTERVAL_SECONDS)
    await update.message.reply_text(
        "🟢 شمارش شروع شد.\n"
        f"عدد بعدی: {state['next_number']}\n"
        f"فاصله: {interval:g} ثانیه",
        reply_markup=panel_keyboard(),
    )


async def stop_counter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    state["running"] = False
    await safe_state_save()
    await cancel_counter_task()
    await update.message.reply_text(
        f"⏸ شمارش متوقف شد.\nعدد بعدی: {state['next_number']}",
        reply_markup=panel_keyboard(),
    )


async def remove_channel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    state["running"] = False
    state["channel"] = None
    await safe_state_save()
    await cancel_counter_task()
    await update.message.reply_text("🗑 کانال حذف شد. عدد فعلی حفظ شده است.", reply_markup=panel_keyboard())


async def show_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    await update.message.reply_text(status_text(), reply_markup=panel_keyboard())


async def panel_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update) or not update.message:
        return
    text = (update.message.text or "").strip()
    mode = context.user_data.get(ADMIN_MODE, MODE_NONE)

    if mode == MODE_WAIT_CHANNEL:
        await process_channel_input(update, context, text)
        return
    if mode == MODE_WAIT_START_NUMBER:
        await process_start_number(update, context, text)
        return
    if mode == MODE_WAIT_INTERVAL:
        await process_interval(update, context, text)
        return

    actions = {
        "📢 تنظیم کانال": set_channel_request,
        "✅ بررسی کانال": check_channel,
        "▶️ شروع": start_counter,
        "⏸ توقف": stop_counter,
        "🔢 عدد شروع": set_start_number_request,
        "⏱ فاصله ارسال": set_interval_request,
        "📊 وضعیت": show_status,
        "🗑 حذف کانال": remove_channel,
        "❓ راهنما": help_command,
    }
    handler = actions.get(text)
    if handler:
        await handler(update, context)
    else:
        await update.message.reply_text("از دکمه‌های پنل استفاده کن یا /start را بزن.", reply_markup=panel_keyboard())


async def cancel_counter_task() -> None:
    global counter_task
    task = counter_task
    counter_task = None
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.exception("Counter task stop error: %s", exc)


async def counter_loop(app: Application) -> None:
    global counter_task
    logger.info("Counter loop started")
    try:
        while True:
            if not state.get("running"):
                await asyncio.sleep(1)
                continue

            channel = state.get("channel")
            if not channel:
                state["running"] = False
                await safe_state_save()
                continue

            number = state.get("next_number", DEFAULT_START_NUMBER)
            chat_id = channel["chat_id"]
            interval = float(state.get("interval_seconds", DEFAULT_INTERVAL_SECONDS))
            interval = max(MIN_INTERVAL_SECONDS, min(MAX_INTERVAL_SECONDS, interval))

            try:
                await app.bot.send_message(chat_id=chat_id, text=str(number))
                state["next_number"] = number + 1
                # Do not kill the event loop with synchronous fsync.
                await safe_state_save()
                await asyncio.sleep(interval)

            except RetryAfter as exc:
                delay = max(1.0, float(getattr(exc, "retry_after", 1)))
                logger.warning("Telegram rate limit (429). Waiting %.2f seconds.", delay)
                await asyncio.sleep(delay)

            except (TimedOut, NetworkError) as exc:
                logger.warning("Temporary Telegram network error: %s", exc)
                await asyncio.sleep(5)

            except Forbidden as exc:
                logger.error("Bot has no access/post permission: %s", exc)
                state["running"] = False
                await safe_state_save()
                try:
                    await app.bot.send_message(
                        chat_id=ADMIN_ID,
                        text=f"⛔ شمارش متوقف شد؛ ربات به کانال دسترسی/اجازه ارسال ندارد.\n\nخطا: {exc}",
                    )
                except TelegramError:
                    pass
                await asyncio.sleep(2)

            except BadRequest as exc:
                logger.error("Telegram BadRequest: %s", exc)
                state["running"] = False
                await safe_state_save()
                try:
                    await app.bot.send_message(
                        chat_id=ADMIN_ID,
                        text=f"⛔ شمارش متوقف شد؛ Telegram درخواست ارسال را رد کرد.\n\nخطا: {exc}",
                    )
                except TelegramError:
                    pass
                await asyncio.sleep(2)

            except TelegramError as exc:
                logger.exception("Telegram error: %s", exc)
                await asyncio.sleep(10)

            except asyncio.CancelledError:
                raise

            except Exception as exc:
                logger.exception("Unexpected counter error: %s", exc)
                await asyncio.sleep(10)

    except asyncio.CancelledError:
        logger.info("Counter loop cancelled")
        raise
    finally:
        counter_task = None


async def ensure_counter_task(app: Application) -> None:
    global counter_task
    if counter_task is None or counter_task.done():
        counter_task = app.create_task(counter_loop(app), name="counter-loop")


async def counter_watchdog(app: Application) -> None:
    while True:
        try:
            await asyncio.sleep(5)
            if state.get("running") and state.get("channel"):
                if counter_task is None or counter_task.done():
                    logger.warning("Counter task died. Restarting it.")
                    await ensure_counter_task(app)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Counter watchdog error: %s", exc)
            await asyncio.sleep(5)


async def polling_watchdog(app: Application) -> None:
    while True:
        try:
            await asyncio.sleep(10)
            updater = app.updater
            if updater is not None and not updater.running:
                logger.warning("Telegram polling stopped. Restarting it.")
                try:
                    await updater.start_polling(drop_pending_updates=False)
                except Exception as exc:
                    logger.exception("Polling restart failed: %s", exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Polling watchdog error: %s", exc)
            await asyncio.sleep(10)


async def post_init(app: Application) -> None:
    global application_ref, watchdog_task, polling_watchdog_task
    application_ref = app
    load_state()
    await app.bot.delete_webhook(drop_pending_updates=False)

    if state.get("running") and state.get("channel"):
        await ensure_counter_task(app)

    watchdog_task = app.create_task(counter_watchdog(app), name="counter-watchdog")
    polling_watchdog_task = app.create_task(polling_watchdog(app), name="polling-watchdog")
    logger.info("Bot initialized")


async def post_shutdown(app: Application) -> None:
    global watchdog_task, polling_watchdog_task
    for task in (watchdog_task, polling_watchdog_task):
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    watchdog_task = None
    polling_watchdog_task = None
    await cancel_counter_task()
    logger.info("Bot shutdown")


async def health(_: web.Request) -> web.Response:
    return web.json_response(
        {
            "ok": True,
            "running": bool(state.get("running")),
            "channel_configured": bool(state.get("channel")),
            "next_number": state.get("next_number"),
            "interval_seconds": state.get("interval_seconds", DEFAULT_INTERVAL_SECONDS),
            "counter_task_alive": bool(counter_task and not counter_task.done()),
            "polling_alive": bool(
                application_ref
                and application_ref.updater
                and application_ref.updater.running
            ),
        }
    )


async def root(_: web.Request) -> web.Response:
    return web.Response(text="Telegram Counter Bot is running.")


async def run_http_server() -> None:
    port = int(os.getenv("PORT", "10000"))
    app = web.Application()
    app.router.add_get("/", root)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("HTTP server listening on 0.0.0.0:%s", port)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


async def main() -> None:
    application = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, panel_message))

    http_task = asyncio.create_task(run_http_server(), name="http-server")

    try:
        await application.initialize()
        await post_init(application)
        await application.start()

        if application.updater is None:
            raise RuntimeError("Telegram updater is not available")

        await application.updater.start_polling(drop_pending_updates=False)
        logger.info("Telegram polling started")
        await asyncio.Event().wait()

    finally:
        try:
            if application.updater and application.updater.running:
                await application.updater.stop()
        finally:
            await application.stop()
            await post_shutdown(application)
            await application.shutdown()
            http_task.cancel()
            try:
                await http_task
            except asyncio.CancelledError:
                pass


if __name__ == "__main__":
    asyncio.run(main())
