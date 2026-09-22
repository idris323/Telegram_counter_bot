# Telegram Counter Bot

A Telegram bot that lets the main admin configure a channel from the bot's own admin panel and send:

`1` → `2` → `3` → `4` → ...

every 5 seconds.

## Required environment variables

Only these two are required:

- `BOT_TOKEN` — token from @BotFather
- `ADMIN_ID` — the numeric Telegram user ID of the main admin

Render automatically provides `PORT`; you do not need to add it manually.

## GitHub

Upload these files to the repository root:

- `bot.py`
- `requirements.txt`
- `render.yaml`
- `.gitignore`
- `README.md`

Do **not** upload your real bot token.

## Render

### Option A — use the included `render.yaml`

Create the service from the Blueprint, then enter the two secret values when Render asks for them.

### Option B — create Web Service manually

- Build Command: `pip install -r requirements.txt`
- Start Command: `python bot.py`
- Health Check Path: `/health`
- Environment variables: `BOT_TOKEN`, `ADMIN_ID`

## Important: make the bot an admin in the channel

A Telegram bot cannot promote itself. The channel owner/admin must add the bot to the channel as an administrator and give it permission to post messages.

After that, open a private chat with the bot as the account whose numeric ID is in `ADMIN_ID` and send `/start`.

Use:

1. `📢 تنظیم کانال`
2. Enter `@channelusername` or the numeric channel ID such as `-1001234567890`
3. `✅ بررسی کانال`
4. `▶️ شروع`

The counter starts from the saved next number (default `1`).

## Controls

- `▶️ شروع` — start sending every 5 seconds
- `⏸ توقف` — stop without resetting the next number
- `🔢 عدد شروع` — set the next number manually
- `📊 وضعیت` — show status and next number
- `📢 تنظیم کانال` — select another channel
- `✅ بررسی کانال` — verify bot admin/post permission
- `🗑 حذف کانال` — remove the selected channel
- `❓ راهنما` — show help

## Persistence on Render

The program stores its state in `DATA_DIR` (default `/var/data`). If a persistent disk is attached and mounted at `/var/data`, the selected channel, next number, and running state survive restarts and deploys.

Render's free web services have an ephemeral filesystem, so local state can be lost when the service restarts or spins down. The free plan also spins down an inactive web service after 15 minutes without inbound traffic. For truly continuous 5-second posting, use a paid always-on service or keep the free service awake with an external health-check monitor that calls `/health` regularly.
