import os
import time
import sqlite3
from fastapi import FastAPI, Request
import uvicorn
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# ===========================================================
# 🔧 CONFIG
# ===========================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN env var is not set")

HELIUS_SECRET = os.environ.get("HELIUS_SECRET", "")

# Solana paywall wallet (where payments go)
SOL_WALLET = "AjQA16fxwyavZP4WZWsQXSGjesXKWXxcZ7yuDdXNy8Wi"

# Private VIP group ID
GROUP_ID = -1002871650386  # your group

# Admins for /admin dashboard (env: ADMIN_IDS="12345,67890")
ADMIN_IDS: set[int] = set()
_admin_env = os.environ.get("ADMIN_IDS", "")
if _admin_env:
    for part in _admin_env.replace(" ", "").split(","):
        if part:
            try:
                ADMIN_IDS.add(int(part))
            except ValueError:
                pass

# Subscription plans
PLANS = {
    "week":  {"label": "Week",     "price_sol": 0.5,  "days": 7},
    "month": {"label": "Month",    "price_sol": 1.0,  "days": 30},
    "year":  {"label": "Year",     "price_sol": 10.0, "days": 365},
    "life":  {"label": "Lifetime", "price_sol": 25.0, "days": None},
}

DB = "subs.db"
LAMPORTS_PER_SOL = 1_000_000_000
TOLERANCE_LAMPORTS = int(0.05 * LAMPORTS_PER_SOL)  # 0.05 SOL tolerance

api = FastAPI()
bot_app = ApplicationBuilder().token(BOT_TOKEN).build()

# ===========================================================
# 🗄  DB HELPERS
# ===========================================================


def init_db():
    conn = sqlite3.connect(DB)
    c = conn.cursor()

    c.execute(
        """
    CREATE TABLE IF NOT EXISTS subs (
        user_id        INTEGER PRIMARY KEY,
        username       TEXT,
        wallet         TEXT,
        plan           TEXT,
        expires_at     INTEGER,
        reminder_sent  INTEGER
    )
    """
    )

    # In case DB already existed without reminder_sent, add it
    try:
        c.execute("ALTER TABLE subs ADD COLUMN reminder_sent INTEGER")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists

    c.execute(
        """
    CREATE TABLE IF NOT EXISTS pending (
        user_id          INTEGER PRIMARY KEY,
        wallet           TEXT,
        plan             TEXT,
        amount_lamports  INTEGER
    )
    """
    )

    conn.commit()
    conn.close()
    print("[DB] initialized")


def set_wallet(user_id: int, username: str | None, wallet: str):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute(
        """
    INSERT INTO subs (user_id, username, wallet, plan, expires_at, reminder_sent)
    VALUES (?, ?, ?, '', NULL, 0)
    ON CONFLICT(user_id) DO UPDATE SET
        username      = excluded.username,
        wallet        = excluded.wallet
    """,
        (user_id, username or "", wallet),
    )
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
    c.execute(
        """
    INSERT INTO pending (user_id, wallet, plan, amount_lamports)
    VALUES (?, ?, ?, ?)
    ON CONFLICT(user_id) DO UPDATE SET
        wallet          = excluded.wallet,
        plan            = excluded.plan,
        amount_lamports = excluded.amount_lamports
    """,
        (user_id, wallet, plan, expected_lamports),
    )
    conn.commit()
    conn.close()
    print(
        f"[DB] create_pending user={user_id} wallet={wallet} "
        f"plan={plan} amount={expected_lamports}"
    )


def complete_payment_from_transfer(from_wallet: str, amount_lamports: int):
    """
    Called from webhook when a transfer to SOL_WALLET is detected.
    Match by sender wallet + amount with 0.05 SOL tolerance.
    """
    conn = sqlite3.connect(DB)
    c = conn.cursor()

    c.execute(
        """
    SELECT user_id, plan, amount_lamports
    FROM pending
    WHERE LOWER(wallet) = LOWER(?)
    """,
        (from_wallet,),
    )
    row = c.fetchone()

    if not row:
        conn.close()
        print(f"[PAY] no pending payment for wallet {from_wallet}")
        return None, None, None

    user_id, plan, expected = row
    print(
        f"[PAY] candidate match user={user_id} plan={plan} "
        f"expected={expected} got={amount_lamports}"
    )

    if amount_lamports + TOLERANCE_LAMPORTS >= expected:
        c.execute("DELETE FROM pending WHERE user_id = ?", (user_id,))

        days = PLANS[plan]["days"]
        if days is None:
            expires = None
        else:
            expires = int(time.time()) + days * 86400

        c.execute(
            """
        UPDATE subs
        SET plan = ?, expires_at = ?, reminder_sent = 0
        WHERE user_id = ?
        """,
            (plan, expires, user_id),
        )

        conn.commit()
        conn.close()
        print(f"[PAY] ACCEPTED user={user_id} plan={plan} expires={expires}")
        return user_id, plan, expires
    else:
        print(
            f"[PAY] REJECTED underpay. "
            f"expected>={expected - TOLERANCE_LAMPORTS}, got={amount_lamports}"
        )
        conn.close()
        return None, None, None


def get_expired():
    now = int(time.time())
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute(
        """
    SELECT user_id
    FROM subs
    WHERE plan <> ''
      AND expires_at IS NOT NULL
      AND expires_at < ?
    """,
        (now,),
    )
    rows = [r[0] for r in c.fetchall()]
    conn.close()
    return rows


def clear_subscription(user_id: int):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute(
        """
    UPDATE subs
    SET plan = '', expires_at = NULL, reminder_sent = 0
    WHERE user_id = ?
    """,
        (user_id,),
    )
    conn.commit()
    conn.close()


def get_soon_expiring(hours: int = 24):
    now = int(time.time())
    cutoff = now + hours * 3600
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute(
        """
    SELECT user_id, expires_at
    FROM subs
    WHERE plan <> ''
      AND expires_at IS NOT NULL
      AND expires_at BETWEEN ? AND ?
      AND (reminder_sent IS NULL OR reminder_sent = 0)
    """,
        (now, cutoff),
    )
    rows = c.fetchall()
    conn.close()
    return rows


def mark_reminded(user_id: int):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute(
        "UPDATE subs SET reminder_sent = 1 WHERE user_id = ?",
        (user_id,),
    )
    conn.commit()
    conn.close()


def get_stats():
    now = int(time.time())
    conn = sqlite3.connect(DB)
    c = conn.cursor()

    c.execute("SELECT COUNT(*) FROM subs")
    total_users = c.fetchone()[0] or 0

    c.execute(
        """
    SELECT COUNT(*)
    FROM subs
    WHERE plan <> ''
      AND (expires_at IS NULL OR expires_at > ?)
    """,
        (now,),
    )
    active = c.fetchone()[0] or 0

    c.execute(
        """
    SELECT COUNT(*)
    FROM subs
    WHERE plan <> ''
      AND expires_at IS NOT NULL
      AND expires_at BETWEEN ? AND ?
    """,
        (now, now + 86400),
    )
    expiring_24h = c.fetchone()[0] or 0

    c.execute("SELECT COUNT(*) FROM pending")
    pending = c.fetchone()[0] or 0

    conn.close()
    return {
        "total_users": total_users,
        "active": active,
        "expiring_24h": expiring_24h,
        "pending": pending,
    }


# ===========================================================
# 🤖 TELEGRAM HANDLERS
# ===========================================================


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS or not ADMIN_IDS  # if empty, treat everyone as admin


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text(
        "👋 *Welcome to the VIP Paywall Bot*\n\n"
        "Here’s how it works:\n"
        "1️⃣ Set your Solana wallet with:\n"
        "   `/setwallet YOUR_SOL_ADDRESS`\n"
        "2️⃣ Use `/subscribe` to choose a plan and pay in SOL.\n\n"
        "Once payment is confirmed, you’ll receive a *private invite link* "
        "to the VIP group.\n\n"
        "_You can share the link if you want, but it only works once — "
        "it’s tied to your account._",
        parse_mode="Markdown",
    )

    # keep username fresh
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
            "📮 Send your wallet like this:\n\n"
            "`/setwallet YOUR_SOL_ADDRESS`",
            parse_mode="Markdown",
        )
        return

    wallet = context.args[0].strip()

    if len(wallet) < 32 or len(wallet) > 60:
        await update.message.reply_text(
            "⚠️ That doesn’t look like a valid Solana address."
        )
        return

    set_wallet(user.id, user.username, wallet)
    await update.message.reply_text(
        "✅ *Wallet saved!*\n\n"
        f"`{wallet}`\n\n"
        "You can now use `/subscribe` to choose a plan.",
        parse_mode="Markdown",
    )


async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    wallet = get_wallet(user_id)
    if not wallet:
        await update.message.reply_text(
            "⚠️ You need to set your wallet first.\n\n"
            "Use:\n`/setwallet YOUR_SOL_ADDRESS`\n\n"
            "Then run `/subscribe` again.",
            parse_mode="Markdown",
        )
        return

    buttons = [
        [InlineKeyboardButton("0.5 SOL • Week", callback_data="week")],
        [InlineKeyboardButton("1 SOL • Month", callback_data="month")],
        [InlineKeyboardButton("10 SOL • Year", callback_data="year")],
        [InlineKeyboardButton("25 SOL • Lifetime", callback_data="life")],
    ]
    await update.message.reply_text(
        "💳 *Choose your subscription plan:*",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )


async def plan_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user = q.from_user
    user_id = user.id

    wallet = get_wallet(user_id)
    if not wallet:
        await q.edit_message_text(
            "⚠️ You need to set your wallet first.\n\n"
            "Use:\n`/setwallet YOUR_SOL_ADDRESS`\n\n"
            "Then run `/subscribe` again.",
            parse_mode="Markdown",
        )
        return

    plan_key = q.data
    plan = PLANS[plan_key]
    price_sol = plan["price_sol"]

    create_pending(user_id, wallet, plan_key)

    await q.edit_message_text(
        f"🧾 *Plan selected:* `{plan['label']} — {price_sol} SOL`\n\n"
        "➡️ *Send payment now:*\n"
        f"• From wallet:\n`{wallet}`\n"
        f"• To wallet:\n`{SOL_WALLET}`\n"
        f"• Amount: *{price_sol} SOL*\n\n"
        "No memo needed.\n\n"
        "💡 You can send *slightly less* (up to `0.05` SOL difference) "
        "and it will still be accepted.\n\n"
        "Once payment is confirmed on-chain, you’ll receive a "
        "*private invite link* to the VIP group.",
        parse_mode="Markdown",
    )


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⛔ You are not allowed to use this command.")
        return

    stats = get_stats()
    text = (
        "📊 *Subscription Dashboard*\n\n"
        f"👥 Total users known: *{stats['total_users']}*\n"
        f"✅ Active subscriptions: *{stats['active']}*\n"
        f"⏰ Expiring in 24h: *{stats['expiring_24h']}*\n"
        f"💲 Pending payments: *{stats['pending']}*\n"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


bot_app.add_handler(CommandHandler("start", start))
bot_app.add_handler(CommandHandler("setwallet", setwallet))
bot_app.add_handler(CommandHandler("subscribe", subscribe))
bot_app.add_handler(CommandHandler("admin", admin_panel))
bot_app.add_handler(CallbackQueryHandler(plan_button))


# ===========================================================
# 🔁 MAINTENANCE JOB (REMINDERS + KICKS)
# ===========================================================


async def maintenance_job(context: ContextTypes.DEFAULT_TYPE):
    now = int(time.time())

    # Reminders
    soon = get_soon_expiring(hours=24)
    for user_id, expires_at in soon:
        hours_left = max(1, int((expires_at - now) / 3600))
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    f"⏰ Your VIP access expires in about *{hours_left} hour(s)*.\n\n"
                    "To renew, just use `/subscribe` again and send the payment.\n\n"
                    "Once the payment is confirmed, you’ll receive a fresh "
                    "invite link to the group.",
                ),
                parse_mode="Markdown",
            )
            mark_reminded(user_id)
            print(f"[JOB] sent reminder to {user_id}")
        except Exception as e:
            print(f"[JOB] failed to remind {user_id}: {e}")

    # Kicks for expired subs
    expired_users = get_expired()
    if expired_users:
        print(f"[JOB] kicking expired users: {expired_users}")
    for user_id in expired_users:
        try:
            await context.bot.ban_chat_member(GROUP_ID, user_id)
            await context.bot.unban_chat_member(GROUP_ID, user_id)
            clear_subscription(user_id)
        except Exception as e:
            print(f"[JOB] failed to kick {user_id}: {e}")


if bot_app.job_queue is not None:
    bot_app.job_queue.run_repeating(maintenance_job, interval=3600, first=60)


# ===========================================================
# 🌐 FASTAPI: TELEGRAM WEBHOOK
# ===========================================================


@api.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, bot_app.bot)
    await bot_app.process_update(update)
    return {"ok": True}


# ===========================================================
# 🌐 FASTAPI: HELIUS WEBHOOK
# ===========================================================


@api.post("/helius-webhook")
async def helius(request: Request):
    if HELIUS_SECRET:
        if request.headers.get("x-webhook-secret") != HELIUS_SECRET:
            print("[WEBHOOK] invalid secret header")
            return {"error": "unauthorized"}

    body = await request.json()
    print("[WEBHOOK] incoming:", body)

    # Helius enhanced webhooks currently send a list of tx objects
    if isinstance(body, list):
        txs = body
    else:
        txs = body.get("transactions", []) or body.get("events", []) or []

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

            print(
                f"[WEBHOOK] transfer to us from {from_acc} "
                f"amount={amount_lamports}"
            )
            user_id, plan_key, expires = complete_payment_from_transfer(
                from_acc, amount_lamports
            )
            if not user_id:
                continue

            # Build per-user invite link (member_limit=1)
            try:
                kwargs = {"chat_id": GROUP_ID, "member_limit": 1}
                if expires is not None:
                    kwargs["expire_date"] = expires

                link = await bot_app.bot.create_chat_invite_link(**kwargs)

                plan_label = PLANS[plan_key]["label"]
                await bot_app.bot.send_message(
                    chat_id=user_id,
                    text=(
                        "✅ *Payment confirmed!*\n\n"
                        f"Your *{plan_label}* subscription is now active.\n\n"
                        "Join the VIP group here (works once, just for you):\n"
                        f"{link.invite_link}"
                    ),
                    parse_mode="Markdown",
                )
                print(f"[PAY] sent invite link to user {user_id}")
            except Exception as e:
                print(f"[PAY] failed to send invite to {user_id}: {e}")

    return {"ok": True}


# ===========================================================
# 🚀 FASTAPI LIFECYCLE
# ===========================================================


@api.on_event("startup")
async def on_startup():
    init_db()
    await bot_app.initialize()
    await bot_app.start()
    print("[APP] Telegram application started (webhook mode)")


@api.on_event("shutdown")
async def on_shutdown():
    await bot_app.stop()
    await bot_app.shutdown()
    print("[APP] Telegram application stopped")


# ===========================================================
# ENTRYPOINT
# ===========================================================


def main():
    uvicorn.run(api, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
