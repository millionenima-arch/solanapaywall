###############################
#  🔥 SOLANA PAYWALL BOT 🔥   #
###############################

import time
import sqlite3
from datetime import datetime, timedelta
from fastapi import FastAPI, Request
import uvicorn
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    ContextTypes
)

# ===========================================================
# 🔧  EDIT THESE VALUES ONLY (VERY IMPORTANT)  🔧
# ===========================================================

# 👉 PASTE YOUR TELEGRAM BOT TOKEN HERE (ONLY EDIT THIS LINE)
BOT_TOKEN = "8374066571:AAEZ8zYgvpwyQgkeQdVEwQdX3KJAwtIKDR4"

# Your Solana wallet (you gave me this)
SOL_WALLET = "AjQA16fxwyavZP4WZWsQXSGjesXKWXxcZ7yuDdXNy8Wi"

# Your Telegram paid-group ID
GROUP_ID = -1002871650386

# Your secret from Helius webhook settings
HELIUS_SECRET = "CHANGE_THIS_TO_YOUR_HELIUS_WEBHOOK_SECRET"

# Your price plans
PLANS = {
    "week":  {"price": 0.5,  "days": 7},
    "month": {"price": 1.0,  "days": 30},
    "year":  {"price": 10.0, "days": 365},
    "life":  {"price": 25.0, "days": None},
}

# ===========================================================
# ❌ DO NOT EDIT ANYTHING BELOW THIS LINE ❌
# ===========================================================

DB = "subs.db"
api = FastAPI()

def init_db():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS subs (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        plan TEXT,
        expires_at INTEGER
    )
    """)
    c.execute("""
    CREATE TABLE IF NOT EXISTS pending (
        code TEXT PRIMARY KEY,
        user_id INTEGER,
        plan TEXT
    )
    """)
    conn.commit()
    conn.close()

def create_pending(user_id, plan, code):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("REPLACE INTO pending (code, user_id, plan) VALUES (?, ?, ?)",
              (code, user_id, plan))
    conn.commit()
    conn.close()

def complete_payment(code):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT user_id, plan FROM pending WHERE code=?", (code,))
    row = c.fetchone()
    if not row:
        conn.close()
        return None, None

    user_id, plan = row
    c.execute("DELETE FROM pending WHERE code=?", (code,))

    if PLANS[plan]["days"] is None:
        expires = None
    else:
        expires = int(time.time()) + PLANS[plan]["days"] * 86400

    c.execute("""
    REPLACE INTO subs (user_id, username, plan, expires_at)
    VALUES (?, COALESCE((SELECT username FROM subs WHERE user_id=?), ''), ?, ?)
    """, (user_id, user_id, plan, expires))

    conn.commit()
    conn.close()
    return user_id, expires


def get_expired():
    now = int(time.time())
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT user_id FROM subs WHERE expires_at IS NOT NULL AND expires_at < ?", (now,))
    rows = [r[0] for r in c.fetchall()]
    conn.close()
    return rows


bot_app = ApplicationBuilder().token(BOT_TOKEN).build()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Welcome.\nUse /subscribe to buy access.")


async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    buttons = [
        [InlineKeyboardButton("0.5 SOL / Week", callback_data="week")],
        [InlineKeyboardButton("1 SOL / Month", callback_data="month")],
        [InlineKeyboardButton("10 SOL / Year", callback_data="year")],
        [InlineKeyboardButton("25 SOL / Lifetime", callback_data="life")],
    ]
    await update.message.reply_text(
        "Choose your subscription:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def plan_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    plan = q.data
    user = q.from_user
    price = PLANS[plan]["price"]

    code = f"{user.id}-{int(time.time())}"
    create_pending(user.id, plan, code)

    await q.edit_message_text(
        f"Plan: {plan.upper()} ({price} SOL)\n\n"
        f"Send EXACTLY {price} SOL to:\n`{SOL_WALLET}`\n\n"
        f"Use this MEMO:\n`{code}`\n\n"
        f"When payment arrives, you'll receive your invite link.",
        parse_mode="Markdown"
    )


bot_app.add_handler(CommandHandler("start", start))
bot_app.add_handler(CommandHandler("subscribe", subscribe))
bot_app.add_handler(CallbackQueryHandler(plan_button))


async def kick_expired(context):
    for user in get_expired():
        try:
            await context.bot.ban_chat_member(GROUP_ID, user)
            await context.bot.unban_chat_member(GROUP_ID, user)
        except:
            pass

bot_app.job_queue.run_repeating(kick_expired, interval=3600, first=10)


@api.post("/helius-webhook")
async def helius(request: Request):
    if request.headers.get("x-webhook-secret") != HELIUS_SECRET:
        return {"error": "unauthorized"}

    body = await request.json()

    for tx in body.get("transactions", []):
        memo = tx.get("memo")
        if not memo:
            continue

        user_id, expires = complete_payment(memo)
        if not user_id:
            continue

        link = await bot_app.bot.create_chat_invite_link(GROUP_ID)
        await bot_app.bot.send_message(
            chat_id=user_id,
            text=f"Payment confirmed!\nJoin here:\n{link.invite_link}"
        )

    return {"ok": True}


def main():
    init_db()

    import threading
    def run_bot():
        bot_app.run_polling()

    t = threading.Thread(target=run_bot, daemon=True)
    t.start()

    uvicorn.run(api, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
