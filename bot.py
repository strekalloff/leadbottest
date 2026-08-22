import os
import logging
import csv
import io
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
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
    """Возвращает соединение с базой данных."""
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def init_db():
    """Создаёт таблицу, если её нет."""
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
    """Добавляет нового лида в базу."""
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
    """Обновляет username, если он был не указан при первом заходе."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE leads SET username = %s WHERE user_id = %s AND manager_code = %s AND username IS NULL",
                (username, user_id, manager_code)
            )
        conn.commit()

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

async def export_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ADMIN_ID:
        await update.message.reply_text("У вас нет доступа к этой команде.")
        return

    if context.args and context.args[0] == "leads":
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute("SELECT manager_code, username FROM leads ORDER BY manager_code, timestamp")
                rows = cur.fetchall()
        if not rows:
            await update.message.reply_text("Пока нет лидов.")
            return
        managers = {}
        for row in rows:
            manager = row['manager_code']
            username = row['username'] if row['username'] else "не указан"
            managers.setdefault(manager, []).append(username)
        text = "📊 Отчёт по лидам:\n\n"
        for manager, usernames in managers.items():
            text += f"Менеджер: {manager}\n"
            text += f"Количество лидов: {len(usernames)}\n"
            usernames_str = ", ".join(f"@{u}" if u != "не указан" else u for u in usernames)
            text += f"Юзернеймы: {usernames_str}\n\n"
        await update.message.reply_text(text)
    else:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute("SELECT manager_code, user_id, username, first_name, last_name, timestamp FROM leads ORDER BY id DESC")
                rows = cur.fetchall()
        if not rows:
            await update.message.reply_text("Пока нет ни одного лида.")
            return
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Менеджер", "User ID", "Username", "Имя", "Фамилия", "Время"])
        for row in rows:
            writer.writerow([row['manager_code'], row['user_id'], row['username'], row['first_name'], row['last_name'], row['timestamp']])
        csv_data = output.getvalue()
        await update.message.reply_document(
            document=io.BytesIO(csv_data.encode('utf-8-sig')),
            filename="leads.csv",
            caption="Список лидов"
        )

# --- Запуск бота ---
def main():
    init_db()
    application = Application.builder().token(BOT_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("export", export_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_username))
    
    logger.info("Бот запущен")
    application.run_polling()

if __name__ == "__main__":
    main()
