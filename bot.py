import os
import sqlite3
import re
import threading
from datetime import date, datetime, timedelta, time
from zoneinfo import ZoneInfo
from supabase import create_client, Client

from dotenv import load_dotenv
from flask import Flask
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    CallbackQueryHandler,
    filters,
)

load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase: Client = create_client(
    SUPABASE_URL,
    SUPABASE_KEY
)

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
    if kind == "NDOG":
        expiration = add_trading_days_after(registered, 5)

        data = {
            "chat_id": chat_id,
            "kind": kind,
            "registered_date": registered.isoformat(),
            "display_name": f"NDOG {short_date(registered)}",
            "ndog_expiration": expiration.isoformat(),
        }

    else:
        e5 = weekly_expiration(registered, 5)
        e7 = weekly_expiration(registered, 7)
        e8 = weekly_expiration(registered, 8)

        data = {
            "chat_id": chat_id,
            "kind": kind,
            "registered_date": registered.isoformat(),
            "display_name": f"NWOG {short_date(registered)}",
            "nwog_5_expiration": e5.isoformat(),
            "nwog_7_expiration": e7.isoformat(),
            "nwog_8_expiration": e8.isoformat(),
        }

    response = (
        supabase
        .table("registrations")
        .upsert(
            data,
            on_conflict="chat_id,kind,registered_date"
        )
        .execute()
    )

    return response.data[0] if response.data else None

def get_registration(chat_id: int, kind: str, registered: date):
    response = (
        supabase
        .table("registrations")
        .select("*")
        .eq("chat_id", chat_id)
        .eq("kind", kind)
        .eq("registered_date", registered.isoformat())
        .limit(1)
        .execute()
    )

    if response.data:
        return response.data[0]

    return None

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


def get_all_registrations(chat_id):
    response = (
        supabase
        .table("registrations")
        .select("*")
        .eq("chat_id", chat_id)
        .order("registered_date", desc=True)
        .execute()
    )

    return response.data or []


def active_rows(chat_id):
    rows = get_all_registrations(chat_id)

    return [
        row for row in rows
        if is_registration_active(row)
    ]


def history_rows(chat_id):
    rows = get_all_registrations(chat_id)

    return [
        row for row in rows
        if not is_registration_active(row)
    ]

# =========================================================
# USER SETTINGS
# =========================================================

def get_user_settings(chat_id):
    response = (
        supabase
        .table("settings")
        .select("*")
        .eq("chat_id", chat_id)
        .limit(1)
        .execute()
    )

    if response.data:
        return response.data[0]

    # ساخت تنظیمات پیش‌فرض برای کاربر
    data = {
        "chat_id": chat_id,
        "timezone": TIMEZONE,
        "alert_hour": ALERT_HOUR,
        "alert_minute": ALERT_MINUTE,
        "alerts_enabled": True,
    }

    response = (
        supabase
        .table("settings")
        .upsert(
            data,
            on_conflict="chat_id"
        )
        .execute()
    )

    return response.data[0] if response.data else data


def update_user_settings(chat_id, **changes):
    response = (
        supabase
        .table("settings")
        .upsert(
            {
                "chat_id": chat_id,
                **changes
            },
            on_conflict="chat_id"
        )
        .execute()
    )

    return response.data[0] if response.data else None
# =========================================================
# KEYBOARDS (Reply + Inline)
# =========================================================

def dashboard_reply_keyboard():
    """Reply Keyboard - پایین صفحه تلگرام"""
    return ReplyKeyboardMarkup([
        ["📅 NDOG", "📆 NWOG"],
        ["🟢 ACTIVE", "📜 HISTORY"],
        ["⚙️ SETTINGS"],
    ], resize_keyboard=True)

def dashboard_inline_keyboard():
    """Inline Keyboard - داخل پیام"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 NDOG", callback_data="ndog")],
        [InlineKeyboardButton("📆 NWOG", callback_data="nwog")],
        [
            InlineKeyboardButton("🟢 ACTIVE", callback_data="active"),
            InlineKeyboardButton("📜 HISTORY", callback_data="history"),
        ],
        [InlineKeyboardButton("⚙️ SETTINGS", callback_data="settings")],
    ])

def back_reply_keyboard():
    """Reply Keyboard - برگشت به خانه"""
    return ReplyKeyboardMarkup([
        ["🏠 HOME"],
    ], resize_keyboard=True, is_persistent=True)

# =========================================================
# DASHBOARD
# =========================================================

def dashboard_text(chat_id):
    active = active_rows(chat_id)
    history = history_rows(chat_id)
    return (
        "📊 TRADING EXPIRATION\n"
        "      TRACKER PRO\n\n"
        f"🟢 Active: {len(active)}\n"
        f"🔴 Expired: {len(history)}\n\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "Track your NDOG / NWOG expirations\n"
        "with automatic countdown & alerts.\n\n"
        "Use buttons below or inline menu:"
    )

async def send_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE, edit=False):
    chat_id = update.effective_chat.id
    text = dashboard_text(chat_id)

    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text)
        await update.callback_query.message.reply_text(
            "👇 Select an option:",
            reply_markup=dashboard_reply_keyboard()
        )

    elif update.message:
        await update.message.reply_text(
            text,
            reply_markup=dashboard_reply_keyboard()
        )

    elif update.callback_query:
        await update.callback_query.message.reply_text(
            text,
            reply_markup=dashboard_reply_keyboard()
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
# VIEW DETAILS (برای Inline Keyboard)
# =========================================================

async def view_details(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    registration_id: int,
    is_history: bool = False
):
    query = update.callback_query

    response = (
        supabase
        .table("registrations")
        .select("*")
        .eq("id", registration_id)
        .eq("chat_id", query.message.chat.id)
        .limit(1)
        .execute()
    )

    row = response.data[0] if response.data else None

    if not row:
        await query.edit_message_text(
            "❌ Registration not found."
        )
        return

    if row["kind"] == "NDOG":
        text = render_ndog(row)
    else:
        text = render_nwog(row)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🏠 HOME", callback_data="home")],
        [
            InlineKeyboardButton(
                "🔙 BACK",
                callback_data="active" if not is_history else "history"
            )
        ],
    ])

    await query.edit_message_text(
        text,
        reply_markup=keyboard
    )

# =========================================================
# START
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await send_dashboard(update, context)

# =========================================================
# MESSAGE HANDLER
# =========================================================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    chat_id = update.effective_chat.id
    mode = context.user_data.get("mode")

    # =====================================================
    # SET ALERT TIME
    # =====================================================

    if mode == "SET_ALERT_TIME":
        match = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", text)

        if not match:
            await update.message.reply_text(
                "❌ INVALID TIME\n\n"
                "Use 24-hour format:\n\n"
                "09:00\n"
                "10:30\n"
                "18:45"
            )
            return

        hour = int(match.group(1))
        minute = int(match.group(2))

        update_user_settings(
            chat_id,
            alert_hour=hour,
            alert_minute=minute
        )

        context.user_data.clear()

        await update.message.reply_text(
            "✅ ALERT TIME UPDATED\n\n"
            f"⏰ New Alert Time\n{hour:02d}:{minute:02d}",
            reply_markup=dashboard_reply_keyboard()
        )
        return

    # =====================================================
    # MAIN MENU BUTTONS
    # اگر کاربر هنگام وارد کردن تاریخ روی یکی از منوها زد
    # حالت ورود تاریخ لغو می‌شود.
    # =====================================================

    main_menu_buttons = {
        "📅 NDOG",
        "📆 NWOG",
        "🟢 ACTIVE",
        "📜 HISTORY",
        "⚙️ SETTINGS",
        "🏠 HOME",
    }

    if mode and text in main_menu_buttons:
        context.user_data.clear()
        mode = None

    # =====================================================
    # REPLY KEYBOARD NAVIGATION
    # =====================================================

    if not mode:

        # HOME
        if text in ("🏠 HOME", "/start"):
            context.user_data.clear()
            await send_dashboard(update, context)
            return

        # =================================================
        # NDOG
        # =================================================

        if text == "📅 NDOG":
            context.user_data["mode"] = "NDOG"

            await update.message.reply_text(
                "📅 REGISTER NDOG\n\n"
                "Send the registration date.\n\n"
                "Examples:\n"
                "NDOG 18 AUG\n"
                "NDOG 18 AUG 2026\n"
                "18 AUG\n"
                "2026-08-18\n\n"
                "💡 NDOG expires after 5 trading days.",
                reply_markup=dashboard_reply_keyboard()
            )
            return

        # =================================================
        # NWOG
        # =================================================

        if text == "📆 NWOG":
            context.user_data["mode"] = "NWOG"

            await update.message.reply_text(
                "📆 REGISTER NWOG\n\n"
                "Send the Sunday registration date.\n\n"
                "Examples:\n"
                "NWOG 16 AUG\n"
                "NWOG 16 AUG 2026\n"
                "16 AUG\n"
                "2026-08-16\n\n"
                "💡 NWOG tracks 5, 7 and 8 weeks.",
                reply_markup=dashboard_reply_keyboard()
            )
            return

        # =================================================
        # ACTIVE
        # =================================================

        if text == "🟢 ACTIVE":
            rows = active_rows(chat_id)

            if not rows:
                await update.message.reply_text(
                    "🟢 ACTIVE\n\n"
                    "━━━━━━━━━━━━━━━━━━\n\n"
                    "No active registrations.\n\n"
                    "Register an NDOG or NWOG to get started.",
                    reply_markup=back_reply_keyboard()
                )
                return

            lines = [
                "🟢 ACTIVE EXPIRATIONS",
                "",
                "━━━━━━━━━━━━━━━━━━",
                ""
            ]

            for row in rows:
                icon = "📅" if row["kind"] == "NDOG" else "📆"
                lines.append(
                    f"{icon} {row['display_name']}"
                )

            lines.extend([
                "",
                "━━━━━━━━━━━━━━━━━━",
                "Select an expiration:"
            ])

            buttons = []

            for row in rows:
                icon = "📅" if row["kind"] == "NDOG" else "📆"

                buttons.append([
                    InlineKeyboardButton(
                        f"🟢 {icon} {row['display_name']}",
                        callback_data=f"view:{row['id']}:0"
                    )
                ])

            buttons.append([
                InlineKeyboardButton(
                    "🏠 HOME",
                    callback_data="home"
                )
            ])

            await update.message.reply_text(
                "\n".join(lines),
                reply_markup=InlineKeyboardMarkup(buttons)
            )
            return

        # =================================================
        # HISTORY
        # =================================================

        if text == "📜 HISTORY":
            rows = history_rows(chat_id)

            if not rows:
                await update.message.reply_text(
                    "📜 HISTORY\n\n"
                    "━━━━━━━━━━━━━━━━━━\n\n"
                    "No fully expired registrations.",
                    reply_markup=back_reply_keyboard()
                )
                return

            lines = [
                "📜 HISTORY",
                "",
                "━━━━━━━━━━━━━━━━━━",
                ""
            ]

            for row in rows:
                icon = "📅" if row["kind"] == "NDOG" else "📆"

                lines.append(
                    f"🔴 {icon} {row['display_name']}"
                )

            lines.extend([
                "",
                "━━━━━━━━━━━━━━━━━━",
                "Select an expiration:"
            ])

            buttons = []

            for row in rows:
                icon = "📅" if row["kind"] == "NDOG" else "📆"

                buttons.append([
                    InlineKeyboardButton(
                        f"🔴 {icon} {row['display_name']}",
                        callback_data=f"view:{row['id']}:1"
                    )
                ])

            buttons.append([
                InlineKeyboardButton(
                    "🏠 HOME",
                    callback_data="home"
                )
            ])

            await update.message.reply_text(
                "\n".join(lines),
                reply_markup=InlineKeyboardMarkup(buttons)
            )
            return

        # =================================================
        # SETTINGS
        # =================================================

        if text == "⚙️ SETTINGS":
            await update.message.reply_text(
                settings_text(chat_id),
                reply_markup=settings_inline_keyboard()
            )
            return

        # =================================================
        # UNKNOWN TEXT
        # =================================================

        await update.message.reply_text(
            "🤖 Please use the buttons below.",
            reply_markup=dashboard_reply_keyboard()
        )
        return

    # =====================================================
    # PARSE DATE
    # =====================================================

    registered = parse_trade_input(text, mode)

    if not registered:
        await update.message.reply_text(
            "❌ INVALID DATE\n\n"
            "Use one of these formats:\n\n"
            "NDOG 18 AUG\n"
            "NDOG 18 AUG 2026\n"
            "18 AUG\n"
            "2026-08-18"
        )
        return

    # =====================================================
    # REGISTER NDOG
    # =====================================================

    if mode == "NDOG":

        save_registration(
            chat_id,
            "NDOG",
            registered
        )

        row = get_registration(
            chat_id,
            "NDOG",
            registered
        )

        context.user_data.clear()

        if not row:
            await update.message.reply_text(
                "❌ خطا در دریافت اطلاعات NDOG.",
                reply_markup=dashboard_reply_keyboard()
            )
            return

        await update.message.reply_text(
            "✅ NDOG REGISTERED\n\n" + render_ndog(row),
            reply_markup=dashboard_reply_keyboard()
        )
        return

    # =====================================================
    # REGISTER NWOG
    # =====================================================

    if mode == "NWOG":

        if registered.weekday() != 6:
            await update.message.reply_text(
                "❌ NWOG ACCEPTS SUNDAYS ONLY\n\n"
                "Example:\n"
                "NWOG 16 AUG"
            )
            return

        save_registration(
            chat_id,
            "NWOG",
            registered
        )

        row = get_registration(
            chat_id,
            "NWOG",
            registered
        )

        context.user_data.clear()

        if not row:
            await update.message.reply_text(
                "❌ خطا در دریافت اطلاعات NWOG.",
                reply_markup=dashboard_reply_keyboard()
            )
            return

        await update.message.reply_text(
            "✅ NWOG REGISTERED\n\n" + render_nwog(row),
            reply_markup=dashboard_reply_keyboard()
        )
        return

# =========================================================
# SETTINGS MENU
# =========================================================

def settings_text(chat_id):
    settings = get_user_settings(chat_id)

    timezone = settings.get("timezone", TIMEZONE)
    hour = settings.get("alert_hour", ALERT_HOUR)
    minute = settings.get("alert_minute", ALERT_MINUTE)
    alerts_enabled = settings.get("alerts_enabled", True)

    alerts_status = "🟢 ON" if alerts_enabled else "🔴 OFF"

    return (
        "⚙️ SETTINGS\n\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"🌍 Timezone\n{timezone}\n\n"
        f"⏰ Alert Time\n{hour:02d}:{minute:02d}\n\n"
        f"🔔 Alerts\n{alerts_status}\n\n"
        "📈 Trading Days\nMonday — Friday\n\n"
        "📅 Weekend\nSaturday — Sunday\n\n"
        "━━━━━━━━━━━━━━━━━━"
    )


def settings_inline_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌍 TIMEZONE", callback_data="set_timezone")],
        [InlineKeyboardButton("⏰ ALERT TIME", callback_data="set_alert_time")],
        [InlineKeyboardButton("🔔 ALERTS ON/OFF", callback_data="toggle_alerts")],
        [InlineKeyboardButton("🔙 BACK", callback_data="home")],
    ])
def timezone_inline_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🇮🇷 IRAN — TEHRAN", callback_data="timezone:Asia/Tehran")],
        [InlineKeyboardButton("🇺🇸 USA — NEW YORK", callback_data="timezone:America/New_York")],
        [InlineKeyboardButton("🔙 BACK", callback_data="settings")],
    ])
# =========================================================
# CALLBACK QUERY HANDLER
# =========================================================

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "home":
        await send_dashboard(update, context, edit=True)
        return

    if data == "ndog":
        context.user_data["mode"] = "NDOG"
        await query.message.reply_text(
            "📅 REGISTER NDOG\n\nSend the registration date.\n\nExamples:\nNDOG 18 AUG\nNDOG 18 AUG 2026\n18 AUG\n2026-08-18\n\n💡 NDOG expires after 5 trading days."
        )
        return

    if data == "nwog":
        context.user_data["mode"] = "NWOG"
        await query.message.reply_text(
            "📆 REGISTER NWOG\n\nSend the Sunday registration date.\n\nExamples:\nNWOG 16 AUG\nNWOG 16 AUG 2026\n16 AUG\n2026-08-16\n\n💡 NWOG tracks 5, 7 and 8 weeks."
        )
        return

    if data == "active":
        rows = active_rows(query.message.chat.id)
        if not rows:
            await query.edit_message_text(
                "🟢 ACTIVE\n\n━━━━━━━━━━━━━━━━━━\n\nNo active registrations.\n\nRegister an NDOG or NWOG to get started.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🏠 HOME", callback_data="home")]
                ])
            )
            return
        
        buttons = []
        for row in rows:
            icon = "📅" if row["kind"] == "NDOG" else "📆"
            buttons.append([InlineKeyboardButton(
                f"🟢 {icon} {row['display_name']}",
                callback_data=f"view:{row['id']}:0"
            )])
        buttons.append([InlineKeyboardButton("🏠 HOME", callback_data="home")])
        
        await query.edit_message_text(
            "🟢 ACTIVE EXPIRATIONS\n\n━━━━━━━━━━━━━━━━━━\n\nSelect an expiration:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    if data == "history":
        rows = history_rows(query.message.chat.id)
        if not rows:
            await query.edit_message_text(
                "📜 HISTORY\n\n━━━━━━━━━━━━━━━━━━\n\nNo fully expired registrations.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🏠 HOME", callback_data="home")]
                ])
            )
            return
        
        buttons = []
        for row in rows:
            icon = "📅" if row["kind"] == "NDOG" else "📆"
            buttons.append([InlineKeyboardButton(
                f"🔴 {icon} {row['display_name']}",
                callback_data=f"view:{row['id']}:1"
            )])
        buttons.append([InlineKeyboardButton("🏠 HOME", callback_data="home")])
        
        await query.edit_message_text(
            "📜 HISTORY\n\n━━━━━━━━━━━━━━━━━━\n\nSelect an expired registration:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    if data == "settings":
        await query.edit_message_text(
            settings_text(query.message.chat.id),
            reply_markup=settings_inline_keyboard()
        )
        return

    if data == "toggle_alerts":
        chat_id = query.message.chat.id

        settings = get_user_settings(chat_id)
        current_status = settings.get("alerts_enabled", True)

        update_user_settings(
            chat_id,
            alerts_enabled=not current_status
        )

        await query.edit_message_text(
            settings_text(chat_id),
            reply_markup=settings_inline_keyboard()
        )
        return
    if data == "set_timezone":
        await query.edit_message_text(
            "🌍 SELECT TIMEZONE\n\n"
            "Choose your timezone:",
            reply_markup=timezone_inline_keyboard()
        )
        return

    if data.startswith("timezone:"):
        timezone = data.split(":", 1)[1]

        # بررسی معتبر بودن timezone
        try:
            ZoneInfo(timezone)
        except Exception:
            await query.answer(
                "❌ Invalid timezone", 
                show_alert=True
            )
            return

        update_user_settings(
            query.message.chat.id,
            timezone=timezone
        )

        await query.edit_message_text(
            settings_text(query.message.chat.id),
            reply_markup=settings_inline_keyboard()
        )
        return
    if data == "set_alert_time":
        context.user_data["mode"] = "SET_ALERT_TIME"

        await query.edit_message_text(
            "⏰ SET ALERT TIME\n\n"
            "Enter the alert time in 24-hour format.\n\n"
            "Examples:\n"
            "09:00\n"
            "10:30\n"
            "18:45\n\n"
            "🔙 Send /cancel to cancel."
        )
        return

    # VIEW DETAILS
    if data.startswith("view:"):
        parts = data.split(":")
        rid = int(parts[1])
        is_history = parts[2] == "1"
        await view_details(update, context, rid, is_history)
        return

    # DELETE
    if data.startswith("del:"):
        rid = int(data.split(":")[1])
        conn = get_db()
        conn.execute("DELETE FROM alerts WHERE registration_id=?", (rid,))
        conn.execute("DELETE FROM registrations WHERE id=?", (rid,))
        conn.commit()
        conn.close()
        
        await query.edit_message_text(
            "🗑 Registration deleted.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏠 HOME", callback_data="home")]
            ])
        )
        return

# =========================================================
# ALERT DATABASE
# =========================================================

def alert_was_sent(registration_id, alert_key):
    response = (
        supabase
        .table("alerts")
        .select("id")
        .eq("registration_id", registration_id)
        .eq("alert_key", alert_key)
        .limit(1)
        .execute()
    )

    return bool(response.data)

def mark_alert_sent(registration_id, alert_key):
    supabase.table("alerts").upsert(
        {
            "registration_id": registration_id,
            "alert_key": alert_key,
            "sent_at": datetime.now(TZ).isoformat()
        },
        on_conflict="registration_id,alert_key"
    ).execute()

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

    response = (
        supabase
        .table("registrations")
        .select("*")
        .execute()
    )

    rows = response.data or []
    for row in rows:
        registration_id = row["id"]
        name = row["display_name"]
        chat_id = row["chat_id"]

        settings = get_user_settings(chat_id)

        if not settings.get("alerts_enabled", True):
            continue

        current_hour = int(settings.get("alert_hour", ALERT_HOUR))
        current_minute = int(settings.get("alert_minute", ALERT_MINUTE))

        now = datetime.now(TZ)

        if now.hour != current_hour or now.minute != current_minute:
            continue
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
    application = Application.builder().token(TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("testalert", test_alert))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(callback_handler))
    
    application.job_queue.run_repeating(
        check_expirations,
        interval=60,
        first=10,
        name="expiration-check"
    )
    
    print("Trading Expiration Bot PRO v3 UI is running...")
    application.run_polling(stop_signals=None)

if __name__ == "__main__":
    init_db()

    port = int(os.environ.get("PORT", 5000))

    bot_thread = threading.Thread(
        target=lambda: run_bot(),
        daemon=True
    )
    bot_thread.start()

    flask_app.run(
        host="0.0.0.0",
        port=port
    )
