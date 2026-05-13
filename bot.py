import os
import sqlite3
from datetime import datetime

from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ForceReply,
)
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

DATA_DIR = os.getenv("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)

DB_FILE = os.path.join(DATA_DIR, "quran_session.db")


# -------------------------
# Database
# -------------------------

def db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS participants (
        chat_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        name TEXT NOT NULL,
        status TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (chat_id, user_id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS sessions (
        chat_id TEXT PRIMARY KEY,
        panel_message_id INTEGER
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS pending_names (
        chat_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        prompt_message_id INTEGER,
        PRIMARY KEY (chat_id, user_id)
    )
    """)

    conn.commit()
    conn.close()


def set_panel_message(chat_id, message_id):
    conn = db()
    conn.execute(
        "INSERT OR REPLACE INTO sessions (chat_id, panel_message_id) VALUES (?, ?)",
        (str(chat_id), message_id),
    )
    conn.commit()
    conn.close()


def get_panel_message(chat_id):
    conn = db()
    row = conn.execute(
        "SELECT panel_message_id FROM sessions WHERE chat_id = ?",
        (str(chat_id),),
    ).fetchone()
    conn.close()
    return row["panel_message_id"] if row else None


def save_pending_name(chat_id, user_id, prompt_message_id):
    conn = db()
    conn.execute(
        "INSERT OR REPLACE INTO pending_names (chat_id, user_id, prompt_message_id) VALUES (?, ?, ?)",
        (str(chat_id), str(user_id), prompt_message_id),
    )
    conn.commit()
    conn.close()


def has_pending_name(chat_id, user_id):
    conn = db()
    row = conn.execute(
        "SELECT * FROM pending_names WHERE chat_id = ? AND user_id = ?",
        (str(chat_id), str(user_id)),
    ).fetchone()
    conn.close()
    return row is not None


def clear_pending_name(chat_id, user_id):
    conn = db()
    conn.execute(
        "DELETE FROM pending_names WHERE chat_id = ? AND user_id = ?",
        (str(chat_id), str(user_id)),
    )
    conn.commit()
    conn.close()


def upsert_participant(chat_id, user_id, name, status="waiting"):
    conn = db()
    conn.execute("""
    INSERT INTO participants (chat_id, user_id, name, status, updated_at)
    VALUES (?, ?, ?, ?, ?)
    ON CONFLICT(chat_id, user_id)
    DO UPDATE SET
        name = excluded.name,
        status = excluded.status,
        updated_at = excluded.updated_at
    """, (
        str(chat_id),
        str(user_id),
        name.strip(),
        status,
        datetime.now().isoformat(timespec="seconds"),
    ))
    conn.commit()
    conn.close()


def update_status(chat_id, user_id, fallback_name, status):
    conn = db()

    existing = conn.execute(
        "SELECT name FROM participants WHERE chat_id = ? AND user_id = ?",
        (str(chat_id), str(user_id)),
    ).fetchone()

    name = existing["name"] if existing else fallback_name

    conn.execute("""
    INSERT INTO participants (chat_id, user_id, name, status, updated_at)
    VALUES (?, ?, ?, ?, ?)
    ON CONFLICT(chat_id, user_id)
    DO UPDATE SET
        status = excluded.status,
        updated_at = excluded.updated_at
    """, (
        str(chat_id),
        str(user_id),
        name,
        status,
        datetime.now().isoformat(timespec="seconds"),
    ))

    conn.commit()
    conn.close()


def delete_participant(chat_id, user_id):
    conn = db()
    conn.execute(
        "DELETE FROM participants WHERE chat_id = ? AND user_id = ?",
        (str(chat_id), str(user_id)),
    )
    conn.commit()
    conn.close()


def clear_session(chat_id):
    conn = db()
    conn.execute("DELETE FROM participants WHERE chat_id = ?", (str(chat_id),))
    conn.execute("DELETE FROM pending_names WHERE chat_id = ?", (str(chat_id),))
    conn.commit()
    conn.close()


def get_participants(chat_id):
    conn = db()
    rows = conn.execute(
        "SELECT name, status FROM participants WHERE chat_id = ? ORDER BY updated_at ASC",
        (str(chat_id),),
    ).fetchall()
    conn.close()
    return rows


# -------------------------
# UI
# -------------------------

def status_text(status):
    if status == "waiting":
        return "⏳ في الانتظار"
    if status == "recited":
        return "✅ قرأ بالفعل"
    if status == "listener":
        return "🎧 مستمع فقط"
    if status == "excused":
        return "🌸 معذور"
    return "⏳ في الانتظار"


def keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➕ أضف اسمي", callback_data="add_name"),
            InlineKeyboardButton("❌ احذف اسمي", callback_data="delete_name"),
        ],
        [
            InlineKeyboardButton("✅ قرأت بالفعل", callback_data="recited"),
            InlineKeyboardButton("🎧 مستمع", callback_data="listener"),
        ],
        [
            InlineKeyboardButton("🌸 معذور", callback_data="excused"),
        ],
    ])


def build_panel_text(chat_id):
    rows = get_participants(chat_id)

    text = "📖 *قائمة جلسة القرآن*\n"
    text += "🌿 *السلام عليكم ورحمة الله وبركاته*\n\n"

    text += "📌 *طريقة الاستخدام:*\n"
    text += "1️⃣ اضغط ➕ *أضف اسمي* للمشاركة في القراءة.\n"
    text += "2️⃣ اكتب اسمك الحقيقي كما تحب أن يظهر.\n"
    text += "3️⃣ بعد الانتهاء من القراءة اضغط ✅ *قرأت بالفعل*.\n"
    text += "4️⃣ إذا كنت ستستمع فقط اضغط 🎧 *مستمع*.\n"
    text += "5️⃣ إذا كنت غير حاضر أو معذور اضغط 🌸 *معذور*.\n\n"

    text += "━━━━━━━━━━━━━━\n"
    text += "📋 *القائمة الحالية:*\n\n"

    if not rows:
        text += "لا يوجد أسماء حتى الآن.\n\n"
        text += "اضغط على زر ➕ *أضف اسمي* للانضمام إلى القائمة."
    else:
        for i, row in enumerate(rows, start=1):
            text += f"{i}. {row['name']} — {status_text(row['status'])}\n"

    text += "\n━━━━━━━━━━━━━━\n"
    text += "اختر حالتك من الأزرار بالأسفل 👇"

    return text


def telegram_display_name(user):
    parts = []

    if user.first_name:
        parts.append(user.first_name)

    if user.last_name:
        parts.append(user.last_name)

    if parts:
        return " ".join(parts)

    if user.username:
        return f"@{user.username}"

    return "مشارك بدون اسم"


async def refresh_panel(context: ContextTypes.DEFAULT_TYPE, chat_id):
    message_id = get_panel_message(chat_id)

    if not message_id:
        return

    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=build_panel_text(chat_id),
            reply_markup=keyboard(),
            parse_mode="Markdown",
        )
    except Exception as e:
        print("Could not refresh panel:", e)


async def pin_panel(context: ContextTypes.DEFAULT_TYPE, chat_id):
    message_id = get_panel_message(chat_id)

    if not message_id:
        return

    try:
        await context.bot.pin_chat_message(
            chat_id=chat_id,
            message_id=message_id,
            disable_notification=True,
        )
    except Exception as e:
        print("Could not pin panel:", e)


# -------------------------
# Commands
# -------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "السلام عليكم 🌿\n\n"
        "أنا بوت تنظيم جلسة القرآن.\n\n"
        "داخل المجموعة، اكتب:\n"
        "/newsession\n\n"
        "وسأقوم بإنشاء القائمة وتثبيتها في الأعلى 📌"
    )


async def new_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    clear_session(chat_id)

    msg = await update.message.reply_text(
        build_panel_text(chat_id),
        reply_markup=keyboard(),
        parse_mode="Markdown",
    )

    set_panel_message(chat_id, msg.message_id)

    try:
        await context.bot.pin_chat_message(
            chat_id=chat_id,
            message_id=msg.message_id,
            disable_notification=True,
        )
    except Exception as e:
        print("Could not pin message:", e)
        await update.message.reply_text(
            "تم إنشاء القائمة ✅\n"
            "لكن لم أستطع تثبيتها.\n\n"
            "تأكد أنني Admin ومعي صلاحية Pin Messages."
        )


async def reset_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    clear_session(chat_id)

    await refresh_panel(context, chat_id)
    await pin_panel(context, chat_id)

    await update.message.reply_text("تم تصفير قائمة الجلسة بنجاح ✅")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📌 *طريقة الاستخدام:*\n\n"
        "/newsession لإنشاء جلسة جديدة وتثبيت القائمة\n"
        "/reset لتصفير القائمة الحالية\n"
        "/help لعرض المساعدة\n\n"
        "الأزرار:\n"
        "➕ أضف اسمي\n"
        "❌ احذف اسمي\n"
        "✅ قرأت بالفعل\n"
        "🎧 مستمع\n"
        "🌸 معذور",
        parse_mode="Markdown",
    )


# -------------------------
# Buttons
# -------------------------

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat_id
    user = query.from_user
    user_id = user.id
    fallback_name = telegram_display_name(user)

    action = query.data

    if action == "add_name":
        if user.username:
            prompt_text = (
                f"@{user.username}\n"
                "اكتب اسمك الحقيقي كما تحب أن يظهر في القائمة 👇"
            )
        else:
            prompt_text = "اكتب اسمك الحقيقي كما تحب أن يظهر في القائمة 👇"

        prompt = await query.message.reply_text(
            prompt_text,
            reply_markup=ForceReply(
                selective=True,
                input_field_placeholder="مثال: الشيخ أحمد / الحاج محمد / أبو يوسف"
            ),
        )

        save_pending_name(chat_id, user_id, prompt.message_id)

        await query.answer(
            "اكتب اسمك في رسالة رد على البوت.",
            show_alert=True,
        )
        return

    if action == "delete_name":
        delete_participant(chat_id, user_id)
        clear_pending_name(chat_id, user_id)

        await refresh_panel(context, chat_id)
        await pin_panel(context, chat_id)

        await query.answer("تم حذف اسمك من القائمة ✅", show_alert=True)
        return

    if action == "recited":
        update_status(chat_id, user_id, fallback_name, "recited")

        await refresh_panel(context, chat_id)
        await pin_panel(context, chat_id)

        await query.answer("تم تسجيلك: قرأت بالفعل ✅", show_alert=True)
        return

    if action == "listener":
        update_status(chat_id, user_id, fallback_name, "listener")

        await refresh_panel(context, chat_id)
        await pin_panel(context, chat_id)

        await query.answer("تم تسجيلك: مستمع فقط 🎧", show_alert=True)
        return

    if action == "excused":
        update_status(chat_id, user_id, fallback_name, "excused")

        await refresh_panel(context, chat_id)
        await pin_panel(context, chat_id)

        await query.answer("تم تسجيلك: معذور 🌸", show_alert=True)
        return


# -------------------------
# Text replies for names
# -------------------------

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    chat_id = update.effective_chat.id
    user = update.effective_user
    user_id = user.id

    if not has_pending_name(chat_id, user_id):
        return

    name = update.message.text.strip()

    if len(name) < 2:
        await update.message.reply_text("من فضلك اكتب اسم واضح.")
        return

    if len(name) > 40:
        await update.message.reply_text("الاسم طويل جدًا. من فضلك اكتب اسم أقصر.")
        return

    upsert_participant(chat_id, user_id, name, "waiting")
    clear_pending_name(chat_id, user_id)

    await refresh_panel(context, chat_id)
    await pin_panel(context, chat_id)

    await update.message.reply_text(
        f"تم إضافة اسمك بنجاح ✅\n"
        f"الاسم: {name}"
    )


# -------------------------
# Error handler
# -------------------------

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    print("Bot error:", context.error)


# -------------------------
# Main
# -------------------------

def main():
    if not BOT_TOKEN:
        raise ValueError(
            "BOT_TOKEN is missing. Add it to .env locally or to Railway Variables in the cloud."
        )

    init_db()

    request = HTTPXRequest(
        connect_timeout=30,
        read_timeout=30,
        write_timeout=30,
        pool_timeout=30,
    )

    get_updates_request = HTTPXRequest(
        connect_timeout=30,
        read_timeout=30,
        write_timeout=30,
        pool_timeout=30,
    )

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(request)
        .get_updates_request(get_updates_request)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("newsession", new_session))
    app.add_handler(CommandHandler("reset", reset_session))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    app.add_error_handler(error_handler)

    print("Quran session bot is starting...")
    print(f"Database file: {DB_FILE}")
    print("Trying to connect to Telegram...")
    print("If it stays running, the bot is active.")

    app.run_polling(
        poll_interval=1,
        timeout=30,
        bootstrap_retries=-1,
    )


if __name__ == "__main__":
    main()