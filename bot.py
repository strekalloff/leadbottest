import os
import logging
import csv
import io
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
import psycopg2
import psycopg2.extras
from datetime import datetime

# --- Настройки ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "https://t.me/your_channel")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL")

# --- Логирование ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Работа с базой данных PostgreSQL ---
def get_conn():
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute('''
                CREATE TABLE IF NOT EXISTS leads (
                    id SERIAL PRIMARY KEY,
                    manager_code TEXT,
                    user_id BIGINT,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    timestamp TEXT
                )
            ''')
        conn.commit()

def add_lead(manager_code, user_id, username, first_name, last_name):
    timestamp = datetime.now().isoformat()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO leads (manager_code, user_id, username, first_name, last_name, timestamp) VALUES (%s, %s, %s, %s, %s, %s)",
                (manager_code, user_id, username, first_name, last_name, timestamp)
            )
        conn.commit()
    logger.info(f"Новый лид: manager={manager_code}, user_id={user_id}, username={username}")

def update_username(user_id, manager_code, username):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE leads SET username = %s WHERE user_id = %s AND manager_code = %s AND username IS NULL",
                (username, user_id, manager_code)
            )
        conn.commit()

# --- Вспомогательные функции для отчётов ---
def get_all_leads():
    """Возвращает все записи из таблицы leads в виде списка словарей."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT manager_code, user_id, username, first_name, last_name, timestamp FROM leads ORDER BY id DESC")
            return [dict(row) for row in cur.fetchall()]

def get_leads_grouped():
    """Возвращает словарь {менеджер: [username, ...]}."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT manager_code, username FROM leads ORDER BY manager_code, timestamp")
            rows = cur.fetchall()
    managers = {}
    for row in rows:
        manager = row['manager_code']
        username = row['username'] if row['username'] else "не указан"
        managers.setdefault(manager, []).append(username)
    return managers

# --- Функция уведомления администратора ---
async def notify_admin(bot, manager_code, user):
    if ADMIN_ID:
        await bot.send_message(
            chat_id=ADMIN_ID,
            text=f"🔔 Новый лид!\n"
                 f"Менеджер: {manager_code}\n"
                 f"Имя: {user.first_name}\n"
                 f"Username: @{user.username if user.username else 'не указан'}"
        )

# --- Обработчики команд ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    manager_code = context.args[0] if context.args else "unknown"

    add_lead(
        manager_code=manager_code,
        user_id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name
    )

    await notify_admin(context.bot, manager_code, user)

    await update.message.reply_text(
        f"Привет, {user.first_name}! 👋\n"
        "Спасибо, что перешли по ссылке.\n"
        "Чтобы мы могли связаться с вами, нам нужен ваш Telegram username."
    )

    if user.username:
        await send_channel_invite(update, context)
    else:
        await update.message.reply_text(
            "Пожалуйста, отправьте ваш @username (например, @ivan_petrov) одним сообщением."
        )
        context.user_data['awaiting_username'] = True
        context.user_data['manager_code'] = manager_code

async def handle_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('awaiting_username'):
        username = update.message.text.strip()
        if username.startswith('@'):
            username = username[1:]

        user = update.effective_user
        manager_code = context.user_data.get('manager_code', 'unknown')

        update_username(user.id, manager_code, username)

        await update.message.reply_text(f"Отлично! Ваш username @{username} сохранён.")
        await send_channel_invite(update, context)
        context.user_data['awaiting_username'] = False
    else:
        pass

async def send_channel_invite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("📢 Перейти в канал", url=CHANNEL_LINK)]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "Нажмите кнопку ниже, чтобы подписаться на наш канал:",
        reply_markup=reply_markup
    )

# --- Команды администратора ---
async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("У вас нет доступа.")
        return

    keyboard = [
        [InlineKeyboardButton("📊 Экспорт CSV", callback_data="export_csv")],
        [InlineKeyboardButton("📋 Экспорт лидов (текст)", callback_data="export_leads")],
        [InlineKeyboardButton("🗑 Очистить базу данных", callback_data="confirm_clear")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "Выберите действие:",
        reply_markup=reply_markup
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        await query.edit_message_text("У вас нет доступа.")
        return

    data = query.data

    if data == "export_csv":
        await export_csv(update, context)
    elif data == "export_leads":
        await export_leads_text(update, context)
    elif data == "confirm_clear":
        await confirm_clear(update, context)
    elif data == "execute_clear":
        await execute_clear(update, context)
    elif data == "cancel_clear":
        await query.edit_message_text("Операция отменена.")

async def export_csv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    leads = get_all_leads()
    if not leads:
        if update.callback_query:
            await update.callback_query.edit_message_text("Пока нет ни одного лида.")
        else:
            await update.message.reply_text("Пока нет ни одного лида.")
        return

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Менеджер", "User ID", "Username", "Имя", "Фамилия", "Время"])
    for lead in leads:
        writer.writerow([lead['manager_code'], lead['user_id'], lead['username'],
                         lead['first_name'], lead['last_name'], lead['timestamp']])
    csv_data = output.getvalue()

    if update.callback_query:
        chat_id = update.callback_query.message.chat_id
        await update.callback_query.message.reply_document(
            document=io.BytesIO(csv_data.encode('utf-8-sig')),
            filename="leads_backup.csv",
            caption="Backup данных"
        )
    else:
        await update.message.reply_document(
            document=io.BytesIO(csv_data.encode('utf-8-sig')),
            filename="leads_backup.csv",
            caption="Backup данных"
        )

async def export_leads_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    managers = get_leads_grouped()
    if not managers:
        if update.callback_query:
            await update.callback_query.edit_message_text("Пока нет лидов.")
        else:
            await update.message.reply_text("Пока нет лидов.")
        return

    text = "📊 Отчёт по лидам:\n\n"
    for manager, usernames in managers.items():
        text += f"Менеджер: {manager}\n"
        text += f"Количество лидов: {len(usernames)}\n"
        usernames_str = ", ".join(f"@{u}" if u != "не указан" else u for u in usernames)
        text += f"Юзернеймы: {usernames_str}\n\n"

    if update.callback_query:
        await update.callback_query.edit_message_text(text)
    else:
        await update.message.reply_text(text)

async def confirm_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("✅ Да, удалить", callback_data="execute_clear")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel_clear")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    warning_text = (
        "Вы действительно хотите удалить ВСЕ данные из Вашей базы данных?\n"
        "Обратите внимание, что это необратимый процесс. "
        "После подтверждения вам будет предоставлен Backup данных, "
        "после чего данные будут удалены БЕЗВОЗВРАТНО."
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(warning_text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(warning_text, reply_markup=reply_markup)

async def execute_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Отправляем backup
    await export_csv(update, context)

    # Очищаем базу
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE leads RESTART IDENTITY")
        conn.commit()

    if update.callback_query:
        await update.callback_query.message.reply_text("База данных очищена.")
    else:
        await update.message.reply_text("База данных очищена.")

# --- Запуск бота ---
def main():
    init_db()
    application = Application.builder().token(BOT_TOKEN).build()

    # Команды
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin_menu))
    application.add_handler(CommandHandler("export", admin_menu))
    application.add_handler(CommandHandler("menu", admin_menu))

    # Обработчики сообщений и кнопок
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_username))
    application.add_handler(CallbackQueryHandler(button_handler))

    # Запуск long polling
    logger.info("Бот запущен")
    application.run_polling()

if __name__ == "__main__":
    main()
