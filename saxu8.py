import telebot
from telebot import types
import sqlite3
from datetime import datetime, timedelta
import random
import string
import os
import csv
import config
import photos
import threading
import pytz
import re  # Added for phone validation

# Безопасная загрузка минимального холда
try:
    MIN_HOLD_MINUTES = int(getattr(config, 'MIN_HOLD_MINUTES', 54))
except Exception:
    MIN_HOLD_MINUTES = 54

def adapt_datetime(dt):
    return dt.isoformat()

sqlite3.register_adapter(datetime, adapt_datetime)

def convert_datetime(s):
    if isinstance(s, bytes):
        s = s.decode('utf-8')
    return datetime.fromisoformat(s)

sqlite3.register_converter("DATETIME", convert_datetime)

bot = telebot.TeleBot(config.BOT_TOKEN)

conn = sqlite3.connect('bot.db', check_same_thread=False, detect_types=sqlite3.PARSE_DECLTYPES)
cursor = conn.cursor()

# Initialize database tables
cursor.execute('''
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT,
    reputation REAL DEFAULT 10.0,
    balance REAL DEFAULT 0.0,
    subscription_type TEXT,
    subscription_end DATETIME,
    referral_code TEXT,
    referrals_count INTEGER DEFAULT 0,
    profit_level TEXT DEFAULT 'новичок',
    card_number TEXT,
    cvv TEXT,
    card_balance REAL DEFAULT 0.0,
    card_status TEXT DEFAULT 'inactive',
    card_password TEXT,
    card_activation_date DATETIME,
    phone_number TEXT,
    last_activity DATETIME,
    api_token TEXT,
    block_reason TEXT
)
''')

cursor.execute('''
CREATE TABLE IF NOT EXISTS admins (
    id INTEGER PRIMARY KEY
)
''')

cursor.execute('''
CREATE TABLE IF NOT EXISTS queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    phone_number TEXT UNIQUE,
    added_time DATETIME,
    type TEXT
)
''')

cursor.execute('''
CREATE TABLE IF NOT EXISTS working (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    phone_number TEXT UNIQUE,
    start_time DATETIME,
    admin_id INTEGER,
    type TEXT
)
''')

cursor.execute('''
CREATE TABLE IF NOT EXISTS successful (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    phone_number TEXT,
    hold_time TEXT,
    acceptance_time DATETIME,
    flight_time DATETIME,
    type TEXT
)
''')

cursor.execute('''
CREATE TABLE IF NOT EXISTS blocked (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    phone_number TEXT,
    type TEXT
)
''')

cursor.execute('''
CREATE TABLE IF NOT EXISTS referrals (
    referer_id INTEGER,
    referee_id INTEGER,
    PRIMARY KEY (referer_id, referee_id)
)
''')

cursor.execute('''
CREATE TABLE IF NOT EXISTS withdraw_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    amount REAL,
    status TEXT DEFAULT 'pending'
)
''')

cursor.execute('''
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    action TEXT,
    timestamp DATETIME
)
''')

cursor.execute('''
CREATE TABLE IF NOT EXISTS admin_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id INTEGER,
    action TEXT,
    timestamp DATETIME
)
''')

cursor.execute('''
CREATE TABLE IF NOT EXISTS status (
    key TEXT PRIMARY KEY,
    value TEXT
)
''')

cursor.execute('''
CREATE TABLE IF NOT EXISTS card_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    amount REAL,
    timestamp DATETIME,
    type TEXT
)
''')

cursor.execute('''
CREATE TABLE IF NOT EXISTS transfers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_user_id INTEGER,
    to_user_id INTEGER,
    amount REAL,
    timestamp DATETIME
)
''')

cursor.execute("INSERT OR IGNORE INTO status (key, value) VALUES ('work_status', 'Full work 🟢')")
conn.commit()

# Add initial admin
cursor.execute("INSERT OR IGNORE INTO admins (id) VALUES (?)", (config.ADMIN_IDS[0],))
conn.commit()

pending_activations = {}  # To store admin_id for pending activations
pending_timers = {}  # To store timers for cancellation

def is_subscribed(user_id):
    try:
        member = bot.get_chat_member(config.CHANNEL, user_id)
        return member.status in ['member', 'administrator', 'creator']
    except:
        return False

def generate_referral_code(user_id):
    return f"ref_{user_id}"

def get_profit_level(referrals, is_admin=False):
    if is_admin:
        return 'ADMIN FAX'
    if referrals < 10:
        return 'новичок'
    elif referrals < 30:
        return 'продвинутый'
    elif referrals < 60:
        return 'воркер'
    elif referrals < 90:
        return 'VIP WORK'
    else:
        return 'VIP WORK'

def is_admin(user_id):
    cursor.execute("SELECT * FROM admins WHERE id = ?", (user_id,))
    return cursor.fetchone() is not None

tz = pytz.timezone('Europe/Moscow')

def log_action(user_id, action):
    cursor.execute("INSERT INTO logs (user_id, action, timestamp) VALUES (?, ?, ?)", (user_id, action, datetime.now(tz)))
    conn.commit()

def log_admin_action(admin_id, action):
    cursor.execute("INSERT INTO admin_logs (admin_id, action, timestamp) VALUES (?, ?, ?)", (admin_id, action, datetime.now(tz)))
    conn.commit()

def get_user(user_id):
    cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    row = cursor.fetchone()
    if row:
        columns = [desc[0] for desc in cursor.description]
        return dict(zip(columns, row))
    return None

def update_user(user_id, **kwargs):
    set_clause = ', '.join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [user_id]
    cursor.execute(f"UPDATE users SET {set_clause} WHERE id = ?", values)
    conn.commit()

def get_queue():
    cursor.execute("SELECT * FROM queue ORDER BY added_time ASC")
    rows = cursor.fetchall()
    columns = [desc[0] for desc in cursor.description]
    return [dict(zip(columns, row)) for row in rows]

def get_user_queue(user_id):
    cursor.execute("SELECT * FROM queue WHERE user_id = ? ORDER BY added_time ASC", (user_id,))
    rows = cursor.fetchall()
    columns = [desc[0] for desc in cursor.description]
    return [dict(zip(columns, row)) for row in rows]

def get_working(user_id=None):
    if user_id:
        cursor.execute("SELECT * FROM working WHERE user_id = ?", (user_id,))
    else:
        cursor.execute("SELECT * FROM working")
    rows = cursor.fetchall()
    columns = [desc[0] for desc in cursor.description]
    return [dict(zip(columns, row)) for row in rows]

def get_successful(user_id=None):
    if user_id:
        cursor.execute("SELECT * FROM successful WHERE user_id = ?", (user_id,))
    else:
        cursor.execute("SELECT * FROM successful")
    rows = cursor.fetchall()
    columns = [desc[0] for desc in cursor.description]
    return [dict(zip(columns, row)) for row in rows]

def get_blocked(user_id=None):
    if user_id:
        cursor.execute("SELECT * FROM blocked WHERE user_id = ?", (user_id,))
    else:
        cursor.execute("SELECT * FROM blocked")
    rows = cursor.fetchall()
    columns = [desc[0] for desc in cursor.description]
    return [dict(zip(columns, row)) for row in rows]

def get_status(key):
    cursor.execute("SELECT value FROM status WHERE key = ?", (key,))
    row = cursor.fetchone()
    return row[0] if row else None

def set_status(key, value):
    cursor.execute("REPLACE INTO status (key, value) VALUES (?, ?)", (key, value))
    conn.commit()

def generate_card_number():
    return ''.join(random.choices(string.digits, k=16))

def generate_cvv():
    return ''.join(random.choices(string.digits, k=3))

def generate_api_token():
    part1 = ''.join(random.choices(string.digits, k=4))
    part2 = ''.join(random.choices(string.digits, k=9))
    return f"{part1}:{part2}"

def calculate_hold(accept_time, flight_time):
    delta = flight_time - accept_time
    minutes = delta.total_seconds() / 60
    if minutes >= MIN_HOLD_MINUTES:
        hours = int(minutes // 60)
        mins = int(minutes % 60)
        return f"{hours:02d}:{mins:02d}"
    return None

def get_price_increase(sub_type):
    if not sub_type:
        return config.PRICES['hour'], config.PRICES['30min']
    sub = config.SUBSCRIPTIONS.get(sub_type, {})
    return sub.get('price_increase_hour', 0), sub.get('price_increase_30min', 0)

def sort_queue(queue):
    def key_func(item):
        user = get_user(item['user_id'])
        rep = user['reputation']
        sub = user['subscription_type']
        priority = 0
        if sub == 'VIP Nexus':
            priority = 4
        elif sub == 'Prime Plus':
            priority = 3
        elif sub == 'Gold Tier':
            priority = 2
        elif sub == 'Elite Access':
            priority = 1
        return (-priority, -rep, item['added_time'])
    return sorted(queue, key=key_func)

def show_main_menu(chat_id, edit_message_id=None):
    user = get_user(chat_id)
    if not user:
        return
    username = user['username']
    status = get_status('work_status')
    reputation = user['reputation']
    balance = user['balance']
    queue_count = len(get_queue())
    user_queue_count = len(get_user_queue(chat_id))
    caption = f"@{username} | Full Work\n➢Статус ворка: {status}\n➣Репутация: {reputation}\n➢Баланс: {balance}\n╓Общая очередь: {queue_count}\n║\n╚Твои номера в очереди: {user_queue_count}"
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("Добавить номер 🚀", callback_data="add_number"), types.InlineKeyboardButton("Мои номера 📱", callback_data="my_numbers"))
    markup.add(types.InlineKeyboardButton("Очередь 🔄", callback_data="queue"), types.InlineKeyboardButton("Статистика 📊", callback_data="stats"))
    markup.row(types.InlineKeyboardButton("Мой профиль 👤", callback_data="profile"))
    if edit_message_id:
        bot.edit_message_media(chat_id=chat_id, message_id=edit_message_id, media=types.InputMediaPhoto(photos.PHOTOS['start'], caption=caption), reply_markup=markup)
    else:
        bot.send_photo(chat_id, photos.PHOTOS['start'], caption=caption, reply_markup=markup)

@bot.message_handler(commands=['start'])
def handle_start(message):
    user_id = message.chat.id
    username = message.from_user.username or str(user_id)
    ref = message.text.split()[1] if len(message.text.split()) > 1 else None
    user = get_user(user_id)
    if not user:
        referral_code = generate_referral_code(user_id)
        cursor.execute("INSERT INTO users (id, username, referral_code, last_activity, profit_level) VALUES (?, ?, ?, ?, ?)", (user_id, username, referral_code, datetime.now(tz), 'новичок'))
        conn.commit()
        if ref and ref.startswith('ref_'):
            referer_id = int(ref[4:])
            if referer_id != user_id:
                cursor.execute("INSERT OR IGNORE INTO referrals (referer_id, referee_id) VALUES (?, ?)", (referer_id, user_id))
                conn.commit()
                referer = get_user(referer_id)
                update_user(referer_id, balance=referer['balance'] + config.REFERRAL_REWARD, referrals_count=referer['referrals_count'] + 1)
                referrals = get_user(referer_id)['referrals_count']
                profit = get_profit_level(referrals, is_admin=is_admin(referer_id))
                update_user(referer_id, profit_level=profit)
                bot.send_message(referer_id, f"+${config.REFERRAL_REWARD} за нового реферала [{user_id}]")
                bot.send_photo(referer_id, photos.PHOTOS['new_profit'])
    else:
        update_user(user_id, last_activity=datetime.now(tz))
    if not is_subscribed(user_id):
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("Подписаться 📢", url="https://t.me/NafuzorTime"))
        markup.add(types.InlineKeyboardButton("Проверить ✅", callback_data="check_sub"))
        markup.add(types.InlineKeyboardButton("Правила 📜", callback_data="rules"))
        bot.send_message(user_id, "Добро пожаловать, подпишись чтобы начать работу.", reply_markup=markup)
    else:
        show_main_menu(user_id)

@bot.callback_query_handler(func=lambda call: call.data == "check_sub")
def check_sub(call):
    if is_subscribed(call.from_user.id):
        bot.delete_message(call.message.chat.id, call.message.message_id)
        show_main_menu(call.message.chat.id)
    else:
        bot.answer_callback_query(call.id, "Вы еще не подписаны!", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data == "add_number")
def add_number_type_choice(call):
    caption = "Выберите тип номера"
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("макс", callback_data="add_max"), types.InlineKeyboardButton("вц", callback_data="add_vc"))
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="back_main"))
    bot.edit_message_media(chat_id=call.message.chat.id, message_id=call.message.message_id, media=types.InputMediaPhoto(photos.PHOTOS['start'], caption=caption), reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data in ["add_max", "add_vc"])
def add_number(call):
    number_type = 'max' if call.data == "add_max" else 'vc'
    if number_type == 'max':
        caption = "Введите номер в формате +7XXXXXXXXXX"
    else:
        caption = "Введите номер в формате 9XXXXXXXXX"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="add_number"))
    bot.edit_message_media(chat_id=call.message.chat.id, message_id=call.message.message_id, media=types.InputMediaPhoto(photos.PHOTOS['start'], caption=caption), reply_markup=markup)
    bot.register_next_step_handler_by_chat_id(call.message.chat.id, process_add_number, call.message.message_id, number_type)

def process_add_number(message, message_id=None, number_type=None):
    phone = message.text.strip()
    if number_type == 'max':
        if not re.match(r'\+7\d{10}', phone):
            bot.send_message(message.chat.id, "Неверный формат. Попробуйте снова.")
            add_number_type_choice(types.CallbackQuery(id=str(random.randint(1,10000)), from_user=message.from_user, message=message, data="add_number"))
            return
    else:
        if len(phone) != 10 or not phone.isdigit() or not phone.startswith('9'):
            bot.send_message(message.chat.id, "Неверный формат. Попробуйте снова.")
            add_number_type_choice(types.CallbackQuery(id=str(random.randint(1,10000)), from_user=message.from_user, message=message, data="add_number"))
            return
    cursor.execute("SELECT * FROM queue WHERE phone_number = ?", (phone,))
    if cursor.fetchone():
        bot.send_message(message.chat.id, "Номер уже добавлен.")
        show_main_menu(message.chat.id)
        return
    cursor.execute("INSERT INTO queue (user_id, phone_number, added_time, type) VALUES (?, ?, ?, ?)", (message.chat.id, phone, datetime.now(tz), number_type))
    conn.commit()
    log_action(message.chat.id, f"Добавлен номер {phone} типа {number_type}")
    show_main_menu(message.chat.id)

@bot.callback_query_handler(func=lambda call: call.data == "my_numbers")
def my_numbers(call):
    caption = "Мои номера"
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("В работе ⚙️", callback_data="my_working"), types.InlineKeyboardButton("Ожидает ⏳", callback_data="my_queue"))
    markup.add(types.InlineKeyboardButton("Успешные ✅", callback_data="my_successful"), types.InlineKeyboardButton("Блок 🛑", callback_data="my_blocked"))
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="back_main"))
    bot.edit_message_media(chat_id=call.message.chat.id, message_id=call.message.message_id, media=types.InputMediaPhoto(photos.PHOTOS['start'], caption=caption), reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("my_"))
def show_my_list(call):
    data = call.data
    if data == "my_queue":
        items = get_user_queue(call.message.chat.id)
        title = "Ожидает"
    elif data == "my_working":
        items = get_working(call.message.chat.id)
        title = "В работе"
    elif data == "my_successful":
        items = get_successful(call.message.chat.id)
        title = "Успешные"
    elif data == "my_blocked":
        items = get_blocked(call.message.chat.id)
        title = "Блок"
    caption = f"{title}\n" + "\n".join(f"{item['phone_number']} ({item['type']})" for item in items) if items else f"{title}: Пусто"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="my_numbers"))
    bot.edit_message_caption(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "queue")
def show_queue(call):
    user = get_user(call.message.chat.id)
    sub = user['subscription_type']
    if sub in ['Gold Tier', 'Prime Plus', 'VIP Nexus']:
        queue = sort_queue(get_queue())
        caption = "Очередь:\n" + "\n".join(f"{item['phone_number']} ({item['type']})" for item in queue) if queue else "Очередь пуста"
    else:
        caption = f"Общая очередь: {len(get_queue())}"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="back_main"))
    bot.edit_message_caption(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "stats")
def show_stats(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Доступно только админам", show_alert=True)
        return
    stats = get_successful()
    caption = "Статистика:\n" + "\n".join(f"{get_user(item['user_id'])['username']}-{item['phone_number']} ({item['type']})-холд: {item['hold_time']}" for item in stats if item['hold_time'])
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="back_main"))
    bot.edit_message_caption(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "profile")
def show_profile(call):
    user = get_user(call.message.chat.id)
    username = user['username']
    reputation = user['reputation']
    sub = user['subscription_type'] or ""
    price_hour, price_30 = get_price_increase(sub)
    price_text = f"час-{price_hour}$ 30мин-{price_30}$" if sub else ""
    balance = user['balance']
    caption = f"▶ Юзернейм: @{username}\n╓Репутация: {reputation}\n║\n╚ Подписка: {sub}\n▶ Прайс: {price_text}\n╓ Баланс: ${balance}"
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("Купить подписку 💳", callback_data="buy_sub"), types.InlineKeyboardButton("Реферальная система 🔗", callback_data="referral"))
    markup.add(types.InlineKeyboardButton("Карта 💳", callback_data="card"), types.InlineKeyboardButton("Правила 📜", callback_data="rules"))
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="back_main"))
    bot.edit_message_media(chat_id=call.message.chat.id, message_id=call.message.message_id, media=types.InputMediaPhoto(photos.PHOTOS['profile'], caption=caption), reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "rules")
def show_rules(call):
    rules_text = "Основные правила бота\n1️⃣ Что нельзя делать ни в коем случае!\n‼️‼️ ЮЗЫ НЕ МЕНЯТЬ, КТО БЫ ВАМ НИ ПИСАЛ! ЧТО БЫ ВАМ НИ ПИСАЛИ! ‼️‼️\n‼️‼️ СМЕНИТЕ ЮЗ – ОСТАНЕТЕСЬ БЕЗ ВЫПЛАТЫ! БУДЕТЕ ПОТОМ ЖАЛОВАТЬСЯ! ‼️‼️\n‼️‼️ ЕСЛИ ВАС ПО КАКОЙ-ТО ПРИЧИНЕ ЗАБАНИЛИ (РЕКЛАМА, СКАМ, ПЕРЕЛИВ И Т.Д.) – ЛИШЕНИЕ ВЫПЛАТЫ! ‼️‼️\n\n2️⃣ Если ваш номер отстоял, например, 1 час, вам не нужно делать никаких отчётов.\nМы сами скинем табель в эту группу.\nЧтобы посмотреть, сколько именно отстоял ваш номер, введите команду /hold – она покажет номер и холд! 📊\n\n3️⃣ Как пользоваться ботом?\n\nНажимаете кнопку «Добавить номер».\n\nВписываете номер в формате 9XXXXXXXXX.\n\nЖдёте, пока ваш номер возьмут в работу.\n\nПосле этого вам придёт сообщение:\n\n✆ (Ваш номер) ЗАПРОС АКТИВАЦИИ\n✎ Ограничение времени активации: 2 минуты\n✔ ТВОЙ КОД: (здесь будет код от скупа)\n\nНиже будут две кнопки: «Ввёл» и «Скип».\n\nЕсли нажали «Ввёл», номер перейдёт в раздел «В работе» – это значит, что вы ввели код. ✅\n\nЕсли нажали «Скип», номер удалится из очереди и не будет активирован. ❌\n\n4️⃣ Как узнать статус вашего номера?\nНажимаете кнопку «Мои номера» и выбираете нужный пункт (всего 4):\n\n🔹 В работе – номер ещё стоит.\n🔹 Ожидает – номер в очереди, его ещё не взяли в работу.\n🔹 Успешные – номер с холдом более 54 минут (будет выплата). 💰\n🔹 Блок – номер слетел без холда.\n\n5️⃣ Полезные команды:\n🔸 /hold – показывает ваш холд (только для номеров с холдом от 54 мин).\n🔸 /del – удалить номер из очереди (формат: /del номер).\n🔸 /menu – обновить меню.\n\n6️⃣ Как повысить прайс? 🚀\nВ нашем боте можно повысить прайс с помощью подписки! Цены низкие, а бонусы сочные! 😍\n\nДоступные подписки:\n\nElite Access (+6,4$) 💵 Цена: 2 USDT\n\nGold Tier (+7$) 💰 Цена: 2,3 USDT\n\nPrime Plus (+9$) 🚀 Цена: 3 USDT\n\nVIP Nexus (+15$) 🔥 Цена: 4 USDT\n\nВсе подписки действуют 1 месяц (потом можно купить снова)."
    bot.edit_message_media(chat_id=call.message.chat.id, message_id=call.message.message_id, media=types.InputMediaPhoto(photos.PHOTOS['rules'], caption="Правила"))
    bot.send_message(call.message.chat.id, rules_text, reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("Назад 🔙", callback_data="profile")))

@bot.callback_query_handler(func=lambda call: call.data == "buy_sub")
def buy_sub(call):
    caption = "Купить подписку"
    markup = types.InlineKeyboardMarkup()
    for sub, data in config.SUBSCRIPTIONS.items():
        markup.add(types.InlineKeyboardButton(sub, url=data['payment_link']))
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="profile"))
    bot.edit_message_media(chat_id=call.message.chat.id, message_id=call.message.message_id, media=types.InputMediaPhoto(photos.PHOTOS['buy_sub'], caption=caption), reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "referral")
def show_referral(call):
    user = get_user(call.message.chat.id)
    referrals = user['referrals_count']
    profit = user['profit_level']
    balance = user['balance']
    ref_link = f"https://t.me/{bot.get_me().username}?start={user['referral_code']}"
    caption = f"▶Рефералы: {referrals}\n▶Профит: {profit}\n▶Баланс: {balance}\n▶Твоя рефералка: {ref_link}"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Вывод 💸", callback_data="withdraw"))
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="profile"))
    bot.edit_message_media(chat_id=call.message.chat.id, message_id=call.message.message_id, media=types.InputMediaPhoto(photos.PHOTOS['referral'], caption=caption), reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "withdraw")
def withdraw(call):
    user = get_user(call.message.chat.id)
    if user['balance'] < config.MIN_WITHDRAW:
        bot.answer_callback_query(call.id, "Минимальный вывод $50", show_alert=True)
        return
    caption = "Укажите сумму и юзернейм"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="referral"))
    bot.edit_message_media(chat_id=call.message.chat.id, message_id=call.message.message_id, media=types.InputMediaPhoto(photos.PHOTOS['withdraw'], caption=caption), reply_markup=markup)
    bot.register_next_step_handler_by_chat_id(call.message.chat.id, process_withdraw, call.message.message_id)

def process_withdraw(message, message_id):
    text = message.text.split()
    if len(text) != 2 or not text[0].replace('.', '', 1).isdigit():
        bot.send_message(message.chat.id, "Неверный формат")
        show_profile(types.CallbackQuery(id=str(random.randint(1,10000)), from_user=message.from_user, message=message, data="profile"))
        return
    amount = float(text[0])
    username = text[1]
    user = get_user(message.chat.id)
    if amount > user['balance'] or amount < config.MIN_WITHDRAW:
        bot.send_message(message.chat.id, "Недостаточно средств или ниже минимума")
        show_profile(types.CallbackQuery(id=str(random.randint(1,10000)), from_user=message.from_user, message=message, data="profile"))
        return
    cursor.execute("INSERT INTO withdraw_requests (user_id, amount) VALUES (?, ?)", (message.chat.id, amount))
    conn.commit()
    bot.send_message(message.chat.id, "Заявка создана")
    show_profile(types.CallbackQuery(id=str(random.randint(1,10000)), from_user=message.from_user, message=message, data="profile"))

@bot.callback_query_handler(func=lambda call: call.data == "card")
def show_card(call):
    user = get_user(call.message.chat.id)
    if user['card_status'] == 'blocked':
        if user['block_reason'] == 'admin':
            caption = "Карта заблокирована администратором"
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="profile"))
            bot.edit_message_caption(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)
            return
        elif user['block_reason'] == 'user':
            if user['card_activation_date'] and (datetime.now(tz) - user['card_activation_date']) >= timedelta(days=30):
                update_user(call.message.chat.id, card_status='inactive', block_reason=None)
                user = get_user(call.message.chat.id)  # Reload user
            else:
                remaining = timedelta(days=30) - (datetime.now(tz) - user['card_activation_date'])
                caption = f"Карта заблокирована на 30 дней. Осталось: {remaining.days} дней"
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="profile"))
                bot.edit_message_caption(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)
                return

    if user['card_status'] == 'inactive':
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("Активировать 🔓", callback_data="activate_card"))
        markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="profile"))
        bot.edit_message_media(chat_id=call.message.chat.id, message_id=call.message.message_id, media=types.InputMediaPhoto(photos.PHOTOS['card'], caption="Карта не активирована"), reply_markup=markup)
        return

    # active
    caption = "Введите пароль от карты"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="profile"))
    bot.edit_message_media(chat_id=call.message.chat.id, message_id=call.message.message_id, media=types.InputMediaPhoto(photos.PHOTOS['card'], caption=caption), reply_markup=markup)
    bot.register_next_step_handler_by_chat_id(call.message.chat.id, check_card_password, call.message.message_id)

def check_card_password(message, message_id):
    user = get_user(message.chat.id)
    if message.text != user['card_password']:
        bot.send_message(message.chat.id, "Неверный пароль")
        show_profile(types.CallbackQuery(id=str(random.randint(1,10000)), from_user=message.from_user, message=message, data="profile"))
        return
    display_card(message.chat.id, message_id)

def display_card(chat_id, edit_id):
    user = get_user(chat_id)
    card_num = user['card_number']
    cvv = user['cvv']
    balance = user['card_balance']
    status = 'активна' if user['card_status'] == 'active' else 'заблокирована'
    api_token = user.get('api_token')  # Use .get to avoid KeyError
    if not api_token:
        api_token = generate_api_token()
        update_user(chat_id, api_token=api_token)
    caption = f"💳номер карты: {card_num}\n⚙️CVV: {cvv}\n💰баланс: {balance}\n💾информация о карте: {status}\n\nваш апи токен для подключение карты:\n{api_token}\n!НИВКОЕМ СЛУЧАЕ НИКОМУ ЕГО НЕ ПОКАЗЫВАЙ!"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Настройки ⚙️", callback_data="card_settings"))
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="profile"))
    bot.edit_message_media(chat_id=chat_id, message_id=edit_id, media=types.InputMediaPhoto(photos.PHOTOS['card'], caption=caption), reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "card_settings")
def card_settings(call):
    caption = "Настройки карты"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Заблокировать карту 🛑", callback_data="block_card"))
    markup.add(types.InlineKeyboardButton("API карты 🔑", callback_data="api_card"))
    markup.add(types.InlineKeyboardButton("История 📜", callback_data="card_history_user"))
    markup.add(types.InlineKeyboardButton("Перевести 💸", callback_data="transfer_money"))
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="card"))
    bot.edit_message_caption(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "transfer_money")
def transfer_money(call):
    caption = "Введите юзернейм сумма"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="card_settings"))
    bot.edit_message_caption(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)
    bot.register_next_step_handler_by_chat_id(call.message.chat.id, process_transfer_money, call.message.message_id)

def process_transfer_money(message, message_id):
    text = message.text.split()
    if len(text) != 2 or not text[1].replace('.', '', 1).isdigit():
        bot.send_message(message.chat.id, "Неверный формат")
        card_settings(types.CallbackQuery(id=str(random.randint(1,10000)), from_user=message.from_user, message=message, data="card_settings"))
        return
    to_username = text[0]
    amount = float(text[1])
    from_user = get_user(message.chat.id)
    if amount > from_user['card_balance'] or amount <= 0:
        bot.send_message(message.chat.id, "Недостаточно средств или неверная сумма")
        card_settings(types.CallbackQuery(id=str(random.randint(1,10000)), from_user=message.from_user, message=message, data="card_settings"))
        return
    cursor.execute("SELECT id FROM users WHERE username = ?", (to_username,))
    row = cursor.fetchone()
    if not row:
        bot.send_message(message.chat.id, "Пользователь не найден")
        card_settings(types.CallbackQuery(id=str(random.randint(1,10000)), from_user=message.from_user, message=message, data="card_settings"))
        return
    to_user_id = row[0]
    to_user = get_user(to_user_id)
    if to_user['card_status'] != 'active' and to_user_id != from_user['id']:
        bot.send_message(message.chat.id, "Получатель не имеет активной карты")
        card_settings(types.CallbackQuery(id=str(random.randint(1,10000)), from_user=message.from_user, message=message, data="card_settings"))
        return
    caption = f"Юзернейм: {to_username}\nСумма: {amount}"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Перевести ✅", callback_data=f"confirm_transfer_{to_user_id}_{amount}"))
    markup.add(types.InlineKeyboardButton("Отмена ❌", callback_data="card_settings"))
    bot.edit_message_caption(caption, message.chat.id, message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("confirm_transfer_"))
def confirm_transfer(call):
    parts = call.data.split("_")
    to_user_id = int(parts[2])
    amount = float(parts[3])
    from_user_id = call.from_user.id
    from_user = get_user(from_user_id)
    if amount > from_user['card_balance']:
        bot.answer_callback_query(call.id, "Недостаточно средств", show_alert=True)
        return
    to_user = get_user(to_user_id)
    update_user(from_user_id, card_balance=from_user['card_balance'] - amount)
    update_user(to_user_id, card_balance=to_user['card_balance'] + amount)
    cursor.execute("INSERT INTO card_history (user_id, amount, timestamp, type) VALUES (?, ?, ?, ?)", (from_user_id, -amount, datetime.now(tz), 'transfer_out'))
    cursor.execute("INSERT INTO card_history (user_id, amount, timestamp, type) VALUES (?, ?, ?, ?)", (to_user_id, amount, datetime.now(tz), 'transfer_in'))
    cursor.execute("INSERT INTO transfers (from_user_id, to_user_id, amount, timestamp) VALUES (?, ?, ?, ?)", (from_user_id, to_user_id, amount, datetime.now(tz)))
    conn.commit()
    # Send check photo
    check_caption = f"Юзернейм: {to_user['username']}\nСумма: {amount}\nДата: {datetime.now(tz).strftime('%Y-%m-%d %H:%M:%S')}"
    check_msg = bot.send_photo(call.message.chat.id, photos.PHOTOS['check'] if 'check' in photos.PHOTOS else photos.PHOTOS['start'], caption=check_caption)
    check_link = f"t.me/{bot.get_me().username}/{call.message.chat.id}/{check_msg.message_id}"  # Approximate link
    bot.send_message(call.message.chat.id, f"Ссылка на чек: {check_link}")
    # Notify receiver
    notify_caption = f"Зачисление денежных средств\nЮзернейм: {from_user['username']}\nСумма: {amount}\nДата: {datetime.now(tz).strftime('%Y-%m-%d %H:%M:%S')}"
    bot.send_message(to_user_id, notify_caption)
    bot.answer_callback_query(call.id, "Перевод выполнен")

@bot.callback_query_handler(func=lambda call: call.data == "card_history_user")
def card_history_user(call):
    user_id = call.from_user.id
    cursor.execute("SELECT amount, timestamp, type, id FROM card_history WHERE user_id = ? ORDER BY timestamp DESC", (user_id,))
    rows = cursor.fetchall()
    if not rows:
        caption = "Нет истории"
    else:
        caption = "История операций:"
    markup = types.InlineKeyboardMarkup()
    for row in rows:
        if row[2] in ['deposit', 'transfer_in']:
            sign = '+'
        else:
            sign = '-'
        if row[2] == 'transfer_in':
            cursor.execute("SELECT from_user_id FROM transfers WHERE to_user_id=? AND amount=? AND timestamp=?", (user_id, row[0], row[1]))
            tr = cursor.fetchone()
            other = get_user(tr[0])['username'] if tr else ''
            text = f"{sign}{abs(row[0])} {row[1].strftime('%Y-%m-%d %H:%M')} от {other}"
        elif row[2] == 'transfer_out':
            cursor.execute("SELECT to_user_id FROM transfers WHERE from_user_id=? AND amount=? AND timestamp=?", (user_id, -row[0], row[1]))
            tr = cursor.fetchone()
            other = get_user(tr[0])['username'] if tr else ''
            text = f"{sign}{abs(row[0])} {row[1].strftime('%Y-%m-%d %H:%M')} кому {other}"
        else:
            text = f"{sign}{abs(row[0])} {row[1].strftime('%Y-%m-%d %H:%M')} {row[2]}"
        markup.add(types.InlineKeyboardButton(text, callback_data=f"dummy_history_{row[3]}"))
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="card_settings"))
    bot.edit_message_caption(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("dummy_history_"))
def dummy_history(call):
    bot.answer_callback_query(call.id, "Информация об операции", show_alert=False)

@bot.callback_query_handler(func=lambda call: call.data == "activate_card")
def activate_card(call):
    user = get_user(call.message.chat.id)
    if user['card_status'] != 'inactive':
        bot.answer_callback_query(call.id, "Карта не готова к активации", show_alert=True)
        return
    caption = "Придумайте пароль из 4 цифр"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="card"))
    bot.edit_message_caption(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)
    bot.register_next_step_handler_by_chat_id(call.message.chat.id, set_card_password, call.message.message_id)

def set_card_password(message, message_id):
    password = message.text
    if not password.isdigit() or len(password) != 4:
        bot.send_message(message.chat.id, "Неверный формат")
        show_card(types.CallbackQuery(id=str(random.randint(1,10000)), from_user=message.from_user, message=message, data="card"))
        return
    user_id = message.chat.id
    card_num = generate_card_number()
    cvv = generate_cvv()
    api_token = generate_api_token()
    update_user(user_id, card_number=card_num, cvv=cvv, card_status='active', card_password=password, card_activation_date=datetime.now(tz), api_token=api_token)
    display_card(user_id, message_id)

@bot.callback_query_handler(func=lambda call: call.data == "block_card")
def block_card(call):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Подтвердить ✅", callback_data="confirm_block_card"))
    markup.add(types.InlineKeyboardButton("Отмена ❌", callback_data="card_settings"))
    bot.edit_message_caption("Подтвердите блокировку карты", call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "confirm_block_card")
def confirm_block_card(call):
    user_id = call.from_user.id
    user = get_user(user_id)
    balance = user['card_balance']
    if balance > 0:
        cursor.execute("INSERT INTO card_history (user_id, amount, timestamp, type) VALUES (?, ?, ?, ?)", (user_id, -balance, datetime.now(tz), 'withdraw'))
        conn.commit()
    update_user(user_id, card_status='blocked', block_reason='user', card_balance=0, card_activation_date=datetime.now(tz))
    bot.edit_message_caption("Карта заблокирована, баланс списан", call.message.chat.id, call.message.message_id)
    show_card(call)

@bot.callback_query_handler(func=lambda call: call.data == "api_card")
def api_card(call):
    user = get_user(call.message.chat.id)
    api_token = user.get('api_token')  # Use .get to avoid KeyError
    caption = f"ваш апи токен для подключение карты:\n<code>{api_token}</code>\n!НИВКОЕМ СЛУЧАЕ НИКОМУ ЕГО НЕ ПОКАЗЫВАЙ!"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="card_settings"))
    bot.edit_message_caption(caption, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("back_"))
def back(call):
    data = call.data
    if data == "back_main":
        show_main_menu(call.message.chat.id, call.message.message_id)
    elif data == "back_admin":
        admin_panel(types.SimpleNamespace(chat=types.SimpleNamespace(id=call.message.chat.id)))
        show_main_menu(call.message.chat.id, call.message.message_id)

@bot.message_handler(commands=['admin'])
def admin_panel(message):
    if not is_admin(message.chat.id):
        return
    caption = "Админ панель"
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("Получить номер 📱", callback_data="get_number"), types.InlineKeyboardButton("Сообщить о слёте 🛩️", callback_data="report_flight"))
    markup.row(types.InlineKeyboardButton("Дополнительно ⚙️", callback_data="admin_extra"))
    bot.send_message(message.chat.id, caption, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "get_number")
def get_number(call):
    queue = sort_queue(get_queue())
    if not queue:
        bot.answer_callback_query(call.id, "Очередь пуста", show_alert=True)
        return
    markup = types.InlineKeyboardMarkup()
    for item in queue:
        user = get_user(item['user_id'])
        sub = user['subscription_type'] or ""
        rep = user['reputation']
        button_text = f"{item['phone_number']} ({item['type']})-реп:{rep}-подписка:{sub}"
        markup.add(types.InlineKeyboardButton(button_text, callback_data=f"select_number_{item['phone_number']}"))
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="back_admin"))
    bot.edit_message_text("Выберите номер", call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("select_number_"))
def select_number(call):
    phone = call.data.split("_")[2]
    cursor.execute("SELECT user_id, type FROM queue WHERE phone_number = ?", (phone,))
    row = cursor.fetchone()
    user_id = row[0]
    number_type = row[1]
    user = get_user(user_id)
    sub = user['subscription_type'] or ""
    price_hour, price_30 = get_price_increase(sub)
    rep = user['reputation']
    caption = f"Номер: {phone} ({number_type})\nПодписка: {sub}\nПрайс: час-{price_hour}$ 30мин-{price_30}$\nРепутация: {rep}"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Отправить код 🔑", callback_data=f"send_code_{phone}"))
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="get_number"))
    bot.edit_message_text(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("send_code_"))
def send_code(call):
    phone = call.data.split("_")[2]
    caption = "Отправьте код текстом или фото"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="select_number_" + phone))
    bot.edit_message_text(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)
    bot.register_next_step_handler_by_chat_id(call.message.chat.id, process_send_code, phone, call.from_user.id)

def process_send_code(message, phone, admin_id):
    cursor.execute("SELECT user_id, type FROM queue WHERE phone_number = ?", (phone,))
    row = cursor.fetchone()
    user_id = row[0]
    number_type = row[1]
    caption_base = f"✆ {phone} ЗАПРОС АКТИВАЦИИ\n✎ Ограничение времени активации: 2 минуты"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Ввёл ✅", callback_data=f"entered_{phone}"))
    markup.add(types.InlineKeyboardButton("Скип ❌", callback_data=f"skip_{phone}"))
    if message.photo:
        caption = caption_base + "\n✔ ТВОЙ КОД: (на фото)"
        sent = bot.send_photo(user_id, message.photo[-1].file_id, caption=caption, reply_markup=markup)
    else:
        code = message.text
        caption = caption_base + f"\n✔ ТВОЙ КОД: {code}"
        sent = bot.send_message(user_id, caption, reply_markup=markup)
    bot.send_message(message.chat.id, "Код отправлен")
    cursor.execute("DELETE FROM queue WHERE phone_number = ?", (phone,))
    conn.commit()
    pending_activations[phone] = admin_id
    # Timer to delete after 2 min if not responded
    def delete_msg():
        try:
            bot.delete_message(user_id, sent.message_id)
            bot.send_message(user_id, f"✎ {phone} Время для подтверждения активации истекло. Номер удален из очереди")
            pending_activations.pop(phone, None)
            pending_timers.pop(phone, None)
        except:
            pass
    timer = threading.Timer(120, delete_msg)
    pending_timers[phone] = timer
    timer.start()

@bot.callback_query_handler(func=lambda call: call.data.startswith("entered_"))
def entered(call):
    phone = call.data.split("_")[1]
    user_id = call.from_user.id
    admin_id = pending_activations.pop(phone, None)
    if admin_id is None:
        bot.answer_callback_query(call.id, "Активация истекла", show_alert=True)
        return
    if phone in pending_timers:
        pending_timers[phone].cancel()
        del pending_timers[phone]
    cursor.execute("SELECT type FROM queue WHERE phone_number = ?", (phone,))
    row = cursor.fetchone()
    number_type = row[0] if row else 'unknown'
    cursor.execute("INSERT INTO working (user_id, phone_number, start_time, admin_id, type) VALUES (?, ?, ?, ?, ?)", (user_id, phone, datetime.now(tz), admin_id, number_type))
    conn.commit()
    bot.edit_message_media(chat_id=call.message.chat.id, message_id=call.message.message_id, media=types.InputMediaPhoto(photos.PHOTOS['entered'], caption="Номер в работе"))
    log_action(user_id, f"Ввёл код для {phone}")
    # Notify admin
    bot.send_message(admin_id, f"Пользователь {user_id} ввёл код для {phone}")

@bot.callback_query_handler(func=lambda call: call.data.startswith("skip_"))
def skip(call):
    phone = call.data.split("_")[1]
    admin_id = pending_activations.pop(phone, None)
    if admin_id is None:
        bot.answer_callback_query(call.id, "Активация истекла", show_alert=True)
        return
    if phone in pending_timers:
        pending_timers[phone].cancel()
        del pending_timers[phone]
    bot.edit_message_media(chat_id=call.message.chat.id, message_id=call.message.message_id, media=types.InputMediaPhoto(photos.PHOTOS['skip'], caption="Номер скипнут"))
    log_action(call.from_user.id, f"Скип {phone}")
    # Notify admin
    bot.send_message(admin_id, f"Пользователь {call.from_user.id} скипнул {phone}")

@bot.callback_query_handler(func=lambda call: call.data == "report_flight")
def report_flight(call):
    working = sorted(get_working(), key=lambda x: get_user(x['user_id'])['reputation'], reverse=True)
    if not working:
        bot.answer_callback_query(call.id, "Нет номеров в работе", show_alert=True)
        return
    markup = types.InlineKeyboardMarkup()
    for item in working:
        user = get_user(item['user_id'])
        button_text = f"{item['phone_number']} ({item['type']})-реп:{user['reputation']}"
        markup.add(types.InlineKeyboardButton(button_text, callback_data=f"flight_number_{item['phone_number']}"))
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="back_admin"))
    bot.edit_message_text("Выберите номер для слёта", call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("flight_number_"))
def flight_number(call):
    phone = call.data.split("_")[2]
    caption = "Введите время слёта (ЧЧ:ММ)"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="report_flight"))
    bot.edit_message_text(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)
    bot.register_next_step_handler_by_chat_id(call.message.chat.id, process_flight_time, phone)

def process_flight_time(message, phone):
    flight_str = message.text
    try:
        flight_time = datetime.strptime(flight_str, "%H:%M")
        flight_time = datetime.now(tz).replace(hour=flight_time.hour, minute=flight_time.minute)
    except:
        bot.send_message(message.chat.id, "Неверный формат")
        return
    cursor.execute("SELECT user_id, start_time, type FROM working WHERE phone_number = ?", (phone,))
    row = cursor.fetchone()
    user_id = row[0]
    accept_time = row[1]
    number_type = row[2]
    hold = calculate_hold(accept_time, flight_time)
    caption = f"{phone} ({number_type}) слетел\nхолд: {hold}"
    markup = types.InlineKeyboardMarkup()
    if hold:
        markup.add(types.InlineKeyboardButton("Слёт 🟢", callback_data=f"success_flight_{phone}_{flight_time.timestamp()}_{number_type}"))
    markup.add(types.InlineKeyboardButton("Блок 🛑", callback_data=f"block_flight_{phone}_{number_type}"))
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="flight_number_" + phone))
    bot.send_message(message.chat.id, caption, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("success_flight_"))
def success_flight(call):
    parts = call.data.split("_")
    phone = parts[2]
    ts = float(parts[3])
    number_type = parts[4]
    flight_time = datetime.fromtimestamp(ts, pytz.UTC).astimezone(tz)
    cursor.execute("SELECT user_id, start_time FROM working WHERE phone_number = ?", (phone,))
    row = cursor.fetchone()
    user_id = row[0]
    accept_time = row[1]
    hold = calculate_hold(accept_time, flight_time)
    if hold:
        cursor.execute("INSERT INTO successful (user_id, phone_number, hold_time, acceptance_time, flight_time, type) VALUES (?, ?, ?, ?, ?, ?)", (user_id, phone, hold, accept_time, flight_time, number_type))
        conn.commit()
        bot.send_photo(user_id, photos.PHOTOS['success'], caption=f"{phone} слетел | 🟢успех🟢\n🗒️номер отображается в разделе Успешные\n🆙Введите команду /hold чтобы посмотреть свой холд")
    cursor.execute("DELETE FROM working WHERE phone_number = ?", (phone,))
    conn.commit()
    bot.answer_callback_query(call.id, "Обработано")

@bot.callback_query_handler(func=lambda call: call.data.startswith("block_flight_"))
def block_flight(call):
    parts = call.data.split("_")
    phone = parts[2]
    number_type = parts[3]
    cursor.execute("SELECT user_id FROM working WHERE phone_number = ?", (phone,))
    row = cursor.fetchone()
    user_id = row[0]
    cursor.execute("INSERT INTO blocked (user_id, phone_number, type) VALUES (?, ?, ?)", (user_id, phone, number_type))
    conn.commit()
    cursor.execute("DELETE FROM working WHERE phone_number = ?", (phone,))
    conn.commit()
    bot.send_photo(user_id, photos.PHOTOS['block'], caption=f"{phone} Заблокирован | 🛑блок🛑\n🗒️номер отображается в разделе Блок\n🆙Введите команду /hold чтобы посмотреть свой холд")
    bot.answer_callback_query(call.id, "Обработано")

@bot.callback_query_handler(func=lambda call: call.data == "admin_extra")
def admin_extra(call):
    caption = "Дополнительно"
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("Рассылка 📢", callback_data="broadcast"), types.InlineKeyboardButton("Напоминалка 🔔", callback_data="reminder"))
    markup.add(types.InlineKeyboardButton("Выдать баланс 💰", callback_data="give_balance"), types.InlineKeyboardButton("Выдать репутацию ⭐", callback_data="give_rep"))
    markup.add(types.InlineKeyboardButton("Выдать подписку 🎁", callback_data="give_sub"), types.InlineKeyboardButton("Пользователи с подпиской 📋", callback_data="subs_users"))
    markup.add(types.InlineKeyboardButton("Очистить статистику 🗑️", callback_data="clear_stats"), types.InlineKeyboardButton("Очистить очередь 🗑️", callback_data="clear_queue"))
    markup.add(types.InlineKeyboardButton("Отчёт 📄", callback_data="report"), types.InlineKeyboardButton("Изменить статус ⚙️", callback_data="change_status"))
    markup.add(types.InlineKeyboardButton("Управление подпиской 🛠️", callback_data="manage_sub"), types.InlineKeyboardButton("Настройки бота ⚙️", callback_data="bot_settings"))
    markup.add(types.InlineKeyboardButton("Реферальная система 🔗", callback_data="admin_referral"), types.InlineKeyboardButton("Управления картами 💳", callback_data="manage_cards"))
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="back_admin"))
    bot.edit_message_text(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "broadcast")
def broadcast(call):
    caption = "Выберите тип рассылки"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Обычная рассылка", callback_data="normal_broadcast"))
    markup.add(types.InlineKeyboardButton("Мега рассылка", callback_data="mega_broadcast"))
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="admin_extra"))
    bot.edit_message_text(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "normal_broadcast")
def normal_broadcast(call):
    caption = "Отправьте содержание для обычной рассылки"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="broadcast"))
    bot.edit_message_text(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)
    bot.register_next_step_handler_by_chat_id(call.message.chat.id, process_broadcast)

def process_broadcast(message):
    cursor.execute("SELECT id FROM users")
    users = cursor.fetchall()
    for u in users:
        try:
            if message.photo:
                bot.send_photo(u[0], message.photo[-1].file_id, caption=message.caption)
            elif message.sticker:
                bot.send_sticker(u[0], message.sticker.file_id)
            elif message.video:
                bot.send_video(u[0], message.video.file_id, caption=message.caption)
            elif message.animation:
                bot.send_animation(u[0], message.animation.file_id, caption=message.caption)
            elif message.document:
                bot.send_document(u[0], message.document.file_id, caption=message.caption)
            elif message.audio:
                bot.send_audio(u[0], message.audio.file_id, caption=message.caption)
            else:
                bot.send_message(u[0], message.text)
        except:
            pass
    bot.send_message(message.chat.id, "Рассылка завершена")
    log_admin_action(message.chat.id, "Рассылка")

@bot.callback_query_handler(func=lambda call: call.data == "mega_broadcast")
def mega_broadcast(call):
    caption = "Выберите расположение кнопок"
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("1", callback_data="layout_1"))
    markup.add(types.InlineKeyboardButton("2", callback_data="layout_2"))
    markup.add(types.InlineKeyboardButton("3", callback_data="layout_3"))
    markup.add(types.InlineKeyboardButton("4", callback_data="layout_4"))
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="broadcast"))
    bot.edit_message_text(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("layout_"))
def select_layout(call):
    global mega_layout
    mega_layout = int(call.data.split("_")[1])
    caption = "Введите формат кнопок:\nназвание кнопки ссылка\n..."
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="mega_broadcast"))
    bot.edit_message_text(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)
    bot.register_next_step_handler_by_chat_id(call.message.chat.id, process_mega_buttons)

def process_mega_buttons(message):
    global mega_buttons, mega_layout
    lines = message.text.split('\n')
    if len(lines) > 10:
        bot.send_message(message.chat.id, "Максимум 10 кнопок")
        return
    mega_buttons = []
    for line in lines:
        parts = line.split()
        if len(parts) < 2:
            continue
        name = ' '.join(parts[:-1])
        url = parts[-1]
        mega_buttons.append(types.InlineKeyboardButton(name, url=url))
    bot.send_message(message.chat.id, "Отправьте фото/видео/текст или пропустите")
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Пропустить", callback_data="skip_mega_content"))
    bot.send_message(message.chat.id, "Или нажмите пропустить", reply_markup=markup)
    bot.register_next_step_handler_by_chat_id(message.chat.id, process_mega_content)

def process_mega_content(message):
    global mega_content
    mega_content = message
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Отправить", callback_data="confirm_mega"))
    bot.send_message(message.chat.id, "Подтвердите отправку", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "skip_mega_content")
def skip_mega_content(call):
    global mega_content
    mega_content = None
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Отправить", callback_data="confirm_mega"))
    bot.send_message(call.message.chat.id, "Подтвердите отправку кнопок без содержания", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "confirm_mega")
def confirm_mega(call):
    global mega_buttons, mega_layout, mega_content
    if not mega_buttons:
        bot.answer_callback_query(call.id, "Нет кнопок", show_alert=True)
        return

    # Build markup based on layout
    markup = types.InlineKeyboardMarkup()
    if mega_layout == 1:
        # По 2 в ряд
        for i in range(0, len(mega_buttons), 2):
            row = mega_buttons[i:i+2]
            markup.row(*row)
    elif mega_layout == 2:
        # Чередование 2,1,2,1,...
        idx = 0
        widths = [2, 1]
        w_idx = 0
        while idx < len(mega_buttons):
            width = widths[w_idx % len(widths)]
            row = mega_buttons[idx:idx+width]
            if row:
                markup.row(*row)
            idx += width
            w_idx += 1
    elif mega_layout == 3:
        # Чередование 1,2,1,2,...
        idx = 0
        widths = [1, 2]
        w_idx = 0
        while idx < len(mega_buttons):
            width = widths[w_idx % len(widths)]
            row = mega_buttons[idx:idx+width]
            if row:
                markup.row(*row)
            idx += width
            w_idx += 1
    elif mega_layout == 4:
        # По 1 в ряд
        for btn in mega_buttons:
            markup.row(btn)

    cursor.execute("SELECT id FROM users")
    users = cursor.fetchall()
    for u in users:
        try:
            if mega_content is None:
                bot.send_message(u[0], "Рассылка", reply_markup=markup)
            elif mega_content.photo:
                bot.send_photo(u[0], mega_content.photo[-1].file_id, caption=mega_content.caption, reply_markup=markup)
            elif mega_content.video:
                bot.send_video(u[0], mega_content.video.file_id, caption=mega_content.caption, reply_markup=markup)
            elif mega_content.text:
                bot.send_message(u[0], mega_content.text, reply_markup=markup)
            # Add other types if needed
        except:
            pass
    bot.send_message(call.message.chat.id, "Мега рассылка завершена")
    mega_buttons = []
    mega_layout = None
    mega_content = None

@bot.callback_query_handler(func=lambda call: call.data == "reminder")
def reminder(call):
    queue = sort_queue(get_queue())[:5]
    for i, item in enumerate(queue, 1):
        bot.send_message(item['user_id'], f"📢 СКОРО АКТИВАЦИЯ ТВОЕГО НОМЕРА\n🗣️⚠️ НОМЕР: {item['phone_number']} ({item['type']}) ({i} в очереди)")
    bot.answer_callback_query(call.id, "Напоминания отправлены")
    log_admin_action(call.from_user.id, "Напоминалка")

@bot.callback_query_handler(func=lambda call: call.data == "clear_stats")
def clear_stats(call):
    caption = "Введите пароль (098890)"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="admin_extra"))
    bot.edit_message_text(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)
    bot.register_next_step_handler_by_chat_id(call.message.chat.id, process_clear_stats)

def process_clear_stats(message):
    if message.text != "098890":
        bot.send_message(message.chat.id, "Неверный пароль")
        return
    cursor.execute("DELETE FROM successful")
    conn.commit()
    bot.send_message(message.chat.id, "Статистика очищена")
    log_admin_action(message.chat.id, "Очистка статистики")

@bot.callback_query_handler(func=lambda call: call.data == "clear_queue")
def clear_queue(call):
    caption = "Введите пароль (098890)"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="admin_extra"))
    bot.edit_message_text(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)
    bot.register_next_step_handler_by_chat_id(call.message.chat.id, process_clear_queue)

def process_clear_queue(message):
    if message.text != "098890":
        bot.send_message(message.chat.id, "Неверный пароль")
        return
    cursor.execute("DELETE FROM queue")
    conn.commit()
    bot.send_message(message.chat.id, "Очередь очищена")
    log_admin_action(message.chat.id, "Очистка очереди")

@bot.callback_query_handler(func=lambda call: call.data == "report")
def report(call):
    stats = get_successful()
    text = "\n".join(f"{get_user(item['user_id'])['username']}-{item['phone_number']} ({item['type']})-холд: {item['hold_time']}" for item in stats if item['hold_time'])
    if not text:
        text = "Нет данных"
    with open("report.txt", "w") as f:
        f.write(text)
    bot.send_document(call.message.chat.id, open("report.txt", "rb"))
    log_admin_action(call.from_user.id, "Отчёт")

@bot.callback_query_handler(func=lambda call: call.data == "change_status")
def change_status(call):
    caption = "Выберите статус"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Full work 🟢", callback_data="set_status_Full work 🟢"))
    markup.add(types.InlineKeyboardButton("Stop work 🛑", callback_data="set_status_Stop work 🛑"))
    markup.add(types.InlineKeyboardButton("Pause ⏸️", callback_data="set_status_Pause ⏸️"))
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="admin_extra"))
    bot.edit_message_text(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("set_status_"))
def set_status_call(call):
    status = call.data.split("_")[2]
    set_status('work_status', status)
    bot.answer_callback_query(call.id, "Статус изменен")
    log_admin_action(call.from_user.id, f"Изменен статус на {status}")

@bot.callback_query_handler(func=lambda call: call.data == "give_rep")
def give_rep(call):
    caption = "Введите репутация юзернейм"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="admin_extra"))
    bot.edit_message_text(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)
    bot.register_next_step_handler_by_chat_id(call.message.chat.id, process_give_rep)

def process_give_rep(message):
    parts = message.text.split()
    if len(parts) != 2 or not parts[0].replace('.', '', 1).isdigit():
        bot.send_message(message.chat.id, "Неверный формат")
        return
    rep = float(parts[0])
    username = parts[1].lstrip('@')
    cursor.execute("SELECT id FROM users WHERE username = ?", (username,))
    row = cursor.fetchone()
    if not row:
        bot.send_message(message.chat.id, "Пользователь не найден")
        return
    user_id = row[0]
    update_user(user_id, reputation=rep)
    bot.send_message(message.chat.id, "Репутация выдана")
    log_admin_action(message.chat.id, f"Выдал репутацию {rep} {username}")

@bot.callback_query_handler(func=lambda call: call.data == "give_balance")
def give_balance(call):
    caption = "Введите сумма юзернейм"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="admin_extra"))
    bot.edit_message_text(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)
    bot.register_next_step_handler_by_chat_id(call.message.chat.id, process_give_balance)

def process_give_balance(message):
    parts = message.text.split()
    if len(parts) != 2 or not parts[0].replace('.', '', 1).isdigit():
        bot.send_message(message.chat.id, "Неверный формат")
        return
    amount = float(parts[0])
    username = parts[1].lstrip('@')
    cursor.execute("SELECT id FROM users WHERE username = ?", (username,))
    row = cursor.fetchone()
    if not row:
        bot.send_message(message.chat.id, "Пользователь не найден")
        return
    user_id = row[0]
    update_user(user_id, balance=amount)
    bot.send_message(message.chat.id, "Баланс пополнен")
    log_admin_action(message.chat.id, f"Пополнил баланс {amount} {username}")

@bot.callback_query_handler(func=lambda call: call.data == "subs_users")
def subs_users(call):
    cursor.execute("SELECT username FROM users WHERE subscription_type IS NOT NULL")
    users = cursor.fetchall()
    text = "\n".join(u[0] for u in users)
    bot.send_message(call.message.chat.id, text or "Нет пользователей с подпиской")
    log_admin_action(call.from_user.id, "Проверил пользователей с подпиской")

@bot.callback_query_handler(func=lambda call: call.data == "give_sub")
def give_sub(call):
    caption = "Введите юзернейм подписка месяцы"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="admin_extra"))
    bot.edit_message_text(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)
    bot.register_next_step_handler_by_chat_id(call.message.chat.id, process_give_sub)

def process_give_sub(message):
    parts = message.text.split()
    if len(parts) < 3 or not parts[-1].isdigit() or int(parts[-1]) not in range(1,13):
        bot.send_message(message.chat.id, "Неверный формат")
        return
    username = parts[0].lstrip('@')
    sub_type = ' '.join(parts[1:-1])
    months = int(parts[-1])
    if sub_type not in config.SUBSCRIPTIONS:
        bot.send_message(message.chat.id, "Неверная подписка")
        return
    cursor.execute("SELECT id FROM users WHERE username = ?", (username,))
    row = cursor.fetchone()
    if not row:
        bot.send_message(message.chat.id, "Пользователь не найден")
        return
    user_id = row[0]
    end = datetime.now(tz) + timedelta(days=30*months)
    update_user(user_id, subscription_type=sub_type, subscription_end=end)
    bot.send_message(message.chat.id, "Подписка выдана")
    log_admin_action(message.chat.id, f"Выдал подписку {sub_type} на {months} мес {username}")

@bot.callback_query_handler(func=lambda call: call.data == "manage_sub")
def manage_sub(call):
    caption = "Управление подпиской\nВведите действие: изменить_цену подписка цена, изменить_описание подписка текст и т.д."
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="admin_extra"))
    bot.edit_message_text(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)
    bot.register_next_step_handler_by_chat_id(call.message.chat.id, process_manage_sub)

def process_manage_sub(message):
    # Placeholder
    bot.send_message(message.chat.id, "Действие выполнено (placeholder)")
    log_admin_action(message.chat.id, f"Управление подпиской: {message.text}")

@bot.callback_query_handler(func=lambda call: call.data == "bot_settings")
def bot_settings(call):
    caption = "Настройки бота"
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("Добавить админа ➕", callback_data="add_admin"))
    markup.add(types.InlineKeyboardButton("Убрать админа ➖", callback_data="remove_admin"))
    markup.add(types.InlineKeyboardButton("Список админов 📋", callback_data="list_admins"))
    markup.add(types.InlineKeyboardButton("Лог админов 📝", callback_data="admin_logs_file"))
    markup.add(types.InlineKeyboardButton("All log 📝", callback_data="all_logs"))
    markup.add(types.InlineKeyboardButton("Настройки слета ⚙️", callback_data="flight_settings"))
    markup.add(types.InlineKeyboardButton("Логи пользователя 📝", callback_data="user_logs"))
    markup.add(types.InlineKeyboardButton("Данные карт 📇", callback_data="cards_data"))
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="admin_extra"))
    bot.edit_message_text(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "add_admin")
def add_admin(call):
    caption = "Введите ID админа"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="bot_settings"))
    bot.edit_message_text(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)
    bot.register_next_step_handler_by_chat_id(call.message.chat.id, process_add_admin)

def process_add_admin(message):
    try:
        admin_id = int(message.text)
        cursor.execute("INSERT OR IGNORE INTO admins (id) VALUES (?)", (admin_id,))
        conn.commit()
        bot.send_message(message.chat.id, "Админ добавлен")
        log_admin_action(message.chat.id, f"Добавил админа {admin_id}")
    except:
        bot.send_message(message.chat.id, "Неверный ID")

@bot.callback_query_handler(func=lambda call: call.data == "remove_admin")
def remove_admin(call):
    caption = "Введите ID админа"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="bot_settings"))
    bot.edit_message_text(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)
    bot.register_next_step_handler_by_chat_id(call.message.chat.id, process_remove_admin)

def process_remove_admin(message):
    try:
        admin_id = int(message.text)
        cursor.execute("DELETE FROM admins WHERE id = ?", (admin_id,))
        conn.commit()
        bot.send_message(message.chat.id, "Админ удален")
        log_admin_action(message.chat.id, f"Удалил админа {admin_id}")
    except:
        bot.send_message(message.chat.id, "Неверный ID")

@bot.callback_query_handler(func=lambda call: call.data == "list_admins")
def list_admins(call):
    cursor.execute("SELECT id FROM admins")
    admins = cursor.fetchall()
    text = "\n".join(get_user(a[0])['username'] for a in admins if get_user(a[0]))
    with open("admins.txt", "w") as f:
        f.write(text)
    bot.send_document(call.message.chat.id, open("admins.txt", "rb"))
    log_admin_action(call.from_user.id, "Список админов")

@bot.callback_query_handler(func=lambda call: call.data == "admin_logs_file")
def admin_logs_file(call):
    cursor.execute("SELECT * FROM admin_logs")
    rows = cursor.fetchall()
    with open("admin_logs.csv", "w", newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['id', 'admin_id', 'action', 'timestamp'])
        writer.writerows(rows)
    bot.send_document(call.message.chat.id, open("admin_logs.csv", "rb"))
    log_admin_action(call.from_user.id, "Лог админов")

@bot.callback_query_handler(func=lambda call: call.data == "all_logs")
def all_logs(call):
    cursor.execute("SELECT * FROM logs")
    rows = cursor.fetchall()
    with open("all_logs.csv", "w", newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['id', 'user_id', 'action', 'timestamp'])
        writer.writerows(rows)
    bot.send_document(call.message.chat.id, open("all_logs.csv", "rb"))
    log_admin_action(call.from_user.id, "All log")

@bot.callback_query_handler(func=lambda call: call.data == "flight_settings")
def flight_settings(call):
    caption = f"Текущий минимальный холд для выплат: {MIN_HOLD_MINUTES} минут\nВведите новое значение (целое число):"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="bot_settings"))
    bot.edit_message_text(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)
    bot.register_next_step_handler_by_chat_id(call.message.chat.id, process_flight_settings)

def process_flight_settings(message):
    global MIN_HOLD_MINUTES
    try:
        new_min = int(message.text)
        if new_min > 0:
            MIN_HOLD_MINUTES = new_min
            try:
                config.MIN_HOLD_MINUTES = new_min
            except Exception:
                pass
            bot.send_message(message.chat.id, f"Минимальный холд установлен на {new_min} минут")
            log_admin_action(message.chat.id, f"Изменен мин холд на {new_min}")
        else:
            bot.send_message(message.chat.id, "Значение должно быть положительным")
    except:
        bot.send_message(message.chat.id, "Неверный формат")

@bot.callback_query_handler(func=lambda call: call.data == "user_logs")
def user_logs(call):
    caption = "Введите юзернейм"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="bot_settings"))
    bot.edit_message_text(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)
    bot.register_next_step_handler_by_chat_id(call.message.chat.id, process_user_logs)

def process_user_logs(message):
    username = message.text.lstrip('@')
    cursor.execute("SELECT id FROM users WHERE username = ?", (username,))
    row = cursor.fetchone()
    if not row:
        bot.send_message(message.chat.id, "Пользователь не найден")
        return
    user_id = row[0]
    cursor.execute("SELECT * FROM logs WHERE user_id = ?", (user_id,))
    rows = cursor.fetchall()
    with open(f"{username}_logs.csv", "w", newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['id', 'user_id', 'action', 'timestamp'])
        writer.writerows(rows)
    bot.send_document(message.chat.id, open(f"{username}_logs.csv", "rb"))
    log_admin_action(message.chat.id, f"Логи {username}")

@bot.callback_query_handler(func=lambda call: call.data == "cards_data")
def cards_data(call):
    cursor.execute("SELECT username, card_number, cvv, api_token, card_password, card_balance, card_status FROM users WHERE card_number IS NOT NULL")
    rows = cursor.fetchall()
    text = ""
    for row in rows:
        text += f"Юзернейм- {row[0]}\nНомер карты- {row[1]}\nCvv код- {row[2]}\nАпи токен- {row[3]}\nПароль- {row[4]}\nБаланс- {row[5]}\nСтатус карты- {row[6]}\n\n"
    if not text:
        text = "Нет карт"
    with open("cards_data.txt", "w") as f:
        f.write(text)
    bot.send_document(call.message.chat.id, open("cards_data.txt", "rb"))
    log_admin_action(call.from_user.id, "Данные карт")

@bot.callback_query_handler(func=lambda call: call.data == "admin_referral")
def admin_referral(call):
    caption = "Реферальная система админ"
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("Отчет по рефералам 📄", callback_data="ref_report"))
    markup.add(types.InlineKeyboardButton("Заявки 📋", callback_data="ref_requests"))
    markup.add(types.InlineKeyboardButton("Выдать профит 🎁", callback_data="give_profit"))
    markup.add(types.InlineKeyboardButton("Выдать рефералов ➕", callback_data="give_refs"))
    markup.add(types.InlineKeyboardButton("Отчет по выплатам 📄", callback_data="payout_report"))
    markup.add(types.InlineKeyboardButton("Настройки рефералки ⚙️", callback_data="ref_settings"))
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="admin_extra"))
    bot.edit_message_text(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "ref_report")
def ref_report(call):
    cursor.execute("SELECT username, balance, referrals_count, profit_level FROM users")
    rows = cursor.fetchall()
    text = "\n".join(f"▶{r[0]}-\n▶Баланс- {r[1]}\n▶Рефералы- {r[2]}\n▶Профит- {r[3]}" for r in rows)
    with open("ref_report.txt", "w") as f:
        f.write(text)
    bot.send_document(call.message.chat.id, open("ref_report.txt", "rb"))
    log_admin_action(call.from_user.id, "Отчет по рефералам")

@bot.callback_query_handler(func=lambda call: call.data == "ref_requests")
def ref_requests(call):
    cursor.execute("SELECT * FROM withdraw_requests WHERE status = 'pending'")
    requests = cursor.fetchall()
    if not requests:
        bot.answer_callback_query(call.id, "Нет заявок")
        return
    markup = types.InlineKeyboardMarkup()
    for req in requests:
        user = get_user(req[1])
        button_text = f"Заявка {req[0]} от {user['username']}"
        markup.add(types.InlineKeyboardButton(button_text, callback_data=f"view_req_{req[0]}"))
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="admin_referral"))
    bot.edit_message_text("Заявки", call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("view_req_"))
def view_req(call):
    req_id = int(call.data.split("_")[2])
    cursor.execute("SELECT * FROM withdraw_requests WHERE id = ?", (req_id,))
    req = cursor.fetchone()
    user = get_user(req[1])
    caption = f"Юзернейм: {user['username']}\nСумма выплата: {req[2]}\nПрофит: {user['profit_level']}\nРефералы: {user['referrals_count']}"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Закрыть заявку ❌", callback_data=f"close_req_{req_id}"))
    markup.add(types.InlineKeyboardButton("Оплачено ✅", callback_data=f"paid_req_{req_id}"))
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="ref_requests"))
    bot.edit_message_text(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("close_req_"))
def close_req(call):
    req_id = int(call.data.split("_")[2])
    cursor.execute("UPDATE withdraw_requests SET status = 'closed' WHERE id = ?", (req_id,))
    conn.commit()
    bot.answer_callback_query(call.id, "Заявка закрыта")
    log_admin_action(call.from_user.id, f"Закрыл заявку {req_id}")

@bot.callback_query_handler(func=lambda call: call.data.startswith("paid_req_"))
def paid_req(call):
    req_id = int(call.data.split("_")[2])
    cursor.execute("SELECT user_id, amount FROM withdraw_requests WHERE id = ?", (req_id,))
    req = cursor.fetchone()
    update_user(req[0], balance = get_user(req[0])['balance'] - req[1])
    cursor.execute("UPDATE withdraw_requests SET status = 'paid' WHERE id = ?", (req_id,))
    conn.commit()
    bot.send_message(req[0], "Выплата одобрена ✔️")
    bot.answer_callback_query(call.id, "Оплачено")
    log_admin_action(call.from_user.id, f"Оплачено заявка {req_id}")

@bot.callback_query_handler(func=lambda call: call.data == "give_profit")
def give_profit(call):
    caption = "Введите юзернейм профит"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="admin_referral"))
    bot.edit_message_text(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)
    bot.register_next_step_handler_by_chat_id(call.message.chat.id, process_give_profit)

def process_give_profit(message):
    parts = message.text.split()
    if len(parts) < 2:
        bot.send_message(message.chat.id, "Неверный формат")
        return
    username = parts[0].lstrip('@')
    profit = ' '.join(parts[1:])
    cursor.execute("SELECT id FROM users WHERE username = ?", (username,))
    row = cursor.fetchone()
    if not row:
        bot.send_message(message.chat.id, "Пользователь не найден")
        return
    user_id = row[0]
    update_user(user_id, profit_level=profit)
    bot.send_message(message.chat.id, "Профит выдан")
    log_admin_action(message.chat.id, f"Выдал профит {profit} {username}")

@bot.callback_query_handler(func=lambda call: call.data == "give_refs")
def give_refs(call):
    caption = "Введите юзернейм рефералы"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="admin_referral"))
    bot.edit_message_text(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)
    bot.register_next_step_handler_by_chat_id(call.message.chat.id, process_give_refs)

def process_give_refs(message):
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        bot.send_message(message.chat.id, "Неверный формат")
        return
    username = parts[0].lstrip('@')
    refs = int(parts[1])
    cursor.execute("SELECT id FROM users WHERE username = ?", (username,))
    row = cursor.fetchone()
    if not row:
        bot.send_message(message.chat.id, "Пользователь не найден")
        return
    user_id = row[0]
    update_user(user_id, referrals_count=refs)
    profit = get_profit_level(refs, is_admin=is_admin(user_id))
    update_user(user_id, profit_level=profit)
    bot.send_message(message.chat.id, "Рефералы выданы")
    log_admin_action(message.chat.id, f"Выдал рефералов {refs} {username}")

@bot.callback_query_handler(func=lambda call: call.data == "payout_report")
def payout_report(call):
    cursor.execute("SELECT * FROM withdraw_requests WHERE status = 'paid'")
    rows = cursor.fetchall()
    text = ""
    for r in rows:
        user = get_user(r[1])
        text += f"▶ Юзернейм: {user['username']}\n▶ Дата: {datetime.now(tz)}\n▶ Сумма запроса на выплату: {r[2]}\n▶ Сумма выплаты: {r[2]}\n▶ Рефералы: {user['referrals_count']}\n▶ Профит: {user['profit_level']}\n╓ админ: {get_user(call.from_user.id)['username']}\n║ \n╚ время: {datetime.now(tz).strftime('%H:%M:%S')}\n\n"
    with open("payout_report.txt", "w") as f:
        f.write(text)
    bot.send_document(call.message.chat.id, open("payout_report.txt", "rb"))
    log_admin_action(call.from_user.id, "Отчет по выплатам")

@bot.callback_query_handler(func=lambda call: call.data == "ref_settings")
def ref_settings(call):
    caption = "Введите новую цену за реферала"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="admin_referral"))
    bot.edit_message_text(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)
    bot.register_next_step_handler_by_chat_id(call.message.chat.id, process_ref_settings)

def process_ref_settings(message):
    try:
        new_price = float(message.text)
        config.REFERRAL_REWARD = new_price
        bot.send_message(message.chat.id, "Цена изменена")
        log_admin_action(message.chat.id, f"Изменена цена рефералки на {new_price}")
    except:
        bot.send_message(message.chat.id, "Неверный формат")

@bot.callback_query_handler(func=lambda call: call.data == "manage_cards")
def manage_cards(call):
    caption = "Управления картами"
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("Заблокировать карту 🛑", callback_data="block_card_admin"))
    markup.add(types.InlineKeyboardButton("Разблокировать карту 🔓", callback_data="unblock_card_admin"))
    markup.add(types.InlineKeyboardButton("Начислить выплату 💰", callback_data="payout_cards"))
    markup.add(types.InlineKeyboardButton("Выдать баланс 💸", callback_data="give_card_balance"))
    markup.add(types.InlineKeyboardButton("Списать баланс ❌", callback_data="deduct_card_balance"))
    markup.add(types.InlineKeyboardButton("История пополнение и списание 📜", callback_data="card_history"))
    markup.add(types.InlineKeyboardButton("Пользователи с картой 📋", callback_data="users_with_card"))
    markup.add(types.InlineKeyboardButton("Заблокированные карты 🛑", callback_data="blocked_cards"))
    markup.add(types.InlineKeyboardButton("Разблокированы карты 🔓", callback_data="unblocked_cards"))
    markup.add(types.InlineKeyboardButton("Заблокировать все карты 🛑", callback_data="block_all_cards"))
    markup.add(types.InlineKeyboardButton("Разблокировать все карты 🔓", callback_data="unblock_all_cards"))
    markup.add(types.InlineKeyboardButton("Отчет по пользователям 📄", callback_data="users_report"))
    markup.add(types.InlineKeyboardButton("Начислить выплату 💰", callback_data="payout_cards"))
    markup.add(types.InlineKeyboardButton("Смотреть переводы 🔍", callback_data="view_transfers"))
    markup.add(types.InlineKeyboardButton("бд", callback_data="card_db"))
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="admin_extra"))
    bot.edit_message_text(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "view_transfers")
def view_transfers(call):
    caption = "Введите юзернейм"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="manage_cards"))
    bot.edit_message_text(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)
    bot.register_next_step_handler_by_chat_id(call.message.chat.id, process_view_transfers)

def process_view_transfers(message):
    username = message.text.lstrip('@')
    cursor.execute("SELECT id FROM users WHERE username = ?", (username,))
    row = cursor.fetchone()
    if not row:
        bot.send_message(message.chat.id, "Пользователь не найден")
        return
    user_id = row[0]
    cursor.execute("SELECT from_user_id, to_user_id, amount, timestamp FROM transfers WHERE from_user_id = ? OR to_user_id = ?", (user_id, user_id))
    rows = cursor.fetchall()
    if not rows:
        bot.send_message(message.chat.id, "Нет переводов")
        return
    text = ""
    for row in rows:
        from_username = get_user(row[0])['username']
        to_username = get_user(row[1])['username']
        direction = "Отправлено" if row[0] == user_id else "Получено"
        text += f"{direction} {row[2]} от @{from_username} к @{to_username} {row[3]}\n"
    bot.send_message(message.chat.id, text)
    log_admin_action(message.chat.id, f"Просмотр переводов {username}")

@bot.callback_query_handler(func=lambda call: call.data == "card_db")
def card_db(call):
    cursor.execute("SELECT username, card_number, cvv, card_password, card_activation_date FROM users WHERE card_number IS NOT NULL")
    rows = cursor.fetchall()
    text = ""
    for row in rows:
        text += f"{row[0]}\n{row[1]}\n{row[2]}\n{row[3]}\n{row[4]}\n\n"
    if not text:
        text = "Нет карт"
    with open("cards_db.txt", "w") as f:
        f.write(text)
    bot.send_document(call.message.chat.id, open("cards_db.txt", "rb"))
    log_admin_action(call.from_user.id, "Просмотр бд карт")

@bot.callback_query_handler(func=lambda call: call.data == "block_card_admin")
def block_card_admin(call):
    caption = "Введите юзернейм"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="manage_cards"))
    bot.edit_message_text(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)
    bot.register_next_step_handler_by_chat_id(call.message.chat.id, process_block_card_admin)

def process_block_card_admin(message):
    username = message.text.lstrip('@')
    cursor.execute("SELECT id FROM users WHERE username = ?", (username,))
    row = cursor.fetchone()
    if not row:
        bot.send_message(message.chat.id, "Пользователь не найден")
        return
    user_id = row[0]
    user = get_user(user_id)
    balance = user['card_balance']
    if balance > 0:
        cursor.execute("INSERT INTO card_history (user_id, amount, timestamp, type) VALUES (?, ?, ?, ?)", (user_id, -balance, datetime.now(tz), 'withdraw'))
        conn.commit()
    update_user(user_id, card_status='blocked', block_reason='admin', card_balance=0.0, card_number=None, cvv=None, card_password=None, api_token=None, card_activation_date=None)
    bot.send_message(message.chat.id, "Карта заблокирована администратором")
    log_admin_action(message.chat.id, f"Заблокировал карту {username}")

@bot.callback_query_handler(func=lambda call: call.data == "unblock_card_admin")
def unblock_card_admin(call):
    caption = "Введите юзернейм"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="manage_cards"))
    bot.edit_message_text(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)
    bot.register_next_step_handler_by_chat_id(call.message.chat.id, process_unblock_card_admin)

def process_unblock_card_admin(message):
    username = message.text.lstrip('@')
    cursor.execute("SELECT id FROM users WHERE username = ?", (username,))
    row = cursor.fetchone()
    if not row:
        bot.send_message(message.chat.id, "Пользователь не найден")
        return
    user_id = row[0]
    update_user(user_id, card_status='inactive', block_reason=None)
    bot.send_message(message.chat.id, "Карта разблокирована, пользователь может активировать заново")
    log_admin_action(message.chat.id, f"Разблокировал карту {username}")

@bot.callback_query_handler(func=lambda call: call.data == "payout_cards")
def payout_cards(call):
    # Backup successful table to CSV
    cursor.execute("SELECT * FROM successful")
    rows = cursor.fetchall()
    backup_filename = f"successful_backup_{datetime.now(tz).strftime('%Y-%m-%d_%H-%M-%S')}.csv"
    with open(backup_filename, "w", newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['id', 'user_id', 'phone_number', 'hold_time', 'acceptance_time', 'flight_time', 'type'])
        writer.writerows(rows)
    bot.send_document(call.message.chat.id, open(backup_filename, "rb"))

    # Generate report with payouts
    stats = get_successful()
    report_filename = f"payout_report_{datetime.now(tz).strftime('%Y-%m-%d_%H-%M-%S')}.csv"
    with open(report_filename, "w", newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['username', 'phone_number', 'type', 'hold_time', 'payout'])
        payouts = {}
        for item in stats:
            user_id = item['user_id']
            user = get_user(user_id)
            sub = user['subscription_type']
            hour_price, min30_price = get_price_increase(sub)
            hold = item['hold_time']
            if hold:
                hours, mins = map(int, hold.split(':'))
                payout = hours * hour_price + (mins // 30) * min30_price
                if user_id not in payouts:
                    payouts[user_id] = 0
                payouts[user_id] += payout
                writer.writerow([user['username'], item['phone_number'], item['type'], hold, payout])

    for user_id, total_payout in payouts.items():
        user = get_user(user_id)
        update_user(user_id, card_balance=user['card_balance'] + total_payout)
        cursor.execute("INSERT INTO card_history (user_id, amount, timestamp, type) VALUES (?, ?, ?, ?)", (user_id, total_payout, datetime.now(tz), 'deposit'))
        bot.send_message(user_id, f"Вам пришла выплата {total_payout}$")

    conn.commit()

    bot.send_document(call.message.chat.id, open(report_filename, "rb"))
    # Send to group/channel
    try:
        bot.send_document(config.CHANNEL, open(report_filename, "rb"))
    except:
        pass  # If fails, ignore

    # Clear successful table
    cursor.execute("DELETE FROM successful")
    conn.commit()

    bot.answer_callback_query(call.id, "Выплаты начислены, отчет отправлен, статистика очищена")
    log_admin_action(call.from_user.id, "Начислил выплаты на карты")

@bot.callback_query_handler(func=lambda call: call.data == "give_card_balance")
def give_card_balance(call):
    caption = "Введите сумма юзернейм"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="manage_cards"))
    bot.edit_message_text(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)
    bot.register_next_step_handler_by_chat_id(call.message.chat.id, process_give_card_balance)

def process_give_card_balance(message):
    parts = message.text.split()
    if len(parts) != 2 or not parts[0].replace('.', '', 1).isdigit():
        bot.send_message(message.chat.id, "Неверный формат")
        return
    amount = float(parts[0])
    username = parts[1].lstrip('@')
    cursor.execute("SELECT id FROM users WHERE username = ?", (username,))
    row = cursor.fetchone()
    if not row:
        bot.send_message(message.chat.id, "Пользователь не найден")
        return
    user_id = row[0]
    user = get_user(user_id)
    update_user(user_id, card_balance=user['card_balance'] + amount)
    cursor.execute("INSERT INTO card_history (user_id, amount, timestamp, type) VALUES (?, ?, ?, ?)", (user_id, amount, datetime.now(tz), 'deposit'))
    conn.commit()
    bot.send_message(user_id, f"Вам пришла выплата {amount}$")
    bot.send_message(message.chat.id, "Баланс выдан")
    log_admin_action(message.chat.id, f"Выдал баланс карты {amount} {username}")

@bot.callback_query_handler(func=lambda call: call.data == "deduct_card_balance")
def deduct_card_balance(call):
    caption = "Введите сумма юзернейм"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="manage_cards"))
    bot.edit_message_text(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)
    bot.register_next_step_handler_by_chat_id(call.message.chat.id, process_deduct_card_balance)

def process_deduct_card_balance(message):
    parts = message.text.split()
    if len(parts) != 2 or not parts[0].replace('.', '', 1).isdigit():
        bot.send_message(message.chat.id, "Неверный формат")
        return
    amount = float(parts[0])
    username = parts[1].lstrip('@')
    cursor.execute("SELECT id FROM users WHERE username = ?", (username,))
    row = cursor.fetchone()
    if not row:
        bot.send_message(message.chat.id, "Пользователь не найден")
        return
    user_id = row[0]
    user = get_user(user_id)
    new_balance = user['card_balance'] - amount
    if new_balance < 0:
        new_balance = 0
        amount = user['card_balance']
    update_user(user_id, card_balance=new_balance)
    cursor.execute("INSERT INTO card_history (user_id, amount, timestamp, type) VALUES (?, ?, ?, ?)", (user_id, -amount, datetime.now(tz), 'withdraw'))
    conn.commit()
    bot.send_message(message.chat.id, "Баланс списан")
    log_admin_action(message.chat.id, f"Списал баланс карты {amount} {username}")

@bot.callback_query_handler(func=lambda call: call.data == "card_history")
def card_history(call):
    cursor.execute("SELECT u.username, h.amount, h.timestamp, h.type, h.id FROM card_history h JOIN users u ON h.user_id = u.id ORDER BY h.timestamp DESC")
    rows = cursor.fetchall()
    text = ""
    for r in rows:
        if r[3] in ['deposit', 'transfer_in']:
            sign = '+'
        else:
            sign = '-'
        text += f"{r[0]} {sign}{abs(r[1])} {r[2]} {r[3]}\n"
    with open("card_history.txt", "w") as f:
        f.write(text)
    bot.send_document(call.message.chat.id, open("card_history.txt", "rb"))
    log_admin_action(call.from_user.id, "История карт")

@bot.callback_query_handler(func=lambda call: call.data == "users_with_card")
def users_with_card(call):
    cursor.execute("SELECT username, card_activation_date FROM users WHERE card_number IS NOT NULL")
    rows = cursor.fetchall()
    text = "\n".join(f"{r[0]} {r[1]}" for r in rows)
    with open("users_with_card.txt", "w") as f:
        f.write(text)
    bot.send_document(call.message.chat.id, open("users_with_card.txt", "rb"))
    log_admin_action(call.from_user.id, "Пользователи с картой")

@bot.callback_query_handler(func=lambda call: call.data == "blocked_cards")
def blocked_cards(call):
    cursor.execute("SELECT username FROM users WHERE card_status = 'blocked'")
    rows = cursor.fetchall()
    text = "\n".join(r[0] for r in rows)
    with open("blocked_cards.txt", "w") as f:
        f.write(text)
    bot.send_document(call.message.chat.id, open("blocked_cards.txt", "rb"))
    log_admin_action(call.from_user.id, "Заблокированные карты")

@bot.callback_query_handler(func=lambda call: call.data == "unblocked_cards")
def unblocked_cards(call):
    cursor.execute("SELECT username FROM users WHERE card_status = 'active'")
    rows = cursor.fetchall()
    text = "\n".join(r[0] for r in rows)
    with open("unblocked_cards.txt", "w") as f:
        f.write(text)
    bot.send_document(call.message.chat.id, open("unblocked_cards.txt", "rb"))
    log_admin_action(call.from_user.id, "Разблокированы карты")

@bot.callback_query_handler(func=lambda call: call.data == "block_all_cards")
def block_all_cards(call):
    cursor.execute("SELECT id, card_balance FROM users WHERE card_number IS NOT NULL")
    rows = cursor.fetchall()
    for row in rows:
        user_id = row[0]
        balance = row[1]
        if balance > 0:
            cursor.execute("INSERT INTO card_history (user_id, amount, timestamp, type) VALUES (?, ?, ?, ?)", (user_id, -balance, datetime.now(tz), 'withdraw'))
    cursor.execute("UPDATE users SET card_status = 'blocked', block_reason='admin', card_balance=0.0, card_number=NULL, cvv=NULL, card_password=NULL, api_token=NULL, card_activation_date=NULL WHERE card_number IS NOT NULL")
    conn.commit()
    bot.answer_callback_query(call.id, "Все карты заблокированы администратором")
    log_admin_action(call.from_user.id, "Заблокировал все карты")

@bot.callback_query_handler(func=lambda call: call.data == "unblock_all_cards")
def unblock_all_cards(call):
    cursor.execute("UPDATE users SET card_status = 'inactive', block_reason=NULL WHERE card_number IS NOT NULL")
    conn.commit()
    bot.answer_callback_query(call.id, "Все карты разблокированы, пользователи могут активировать заново")
    log_admin_action(call.from_user.id, "Разблокировал все карты")

@bot.callback_query_handler(func=lambda call: call.data == "users_report")
def users_report(call):
    cursor.execute("SELECT username FROM users")
    rows = cursor.fetchall()
    text = "\n".join(r[0] for r in rows)
    with open("users_report.txt", "w") as f:
        f.write(text)
    bot.send_document(call.message.chat.id, open("users_report.txt", "rb"))
    log_admin_action(call.from_user.id, "Отчет по пользователям")

@bot.callback_query_handler(func=lambda call: call.data == "back_admin")
def back_admin(call):
    fake_message = types.Message(message_id=call.message.message_id, from_user=call.from_user, chat=call.message.chat, text='/admin', date=0)
    admin_panel(fake_message)

@bot.message_handler(commands=['hold'])
def hold(message):
    successful = get_successful(message.chat.id)
    text = "\n".join(f"{item['phone_number']} ({item['type']}) холд: {item['hold_time']}" for item in successful if item['hold_time'])
    bot.send_message(message.chat.id, text or f"Нет холдов >= {MIN_HOLD_MINUTES} мин")

@bot.message_handler(commands=['del'])
def del_number(message):
    phone = message.text.split()[1] if len(message.text.split()) > 1 else None
    if not phone:
        bot.send_message(message.chat.id, "Формат /del номер")
        return
    cursor.execute("DELETE FROM queue WHERE phone_number = ? AND user_id = ?", (phone, message.chat.id))
    conn.commit()
    bot.send_message(message.chat.id, "Номер удален" if cursor.rowcount > 0 else "Номер не найден")
    log_action(message.chat.id, f"Удалил номер {phone}")

@bot.message_handler(commands=['menu'])
def menu(message):
    show_main_menu(message.chat.id)

@bot.message_handler(commands=['holdall'])
def holdall(message):
    if not is_admin(message.chat.id):
        return
    successful = get_successful()
    text = "\n".join(f"{get_user(item['user_id'])['username']} {item['phone_number']} ({item['type']}) холд: {item['hold_time']}" for item in successful if item['hold_time'])
    bot.send_message(message.chat.id, text or "Нет холдов")

@bot.message_handler(commands=['queue'])
def queue_cmd(message):
    user = get_user(message.chat.id)
    sub = user['subscription_type']
    if sub not in ['Gold Tier', 'Prime Plus', 'VIP Nexus']:
        bot.send_message(message.chat.id, "Доступно только с подпиской")
        return
    queue = sort_queue(get_queue())
    text = "\n".join(f"{item['phone_number']} ({item['type']})" for item in queue)
    bot.send_message(message.chat.id, text or "Очередь пуста")

@bot.message_handler(commands=['moder'])
def moder(message):
    user = get_user(message.chat.id)
    sub = user['subscription_type']
    if sub not in ['Prime Plus', 'VIP Nexus']:
        bot.send_message(message.chat.id, "Доступно только с подпиской")
        return
    # Placeholder
    bot.send_message(message.chat.id, "Модер выдан")

@bot.message_handler(commands=['mut'])
def mut(message):
    user = get_user(message.chat.id)
    sub = user['subscription_type']
    if sub != 'VIP Nexus':
        bot.send_message(message.chat.id, "Доступно только с подпиской VIP Nexus")
        return
    parts = message.text.split()
    if len(parts) != 3:
        bot.send_message(message.chat.id, "Формат /mut юзернейм время")
        return
    username = parts[1]
    time = parts[2]
    # Placeholder
    bot.send_message(message.chat.id, f"Мут выдан {username} на {time}")

@bot.message_handler(commands=['help'])
def help_cmd(message):
    text = "/start - Перезапуск бота\n/help - Список команд\n/hold - Твой холд\n/del - Удалить номер из очереди (формат /del номер)\n/menu - Обновить меню"
    bot.send_message(message.chat.id, text)

def check_inactivity():
    threshold = datetime.now(tz) - timedelta(days=config.INACTIVITY_DAYS)
    cursor.execute("SELECT id FROM users WHERE last_activity < ?", (threshold,))
    inactive = cursor.fetchall()
    for u in inactive:
        cursor.execute("SELECT referer_id FROM referrals WHERE referee_id = ?", (u[0],))
        referers = cursor.fetchall()
        for ref in referers:
            referer = get_user(ref[0])
            update_user(ref[0], balance=max(0, referer['balance'] - config.REFERRAL_REWARD), referrals_count=referer['referrals_count'] - 1)
            bot.send_message(ref[0], f"-$ {config.REFERRAL_REWARD}: реферал {u[0]} неактивен")

threading.Timer(86400, check_inactivity).start()

# Global variables for mega broadcast
mega_layout = None
mega_buttons = []
mega_content = None

bot.infinity_polling()