import os
import sqlite3
import re
import threading
from datetime import date, datetime, timedelta, time
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from flask import Flask
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TIMEZONE = os.getenv("TIMEZONE", "Asia/Tehran")
ALERT_HOUR = int(os.getenv("ALERT_HOUR", "9"))
ALERT_MINUTE = int(os.getenv("ALERT_MINUTE", "0"))
DB_PATH = os.getenv("DB_PATH", "expiration_bot_pro.db")

if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is missing in .env")

TZ = ZoneInfo(TIMEZONE)

MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4,
    "MAY": 5, "JUN": 6, "JUL": 7, "AUG": 8,
    "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

# =========================================================
# FLASK APP FOR HEALTH CHECK
# =========================================================
flask_app = Flask(__name__)

@flask_app.route('/')
@flask_app.route('/health')
def health_check():
    return "Bot is running!", 200

# =========================================================
# DATABASE
# =========================================================

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS registrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            registered_date TEXT NOT NULL,
            display_name TEXT NOT NULL,
            ndog_expiration TEXT,
            nwog_5_expiration TEXT,
            nwog_7_expiration TEXT,
            nwog_8_expiration TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(chat_id, kind, registered_date)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            registration_id INTEGER NOT NULL,
            alert_key TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            UNIQUE(registration_id, alert_key)
        )
    """)
    conn.commit()
    conn.close()

# =========================================================
# DATE LOGIC
# =========================================================

def is_trading_day(d: date) -> bool:
    return d.weekday() < 5

def next_trading_day(d: date) -> date:
    d += timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d

def add_trading_days_after(start: date, count: int) -> date:
    current = start
    done = 0
    while done < count:
        current += timedelta(days=1)
        if is_trading_day(current):
            done += 1
    return current

def next_monday_after(d: date) -> date:
    return d + timedelta(days=1)

def weekly_expiration(sunday: date, weeks: int) -> date:
    monday = next_monday_after(sunday)
    return monday + timedelta(days=(weeks - 1) * 7 + 4)

def short_date(d: date) -> str:
    return d.strftime("%d %b").upper()

def long_date(d: date) -> str:
    return d.strftime("%A, %d %B %Y")

# =========================================================
# COUNTDOWN
# =========================================================

def trading_days_remaining(expiration: date, today: date) -> int:
    if today >= expiration:
        return 0
    current = today
    count = 0
    while current < expiration:
        current += timedelta(days=1)
        if is_trading_day(current):
            count += 1
    return count

def calendar_days_remaining(expiration: date, today: date) -> int:
    if today >= expiration:
        return 0
    return (expiration - today).days

def expiration_status(expiration: date, today: date):
    if today > expiration:
        return "🔴 EXPIRED"
    if today == expiration:
        return "🔴 EXPIRATION DAY"
    trading_left = trading_days_remaining(expiration, today)
    if trading_left <= 1:
        return "🟡 EXPIRING SOON"
    return "🟢 ACTIVE"

def countdown_text(expiration: date, today: date):
    if today > expiration:
        return "❌ NO LONGER VALID"
    if today == expiration:
        return "🔥 EXPIRES TODAY"
    trading_left = trading_days_remaining(expiration, today)
    calendar_left = calendar_days_remaining(expiration, today)
    return (
        f"⏳ {trading_left} TRADING DAY(S) LEFT\n"
        f"📅 {calendar_left} CALENDAR DAY(S) LEFT"
    )

# =========================================================
# INPUT PARSER
# =========================================================

def parse_trade_input(text: str, expected_kind: str):
    raw = text.strip().upper()
    cleaned = re.sub(r"^(NDOG|NWOG)\s+", "", raw).strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", cleaned):
            return datetime.strptime(cleaned, "%Y-%m-%d").date()
    except ValueError:
        return None
    m = re.fullmatch(r"(\d{1,2})\s+([A-Z]{3})(?:\s+(\d{4}))?", cleaned)
    if not m:
        return None
    day = int(m.group(1))
    month_text = m.group(2)
    year = int(m.group(3)) if m.group(3) else datetime.now(TZ).year
    if month_text not in MONTHS:
        return None
    try:
        return date(year, MONTHS[month_text], day)
    except ValueError:
        return None

# =========================================================
# REGISTRATION
# =========================================================

def save_registration(chat_id: int, kind: str, registered: date):
    conn = get_db()
    if kind == "NDOG":
        expiration = add_trading_days_after(registered, 5)
        conn.execute("""
            INSERT INTO registrations
            (chat_id, kind, registered_date, display_name, ndog_expiration, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, kind, registered_date)
            DO UPDATE SET ndog_expiration=excluded.ndog_expiration, display_name=excluded.display_name
        """, (
            chat_id, kind, registered.isoformat(),
            f"NDOG {short_date(registered)}",
            expiration.isoformat(),
            datetime.now(TZ).isoformat(),
        ))
    else:
        e5 = weekly_expiration(registered, 5)
        e7 = weekly_expiration(registered, 7)
        e8 = weekly_expiration(registered, 8)
        conn.execute("""
            INSERT INTO registrations
            (chat_id, kind, registered_date, display_name, nwog_5_expiration, nwog_7_expiration, nwog_8_expiration, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, kind, registered_date)
            DO UPDATE SET
                nwog_5_expiration=excluded.nwog_5_expiration,
                nwog_7_expiration=excluded.nwog_7_expiration,
                nwog_8_expiration=excluded.nwog_8_expiration,
                display_name=excluded.display_name
        """, (
            chat_id, kind, registered.isoformat(),
            f"NWOG {short_date(registered)}",
            e5.isoformat(), e7.isoformat(), e8.isoformat(),
            datetime.now(TZ).isoformat(),
        ))
    conn.commit()
    conn.close()

# =========================================================
# REGISTRATION STATUS
# =========================================================

def is_registration_active(row):
    today = datetime.now(TZ).date()
    if row["kind"] == "NDOG":
        expiration = date.fromisoformat(row["ndog_expiration"])
        return expiration >= today
    expirations = [
        date.fromisoformat(row["nwog_5_expiration"]),
        date.fromisoformat(row["nwog_7_expiration"]),
        date.fromisoformat(row["nwog_8_expiration"]),
    ]
    return any(expiration >= today for expiration in expirations)

def active_rows(chat_id):
    conn = get_db()
    rows = conn.execute("""
        SELECT * FROM registrations WHERE chat_id=? ORDER BY registered_date DESC
    """, (chat_id,)).fetchall()
    conn.close()
    return [row for row in rows if is_registration_active(row)]

def history_rows(chat_id):
    conn = get_db()
    rows = conn.execute("""
        SELECT * FROM registrations WHERE chat_id=? ORDER BY registered_date DESC
    """, (chat_id,)).fetchall()
    conn.close()
    return [row for row in rows if not is_registration_active(row)]

# =========================================================
# DASHBOARD
# =========================================================

def dashboard_keyboard():
    return ReplyKeyboardMarkup([
        ["📅 NDOG", "📆 NWOG"],
        ["🟢 ACTIVE", "📜 HISTORY"],
        ["⚙️ SETTINGS"],
    ], resize_keyboard=True, is_persistent=True)

def dashboard_text(chat_id):
    active = active_rows(chat_id)
    history = history_rows(chat_id)
    return (
        "📊 TRADING EXPIRATION\n"
        "      TRACKER PRO\n\n"
        f"🟢 Active: {len(active)}\n"
        f"🔴 Expired: {len(history)}\n\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "        ➕ REGISTER\n\n"
        "Track your NDOG / NWOG expirations\n"
        "with automatic countdown & alerts."
    )

async def send_dashboard(update: Update, edit=False):
    chat_id = update.effective_chat.id
    text = dashboard_text(chat_id)
    if update.message:
        await update.message.reply_text(text, reply_markup=dashboard_keyboard())
    elif update.callback_query:
        await update.callback_query.message.reply_text(text, reply_markup=dashboard_keyboard())

# =========================================================
# REGISTER MENU
# =========================================================

def register_keyboard():
    return ReplyKeyboardMarkup([
        ["📅 NDOG", "📆 NWOG"],
        ["🏠 HOME"],
    ], resize_keyboard=True, is_persistent=True)

async def register_menu(update):
    await update.message.reply_text(
        "➕ REGISTER NEW EXPIRATION\n\nChoose the expiration type:",
        reply_markup=register_keyboard()
    )

# =========================================================
# INPUT MODE
# =========================================================

async def ask_ndog(update, context):
    context.user_data["mode"] = "NDOG"
    await update.message.reply_text(
        "📅 REGISTER NDOG\n\nSend the registration date.\n\nExamples:\n\nNDOG 18 AUG\nNDOG 18 AUG 2026\n18 AUG\n2026-08-18\n\n━━━━━━━━━━━━━━━━━━\n💡 NDOG expires after 5 trading days.",
        reply_markup=ReplyKeyboardRemove()
    )

async def ask_nwog(update, context):
    context.user_data["mode"] = "NWOG"
    await update.message.reply_text(
        "📆 REGISTER NWOG\n\nSend the Sunday registration date.\n\nExamples:\n\nNWOG 16 AUG\nNWOG 16 AUG 2026\n16 AUG\n2026-08-16\n\n━━━━━━━━━━━━━━━━━━\n💡 NWOG tracks 5, 7 and 8 weeks.",
        reply_markup=ReplyKeyboardRemove()
    )

# =========================================================
# RENDER NDOG
# =========================================================

def render_ndog(row):
    today = datetime.now(TZ).date()
    registered = date.fromisoformat(row["registered_date"])
    expiration = date.fromisoformat(row["ndog_expiration"])
    status = expiration_status(expiration, today)
    countdown = countdown_text(expiration, today)
    start = next_trading_day(registered)
    return (
        f"📅 NDOG {short_date(registered)}\n\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"{status}\n\n"
        f"REGISTERED\n{short_date(registered)} {registered.year}\n\n"
        f"START\n{short_date(start)} {start.year}\n\n"
        f"EXPIRATION\n{short_date(expiration)} {expiration.year}\n\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"{countdown}"
    )

# =========================================================
# RENDER NWOG
# =========================================================

def render_nwog(row):
    today = datetime.now(TZ).date()
    registered = date.fromisoformat(row["registered_date"])
    e5 = date.fromisoformat(row["nwog_5_expiration"])
    e7 = date.fromisoformat(row["nwog_7_expiration"])
    e8 = date.fromisoformat(row["nwog_8_expiration"])
    monday = next_monday_after(registered)
    def block(label, expiration):
        status = expiration_status(expiration, today)
        countdown = countdown_text(expiration, today)
        return f"{label}\n📅 {short_date(expiration)} {expiration.year}\n{status}\n{countdown}"
    return (
        f"📆 NWOG {short_date(registered)}\n\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"🟢 WEEK 1 START\n{short_date(monday)} {monday.year}\n\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"{block('5 WEEKS', e5)}\n\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"{block('7 WEEKS', e7)}\n\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"{block('8 WEEKS', e8)}\n\n"
        f"━━━━━━━━━━━━━━━━━━"
    )

# =========================================================
# ACTIVE UI
# =========================================================

def active_keyboard():
    return ReplyKeyboardMarkup([
        ["🏠 HOME"],
    ], resize_keyboard=True, is_persistent=True)

def history_keyboard():
    return ReplyKeyboardMarkup([
        ["🏠 HOME"],
    ], resize_keyboard=True, is_persistent=True)

async def show_active(update):
    rows = active_rows(update.effective_chat.id)
    if not rows:
        await update.message.reply_text(
            "🟢 ACTIVE\n\n━━━━━━━━━━━━━━━━━━\n\nNo active registrations.\n\nRegister an NDOG or NWOG to get started.",
            reply_markup=active_keyboard()
        )
        return
    lines = ["🟢 ACTIVE EXPIRATIONS", "", "━━━━━━━━━━━━━━━━━━", ""]
    for row in rows:
        icon = "📅" if row["kind"] == "NDOG" else "📆"
        lines.append(f"{icon} {row['display_name']}")
    lines.extend(["", "━━━━━━━━━━━━━━━━━━", "Tap REGISTER to add another expiration."])
    await update.message.reply_text("\n".join(lines), reply_markup=active_keyboard())

async def show_history(update):
    rows = history_rows(update.effective_chat.id)
    if not rows:
        await update.message.reply_text(
            "📜 HISTORY\n\n━━━━━━━━━━━━━━━━━━\n\nNo fully expired registrations.",
            reply_markup=history_keyboard()
        )
        return
    lines = ["📜 HISTORY", "", "━━━━━━━━━━━━━━━━━━", ""]
    for row in rows:
        icon = "📅" if row["kind"] == "NDOG" else "📆"
        lines.append(f"🔴 {icon} {row['display_name']}")
    await update.message.reply_text("\n".join(lines), reply_markup=history_keyboard())

# =========================================================
# SETTINGS
# =========================================================

async def show_settings(update):
    await update.message.reply_text(
        f"⚙️ SETTINGS\n\n━━━━━━━━━━━━━━━━━━\n\n🌍 Timezone\n{TIMEZONE}\n\n⏰ Alert Time\n{ALERT_HOUR:02d}:{ALERT_MINUTE:02d}\n\n📈 Trading Days\nMonday — Friday\n\n📅 Weekend\nSaturday — Sunday\n\n🔔 Alerts\n• 1 Trading Day Left\n• Expiration Day\n\n━━━━━━━━━━━━━━━━━━",
        reply_markup=dashboard_keyboard()
    )

# =========================================================
# START
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        dashboard_text(update.effective_chat.id),
        reply_markup=dashboard_keyboard()
    )

# =========================================================
# MESSAGE HANDLER
# =========================================================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    chat_id = update.effective_chat.id
    mode = context.user_data.get("mode")

    if not mode:
        if text in ("🏠 HOME", "/start"):
            context.user_data.clear()
            await update.message.reply_text(dashboard_text(chat_id), reply_markup=dashboard_keyboard())
            return
        if text == "📅 NDOG":
            await ask_ndog(update, context)
            return
        if text == "📆 NWOG":
            await ask_nwog(update, context)
            return
        if text == "🟢 ACTIVE":
            await show_active(update)
            return
        if text == "📜 HISTORY":
            await show_history(update)
            return
        if text == "⚙️ SETTINGS":
            await show_settings(update)
            return
        await update.message.reply_text("🤖 Please use the buttons below.", reply_markup=dashboard_keyboard())
        return

    registered = parse_trade_input(text, mode)
    if not registered:
        await update.message.reply_text(
            "❌ INVALID DATE\n\nUse one of these formats:\n\nNDOG 18 AUG\nNDOG 18 AUG 2026\n18 AUG\n2026-08-18"
        )
        return

    if mode == "NDOG":
        save_registration(chat_id, "NDOG", registered)
        conn = get_db()
        row = conn.execute("""
            SELECT * FROM registrations WHERE chat_id=? AND kind='NDOG' AND registered_date=?
        """, (chat_id, registered.isoformat())).fetchone()
        conn.close()
        context.user_data.clear()
        await update.message.reply_text("✅ NDOG REGISTERED\n\n" + render_ndog(row), reply_markup=dashboard_keyboard())
        return

    if registered.weekday() != 6:
        await update.message.reply_text("❌ NWOG ACCEPTS SUNDAYS ONLY\n\nExample:\nNWOG 16 AUG")
        return

    save_registration(chat_id, "NWOG", registered)
    conn = get_db()
    row = conn.execute("""
        SELECT * FROM registrations WHERE chat_id=? AND kind='NWOG' AND registered_date=?
    """, (chat_id, registered.isoformat())).fetchone()
    conn.close()
    context.user_data.clear()
    await update.message.reply_text("✅ NWOG REGISTERED\n\n" + render_nwog(row), reply_markup=dashboard_keyboard())

# =========================================================
# ALERT DATABASE
# =========================================================

def alert_was_sent(registration_id, alert_key):
    conn = get_db()
    row = conn.execute("SELECT 1 FROM alerts WHERE registration_id=? AND alert_key=?", (registration_id, alert_key)).fetchone()
    conn.close()
    return row is not None

def mark_alert_sent(registration_id, alert_key):
    conn = get_db()
    conn.execute("INSERT OR IGNORE INTO alerts (registration_id, alert_key, sent_at) VALUES (?, ?, ?)",
                 (registration_id, alert_key, datetime.now(TZ).isoformat()))
    conn.commit()
    conn.close()

# =========================================================
# SEND ALERT
# =========================================================

async def send_alert(context, chat_id, message):
    try:
        await context.bot.send_message(chat_id=chat_id, text=message)
        return True
    except Exception as e:
        print("Alert send error:", e)
        return False

# =========================================================
# ALERT LEVEL
# =========================================================

def alert_level(expiration: date, today: date):
    if today > expiration:
        return None
    if today == expiration:
        return "EXPIRATION_DAY"
    remaining = trading_days_remaining(expiration, today)
    if remaining == 1:
        return "ONE_TRADING_DAY"
    return None

# =========================================================
# ALERT MESSAGE
# =========================================================

def build_alert_message(name, label, registered, expiration, level):
    if level == "ONE_TRADING_DAY":
        title = "⚠️ EXPIRATION ALERT"
        status = "🟠 1 TRADING DAY LEFT"
    else:
        title = "🔴 EXPIRATION DAY"
        status = "🔴 EXPIRED TODAY"
    calendar_left = calendar_days_remaining(expiration, datetime.now(TZ).date())
    return (
        f"{title}\n\n{name}\n\n{label}\n\nExpiration:\n{short_date(expiration)} {expiration.year}\n\nStatus:\n{status}\n\n📅 Calendar Days Left: {calendar_left}\n\nRegistered:\n{short_date(registered)} {registered.year}"
    )

# =========================================================
# AUTOMATIC ALERT CHECK
# =========================================================

async def check_expirations(context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now(TZ).date()
    conn = get_db()
    rows = conn.execute("SELECT * FROM registrations").fetchall()
    conn.close()
    for row in rows:
        registration_id = row["id"]
        name = row["display_name"]
        chat_id = row["chat_id"]
        registered = date.fromisoformat(row["registered_date"])
        expirations = []
        if row["kind"] == "NDOG":
            expirations.append(("NDOG", row["ndog_expiration"]))
        else:
            expirations.extend([
                ("5 WEEKS", row["nwog_5_expiration"]),
                ("7 WEEKS", row["nwog_7_expiration"]),
                ("8 WEEKS", row["nwog_8_expiration"]),
            ])
        for label, expiration_text in expirations:
            expiration = date.fromisoformat(expiration_text)
            level = alert_level(expiration, today)
            if not level:
                continue
            alert_key = f"{label}_{level}_{expiration.isoformat()}"
            if alert_was_sent(registration_id, alert_key):
                continue
            message = build_alert_message(name, label, registered, expiration, level)
            sent = await send_alert(context, chat_id, message)
            if sent:
                mark_alert_sent(registration_id, alert_key)

# =========================================================
# TEST ALERT
# =========================================================

async def test_alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now(TZ).date()
    registered = today
    expiration_1 = add_trading_days_after(today, 1)
    message_1 = build_alert_message("NDOG TEST", "NDOG", registered, expiration_1, "ONE_TRADING_DAY")
    await update.message.reply_text("🧪 TEST — 1 TRADING DAY LEFT\n\n" + message_1)
    message_2 = build_alert_message("NDOG TEST", "NDOG", registered, today, "EXPIRATION_DAY")
    await update.message.reply_text("🧪 TEST — EXPIRATION DAY\n\n" + message_2)

# =========================================================
# MAIN - RUN BOT + FLASK
# =========================================================

def run_bot():
    """ربات رو در یک ترد جداگانه اجرا کن"""
    application = Application.builder().token(TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("testalert", test_alert))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    application.job_queue.run_daily(
        check_expirations,
        time=time(hour=ALERT_HOUR, minute=ALERT_MINUTE, tzinfo=TZ),
        name="expiration-check"
    )
    
    print("Trading Expiration Bot PRO v3 UI is running...")
    application.run_polling()

if __name__ == "__main__":
    init_db()
    
    # ربات رو در ترد جداگانه اجرا کن
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    
    # سرور Flask رو روی پورتی که رندر میده راه‌اندازی کن
    port = int(os.environ.get("PORT", 5000))
    flask_app.run(host="0.0.0.0", port=port)