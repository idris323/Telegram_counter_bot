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
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ============================================================
# Telegram Counter Bot
# Required environment variables:
#   BOT_TOKEN  = Telegram BotFather token
#   ADMIN_ID   = numeric Telegram user ID of the main admin
#
# The bot can be configured from the admin panel.
# It sends: 1, 2, 3, 4, ... every 5 seconds while running.
# ============================================================

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

INTERVAL_SECONDS = 5
DEFAULT_START_NUMBER = 1
DATA_DIR = Path(os.getenv("DATA_DIR", "/var/data"))
STATE_FILE = DATA_DIR / "state.json"

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("counter-bot")

# Protects the JSON state file and counter changes from concurrent writes.
state_lock = asyncio.Lock()

# In-memory state loaded from disk. Example:
# {
#   "channel": {"chat_id": -100123..., "title": "My Channel"},
#   "next_number": 1,
#   "running": false
# }
state: Dict[str, Any] = {
    "channel": None,
    "next_number": DEFAULT_START_NUMBER,
    "running": False,
}

# Admin panel conversation mode. Only the single ADMIN_ID can use it.
ADMIN_MODE = "admin_mode"
MODE_NONE = None
MODE_WAIT_CHANNEL = "wait_channel"
MODE_WAIT_START_NUMBER = "wait_start_number"

# A reference to the application is needed by the counter task.
application_ref: Optional[Application] = None
counter_task: Optional[asyncio.Task] = None


def ensure_data_dir() -> None:
    """Create the data directory when possible."""
    global STATE_FILE, DATA_DIR
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        # Fall back to the project directory if /var/data isn't writable.
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
        if channel is not None:
            if not isinstance(channel, dict) or "chat_id" not in channel:
                channel = None

        next_number = int(loaded.get("next_number", DEFAULT_START_NUMBER))
        if next_number < 0:
            next_number = DEFAULT_START_NUMBER

        running = bool(loaded.get("running", False))
        state = {
            "channel": channel,
            "next_number": next_number,
            "running": running,
        }
        logger.info("State loaded: %s", state)
    except Exception as exc:
        logger.error("Could not load state file: %s", exc)
        state = {
            "channel": None,
            "next_number": DEFAULT_START_NUMBER,
            "running": False,
        }


def save_state() -> None:
    """Atomically save state to JSON."""
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


def is_admin(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id == ADMIN_ID)


def panel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["📢 تنظیم کانال", "✅ بررسی کانال"],
            ["▶️ شروع", "⏸ توقف"],
            ["🔢 عدد شروع", "📊 وضعیت"],
            ["🗑 حذف کانال", "❓ راهنما"],
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
    return (
        "📊 وضعیت ربات\n\n"
        f"کانال: {current_channel_text()}\n"
        f"وضعیت: {running}\n"
        f"عدد بعدی: {state.get('next_number', DEFAULT_START_NUMBER)}\n"
        f"فاصله ارسال: {INTERVAL_SECONDS} ثانیه"
    )


async def safe_state_save() -> None:
    async with state_lock:
        save_state()


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
    text = (
        "❓ راهنما\n\n"
        "1) ربات را در کانال خود Administrator کن و اجازه ارسال پیام بده.\n"
        "2) در پنل «📢 تنظیم کانال» را بزن.\n"
        "3) آیدی کانال مثل -1001234567890 یا یوزرنیم مثل @mychannel را بفرست.\n"
        "4) «✅ بررسی کانال» را بزن.\n"
        "5) برای شروع «▶️ شروع» را بزن.\n\n"
        "ربات هر ۵ ثانیه یک عدد می‌فرستد: 1، 2، 3، 4، ...\n"
        "با «⏸ توقف» متوقف می‌شود و با شروع دوباره از همان عدد ادامه می‌دهد."
    )
    await update.message.reply_text(text, reply_markup=panel_keyboard())


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
        f"🔢 عدد شروع را بفرست.\nمثلاً 1 یا 1000\n\nبرای لغو /cancel را بفرست.",
        reply_markup=ReplyKeyboardRemove(),
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    context.user_data[ADMIN_MODE] = MODE_NONE
    await update.message.reply_text("❌ لغو شد.", reply_markup=panel_keyboard())


async def validate_bot_admin(chat_id: Any, context: ContextTypes.DEFAULT_TYPE) -> tuple[bool, str, Any]:
    """Verify channel exists and the bot is an administrator with posting rights."""
    try:
        chat = await context.bot.get_chat(chat_id)
    except (BadRequest, Forbidden) as exc:
        return False, f"❌ کانال پیدا نشد یا ربات به آن دسترسی ندارد.\n{exc}", None
    except TelegramError as exc:
        return False, f"❌ خطا هنگام دسترسی به کانال:\n{exc}", None

    try:
        me = await context.bot.get_me()
        member = await context.bot.get_chat_member(chat.id, me.id)
    except TelegramError as exc:
        return False, f"❌ نتوانستم وضعیت ادمین ربات را بررسی کنم:\n{exc}", chat

    if member.status != ChatMemberStatus.ADMINISTRATOR:
        return (
            False,
            "❌ ربات ادمین این کانال نیست.\n"
            "ابتدا ربات را در کانال Administrator کن و دوباره بررسی کن.",
            chat,
        )

    # For channels, can_post_messages is the relevant right. The attribute is
    # optional in Telegram's API; True is what we need.
    can_post = getattr(member, "can_post_messages", True)
    if can_post is False:
        return (
            False,
            "❌ ربات ادمین است، اما اجازه ارسال پیام در کانال را ندارد.\n"
            "مجوز ارسال پیام را برای ربات فعال کن.",
            chat,
        )

    return True, "✅ دسترسی ارسال پیام تأیید شد.", chat


async def process_channel_input(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    value = text.strip()
    if not value:
        await update.message.reply_text("❌ مقدار خالی است. دوباره بفرست یا /cancel بزن.")
        return

    # Normalize bare usernames to @username. Numeric IDs are kept as ints.
    chat_id: Any = value
    if value.lstrip("-").isdigit():
        try:
            chat_id = int(value)
        except ValueError:
            chat_id = value
    elif not value.startswith("@"):
        chat_id = "@" + value.lstrip("@")

    ok, message, chat = await validate_bot_admin(chat_id, context)
    if not ok:
        await update.message.reply_text(message)
        return

    channel_data = {
        "chat_id": chat.id,
        "title": getattr(chat, "title", None) or getattr(chat, "full_name", None) or "",
        "username": getattr(chat, "username", None),
    }

    state["channel"] = channel_data
    # Keep the current counter when changing between channels.
    # If there was no configured channel before, start from the saved number.
    await safe_state_save()
    context.user_data[ADMIN_MODE] = MODE_NONE

    await update.message.reply_text(
        "✅ کانال ثبت شد.\n\n"
        f"نام: {channel_data['title'] or 'بدون عنوان'}\n"
        f"آیدی: {channel_data['chat_id']}\n\n"
        "حالا می‌توانی «▶️ شروع» را بزنی.",
        reply_markup=panel_keyboard(),
    )


async def process_start_number(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    try:
        number = int(text.strip())
    except ValueError:
        await update.message.reply_text("❌ فقط عدد صحیح بفرست. مثل 1 یا 1000")
        return

    if number < 0:
        await update.message.reply_text("❌ عدد نمی‌تواند منفی باشد.")
        return
    if number > 10**300:
        await update.message.reply_text("❌ عدد بیش از حد بزرگ است.")
        return

    state["next_number"] = number
    await safe_state_save()
    context.user_data[ADMIN_MODE] = MODE_NONE
    await update.message.reply_text(
        f"✅ عدد بعدی روی {number} تنظیم شد.",
        reply_markup=panel_keyboard(),
    )


async def check_channel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    channel = state.get("channel")
    if not channel:
        await update.message.reply_text("❌ هنوز کانالی تنظیم نشده است.")
        return

    ok, message, chat = await validate_bot_admin(channel["chat_id"], context)
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

    await update.message.reply_text(
        "🟢 شمارش شروع شد.\n"
        f"عدد بعدی: {state['next_number']}\n"
        "هر ۵ ثانیه یک پیام ارسال می‌شود.",
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

    await update.message.reply_text(
        "🗑 کانال حذف شد. عدد فعلی حفظ شده است.",
        reply_markup=panel_keyboard(),
    )


async def show_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    await update.message.reply_text(status_text(), reply_markup=panel_keyboard())


async def panel_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update) or not update.message:
        return

    text = (update.message.text or "").strip()
    mode = context.user_data.get(ADMIN_MODE, MODE_NONE)

    # Conversation input has priority over panel buttons.
    if mode == MODE_WAIT_CHANNEL:
        await process_channel_input(update, context, text)
        return
    if mode == MODE_WAIT_START_NUMBER:
        await process_start_number(update, context, text)
        return

    actions = {
        "📢 تنظیم کانال": set_channel_request,
        "✅ بررسی کانال": check_channel,
        "▶️ شروع": start_counter,
        "⏸ توقف": stop_counter,
        "🔢 عدد شروع": set_start_number_request,
        "📊 وضعیت": show_status,
        "🗑 حذف کانال": remove_channel,
        "❓ راهنما": help_command,
    }
    handler = actions.get(text)
    if handler:
        await handler(update, context)
    else:
        await update.message.reply_text(
            "از دکمه‌های پنل استفاده کن یا /start را بزن.",
            reply_markup=panel_keyboard(),
        )


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

            try:
                await app.bot.send_message(chat_id=chat_id, text=str(number))
            except (Forbidden, BadRequest, TelegramError) as exc:
                logger.error("Could not send number %s to %s: %s", number, chat_id, exc)
                # Stop on permission/access problems instead of spamming failed requests.
                state["running"] = False
                await safe_state_save()
                try:
                    await app.bot.send_message(
                        chat_id=ADMIN_ID,
                        text=(
                            "⛔ شمارش متوقف شد چون ارسال به کانال ناموفق بود.\n\n"
                            f"کانال: {current_channel_text()}\n"
                            f"خطا: {exc}"
                        ),
                    )
                except TelegramError:
                    pass
                await asyncio.sleep(2)
                continue

            # Increment only after a successful send, so no numbers are skipped
            # because of a Telegram/API error.
            state["next_number"] = number + 1
            await safe_state_save()
            await asyncio.sleep(INTERVAL_SECONDS)
    except asyncio.CancelledError:
        logger.info("Counter loop cancelled")
        raise
    finally:
        counter_task = None


async def ensure_counter_task(app: Application) -> None:
    global counter_task
    if counter_task is None or counter_task.done():
        counter_task = app.create_task(counter_loop(app), name="counter-loop")


async def post_init(app: Application) -> None:
    global application_ref
    application_ref = app
    load_state()
    await app.bot.delete_webhook(drop_pending_updates=False)
    if state.get("running") and state.get("channel"):
        # Start automatically after a restart when saved state says running.
        await ensure_counter_task(app)
    logger.info("Bot initialized")


async def post_shutdown(app: Application) -> None:
    await cancel_counter_task()
    logger.info("Bot shutdown")


async def health(_: web.Request) -> web.Response:
    # Render health check / uptime monitors can call this endpoint.
    return web.json_response(
        {
            "ok": True,
            "running": bool(state.get("running")),
            "channel_configured": bool(state.get("channel")),
            "next_number": state.get("next_number"),
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

    # Keep this coroutine alive for the lifetime of the process.
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


async def main() -> None:
    builder = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
    )
    application = builder.build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, panel_message))

    # Run Telegram polling and the HTTP server in the same asyncio loop.
    http_task = asyncio.create_task(run_http_server())
    try:
        await application.initialize()
        # ApplicationBuilder.post_init/post_shutdown are normally invoked by
        # run_polling()/run_webhook(). Because this bot runs its own asyncio
        # loop together with aiohttp, invoke them explicitly.
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
