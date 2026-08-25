import os
import logging
import csv
import io
import threading
import asyncio
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

DEFAULT_LINKS = {
    "houses": "https://example.com/sale",
    "projects": "https://example.com/projects",
    "kalinov": "https://example.com/kalinov",
    "manager": "https://t.me/username_manager"
}

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
            cur.execute('''
                CREATE TABLE IF NOT EXISTS links (
                    key TEXT PRIMARY KEY,
                    url TEXT NOT NULL
                )
            ''')
        conn.commit()
    with get_conn() as conn:
        with conn.cursor() as cur:
            for key, url in DEFAULT_LINKS.items():
                cur.execute("""
                    INSERT INTO links (key, url)
                    VALUES (%s, %s)
                    ON CONFLICT (key) DO NOTHING
                """, (key, url))
        conn.commit()

def get_link(key):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT url FROM links WHERE key = %s", (key,))
            row = cur.fetchone()
            return row[0] if row else DEFAULT_LINKS.get(key)

def set_link(key, url):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO links (key, url)
                VALUES (%s, %s)
                ON CONFLICT (key) DO UPDATE SET url = EXCLUDED.url
            """, (key, url))
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

# --- Клавиатура администратора (reply) ---
def get_admin_reply_keyboard():
    keyboard = [[KeyboardButton("Меню")]]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

async def maybe_show_reply_keyboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id == ADMIN_ID:
        if not context.user_data.get('reply_keyboard_shown'):
            reply_keyboard = get_admin_reply_keyboard()
            await update.message.reply_text(
                "Клавиатура администратора активирована. Нажмите кнопку «Меню» внизу.",
                reply_markup=reply_keyboard
            )
            context.user_data['reply_keyboard_shown'] = True

# --- Обработчик команды /start ---
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

    # Для администратора: показываем reply-клавиатуру и сообщение
    if user.id == ADMIN_ID:
        await maybe_show_reply_keyboard(update, context)
        await update.message.reply_text("Вы администратор. Используйте кнопку «Меню» или команду /admin для управления.")
        return

    # Для обычного пользователя: запускаем анкету
    if user.username:
        await send_channel_invite(update, context)
        await ask_beds(update, context)
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

        await update.message.reply_text(f"Отлично! Ваш username @{username} сохранён.")
        await send_channel_invite(update, context)

        if user.id != ADMIN_ID:
            await ask_beds(update, context)

        context.user_data['awaiting_username'] = False
    else:
        if update.effective_user.id == ADMIN_ID and context.user_data.get('awaiting_link_for'):
            await handle_new_link(update, context)

async def send_channel_invite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("📢 Перейти в канал", url=CHANNEL_LINK)]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "Нажмите кнопку ниже, чтобы подписаться на наш канал:",
        reply_markup=reply_markup
    )

# --- Анкета (вопросы) ---
async def ask_beds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("2", callback_data="beds_2"),
         InlineKeyboardButton("3", callback_data="beds_3"),
         InlineKeyboardButton("4", callback_data="beds_4"),
         InlineKeyboardButton("5 и более", callback_data="beds_5+")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "Сколько спален вы хотите?",
        reply_markup=reply_markup
    )

async def handle_beds_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    beds_choice = query.data
    context.user_data['beds_choice'] = beds_choice

    keyboard = [
        [InlineKeyboardButton("1", callback_data="floors_1"),
         InlineKeyboardButton("2", callback_data="floors_2"),
         InlineKeyboardButton("3", callback_data="floors_3")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        "Сколько вам нужно этажей?",
        reply_markup=reply_markup
    )

async def handle_floors_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    floors_choice = query.data
    context.user_data['floors_choice'] = floors_choice

    links = {
        "houses": get_link("houses"),
        "projects": get_link("projects"),
        "kalinov": get_link("kalinov"),
        "manager": get_link("manager")
    }

    keyboard = [
        [InlineKeyboardButton("ДОМА В ПРОДАЖЕ", url=links["houses"])],
        [InlineKeyboardButton("ТИПОВЫЕ ПРОЕКТЫ", url=links["projects"])],
        [InlineKeyboardButton("КАЛИНОВ ПАРК", url=links["kalinov"])],
        [InlineKeyboardButton("ПЕРСОНАЛЬНЫЙ МЕНЕДЖЕР", url=links["manager"])]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        "Выберите категорию для просмотра:",
        reply_markup=reply_markup
    )

    # Отправляем сводку администратору
    user = query.from_user
    manager_code = context.user_data.get('manager_code', 'unknown')
    beds = context.user_data.get('beds_choice', 'не указано')
    floors = context.user_data.get('floors_choice', 'не указано')
    username = user.username if user.username else "не указан"
    time_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    summary = (
        f"📋 Описание заявки\n"
        f"Username: @{username}\n"
        f"Менеджер: {manager_code}\n"
        f"Время: {time_now}\n"
        f"Спальни: {beds.replace('beds_', '') if beds != 'beds_5+' else '5+'}\n"
        f"Этажи: {floors.replace('floors_', '')}"
    )
    if ADMIN_ID:
        await context.bot.send_message(chat_id=ADMIN_ID, text=summary)

# --- Админ-панель для ссылок ---
async def links_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("У вас нет доступа.")
        return

    keyboard = [
        [InlineKeyboardButton("Дома в продаже", callback_data="edit_links_houses")],
        [InlineKeyboardButton("Типовые проекты", callback_data="edit_links_projects")],
        [InlineKeyboardButton("Калинов парк", callback_data="edit_links_kalinov")],
        [InlineKeyboardButton("Менеджер", callback_data="edit_links_manager")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "Выберите ссылку для замены:",
        reply_markup=reply_markup
    )

async def handle_edit_link_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        await query.edit_message_text("У вас нет доступа.")
        return

    data = query.data
    key = data.replace("edit_links_", "")
    context.user_data['awaiting_link_for'] = key

    await query.edit_message_text(
        f"Отправьте новую ссылку для '{key}' (например, https://...):"
    )

async def handle_new_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("У вас нет доступа.")
        return

    key = context.user_data.get('awaiting_link_for')
    if not key:
        return

    new_url = update.message.text.strip()
    set_link(key, new_url)
    context.user_data.pop('awaiting_link_for', None)

    await update.message.reply_text(f"Ссылка для '{key}' обновлена на:\n{new_url}")

# --- Команды администратора (меню) ---
async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("У вас нет доступа.")
        return

    await show_inline_menu(update, context)

async def show_inline_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📊 Экспорт CSV", callback_data="export_csv")],
        [InlineKeyboardButton("📋 Экспорт лидов (текст)", callback_data="export_leads")],
        [InlineKeyboardButton("🗑 Очистить базу данных", callback_data="confirm_clear")],
        [InlineKeyboardButton("🔗 Настройки ссылок", callback_data="links_settings")]
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
    elif data == "links_settings":
        await links_settings(update, context)
    elif data.startswith("edit_links_"):
        await handle_edit_link_callback(update, context)

# --- Обработчик reply-кнопок ---
async def handle_reply_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id == ADMIN_ID:
        await show_inline_menu(update, context)
    else:
        await update.message.reply_text("У вас нет доступа.")

# --- Экспорт и очистка (как раньше) ---
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

    if update.callback_query:
        await update.callback_query.message.reply_text("База данных очищена.")
    else:
        await update.message.reply_text("База данных очищена.")

# --- Ежедневный бэкап ---
async def daily_backup(application: Application):
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
        logger.info("Ежедневный бэкап отправлен")
    except Exception as e:
        logger.error(f"Ошибка при бэкапе: {e}")

def backup_job(application):
    asyncio.run(daily_backup(application))

# --- Запуск Flask в отдельном потоке ---
def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT)

# --- Запуск бота ---
def main():
    init_db()
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin_menu))
    application.add_handler(CommandHandler("export", admin_menu))
    application.add_handler(CommandHandler("menu", admin_menu))
    application.add_handler(CommandHandler("links", links_settings))

    application.add_handler(MessageHandler(filters.Text(["Меню"]), handle_reply_menu))

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_username))

    application.add_handler(CallbackQueryHandler(handle_beds_callback, pattern="^beds_"))
    application.add_handler(CallbackQueryHandler(handle_floors_callback, pattern="^floors_"))
    application.add_handler(CallbackQueryHandler(button_handler))

    threading.Thread(target=run_flask, daemon=True).start()

    scheduler = BackgroundScheduler()
    scheduler.add_job(
        lambda: backup_job(application),
        'cron',
        hour=3,
        minute=20,
        timezone="Etc/GMT-2"
    )
    scheduler.start()

    logger.info("Бот запущен")
    application.run_polling()

if __name__ == "__main__":
    main()
