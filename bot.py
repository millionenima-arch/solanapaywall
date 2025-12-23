import os
import time
import sqlite3
from datetime import datetime
from fastapi import FastAPI, Request
import uvicorn
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    ContextTypes
)

# ===========================================================
# 🔧 CONFIG (READ FROM ENV WHERE NEEDED)
# ===========================================================

# Read secrets from environment variables (set in Render)
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN env var is not set")

HELIUS_SECRET = os.environ.get("HELIUS_SECRET", "")

# Your Solana wallet (public address – safe to keep in code)
SOL_WALLET = "AjQA16fxwyavZP4WZWsQXSGjesXKWXxcZ7yuDdXNy8Wi"

# Your Telegram paid-group ID
GROUP_ID = -1002871650386

# Subscription plans
PLANS = {
    "week":  {"price": 0.5,  "days": 7},
    "month": {"price": 1.0,  "days": 30},
    "year":  {"price": 10.0, "days": 365},
    "life":  {"price": 25.0, "days": None},  # None = lifetime
}

DB = "subs.db"

# FastAPI app for Helius webhooks
api = FastAPI()

# ===========================================================
# 🔧 DATABASE HELPERS
# ===========================================================

def init_db():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS subs (
        user_id    INTEGER PRIMARY KEY,
        username   TEXT,
        plan       TEXT,
        expires_at INTEGER  -- unix timestamp; NULL for lifetime
    )
    """)
    c.execute("""
    CREATE TABLE IF NOT EXISTS pending (
        code    TEXT PRIMARY KEY,
        user_id INTEGER,
        plan    TEXT
    )
    """)
    conn.commit()
    conn.close()


def create_pending(user_id: int, plan: str, code: str):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute(
        "REPLACE INTO pending (code, user_id, plan) VALUES (?, ?, ?)",
        (code, user_id, plan),
    )
    conn.commit()
    conn.close()


def complete_payment(code: str):
    """
    Called when a payment with this memo/code is detected.
    Returns (user_id, expires_at) or (None, None) if no pending payment.
    """
    conn = sqlite3.connect(DB)
    c = conn.cursor()

    c.execute("SELECT user_id, plan FROM pending WHERE code = ?", (code,))
    row = c.fetchone()
    if not row:
        conn.close()
        return None, None

    user_id, plan = row

    # Remove from pending
    c.execute("DELETE FROM pending WHERE code = ?", (code,))

    # Compute expiry
    if PLANS[plan]["days"] is None:
        expires = None
    else:
        expires = int(time.time()) + PLANS[plan]["days"] * 86400

    # Upsert subscription (keep old username if any)
    c.execute(
        """
        REPLACE INTO subs (user_id, username, plan, expires_at)
        VALUES (
            ?,
            COALESCE((SELECT username FROM subs WHERE user_id = ?), ''),
            ?,
            ?
        )
        """,
        (user_id, user_id, plan, expires),
    )

    conn.commit()
    conn.close()
    return user_id, expires


def get_expired():
    """
    Return list of user_ids whose subscription has expired.
    """
    now = int(time.time())
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute(
        "SELECT user_id FROM subs WHERE expires_at IS NOT NULL AND expires_at < ?",
        (now,),
    )
    rows = [r[0] for r in c.fetchall()]
    conn.close()
    return rows


# ===========================================================
# 🤖 TELEGRAM BOT SETUP
# ===========================================================

bot_app = ApplicationBuilder().token(BOT_TOKEN).build()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    # Save username (best-effort)
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute(
        "UPDATE subs SET username = ? WHERE user_id = ?",
        (user.username or "", user.id),
    )
    conn.commit()
    conn.close()

    await update.message.reply_text(
        "Welcome!\n\nUse /subscribe to buy access to the private group."
    )


async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    buttons = [
        [InlineKeyboardButton("0.5 SOL / Week", callback_data="week")],
        [InlineKeyboardButton("1 SOL / Month", callback_data="month")],
        [InlineKeyboardButton("10 SOL / Year", callback_data="year")],
        [InlineKeyboardButton("25 SOL / Lifetime", callback_data="life")],
    ]
    await update.message.reply_text(
        "Choose your subscription:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def plan_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    plan = q.data
    user = q.from_user
    price = PLANS[plan]["price"]

    # Unique memo code for this user + plan
    code = f"{user.id}-{int(time.time())}"
    create_pending(user.id, plan, code)

    await q.edit_message_text(
        f"Plan: *{plan.upper()}* — `{price}` SOL\n\n"
        f"1️⃣ Send *exactly* `{price}` SOL to:\n"
        f"`{SOL_WALLET}`\n\n"
        f"2️⃣ Set this MEMO / reference:\n"
        f"`{code}`\n\n"
        f"Once the transaction is confirmed, you'll receive an invite link.",
        parse_mode="Markdown",
    )


bot_app.add_handler(CommandHandler("start", start))
bot_app.add_handler(CommandHandler("subscribe", subscribe))
bot_app.add_handler(CallbackQueryHandler(plan_button))


# ===========================================================
# 🧹 JOB: KICK EXPIRED SUBSCRIBERS
# ===========================================================

async def kick_expired(context: ContextTypes.DEFAULT_TYPE):
    expired_users = get_expired()
    for user_id in expired_users:
        try:
            # Kick + unban to remove access but allow re-join via new link
            await context.bot.ban_chat_member(GROUP_ID, user_id)
            await context.bot.unban_chat_member(GROUP_ID, user_id)
        except Exception:
            # Ignore failures (e.g. not in group, no rights, etc.)
            pass


# Attach job queue if available
if bot_app.job_queue is not None:
    bot_app.job_queue.run_repeating(kick_expired, interval=3600, first=10)


# ===========================================================
# 🌐 FASTAPI: HELIUS WEBHOOK ENDPOINT
# ===========================================================

@api.post("/helius-webhook")
async def helius(request: Request):
    # Simple auth check using header set in Helius dashboard
    if HELIUS_SECRET:
        if request.headers.get("x-webhook-secret") != HELIUS_SECRET:
            return {"error": "unauthorized"}

    body = await request.json()

    # Helius enhanced webhooks send a list of transactions
    for tx in body.get("transactions", []):
        memo = tx.get("memo")
        if not memo:
            continue

        user_id, expires = complete_payment(memo)
        if not user_id:
            continue

        # Create a fresh invite link and DM the user
        link = await bot_app.bot.create_chat_invite_link(GROUP_ID)
        try:
            await bot_app.bot.send_message(
                chat_id=user_id,
                text=(
                    "✅ Payment confirmed!\n\n"
                    f"Plan is now active.\n\n"
                    f"Join the private group here:\n{link.invite_link}"
                ),
            )
        except Exception:
            # User might have blocked the bot, etc.
            pass

    return {"ok": True}


# ===========================================================
# 🚀 ENTRYPOINT
# ===========================================================

def main():
    init_db()

    import threading

    # Run FastAPI (webhook server) in a background thread
    def run_api():
        uvicorn.run(api, host="0.0.0.0", port=8000)

    api_thread = threading.Thread(target=run_api, daemon=True)
    api_thread.start()

    # Run Telegram bot in the main thread (needs main asyncio loop)
    bot_app.run_polling()


if __name__ == "__main__":
    main()
