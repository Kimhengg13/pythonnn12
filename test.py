"""
Money Calculator Telegram Bot
------------------------------
Features:
  /calc <expression>   -> safely evaluate a math expression (+, -, *, /, %, parentheses)
  You can also just type a math expression directly (no command needed).
  /add <amount> <note>  -> add income (positive) or expense (negative) to your running balance
  /balance              -> show current balance
  /history               -> show last 10 transactions
  /reset                 -> reset your balance and history
  /start, /help           -> instructions

Storage: simple per-user JSON file (data.json) - no external DB needed.
"""

import json
import os
import re
import logging
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()  # Load .env before reading BOT_TOKEN


# pyrefly: ignore [missing-import]
from telegram import Update
# pyrefly: ignore [missing-import]
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "PUT_YOUR_TOKEN_HERE")
DATA_FILE = os.path.join(os.path.dirname(__file__), "data.json")

# ---------- Storage helpers ----------

def load_data():
    if not os.path.exists(DATA_FILE):
        return {}
    with open(DATA_FILE, "r") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}


def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)


def get_user(data, user_id):
    uid = str(user_id)
    if uid not in data:
        data[uid] = {"balance": 0.0, "history": []}
    return data[uid]


# ---------- ABA PayWay parser ----------

DEFAULT_USD_RATE = 4000  # 1 USD = 4000 KHR (Riel)

# Matches ABA PayWay notifications:
#   ៛200,000 paid by SROY MENGLIM (*544) ...   → KHR (Riel)
#   $3.00 paid by NAME (*048) ...               → USD
# Captures the currency symbol so we can tell them apart.
ABA_PAY_RE = re.compile(
    r"([$\u17db\uff04])\s*([\d,]+(?:\.\d{1,2})?)\s+paid\s+by\s+(.+?)\s+\(\*\d+\)",
    re.IGNORECASE,
)



def get_usd_rate(data: dict, user_id) -> float:
    """Get the user's configured USD→KHR exchange rate."""
    uid = str(user_id)
    if uid in data and "usd_rate" in data[uid]:
        return float(data[uid]["usd_rate"])
    return DEFAULT_USD_RATE


def parse_aba_payment(text: str):
    """Return (raw_amount, is_usd, payer_name) if text is an ABA PayWay notification, else None.

    Detection rule:
      - ៛ symbol → KHR (Riel), keep as-is
      - $ symbol  → USD, will be converted to KHR
    """
    m = ABA_PAY_RE.search(text)
    if not m:
        return None
    symbol = m.group(1)          # ៛ or $
    raw_str = m.group(2)         # e.g. "200,000" or "3.00"
    is_usd = symbol == "$"       # ៛ = Riel, $ = USD
    amount_str = raw_str.replace(",", "")
    try:
        amount = float(amount_str)
    except ValueError:
        return None
    payer = m.group(3).strip().title()
    return amount, is_usd, payer




# ---------- Safe math evaluation ----------

# Only allow digits, whitespace, and the operators + - * / % ( ) .
SAFE_EXPR_RE = re.compile(r"^[0-9\.\+\-\*\/\%\(\)\s]+$")


def safe_eval(expr: str) -> float:
    expr = expr.strip()
    if not expr:
        raise ValueError("Empty expression.")
    if not SAFE_EXPR_RE.match(expr):
        raise ValueError("Expression contains disallowed characters.")
    # Compile then evaluate using eval with no builtins for safety.
    try:
        code = compile(expr, "<expr>", "eval")
    except SyntaxError:
        raise ValueError("Invalid expression syntax.")
    for name in code.co_names:
        raise ValueError(f"Use of '{name}' is not allowed.")
    result = eval(code, {"__builtins__": {}}, {})
    return result


def fmt_money(x: float) -> str:
    return f"{x:,.2f}"


# ---------- Command handlers ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💰 *Money Calculator Bot*\n\n"
        "Send me a math expression and I'll calculate it, e.g.\n"
        "`12.50 + 30 - 4*2`\n\n"
        "🏦 *ABA PayWay:* Forward payment notifications here and I'll auto-add them to your balance!\n\n"
        "*Commands:*\n"
        "/calc <expr> - calculate an expression\n"
        "/add <amount> [note] - add income (+) or expense (-) to your balance\n"
        "/balance - show your current balance\n"
        "/today - sum of ABA payments received today\n"
        "/history - show your last 10 transactions\n"
        "/reset - reset your balance and history\n"
        "/help - show this message",
        parse_mode="Markdown",
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


async def calc_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    expr = " ".join(context.args)
    if not expr:
        await update.message.reply_text("Usage: /calc 12.5 + 30 * 2")
        return
    await _do_calc(update, expr)


async def plain_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle plain messages: ABA PayWay notifications or math expressions."""
    text = update.message.text.strip()

    # Debug: log raw text so we can see the exact format ABA sends
    logger.info("Received text (repr): %r", text[:200])

    # 1. Try ABA PayWay forwarded payment
    parsed = parse_aba_payment(text)
    if parsed:
        raw_amount, is_usd, payer = parsed
        data = load_data()
        user = get_user(data, update.effective_user.id)
        rate = get_usd_rate(data, update.effective_user.id)

        if is_usd:
            khr_amount = raw_amount * rate
            currency_note = f"USD ${raw_amount:,.2f} × {int(rate)} = ៛{khr_amount:,.0f}"
        else:
            khr_amount = raw_amount
            currency_note = f"៛{khr_amount:,.0f} Riel"

        user["balance"] += khr_amount
        user["history"].append(
            {
                "amount": khr_amount,
                "note": f"ABA: {payer}" + (" [USD]" if is_usd else ""),
                "time": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            }
        )
        user["history"] = user["history"][-100:]
        save_data(data)
        await update.message.reply_text(
            f"🏦 *ABA Payment received!*\n"
            f"👤 Payer: {payer}\n"
            f"➕ Amount: *{currency_note}*\n"
            f"💰 New balance: *៛{user['balance']:,.0f} Riel*",
            parse_mode="Markdown",
        )
        return

    # 2. Try math expression
    if SAFE_EXPR_RE.match(text) and any(c.isdigit() for c in text):
        await _do_calc(update, text)
    else:
        await update.message.reply_text(
            "I didn't understand that. Try /calc 2+2, /add 50 groceries, or forward an ABA PayWay notification!"
        )


async def _do_calc(update: Update, expr: str):
    try:
        result = safe_eval(expr)
        await update.message.reply_text(f"🧮 `{expr}` = *{fmt_money(result)}*", parse_mode="Markdown")
    except ZeroDivisionError:
        await update.message.reply_text("⚠️ Division by zero.")
    except ValueError as e:
        await update.message.reply_text(f"⚠️ {e}")
    except Exception:
        await update.message.reply_text("⚠️ Couldn't evaluate that expression.")


async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Usage: /add <amount> [note]\n"
            "Positive = income, negative = expense.\n"
            "Example: /add -12.50 coffee"
        )
        return

    amount_str = context.args[0]
    note = " ".join(context.args[1:]) if len(context.args) > 1 else ""

    try:
        amount = safe_eval(amount_str)
    except (ValueError, ZeroDivisionError):
        await update.message.reply_text("⚠️ Couldn't parse the amount. Use a plain number like -12.5 or 100.")
        return

    data = load_data()
    user = get_user(data, update.effective_user.id)
    user["balance"] += amount
    user["history"].append(
        {
            "amount": amount,
            "note": note,
            "time": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        }
    )
    user["history"] = user["history"][-100:]  # keep last 100
    save_data(data)

    sign = "➕" if amount >= 0 else "➖"
    await update.message.reply_text(
        f"{sign} {fmt_money(abs(amount))} recorded"
        + (f" ({note})" if note else "")
        + f"\n💰 New balance: *{fmt_money(user['balance'])}*",
        parse_mode="Markdown",
    )


async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    user = get_user(data, update.effective_user.id)
    await update.message.reply_text(f"💰 Your balance: *{fmt_money(user['balance'])}*", parse_mode="Markdown")


async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    user = get_user(data, update.effective_user.id)
    if not user["history"]:
        await update.message.reply_text("No transactions yet. Use /add <amount> [note].")
        return
    lines = []
    for tx in user["history"][-10:][::-1]:
        sign = "+" if tx["amount"] >= 0 else ""
        note = f" - {tx['note']}" if tx["note"] else ""
        lines.append(f"{tx['time'][:16].replace('T',' ')}  {sign}{fmt_money(tx['amount'])}{note}")
    await update.message.reply_text("🧾 Last transactions:\n" + "\n".join(lines))


async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    data[str(update.effective_user.id)] = {"balance": 0.0, "history": []}
    save_data(data)
    await update.message.reply_text("🔄 Your balance and history have been reset.")


async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sum all ABA payments recorded today."""
    data = load_data()
    user = get_user(data, update.effective_user.id)
    today = datetime.utcnow().strftime("%Y-%m-%d")
    aba_today = [
        tx for tx in user["history"]
        if tx["time"].startswith(today) and tx["note"].startswith("ABA:")
    ]
    if not aba_today:
        await update.message.reply_text("No ABA payments recorded today yet.")
        return
    total = sum(tx["amount"] for tx in aba_today)
    lines = []
    for tx in aba_today:
        label = tx["note"][5:]  # strip "ABA: "
        lines.append(f"  {label} → *៛{tx['amount']:,.0f}*")
    rate = get_usd_rate(data, update.effective_user.id)
    await update.message.reply_text(
        f"🏦 *ABA Payments Today ({today} UTC)*\n"
        + "\n".join(lines)
        + f"\n\n💰 *Total: ៛{total:,.0f} Riel*"
        + f"\n💵 *(≈ ${total/rate:,.2f} USD @ {int(rate)} rate)*",
        parse_mode="Markdown",
    )


async def setrate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set USD to KHR exchange rate. Usage: /setrate 4100"""
    if not context.args or not context.args[0].isdigit():
        data = load_data()
        current = get_usd_rate(data, update.effective_user.id)
        await update.message.reply_text(
            f"💱 Current rate: *1 USD = {int(current)} Riel*\n"
            f"To change: /setrate 4100",
            parse_mode="Markdown",
        )
        return
    new_rate = int(context.args[0])
    if new_rate < 100 or new_rate > 100000:
        await update.message.reply_text("⚠️ Rate must be between 100 and 100,000.")
        return
    data = load_data()
    user = get_user(data, update.effective_user.id)
    user["usd_rate"] = new_rate
    save_data(data)
    await update.message.reply_text(
        f"✅ Exchange rate updated: *1 USD = {new_rate} Riel*",
        parse_mode="Markdown",
    )


def main():
    if BOT_TOKEN == "PUT_YOUR_TOKEN_HERE" or not BOT_TOKEN:
        print("ERROR: Set your bot token via the TELEGRAM_BOT_TOKEN environment variable.")
        return

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("calc", calc_cmd))
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CommandHandler("setrate", setrate_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, plain_text))

    print("Bot is starting (polling)...")
    app.run_polling()


if __name__ == "__main__":
    main()