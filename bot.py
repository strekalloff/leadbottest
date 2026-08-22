import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import sqlite3
from datetime import datetime

# --- Настройки ---
BOT_TOKEN = "8660596044:AAFlLyoMEnLk7HI8WZK_qGCI_TP1MPBCZso"
CHANNEL_LINK = "https://t.me/ssylka0987654321"  # публичная ссылка или invite link
DB_FILE = "leads.db"

# --- Логирование ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Работа с базой данных ---
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS leads
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  manager_code TEXT,
                  user_id INTEGER,
                  username TEXT,
                  first_name TEXT,
                  last_name TEXT,
                  timestamp TEXT)''')
    conn.commit()
    conn.close()

def add_lead(manager_code, user_id, username, first_name, last_name):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    timestamp = datetime.now().isoformat()
    c.execute("INSERT INTO leads (manager_code, user_id, username, first_name, last_name, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
              (manager_code, user_id, username, first_name, last_name, timestamp))
    conn.commit()
    conn.close()
    logger.info(f"Новый лид: manager={manager_code}, user_id={user_id}, username={username}")

# --- Обработчики команд ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает команду /start с параметром менеджера"""
    user = update.effective_user
    manager_code = context.args[0] if context.args else "unknown"
    
    # Сохраняем лид в базу
    add_lead(
        manager_code=manager_code,
        user_id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name
    )
    
    # Приветствие
    await update.message.reply_text(
        f"Привет, {user.first_name}! 👋\n"
        "Спасибо, что перешли по ссылке.\n"
        "Чтобы мы могли связаться с вами, нам нужен ваш Telegram username."
    )
    
    # Если username уже есть, сразу предлагаем канал
    if user.username:
        await send_channel_invite(update, context)
    else:
        # Просим отправить username
        await update.message.reply_text(
            "Пожалуйста, отправьте ваш @username (например, @ivan_petrov) одним сообщением."
        )
        # Устанавливаем состояние ожидания username
        context.user_data['awaiting_username'] = True
        context.user_data['manager_code'] = manager_code

async def handle_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получает username от пользователя, если его не было в профиле"""
    if context.user_data.get('awaiting_username'):
        username = update.message.text.strip()
        # Убираем @ если есть
        if username.startswith('@'):
            username = username[1:]
        
        # Обновляем запись в базе (упрощённо – можно добавить отдельную функцию)
        # Здесь мы просто сохраняем в context, но в реальности нужно обновить БД
        user = update.effective_user
        manager_code = context.user_data.get('manager_code', 'unknown')
        
        # Обновляем username в базе (можно добавить функцию update_username)
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("UPDATE leads SET username=? WHERE user_id=? AND manager_code=? AND username IS NULL",
                  (username, user.id, manager_code))
        conn.commit()
        conn.close()
        
        await update.message.reply_text(f"Отлично! Ваш username @{username} сохранён.")
        
        # Предлагаем перейти в канал
        await send_channel_invite(update, context)
        
        context.user_data['awaiting_username'] = False
    else:
        # Если не ожидали username, просто игнорируем или обрабатываем другие сообщения
        pass

async def send_channel_invite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отправляет кнопку для перехода в канал"""
    keyboard = [[InlineKeyboardButton("📢 Перейти в канал", url=CHANNEL_LINK)]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "Нажмите кнопку ниже, чтобы подписаться на наш канал:",
        reply_markup=reply_markup
    )

# --- Запуск бота ---
def main():
    init_db()
    application = Application.builder().token(BOT_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_username))
    
    logger.info("Бот запущен")
    application.run_polling()

if __name__ == "__main__":
    main()