"""
Telegram-бот для привязки номера телефона к аккаунту.

Сценарий:
  1. Сотрудник пишет /start
  2. Бот просит поделиться номером (кнопка)
  3. После получения номера — сохраняет phone ↔ chat_id в БД
  4. Теперь OTP-коды приходят в Telegram вместо SMS
"""

import logging
import os
import re
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from telegram import (
    Update,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DSN       = os.getenv("DATABASE_URL", "postgresql://admin@localhost/lenta_payments")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)


# ── БД ────────────────────────────────────────────────────────────────────────

def get_conn():
    return psycopg2.connect(DSN)

def normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return digits

def find_employee(phone: str):
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT employee_id, full_name FROM employees WHERE phone = %s",
                (phone,)
            )
            return cur.fetchone()
    finally:
        conn.close()

def save_link(phone: str, chat_id: int, username):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO telegram_links(phone, chat_id, username)
                VALUES (%s, %s, %s)
                ON CONFLICT(phone) DO UPDATE
                  SET chat_id   = EXCLUDED.chat_id,
                      username  = EXCLUDED.username,
                      linked_at = NOW()
            """, (phone, chat_id, username))
        conn.commit()
    finally:
        conn.close()

def get_link_by_chat(chat_id):
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT phone FROM telegram_links WHERE chat_id = %s",
                (chat_id,)
            )
            return cur.fetchone()
    finally:
        conn.close()


# ── Клавиатура запроса номера ─────────────────────────────────────────────────

def phone_keyboard():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("📱 Поделиться номером телефона", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


# ── Хендлеры ─────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    # Если уже привязан — показываем статус
    link = get_link_by_chat(chat_id)
    if link:
        emp = find_employee(link["phone"])
        name = emp["full_name"] if emp else "сотрудник"
        await update.message.reply_text(
            f"✅ Вы уже привязаны как *{name}*\n"
            f"📱 Телефон: `{link['phone']}`\n\n"
            "OTP-коды для входа на сайт будут приходить сюда.",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    await update.message.reply_text(
        "👋 Привет! Я бот сервиса *Лента — Мои выплаты*.\n\n"
        "Чтобы получать коды входа здесь, мне нужен ваш номер телефона.\n"
        "Нажмите кнопку ниже 👇",
        parse_mode="Markdown",
        reply_markup=phone_keyboard(),
    )


async def handle_contact(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    contact  = update.message.contact
    chat_id  = update.effective_chat.id
    username = update.effective_user.username

    phone = normalize_phone(contact.phone_number)
    emp   = find_employee(phone)

    if not emp:
        await update.message.reply_text(
            f"❌ Номер *{phone}* не найден в базе сотрудников.\n\n"
            "Обратитесь к супервайзеру для добавления.",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    save_link(phone, chat_id, username)
    log.info(f"Привязка: {phone} ↔ chat_id={chat_id}")

    await update.message.reply_text(
        f"✅ Отлично, *{emp['full_name']}*!\n\n"
        "Ваш Telegram привязан. Теперь при входе на сайт "
        "код подтверждения будет приходить прямо сюда 🎉",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    link = get_link_by_chat(update.effective_chat.id)
    if not link:
        await update.message.reply_text(
            "Вы ещё не привязали номер. Напишите /start",
            reply_markup=phone_keyboard(),
        )
        return
    emp = find_employee(link["phone"])
    name = emp["full_name"] if emp else "—"
    await update.message.reply_text(
        f"👤 *{name}*\n📱 `{link['phone']}`\n\n"
        "Всё готово — коды входа приходят сюда.",
        parse_mode="Markdown",
    )


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Нажмите /start для привязки номера или /status для проверки.",
    )


# ── Запуск ────────────────────────────────────────────────────────────────────

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(MessageHandler(filters.CONTACT,    handle_contact))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    log.info("Бот запущен — ожидаю сообщений…")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
