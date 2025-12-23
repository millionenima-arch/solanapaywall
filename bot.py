import os
import time
import sqlite3
from fastapi import FastAPI, Request
import uvicorn
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    ContextTypes
)

# ===========================================================
# 🔧 CONFIG
# ===========================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN env var is not set")

HELIUS_SECRET = os.environ.get("HELIUS_SECRET", "")

# Your receiving SOL wallet (paywall address)
SOL_WALLET = "AjQA16fxwyavZP4WZWsQXSGjesXKWXxcZ7yuDdXNy8Wi"

# VIP PRIVATE GROUP ID (the real paywalled group)
GROUP_ID = -1002871650386

# Prices
PLANS = {
    "week":  {"price_sol": 0.5,  "days": 7},
    "month": {"price_sol": 1.0,  "days": 30},
    "year":  {"price_sol": 10.0, "days": 365},
    "life":  {"price_sol": 25.0, "days": None},
}

DB = "subs.db"
LAMPORTS_PER_SOL = 1_000_000_000
# 0.05 SOL tolerance
TOLERANCE_LAMPORTS = int(0.05 * LAMPORTS_PER_SOL)

api = FastAPI()

# ===========================================================
# DB HELPERS
# ===========================================================

def init_db():
    conn = sqlite3.connect(DB)
    c = conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS subs (
        user_id     INTEGER PRIMARY KEY,
        username    TEXT,
        wallet      TEXT,
        plan        TEXT,
        expires_at  INTEGER
    )
    """)

    # one pending payment per user
    c.execute("""
    CREATE TABLE IF NOT EXISTS pending (
        user_id          INTEGER PRIMARY KEY,
        wallet           TEXT,
        plan             TEXT,
        amount_lamports  INTEGER
    )
    """)

    conn.commit()
    conn.close()
    print("[DB] initialized")


def set_wallet(user_id: int, username: str | None, wallet: str):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("""
    INSERT INTO subs (user_id, username, wallet, plan, expires_at)
    VALUES (?, ?, ?, '', NULL)
    ON CONFLICT(user_id) DO UPDATE SET
        username = excluded.username,
        wallet   = excluded.wallet
    """, (user_id, username or "", wallet))
    conn.commit()
    conn.close()
    print(f"[DB] set_wallet user={user_id} wallet={wallet}")


def get_wallet(user_id: int) -> str | None:
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT wallet FROM subs WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row and row[0] else None


def create_pending(user_id: int, wallet: str, plan: str):
    price_sol = PLANS[plan]["price_sol"]
    expected_lamports = int(price_sol * LAMPORTS_PER_SOL)

    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("""
    INSERT INTO pending (user_id, wallet, plan, amount_lamports)
    VALUES (?, ?, ?, ?)
    ON CONFLICT(user_id) DO UPDATE SET
        wallet = excluded.wallet,
        plan   = excluded.plan,
        amount_lamports = excluded.amount_lamports
    """, (user_id, wallet, plan, expected_lamports))
    conn.commit()
    conn.close()
    print(f"[DB] create_pending user={user_id} wallet={wallet} plan={plan} amount={expected_lamports}")


def complete_payment_from_transfer(from_wallet: str, amount_lamports: int):
    """
    Called from webhook when a transfer to SOL_WALLET is detected.
    Match by sender wallet + amount with 0.05 SOL tolerance.
    """
    conn = sqlite3.connect(DB)
    c = conn.cursor()

    c.execute("""
    SELECT user_id, plan, amount_lamports
    FROM pending
    WHERE LOWER(wallet) = LOWER(?)
    """, (from_wallet,))
    row = c.fetchone()

    if not row:
        conn.close()
        print(f"[PAY] no pending payment for wallet {from_wallet}")
    else:
        user_id, plan, expected = row
        print(f"[PAY] candidate match user={user_id} plan={plan} expected={expected} got={amount_lamports}")

        # accept if sent >= expected - 0.05 SOL
        if amount_lamports + TOLERANCE_LAMPORTS >= expected:
            # delete pending
            c.execute("DELETE FROM pending WHERE user_id = ?", (user_id,))

            days = PLANS[plan]["days"]
            if days is None:
                expires = None
            else:
                expires = int(time.time()) + days * 86400

            c.execute("""
            UPDATE subs
            SET plan = ?, expires_at = ?
            WHERE user_id = ?
            """, (plan, expires, user_id))

            conn.commit()
            conn.close()
            print(f"[PAY] ACCEPTED user={user_id} plan={plan} expires={expires}")
            return user_id, expires
        else:
            print(f"[PAY] REJECTED underpay. expected>={expected - TOLERANCE_LAMPORTS}, got={amount_lamports}")
            conn.close()

    return None, None


def get_expired():
    now = int(time.time())
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("""
    SELECT user_id FROM subs
    WHERE expires_at IS NOT NULL AND expires_at < ?
    """, (now,))
    rows = [r[0] for r in c.fetchall()]
    conn.close()
    return rows


# ===========================================================
# TELEGRAM BOT
# ===========================================================

bot_app = ApplicationBuilder().token(BOT_TOKEN).build()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text(
        "Welcome!\n\n"
        "1️⃣ Set your Solana wallet with:\n"
        "`/setwallet YOUR_SOL_ADDRESS`\n\n"
        "2️⃣ Then use `/subscribe` to buy access to the VIP group.",
        parse_mode="Markdown",
    )

    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute(
        "UPDATE subs SET username = ? WHERE user_id = ?",
        (user.username or "", user.id),
    )
    conn.commit()
    conn.close()


async def setwallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not context.args:
        await update.message.reply_text(
            "Send your wallet like this:\n\n"
            "`/setwallet YOUR_SOL_ADDRESS`",
            parse_mode="Markdown",
        )
        return

    wallet = context.args[0].strip()

    if len(wallet) < 32 or len(wallet) > 60:
        await update.message.reply_text("That doesn't look like a valid Solana address.")
        return

    set_wallet(user.id, user.username, wallet)
    await update.message.reply_text(
        f"✅ Wallet saved:\n`{wallet}`\n\nYou can now use /subscribe.",
        parse_mode="Markdown",
    )


async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    wallet = get_wallet(user_id)
    if not wallet:
        await update.message.reply_text(
            "You need to set your wallet first.\n\n"
            "Use `/setwallet YOUR_SOL_ADDRESS` and then /subscribe.",
            parse_mode="Markdown",
        )
        return

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
    user = q.from_user
    user_id = user.id

    wallet = get_wallet(user_id)
    if not wallet:
        await q.edit_message_text(
            "You need to set your wallet first.\n\n"
            "Use `/setwallet YOUR_SOL_ADDRESS` and then /subscribe.",
            parse_mode="Markdown",
        )
        return

    plan = q.data
    price_sol = PLANS[plan]["price_sol"]

    create_pending(user_id, wallet, plan)

    await q.edit_message_text(
        f"Plan: *{plan.upper()}* — `{price_sol}` SOL\n\n"
        f"1️⃣ Send *exactly* `{price_sol}` SOL\n"
        f"from your wallet:\n`{wallet}`\n\n"
        f"to this address:\n`{SOL_WALLET}`\n\n"
        f"No memo needed.\n"
        f"If you send *slightly less* (up to 0.05 SOL difference) "
        f"it will still be accepted.\n\n"
        f"After the transaction confirms, you'll receive an invite link "
        f"to the VIP group.",
        parse_mode="Markdown",
    )


bot_app.add_handler(CommandHandler("start", start))
bot_app.add_handler(CommandHandler("setwallet", setwallet))
bot_app.add_handler(CommandHandler("subscribe", subscribe))
bot_app.add_handler(CallbackQueryHandler(plan_button))


# ===========================================================
# JOB: KICK EXPIRED
# ===========================================================

async def kick_expired_job(context: ContextTypes.DEFAULT_TYPE):
    expired_users = get_expired()
    if expired_users:
        print(f"[JOB] kicking expired users: {expired_users}")
    for user_id in expired_users:
        try:
            await context.bot.ban_chat_member(GROUP_ID, user_id)
            await context.bot.unban_chat_member(GROUP_ID, user_id)
        except Exception as e:
            print(f"[JOB] failed to kick {user_id}: {e}")


if bot_app.job_queue is not None:
    bot_app.job_queue.run_repeating(kick_expired_job, interval=3600, first=10)


# ===========================================================
# FASTAPI: HELIUS WEBHOOK
# ===========================================================

@api.post("/helius-webhook")
async def helius(request: Request):
    # header auth
    if HELIUS_SECRET:
        if request.headers.get("x-webhook-secret") != HELIUS_SECRET:
            print("[WEBHOOK] invalid secret header")
            return {"error": "unauthorized"}

    body = await request.json()
    print("[WEBHOOK] incoming:", body)

    txs = body.get("transactions", []) or body.get("events", [])

    for tx in txs:
        for nt in tx.get("nativeTransfers", []):
            to_acc = nt.get("toUserAccount")
            from_acc = nt.get("fromUserAccount")
            amount_raw = nt.get("amount", 0)

            if not to_acc or not from_acc:
                continue

            if to_acc.lower() != SOL_WALLET.lower():
                continue

            try:
                amount_lamports = int(amount_raw)
            except (TypeError, ValueError):
                print(f"[WEBHOOK] bad amount {amount_raw}")
                continue

            print(f"[WEBHOOK] transfer to us from {from_acc} amount={amount_lamports}")
            user_id, expires = complete_payment_from_transfer(from_acc, amount_lamports)
            if not user_id:
                continue

            # create invite and DM user
            try:
                link = await bot_app.bot.create_chat_invite_link(GROUP_ID)
                await bot_app.bot.send_message(
                    chat_id=user_id,
                    text=(
                        "✅ Payment confirmed!\n\n"
                        "Your subscription is now active.\n\n"
                        f"Join the VIP group here:\n{link.invite_link}"
                    ),
                )
                print(f"[PAY] sent invite link to user {user_id}")
            except Exception as e:
                print(f"[PAY] failed to send invite to {user_id}: {e}")

    return {"ok": True}


# ===========================================================
# ENTRYPOINT
# ===========================================================

def main():
    init_db()

    import threading

    def run_api():
        uvicorn.run(api, host="0.0.0.0", port=8000)

    t = threading.Thread(target=run_api, daemon=True)
    t.start()

    bot_app.run_polling()


if __name__ == "__main__":
    main()
