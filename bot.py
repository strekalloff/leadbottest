import os
import logging
import csv
import io
import threading
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
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
from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler

# --- Настройки ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "https://t.me/your_channel")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL")
PORT = int(os.getenv("PORT", 8080))

# --- Логирование ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Flask для health check ---
flask_app = Flask(__name__)

@flask_app.route('/')
def health():
    return 'Bot is running', 200

# --- Работа с базой данных PostgreSQL ---
def get_conn():
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Таблица лидов
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
            # Таблица логов действий
            cur.execute('''
                CREATE TABLE IF NOT EXISTS action_logs (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    action TEXT,
                    details TEXT,
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

def log_action(user_id, action, details=""):
    """Записывает действие в таблицу логов."""
    timestamp = datetime.now().isoformat()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO action_logs (user_id, action, details, timestamp) VALUES (%s, %s, %s, %s)",
                (user_id, action, details, timestamp)
            )
        conn.commit()

# --- Вспомогательные функции для отчётов ---
def get_all_leads():
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT manager_code, user_id, username, first_name, last_name, timestamp FROM leads ORDER BY id DESC")
            return [dict(row) for row in cur.fetchall()]

def get_leads_grouped():
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

# --- Клавиатура администратора ---
def get_admin_reply_keyboard():
    """Создаёт reply-клавиатуру с кнопками 'Меню' и 'Логи'."""
    keyboard = [
        [KeyboardButton("Меню"), KeyboardButton("Логи")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

async def maybe_show_reply_keyboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает reply-клавиатуру администратору, если её ещё нет."""
    if update.effective_user.id == ADMIN_ID:
        if not context.user_data.get('reply_keyboard_shown'):
            reply_keyboard = get_admin_reply_keyboard()
            await update.message.reply_text(
                "Клавиатура администратора активирована. Используйте кнопки «Меню» и «Логи».",
                reply_markup=reply_keyboard
            )
            context.user_data['reply_keyboard_shown'] = True

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

    log_action(user.id, "new_lead", f"manager={manager_code}")

    await notify_admin(context.bot, manager_code, user)

    # Показываем reply-клавиатуру, если это админ
    await maybe_show_reply_keyboard(update, context)

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
    await maybe_show_reply_keyboard(update, context)

    if context.user_data.get('awaiting_username'):
        username = update.message.text.strip()
        if username.startswith('@'):
            username = username[1:]

        user = update.effective_user
        manager_code = context.user_data.get('manager_code', 'unknown')

        update_username(user.id, manager_code, username)

        log_action(user.id, "username_updated", f"username={username}, manager={manager_code}")

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
        log_action(update.effective_user.id, "unauthorized_access", "Tried to access admin menu")
        await update.message.reply_text("У вас нет доступа.")
        return

    await show_inline_menu(update, context)

async def show_inline_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        log_action(query.from_user.id, "unauthorized_access", "Tried to use inline button")
        await query.edit_message_text("У вас нет доступа.")
        return

    data = query.data

    if data == "export_csv":
        log_action(query.from_user.id, "export_csv", "CSV export requested")
        await export_csv(update, context)
    elif data == "export_leads":
        log_action(query.from_user.id, "export_leads_text", "Text report requested")
        await export_leads_text(update, context)
    elif data == "confirm_clear":
        await confirm_clear(update, context)
    elif data == "execute_clear":
        await execute_clear(update, context)
    elif data == "cancel_clear":
        await query.edit_message_text("Операция отменена.")

async def handle_reply_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id == ADMIN_ID:
        await show_inline_menu(update, context)
    else:
        log_action(update.effective_user.id, "unauthorized_access", "Tried to use reply menu button")
        await update.message.reply_text("У вас нет доступа.")

async def handle_reply_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id == ADMIN_ID:
        await logs_command(update, context)
    else:
        log_action(update.effective_user.id, "unauthorized_access", "Tried to use reply logs button")
        await update.message.reply_text("У вас нет доступа.")

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
    await export_csv(update, context)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE leads RESTART IDENTITY")
        conn.commit()

    log_action(update.effective_user.id, "clear_database", "All leads deleted")

    if update.callback_query:
        await update.callback_query.message.reply_text("База данных очищена.")
    else:
        await update.message.reply_text("База данных очищена.")

# --- Команда /logs ---
async def logs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        log_action(update.effective_user.id, "unauthorized_access", "Tried to view logs")
        await update.message.reply_text("У вас нет доступа.")
        return

    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT user_id, action, details, timestamp FROM action_logs ORDER BY id DESC LIMIT 50")
            rows = cur.fetchall()

    if not rows:
        await update.message.reply_text("Логов пока нет.")
        return

    text = "📋 Последние действия:\n\n"
    for row in rows:
        text += f"🕒 {row['timestamp']}\n"
        text += f"👤 User ID: {row['user_id']}\n"
        text += f"🔹 Действие: {row['action']}\n"
        text += f"📝 Детали: {row['details']}\n\n"

    await update.message.reply_text(text)

# --- Ежедневный бэкап ---
async def daily_backup(application: Application):
    """Отправляет CSV-файл администратору."""
    try:
        leads = get_all_leads()
        if not leads:
            logger.info("Нет данных для бэкапа")
            return
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Менеджер", "User ID", "Username", "Имя", "Фамилия", "Время"])
        for lead in leads:
            writer.writerow([lead['manager_code'], lead['user_id'], lead['username'],
                             lead['first_name'], lead['last_name'], lead['timestamp']])
        csv_data = output.getvalue()
        await application.bot.send_document(
            chat_id=ADMIN_ID,
            document=io.BytesIO(csv_data.encode('utf-8-sig')),
            filename=f"leads_backup_{datetime.now().strftime('%Y-%m-%d')}.csv",
            caption="Ежедневный бэкап данных"
        )
        log_action(ADMIN_ID, "daily_backup", "Backup sent")
        logger.info("Ежедневный бэкап отправлен")
    except Exception as e:
        logger.error(f"Ошибка при бэкапе: {e}")

# --- Запуск Flask в отдельном потоке ---
def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT)

# --- Запуск бота ---
def main():
    init_db()
    application = Application.builder().token(BOT_TOKEN).build()

    # Регистрация обработчиков
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin_menu))
    application.add_handler(CommandHandler("export", admin_menu))
    application.add_handler(CommandHandler("menu", admin_menu))
    application.add_handler(CommandHandler("logs", logs_command))

    # Обработчики reply-кнопок (должны быть перед общим обработчиком текста)
    application.add_handler(MessageHandler(filters.Text(["Меню"]), handle_reply_menu))
    application.add_handler(MessageHandler(filters.Text(["Логи"]), handle_reply_logs))

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_username))
    application.add_handler(CallbackQueryHandler(button_handler))

    # Запуск Flask
    threading.Thread(target=run_flask, daemon=True).start()

    # Запуск планировщика ежедневного бэкапа
    scheduler = BackgroundScheduler()
    scheduler.add_job(lambda: daily_backup(application), 'interval', hours=24)
    scheduler.start()

    logger.info("Бот запущен")
    application.run_polling()

if __name__ == "__main__":
    main()
