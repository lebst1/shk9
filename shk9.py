import logging
import asyncio
import re
import time
import sqlite3

from datetime import datetime, timedelta

# Настройки базы данных
DB_FILE = "message_stats.db"


# Создание таблицы, если она не существует
async def create_table():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS message_stats (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            last_message_time TIMESTAMP,
            messages_hour INTEGER DEFAULT 0,
            messages_day INTEGER DEFAULT 0,
            messages_total INTEGER DEFAULT 0
        )
    ''')
    conn.commit()
    conn.close()


# Вызов функции для создания таблицы при старте бота
loop = asyncio.get_event_loop()
loop.run_until_complete(create_table())


async def update_user_stats(user_id: int, username: str):
    """Обновляет статистику сообщений пользователя."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    # Проверяем, есть ли уже запись для этого пользователя
    cursor.execute("SELECT * FROM message_stats WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()

    now = datetime.now()
    if row:
        # Пользователь уже существует, обновляем статистику
        last_message_time = datetime.strptime(row[2], '%Y-%m-%d %H:%M:%S.%f')
        if now - last_message_time <= timedelta(hours=1): 
            cursor.execute('''
                UPDATE message_stats SET 
                    last_message_time = ?, 
                    messages_hour = messages_hour + 1,
                    messages_day = messages_day + 1,
                    messages_total = messages_total + 1
                WHERE user_id = ?
            ''', (now, user_id))
        elif now - last_message_time <= timedelta(days=1):
            cursor.execute('''
                UPDATE message_stats SET 
                    last_message_time = ?, 
                    messages_hour = 1,
                    messages_day = messages_day + 1,
                    messages_total = messages_total + 1
                WHERE user_id = ?
            ''', (now, user_id))
        else:
            cursor.execute('''
                UPDATE message_stats SET 
                    last_message_time = ?, 
                    messages_hour = 1,
                    messages_day = 1,
                    messages_total = messages_total + 1
                WHERE user_id = ?
            ''', (now, user_id))
    else:
        # Новый пользователь, создаём запись
        cursor.execute('''
            INSERT INTO message_stats (user_id, username, last_message_time, messages_hour, messages_day, messages_total)
            VALUES (?, ?, ?, 1, 1, 1)
        ''', (user_id, username, now))

    conn.commit()
    conn.close()


from aiogram import Dispatcher, Bot, executor, types, filters
from aiogram.contrib.fsm_storage.memory import MemoryStorage
   

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ParseMode, ReplyKeyboardMarkup, KeyboardButton, \
    ChatPermissions
from aiogram.dispatcher.filters.state import StatesGroup, State
from aiogram.dispatcher import FSMContext 
from aiogram.contrib.middlewares.logging import LoggingMiddleware



CHANNEL_ID = -1002288345419
ADMIN_CHAT_ID = -4566366187

authorized_users = {983681689, 1228200514}

blocked_words = []
banned_users = []
moderation_chat_id = -4538417819

moderation_messages = {}

logging.basicConfig(level=logging.INFO)

bot = Bot('7389926138:AAGs4rlT6bw9OlxDSj8b7qYMJYo_UDXloTg')
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

# Глобальный словарь для отслеживания времени блокировки
user_cooldowns = {}
# Задержка в секундах (1 минута)
MESSAGE_DELAY = 5

# Словарь для хранения замученных пользователей (user_id: unmute_time)
muted_users = {}

deleted_messages_count = 0

# Список ID администраторов канала
ADMIN_ID_DELETE = [1228200514] # Замените на реальные ID администраторов

# Айди админов
ADMINS = [1228200514]  # ID администраторов

# Cловарь для зранения фото
moderation_photos = {}


dp.middleware.setup(LoggingMiddleware())

class DeletePost(StatesGroup):
    waiting_for_post = State()

@dp.message_handler(commands=['start'])
async def start(message: types.Message):
    """
    Обработчик команды /start.
    Приветствует пользователя по имени и предлагает выбрать режим отправки.
    """
    user_name = message.from_user.first_name
    await message.answer(
        f"👋 Привет, {user_name}! Рассказывай, чё подслушал)\n"
    )

    keyboard = ReplyKeyboardMarkup(row_width=1)
    keyboard.add(
        KeyboardButton("Отправить сообщение 📨")
    )
    await message.answer("Нажми на кнопку, чтобы отправить сообщение.", reply_markup=keyboard)

@dp.message_handler(text="Отправить сообщение 📨")
async def handle_send_message(message: types.Message):
    keyboard = InlineKeyboardMarkup(resize_keyboard=True)
    keyboard.row(
        InlineKeyboardButton("👻 Анонимно", callback_data="send_anon"), 
        InlineKeyboardButton("😎 Публично", callback_data="send_public")
    )
    await message.answer(f"🤫 Выбери режим отправки: ", reply_markup=keyboard)


class SendMessageStates(StatesGroup):
    waiting_for_message = State()


@dp.callback_query_handler(lambda c: c.data.startswith('send_'))
async def choose_send_mode(callback_query: types.CallbackQuery, state: FSMContext):
    """
    Обработчик выбора режима отправки. Сохраняет выбранный режим,
    сообщает пользователю и переводит в состояние ожидания сообщения.
    """
    send_mode = callback_query.data.split('_')[1]
    await dp.storage.set_data(chat=callback_query.from_user.id, data={'send_mode': send_mode})
    await callback_query.message.edit_text(
        f"✅ Режим отправки: {'анонимный' if send_mode == 'anon' else 'публичный'}\n"
        f"✍️ Теперь отправь своё сообщение:"
    )
    await state.set_state(SendMessageStates.waiting_for_message)


# --- Вспомогательные функции ---

async def delayed_send(message: types.Message, send_mode: str):
    """
    Функция для отправки сообщения без задержки.
    """
    if any(word in message.text for word in blocked_words):
        moderation_messages[message.message_id] = message
        await bot.send_message(moderation_chat_id,
                               f"Сообщение на модерации:\n🆔 ID: {message.message_id}\n👨🏻‍💻 Пользователь: {message.from_user.username}\nദ്ദി(˵ •̀ ᴗ - ˵ ) ✧ Текст: {message.text}")
    else:
        if send_mode == 'anon':
            await bot.copy_message(CHANNEL_ID, message.from_user.id, message.message_id)
        else:
            await message.forward(CHANNEL_ID)


def update_blocked_words(new_words):
    global blocked_words
    blocked_words.extend(new_words)


async def send_chat_notification(message: types.Message):
    """
    Отправляет сообщение администратору о новом сообщении,
    включая текст сообщения, тип контента и ID пользователя.
    """
    try:
        user = message.from_user
        text = (
            f"✉️ Новое сообщение от 👤 {user.mention} ({user.full_name})\n"
            f"🆔 ID: tg://user?id= {user.id} \n"
            f"⏰ Время отправки: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        )
        if message.text:
            text += f"💬 Текст: {message.text}\n"
        elif message.caption:
            text += f"📎 Файл: {message.caption}\n"
        else:
            text += "📎 Файл без подписи\n"

        await bot.send_message(
            ADMIN_CHAT_ID,
            text
        )
    except Exception as e:
        logging.error(f"Ошибка при отправке уведомления администратору: {e}")

class AdminStates(StatesGroup):
    waiting_for_admin_id = State()


async def is_admin(user_id: int) -> bool:
    """Проверяет, является ли пользователь администратором."""
    return user_id in ADMINS


@dp.message_handler(commands=['addword'])
async def add_blocked_word(message: types.Message):
    try:
        new_word = message.text.split(' ', 1)[1]  # Пытаемся взять второй элемент после разделения по пробелу
        update_blocked_words([new_word])
        await message.answer(f"Слово '{new_word}' добавлено в список заблокированных.")
    except IndexError:
        await message.answer("Пожалуйста, укажите слово для добавления после команды /addword.")


# --- Обработчики команд ---
@dp.message_handler(state=SendMessageStates.waiting_for_message, content_types=types.ContentType.ANY)
async def handle_message(message: types.Message, state: FSMContext):
    """
    Обработчик сообщений. Проверяет на задержку, запрещенный контент и отправляет сообщение.
    """
    global user_cooldowns
    user_id = message.from_user.id

    if user_id in user_cooldowns and user_cooldowns[user_id] > asyncio.get_event_loop().time():
        remaining_time = int(user_cooldowns[user_id] - asyncio.get_event_loop().time())
        await message.reply(f"⏳ Следующее сообщение можно отправить через {remaining_time} секунд.")
        return

    user_data = await dp.storage.get_data(chat=message.from_user.id)
    send_mode = user_data.get('send_mode', 'anon')

    try:
        await delayed_send(message, send_mode)
        await send_chat_notification(message)

        user_cooldowns[user_id] = asyncio.get_event_loop().time() + MESSAGE_DELAY
        if any(word in message.text for word in blocked_words):
            await message.answer(
                "В вашем сообщении обнаружены запрещенные слова, поэтому ваше сообщение отправлено на модерацию. Ожидайте 👮")
        else:
            await message.reply("🚀 Сообщение отправлено!")

    except Exception as e:
        logging.error(f"Ошибка при обработке сообщения: {e}")
        await message.reply("⚠️ Произошла ошибка.")
    finally:
        # Сброс состояния
        await state.finish()

@dp.message_handler(lambda message: message.text.lower().startswith('одобрить'))
async def approve_message(message: types.Message):
    if len(message.text.split()) > 1:
        message_id = int(message.text.split()[1])
        if message_id in moderation_messages:
            approved_message = moderation_messages[message_id]
            await bot.send_message(CHANNEL_ID, approved_message.text)
            await message.answer(f"✅ Сообщение одобрено и отправлено в канал")
        else:
            await message.answer(f"🚫 Сообщение не найдено")
    else:
        await message.answer("Используйте команду в формате: одобрить <id сообщения>")


@dp.message_handler(commands=['отклонить'])
async def reject_message(message: types.Message):
    await message.answer(f"❌ Сообщение отклонено")


@dp.callback_query_handler(lambda query: query.data == "approve")
async def approve_callback(query: types.CallbackQuery):
    await bot.send_message(CHANNEL_ID, "Сообщение одобрено")
    await query.answer(" Сообщение одобрено и отправлено в канал")


@dp.callback_query_handler(lambda query: query.data == "reject")
async def reject_callback(query: types.CallbackQuery):
    await query.answer(" Сообщение отклонено")

# <-------------------------------------------------------> #

@dp.message_handler(content_types=types.ContentType.PHOTO)
async def handle_photo(message: types.Message):
    photo_id = message.photo[-1].file_id
    moderation_photos[message.message_id] = message
    sender_info = f"Отправитель: {message.from_user.first_name}"
    if message.from_user.username:
        sender_info += f" (@{message.from_user.username})"
    caption = f"{sender_info}\n"
    caption += message.caption if message.caption else "Нет описания"
    
    # Создание инлайн клавиатуры с кнопками "Одобрить" и "Отклонить"
    inline_keyboard = InlineKeyboardMarkup()
    inline_keyboard.row(
        InlineKeyboardButton("Одобрить", callback_data=f"approve_photo {message.message_id}"),
        InlineKeyboardButton("Отклонить", callback_data=f"reject_photo {message.message_id}")
    )
    
    await bot.send_photo(ADMIN_CHAT_ID, photo_id, caption=caption, reply_markup=inline_keyboard)



@dp.callback_query_handler(lambda query: query.data.startswith('approve_photo'))
async def approve_photo_callback(query: types.CallbackQuery):
    message_id = int(query.data.split()[1])
    if message_id in moderation_photos:
        approved_photo = moderation_photos[message_id]
        await bot.send_photo(CHANNEL_ID, approved_photo.photo[-1].file_id, caption=approved_photo.caption)
        await query.answer("Фото одобрено и отправлено в канал")
    else:
        await query.answer("Фото не найдено")

@dp.callback_query_handler(lambda query: query.data.startswith('reject_photo'))
async def reject_photo_callback(query: types.CallbackQuery):
    message_id = int(query.data.split()[1])
    if message_id in moderation_photos:
        del moderation_photos[message_id]
        await query.answer("Фото отклонено")
    else:
        await query.answer("Фото не найдено")




@dp.message_handler(lambda message: any(entity.type == 'url' and 'youtube.com' in message.text for entity in message.entities), content_types=types.ContentType.TEXT)
async def block_youtube_links(message: types.Message):
    await message.reply("Извините, отправка YouTube ссылок запрещена.")


# Создание группы состояний
class States(StatesGroup):
    waiting_for_message = State()

# Обработчик команд
@dp.message_handler(commands=['watch'])
async def start_command(message: types.Message):
    await message.answer("Я слежу за постом в канале!")


async def handle_messages(message: types.Message):
    user_id = message.from_user.id
    
    # Check if the message contains a GIF or sticker
    if message.sticker or message.animation:
        if user_id not in authorized_users:
            await message.reply("🚫 Вы не можете отправлять GIF или стикеры.")
            return  # Stop processing this message


@dp.message_handler()
async def handle_message(message: types.Message):
    if message.text:  # Проверяем наличие текста в сообщении
        user_id = message.from_user.id
        username = message.from_user.username
        await update_user_stats(user_id, username)
    else:
        await message.reply("⚠️ Пустое сообщение. Пожалуйста, отправьте текстовое сообщение.")

if __name__ == '__main__':
    executor.start_polling(dp, skip_updates=True)
