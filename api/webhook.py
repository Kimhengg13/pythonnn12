"""
Money Calculator Telegram Bot — Vercel Webhook Edition
-------------------------------------------------------
Vercel serverless entry point: POST /api/webhook

Telegram pushes updates to this endpoint instead of the bot polling.
Data is stored in Vercel KV (Redis) instead of a local data.json file.

Environment variables required (set in Vercel dashboard):
  TELEGRAM_BOT_TOKEN       — from @BotFather
  KV_REST_API_URL          — auto-set when you link a Vercel KV store
  KV_REST_API_TOKEN        — auto-set when you link a Vercel KV store
"""

import json
import os
import re
import logging
from datetime import datetime
from http.server import BaseHTTPRequestHandler

import httpx  # lightweight HTTP client, no heavy dependencies

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Vercel KV (Redis-compatible REST API)
KV_URL = os.environ.get("KV_REST_API_URL", "")
KV_TOKEN = os.environ.get("KV_REST_API_TOKEN", "")

DEFAULT_USD_RATE = 4000  # 1 USD = 4000 KHR (Riel)

ABA_PAY_RE = re.compile(
    r"([$\u17db\uff04])\s*([\d,]+(?:\.\d{1,2})?)\s+paid\s+by\s+(.+?)\s+\(\*\d+\)",
    re.IGNORECASE,
)
SAFE_EXPR_RE = re.compile(r"^[0-9\.\+\-\*\/\%\(\)\s]+$")


# ---------- Vercel KV helpers ----------

def _kv_headers():
    return {"Authorization": f"Bearer {KV_TOKEN}"}


def kv_get(key: str) -> dict | None:
    """Get a JSON value from Vercel KV."""
    if not KV_URL or not KV_TOKEN:
        logger.warning("KV not configured, using in-memory fallback")
        return None
    try:
        r = httpx.get(f"{KV_URL}/get/{key}", headers=_kv_headers(), timeout=5)
        if r.status_code == 200:
            result = r.json().get("result")
            if result:
                return json.loads(result)
    except Exception as e:
        logger.error("KV get error: %s", e)
    return None


def kv_set(key: str, value: dict):
    """Set a JSON value in Vercel KV."""
    if not KV_URL or not KV_TOKEN:
        return
    try:
        httpx.post(
            f"{KV_URL}/set/{key}",
            headers=_kv_headers(),
            json=json.dumps(value),
            timeout=5,
        )
    except Exception as e:
        logger.error("KV set error: %s", e)


# ---------- Storage helpers ----------

def load_data() -> dict:
    data = kv_get("bot_data")
    return data if isinstance(data, dict) else {}


def save_data(data: dict):
    kv_set("bot_data", data)


def get_user(data: dict, user_id) -> dict:
    uid = str(user_id)
    if uid not in data:
        data[uid] = {"balance": 0.0, "history": []}
    return data[uid]


# ---------- Telegram API helpers ----------

def send_message(chat_id: int, text: str, parse_mode: str = "Markdown"):
    try:
        httpx.post(
            f"{TELEGRAM_API}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode},
            timeout=10,
        )
    except Exception as e:
        logger.error("sendMessage error: %s", e)


# ---------- ABA PayWay parser ----------

def get_usd_rate(data: dict, user_id) -> float:
    uid = str(user_id)
    if uid in data and "usd_rate" in data[uid]:
        return float(data[uid]["usd_rate"])
    return DEFAULT_USD_RATE


def parse_aba_payment(text: str):
    """Return (raw_amount, is_usd, payer_name) if ABA PayWay notification, else None."""
    m = ABA_PAY_RE.search(text)
    if not m:
        return None
    symbol = m.group(1)
    raw_str = m.group(2)
    is_usd = symbol == "$"
    amount_str = raw_str.replace(",", "")
    try:
        amount = float(amount_str)
    except ValueError:
        return None
    payer = m.group(3).strip().title()
    return amount, is_usd, payer


# ---------- Safe math evaluation ----------

def safe_eval(expr: str) -> float:
    expr = expr.strip()
    if not expr:
        raise ValueError("Empty expression.")
    if not SAFE_EXPR_RE.match(expr):
        raise ValueError("Expression contains disallowed characters.")
    try:
        code = compile(expr, "<expr>", "eval")
    except SyntaxError:
        raise ValueError("Invalid expression syntax.")
    for name in code.co_names:
        raise ValueError(f"Use of '{name}' is not allowed.")
    return eval(code, {"__builtins__": {}}, {})


def fmt_money(x: float) -> str:
    return f"{x:,.2f}"


# ---------- Command handlers ----------

def handle_start(chat_id: int):
    send_message(
        chat_id,
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
        "/setrate <rate> - set USD→KHR exchange rate\n"
        "/reset - reset your balance and history\n"
        "/help - show this message",
    )


def handle_calc(chat_id: int, args: list[str]):
    expr = " ".join(args)
    if not expr:
        send_message(chat_id, "Usage: /calc 12.5 + 30 * 2")
        return
    _do_calc(chat_id, expr)


def _do_calc(chat_id: int, expr: str):
    try:
        result = safe_eval(expr)
        send_message(chat_id, f"🧮 `{expr}` = *{fmt_money(result)}*")
    except ZeroDivisionError:
        send_message(chat_id, "⚠️ Division by zero.")
    except ValueError as e:
        send_message(chat_id, f"⚠️ {e}")
    except Exception:
        send_message(chat_id, "⚠️ Couldn't evaluate that expression.")


def handle_add(chat_id: int, user_id: int, args: list[str]):
    if not args:
        send_message(
            chat_id,
            "Usage: /add <amount> [note]\n"
            "Positive = income, negative = expense.\n"
            "Example: /add -12.50 coffee",
        )
        return
    try:
        amount = safe_eval(args[0])
    except (ValueError, ZeroDivisionError):
        send_message(chat_id, "⚠️ Couldn't parse the amount. Use a plain number like -12.5 or 100.")
        return
    note = " ".join(args[1:]) if len(args) > 1 else ""
    data = load_data()
    user = get_user(data, user_id)
    user["balance"] += amount
    user["history"].append({
        "amount": amount,
        "note": note,
        "time": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    })
    user["history"] = user["history"][-100:]
    save_data(data)
    sign = "➕" if amount >= 0 else "➖"
    send_message(
        chat_id,
        f"{sign} {fmt_money(abs(amount))} recorded"
        + (f" ({note})" if note else "")
        + f"\n💰 New balance: *{fmt_money(user['balance'])}*",
    )


def handle_balance(chat_id: int, user_id: int):
    data = load_data()
    user = get_user(data, user_id)
    send_message(chat_id, f"💰 Your balance: *{fmt_money(user['balance'])}*")


def handle_history(chat_id: int, user_id: int):
    data = load_data()
    user = get_user(data, user_id)
    if not user["history"]:
        send_message(chat_id, "No transactions yet. Use /add <amount> [note].")
        return
    lines = []
    for tx in user["history"][-10:][::-1]:
        sign = "+" if tx["amount"] >= 0 else ""
        note = f" - {tx['note']}" if tx["note"] else ""
        lines.append(f"{tx['time'][:16].replace('T', ' ')}  {sign}{fmt_money(tx['amount'])}{note}")
    send_message(chat_id, "🧾 Last transactions:\n" + "\n".join(lines), parse_mode="")


def handle_reset(chat_id: int, user_id: int):
    data = load_data()
    data[str(user_id)] = {"balance": 0.0, "history": []}
    save_data(data)
    send_message(chat_id, "🔄 Your balance and history have been reset.")


def handle_today(chat_id: int, user_id: int):
    data = load_data()
    user = get_user(data, user_id)
    today = datetime.utcnow().strftime("%Y-%m-%d")
    aba_today = [
        tx for tx in user["history"]
        if tx["time"].startswith(today) and tx["note"].startswith("ABA:")
    ]
    if not aba_today:
        send_message(chat_id, "No ABA payments recorded today yet.")
        return
    total = sum(tx["amount"] for tx in aba_today)
    lines = [f"  {tx['note'][5:]} → *៛{tx['amount']:,.0f}*" for tx in aba_today]
    rate = get_usd_rate(data, user_id)
    send_message(
        chat_id,
        f"🏦 *ABA Payments Today ({today} UTC)*\n"
        + "\n".join(lines)
        + f"\n\n💰 *Total: ៛{total:,.0f} Riel*"
        + f"\n💵 *(≈ ${total / rate:,.2f} USD @ {int(rate)} rate)*",
    )


def handle_setrate(chat_id: int, user_id: int, args: list[str]):
    if not args or not args[0].isdigit():
        data = load_data()
        current = get_usd_rate(data, user_id)
        send_message(
            chat_id,
            f"💱 Current rate: *1 USD = {int(current)} Riel*\nTo change: /setrate 4100",
        )
        return
    new_rate = int(args[0])
    if new_rate < 100 or new_rate > 100000:
        send_message(chat_id, "⚠️ Rate must be between 100 and 100,000.")
        return
    data = load_data()
    user = get_user(data, user_id)
    user["usd_rate"] = new_rate
    save_data(data)
    send_message(chat_id, f"✅ Exchange rate updated: *1 USD = {new_rate} Riel*")


def handle_plain_text(chat_id: int, user_id: int, text: str):
    logger.info("Received text (repr): %r", text[:200])
    parsed = parse_aba_payment(text)
    if parsed:
        raw_amount, is_usd, payer = parsed
        data = load_data()
        user = get_user(data, user_id)
        rate = get_usd_rate(data, user_id)
        if is_usd:
            khr_amount = raw_amount * rate
            currency_note = f"USD ${raw_amount:,.2f} × {int(rate)} = ៛{khr_amount:,.0f}"
        else:
            khr_amount = raw_amount
            currency_note = f"៛{khr_amount:,.0f} Riel"
        user["balance"] += khr_amount
        user["history"].append({
            "amount": khr_amount,
            "note": f"ABA: {payer}" + (" [USD]" if is_usd else ""),
            "time": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        })
        user["history"] = user["history"][-100:]
        save_data(data)
        send_message(
            chat_id,
            f"🏦 *ABA Payment received!*\n"
            f"👤 Payer: {payer}\n"
            f"➕ Amount: *{currency_note}*\n"
            f"💰 New balance: *៛{user['balance']:,.0f} Riel*",
        )
        return
    if SAFE_EXPR_RE.match(text) and any(c.isdigit() for c in text):
        _do_calc(chat_id, text)
    else:
        send_message(
            chat_id,
            "I didn't understand that. Try /calc 2+2, /add 50 groceries, or forward an ABA PayWay notification!",
        )


# ---------- Update dispatcher ----------

def dispatch(update: dict):
    message = update.get("message") or update.get("edited_message")
    if not message:
        return
    chat_id = message["chat"]["id"]
    user_id = message["from"]["id"]
    text = (message.get("text") or "").strip()
    if not text:
        return

    if text.startswith("/"):
        parts = text.split()
        # Strip @BotName suffix (e.g. /start@MyBot)
        command = parts[0].split("@")[0].lstrip("/")
        args = parts[1:]
        if command in ("start", "help"):
            handle_start(chat_id)
        elif command == "calc":
            handle_calc(chat_id, args)
        elif command == "add":
            handle_add(chat_id, user_id, args)
        elif command == "balance":
            handle_balance(chat_id, user_id)
        elif command == "history":
            handle_history(chat_id, user_id)
        elif command == "reset":
            handle_reset(chat_id, user_id)
        elif command == "today":
            handle_today(chat_id, user_id)
        elif command == "setrate":
            handle_setrate(chat_id, user_id, args)
    else:
        handle_plain_text(chat_id, user_id, text)


# ---------- Vercel handler ----------

class handler(BaseHTTPRequestHandler):
    """Vercel expects a class named 'handler' with do_GET / do_POST methods."""

    def log_message(self, format, *args):
        logger.info(format, *args)

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot webhook is active.")

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        try:
            update = json.loads(body)
            dispatch(update)
        except Exception as e:
            logger.error("Error processing update: %s", e)
        # Always respond 200 — Telegram will retry on non-200
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
