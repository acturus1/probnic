import logging
import asyncio
import random
import sqlite3
import os
import time
from datetime import datetime, timedelta
import sys
from typing import Optional

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv
from aiogram.exceptions import TelegramBadRequest

# Загрузка переменных окружения
load_dotenv()

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('bot.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

# Конфигурация
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x.strip()) for x in os.getenv(
    "ADMIN_IDS", "1735089952").split(",") if x.strip()]

if not BOT_TOKEN:
    logger.error("❌ BOT_TOKEN не найден в .env файле!")
    exit(1)

logger.info(f"✅ Бот запускается с токеном: {BOT_TOKEN[:10]}...")
logger.info(f"✅ Администраторы: {ADMIN_IDS}")

MIN_BET = 10
MAX_BET = 10000
INITIAL_BALANCE = 1000

# Инициализация бота
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Состояния FSM


class UserStates(StatesGroup):
    setting_bet = State()
    admin_balance = State()
    admin_broadcast = State()
    auto_spin_settings = State()

# База данных


class Database:
    def __init__(self, db_path="casino_bot.db"):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self.init_db()

    def get_connection(self):
        """Получить соединение с повторными попытками при блокировке"""
        max_retries = 5
        for attempt in range(max_retries):
            try:
                if self._conn is None:
                    self._conn = sqlite3.connect(
                        self.db_path,
                        check_same_thread=False,
                        timeout=30.0
                    )
                    # Настройки для лучшей производительности
                    self._conn.execute("PRAGMA journal_mode=WAL")
                    self._conn.execute("PRAGMA synchronous=NORMAL")
                    self._conn.execute("PRAGMA busy_timeout=5000")
                    self._conn.row_factory = sqlite3.Row
                return self._conn
            except sqlite3.OperationalError as e:
                if "locked" in str(e) and attempt < max_retries - 1:
                    time.sleep(0.1 * (attempt + 1))
                    continue
                raise

    def execute_with_retry(self, query: str, params=(), max_retries=3):
        """Выполнить запрос с повторными попытками"""
        for attempt in range(max_retries):
            try:
                conn = self.get_connection()
                cursor = conn.cursor()
                cursor.execute(query, params)
                conn.commit()
                return cursor
            except sqlite3.OperationalError as e:
                if "locked" in str(e) and attempt < max_retries - 1:
                    time.sleep(0.1 * (attempt + 1))
                    # Пересоздаем соединение
                    if self._conn:
                        try:
                            self._conn.close()
                        except:
                            pass
                    self._conn = None
                    continue
                logger.error(f"Database error after {
                             attempt + 1} attempts: {e}")
                raise

    def init_db(self):
        try:
            cursor = self.execute_with_retry('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    full_name TEXT,
                    balance REAL DEFAULT 1000.0,
                    current_bet REAL DEFAULT 10.0,
                    total_spins INTEGER DEFAULT 0,
                    total_wins INTEGER DEFAULT 0,
                    total_wagered REAL DEFAULT 0,
                    total_won REAL DEFAULT 0,
                    biggest_win REAL DEFAULT 0,
                    win_streak INTEGER DEFAULT 0,
                    max_win_streak INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            self.execute_with_retry('''
                CREATE TABLE IF NOT EXISTS game_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    bet_amount REAL,
                    win_amount REAL,
                    symbols TEXT,
                    is_win BOOLEAN,
                    rtp REAL,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            self.execute_with_retry('''
                CREATE TABLE IF NOT EXISTS bonuses (
                    user_id INTEGER PRIMARY KEY,
                    last_bonus TIMESTAMP,
                    streak INTEGER DEFAULT 0
                )
            ''')

            self.execute_with_retry('''
                CREATE TABLE IF NOT EXISTS admin_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    admin_id INTEGER,
                    action TEXT,
                    details TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            self.execute_with_retry('''
                CREATE TABLE IF NOT EXISTS auto_spin_settings (
                    user_id INTEGER PRIMARY KEY,
                    stop_win REAL DEFAULT 0,
                    stop_loss REAL DEFAULT 0,
                    min_balance REAL DEFAULT 0,
                    speed_mode TEXT DEFAULT 'normal',
                    display_mode TEXT DEFAULT 'full',
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users (user_id)
                )
            ''')

        except Exception as e:
            logger.error(f"Error initializing database: {e}")

    def get_user(self, user_id: int):
        try:
            cursor = self.execute_with_retry(
                'SELECT * FROM users WHERE user_id = ?',
                (user_id,)
            )
            user = cursor.fetchone()

            if not user:
                self.execute_with_retry(
                    'INSERT INTO users (user_id, balance, current_bet) VALUES (?, ?, ?)',
                    (user_id, INITIAL_BALANCE, MIN_BET)
                )
                cursor = self.execute_with_retry(
                    'SELECT * FROM users WHERE user_id = ?',
                    (user_id,)
                )
                user = cursor.fetchone()

            return dict(user) if user else {}
        except Exception as e:
            logger.error(f"Error in get_user: {e}")
            return {'user_id': user_id, 'balance': INITIAL_BALANCE, 'current_bet': MIN_BET}

    def update_user(self, user_id: int, **kwargs):
        if not kwargs:
            return

        try:
            set_clause = ", ".join([f"{k} = ?" for k in kwargs.keys()])
            values = list(kwargs.values()) + [user_id]

            self.execute_with_retry(
                f'UPDATE users SET {
                    set_clause}, last_active = CURRENT_TIMESTAMP WHERE user_id = ?',
                values
            )
        except Exception as e:
            logger.error(f"Error in update_user: {e}")

    def add_game_record(self, user_id: int, bet: float, win: float,
                        symbols: str, is_win: bool):
        try:
            rtp = (win / bet * 100) if bet > 0 else 0

            self.execute_with_retry(
                '''INSERT INTO game_history (user_id, bet_amount, win_amount, symbols, is_win, rtp)
                   VALUES (?, ?, ?, ?, ?, ?)''',
                (user_id, bet, win, symbols, is_win, rtp)
            )

            # Обновляем статистику пользователя
            self.execute_with_retry(
                '''UPDATE users SET total_spins = total_spins + 1,
                    total_wagered = total_wagered + ?,
                    total_won = total_won + ?,
                    biggest_win = MAX(biggest_win, ?)
                   WHERE user_id = ?''',
                (bet, win, win, user_id)
            )

            if is_win:
                self.execute_with_retry(
                    '''UPDATE users SET total_wins = total_wins + 1,
                        win_streak = win_streak + 1,
                        max_win_streak = MAX(max_win_streak, win_streak + 1)
                       WHERE user_id = ?''',
                    (user_id,)
                )
            else:
                self.execute_with_retry(
                    'UPDATE users SET win_streak = 0 WHERE user_id = ?',
                    (user_id,)
                )

        except Exception as e:
            logger.error(f"Error in add_game_record: {e}")

    def get_statistics(self, user_id: int = None):
        try:
            if user_id:
                cursor = self.execute_with_retry(
                    '''SELECT 
                        COUNT(*) as total_spins,
                        SUM(bet_amount) as total_wagered,
                        SUM(win_amount) as total_won,
                        AVG(rtp) as avg_rtp
                    FROM game_history 
                    WHERE user_id = ?''',
                    (user_id,)
                )
            else:
                cursor = self.execute_with_retry('''
                    SELECT 
                        COUNT(*) as total_spins,
                        SUM(bet_amount) as total_wagered,
                        SUM(win_amount) as total_won,
                        AVG(rtp) as avg_rtp,
                        COUNT(DISTINCT user_id) as total_players
                    FROM game_history
                ''')

            result = cursor.fetchone()
            columns = [desc[0] for desc in cursor.description]

            return dict(zip(columns, result)) if result else {}

        except Exception as e:
            logger.error(f"Error in get_statistics: {e}")
            return {}

    def get_top_players(self, limit: int = 10):
        try:
            cursor = self.execute_with_retry(
                '''SELECT 
                    user_id,
                    username,
                    total_won,
                    total_spins,
                    balance
                FROM users 
                WHERE total_spins > 0 
                ORDER BY total_won DESC 
                LIMIT ?''',
                (limit,)
            )

            players = cursor.fetchall()
            columns = [desc[0] for desc in cursor.description]

            return [dict(zip(columns, player)) for player in players]

        except Exception as e:
            logger.error(f"Error in get_top_players: {e}")
            return []

    def get_user_by_id(self, user_id: int):
        return self.get_user(user_id)

    def get_all_users(self):
        try:
            cursor = self.execute_with_retry('SELECT user_id FROM users')
            return [row[0] for row in cursor.fetchall()]

        except Exception as e:
            logger.error(f"Error in get_all_users: {e}")
            return []

    def log_admin_action(self, admin_id: int, action: str, details: str = ""):
        try:
            self.execute_with_retry(
                '''INSERT INTO admin_log (admin_id, action, details)
                   VALUES (?, ?, ?)''',
                (admin_id, action, details)
            )
        except Exception as e:
            logger.error(f"Error in log_admin_action: {e}")

    def get_auto_settings(self, user_id: int):
        """Получить настройки авто-спинов пользователя"""
        try:
            cursor = self.execute_with_retry(
                'SELECT * FROM auto_spin_settings WHERE user_id = ?',
                (user_id,)
            )
            settings = cursor.fetchone()

            if not settings:
                # Создаем настройки по умолчанию
                default_settings = {
                    'user_id': user_id,
                    'stop_win': 0,
                    'stop_loss': 0,
                    'min_balance': 0,
                    'speed_mode': 'normal',
                    'display_mode': 'full',
                    'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                }
                self.save_auto_settings(user_id, default_settings)
                return default_settings

            columns = [desc[0] for desc in cursor.description]
            return dict(zip(columns, settings))

        except Exception as e:
            logger.error(f"Error in get_auto_settings: {e}")
            return {
                'stop_win': 0,
                'stop_loss': 0,
                'min_balance': 0,
                'speed_mode': 'normal',
                'display_mode': 'full'
            }

    def save_auto_settings(self, user_id: int, settings: dict):
        """Сохранить настройки авто-спинов"""
        try:
            # Проверяем существующие настройки
            cursor = self.execute_with_retry(
                'SELECT 1 FROM auto_spin_settings WHERE user_id = ?',
                (user_id,)
            )

            stop_win = settings.get('stop_win', 0)
            stop_loss = settings.get('stop_loss', 0)
            min_balance = settings.get('min_balance', 0)
            speed_mode = settings.get('speed_mode', 'normal')
            display_mode = settings.get('display_mode', 'full')

            if cursor.fetchone():
                # Обновляем существующие
                query = '''
                    UPDATE auto_spin_settings 
                    SET stop_win = ?, stop_loss = ?, min_balance = ?,
                        speed_mode = ?, display_mode = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = ?
                '''
                values = (stop_win, stop_loss, min_balance,
                          speed_mode, display_mode, user_id)
            else:
                # Создаем новые
                query = '''
                    INSERT INTO auto_spin_settings 
                    (user_id, stop_win, stop_loss, min_balance, speed_mode, display_mode)
                    VALUES (?, ?, ?, ?, ?, ?)
                '''
                values = (user_id, stop_win, stop_loss,
                          min_balance, speed_mode, display_mode)

            self.execute_with_retry(query, values)

        except Exception as e:
            logger.error(f"Error in save_auto_settings: {e}")

    def update_auto_setting(self, user_id: int, setting_name: str, value):
        """Обновить одну настройку"""
        try:
            # Получаем текущие настройки
            current = self.get_auto_settings(user_id)
            current[setting_name] = value

            # Сохраняем обновленные
            self.save_auto_settings(user_id, current)

        except Exception as e:
            logger.error(f"Error in update_auto_setting: {e}")

    def reset_auto_settings(self, user_id: int):
        """Сбросить настройки авто-спинов к значениям по умолчанию"""
        try:
            default_settings = {
                'stop_win': 0,
                'stop_loss': 0,
                'min_balance': 0,
                'speed_mode': 'normal',
                'display_mode': 'full'
            }
            self.save_auto_settings(user_id, default_settings)

        except Exception as e:
            logger.error(f"Error in reset_auto_settings: {e}")


db = Database()

# Игровой движок


class SlotMachine:
    def __init__(self):
        self.symbols = {
            "🍒": {"weight": 30, "payout": {3: 20, 2: 3}},
            "🍋": {"weight": 25, "payout": {3: 15, 2: 2}},
            "🍊": {"weight": 20, "payout": {3: 10, 2: 2}},
            "⭐": {"weight": 15, "payout": {3: 8, 2: 1}},
            "🔔": {"weight": 7, "payout": {3: 5, 2: 1}},
            "7": {"weight": 3, "payout": {3: 50, 2: 10}}
        }

    def spin(self, bet_amount: float) -> dict:
        # Генерация результатов
        result = []
        weighted_symbols = []

        for symbol, data in self.symbols.items():
            weighted_symbols.extend([symbol] * data["weight"])

        for _ in range(3):
            result.append(random.choice(weighted_symbols))

        # Проверка выигрыша
        win_amount = 0
        multiplier = 0

        if result[0] == result[1] == result[2]:
            win_amount = bet_amount * self.symbols[result[0]]["payout"][3]
            multiplier = self.symbols[result[0]]["payout"][3]
        elif result[0] == result[1]:
            win_amount = bet_amount * self.symbols[result[0]]["payout"][2]
            multiplier = self.symbols[result[0]]["payout"][2]

        return {
            "symbols": result,
            "win_amount": win_amount,
            "multiplier": multiplier,
            "is_win": win_amount > 0
        }


slot_machine = SlotMachine()

# Клавиатуры


def main_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton(text="🎰 Крутить барабан"),
         KeyboardButton(text="⚡ Авто-спины")],
        [KeyboardButton(text="💰 Мой баланс"),
         KeyboardButton(text="📊 Статистика")],
        [KeyboardButton(text="⚙️ Настройки ставки"),
         KeyboardButton(text="🎁 Бонус")],
        [KeyboardButton(text="🏆 Топ игроков"),
         KeyboardButton(text="ℹ️ Помощь")]
    ]
    if ADMIN_IDS:
        keyboard.append([KeyboardButton(text="👑 Админ панель")])
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


def admin_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="📈 Статистика системы",
                              callback_data="admin_stats")],
        [InlineKeyboardButton(text="👤 Все пользователи",
                              callback_data="admin_users")],
        [InlineKeyboardButton(text="💰 Изменить баланс",
                              callback_data="admin_change_balance")],
        [InlineKeyboardButton(text="📢 Сделать рассылку",
                              callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="🔄 Сбросить статистику",
                              callback_data="admin_reset_stats")],
        [InlineKeyboardButton(text="🔙 В главное меню",
                              callback_data="admin_back_to_main")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def auto_spin_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="🎯 10 спинов", callback_data="auto_10")],
        [InlineKeyboardButton(text="⚡ 25 спинов", callback_data="auto_25")],
        [InlineKeyboardButton(text="🔥 50 спинов", callback_data="auto_50")],
        [InlineKeyboardButton(text="🚀 100 спинов", callback_data="auto_100")],
        [InlineKeyboardButton(text="⚙️ Настройки авто-спинов",
                              callback_data="auto_settings")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="auto_back")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# Обработчики команд


@dp.message(CommandStart())
async def cmd_start(message: Message):
    user = db.get_user(message.from_user.id)

    welcome_text = f"""
🎰 *Добро пожаловать в Vegas Slots Bot!* 🎰

*Ваш баланс:* `{user['balance']:.2f} ₽`
*Текущая ставка:* `{user.get('current_bet', MIN_BET)} ₽`

*Доступные команды:*
🎰 /spin - Крутить барабан
⚡ /auto - Автоматические спины
💰 /balance - Мой баланс
📊 /stats - Моя статистика
⚙️ /settings - Настройки ставки
🎁 /bonus - Ежедневный бонус
🏆 /top - Топ игроков

*Правила игры:*
• Минимальная ставка: {MIN_BET} ₽
• 3 одинаковых символа = джекпот!
• 2 одинаковых символа = малый выигрыш

*Удачи!* 🍀
    """

    await message.answer(
        welcome_text,
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )


@dp.message(Command("help"))
async def cmd_help(message: Message):
    help_text = """
*🎰 Vegas Slots Bot - Помощь*

*Основные команды:*
/start - Запустить бота
/spin - Крутить барабан
/auto - Автоматические спины
/balance - Показать баланс
/stats - Показать статистику
/settings - Настройки ставки
/bonus - Ежедневный бонус
/top - Топ игроков

*Авто-спины:*
🎯 10 спинов - Быстрая проверка
⚡ 25 спинов - Стандартная серия
🔥 50 спинов - Продвинутая игра
🚀 100 спинов - Максимальная серия

*Правила игры:*
- Минимальная ставка: 10 ₽
- Максимальная ставка: 10000 ₽
- Джекпот за 3 одинаковых символа
- Меньший выигрыш за 2 одинаковых

*Символы и множители:*
🍒 7️⃣ x20 | x3
🍋 7️⃣ x15 | x2
🍊 7️⃣ x10 | x2
⭐ 7️⃣ x8 | x1
🔔 7️⃣ x5 | x1
7️⃣ 7️⃣ 7️⃣ x50 | x10

*Удачи в игре!* 🍀
    """

    await message.answer(help_text, parse_mode="Markdown")


@dp.message(F.text == "🎰 Крутить барабан")
@dp.message(Command("spin"))
async def spin_slot(message: Message):
    user = db.get_user(message.from_user.id)
    user_id = user['user_id']
    balance = user['balance']

    # Определяем ставку
    bet_amount = user.get('current_bet', MIN_BET)
    if bet_amount < MIN_BET:
        bet_amount = MIN_BET
    if bet_amount > MAX_BET:
        bet_amount = MAX_BET

    # Проверка баланса
    if balance < bet_amount:
        await message.answer(
            f"❌ *Недостаточно средств!*\n\n"
            f"Ваш баланс: `{balance:.2f} ₽`\n"
            f"Требуется: `{bet_amount:.2f} ₽`\n\n"
            f"Используйте /bonus для получения бонуса\n"
            f"Или /settings для изменения ставки",
            parse_mode="Markdown"
        )
        return

    # Обновление баланса
    new_balance = balance - bet_amount
    db.update_user(user_id, balance=new_balance)

    # Анимация вращения
    spin_msg = await message.answer("🌀 *Барабаны крутятся...*", parse_mode="Markdown")

    # Имитация вращения с анимацией
    symbols_for_animation = ["🎰", "🎲", "🎯",
                             "💰", "🍒", "🍋", "🍊", "⭐", "🔔", "7️⃣"]

    for i in range(5):
        await asyncio.sleep(0.3)
        anim_symbols = [random.choice(symbols_for_animation) for _ in range(3)]
        anim_text = f"🌀 *Вращение...* \n\n{' | '.join(anim_symbols)}"
        await spin_msg.edit_text(anim_text, parse_mode="Markdown")

    await asyncio.sleep(0.5)

    # Результат вращения
    result = slot_machine.spin(bet_amount)
    symbols_display = " | ".join(result["symbols"])
    win_amount = result["win_amount"]

    if win_amount > 0:
        new_balance += win_amount
        db.update_user(user_id, balance=new_balance)

    # Сохранение игры
    db.add_game_record(
        user_id,
        bet_amount,
        win_amount,
        symbols_display,
        result["is_win"]
    )

    # Формирование результата
    result_text = f"""
🎰 *РЕЗУЛЬТАТ ВРАЩЕНИЯ* 🎰

*Символы:* {symbols_display}
*Ставка:* `{bet_amount:.2f} ₽`
────────────────────
"""

    if win_amount > 0:
        emoji = "🎯" if result["multiplier"] >= 20 else "🎉" if result["multiplier"] >= 10 else "💰"
        result_text += f"""
{emoji} *ПОБЕДА!* {emoji}
*Выигрыш:* `{win_amount:.2f} ₽`
*Множитель:* x{result['multiplier']:.1f}
"""

        if result["symbols"][0] == result["symbols"][1] == result["symbols"][2]:
            result_text += "\n🔥 *ДЖЕКПОТ! 3 ОДИНАКОВЫХ СИМВОЛА!* 🔥"
    else:
        result_text += "\n😔 *Повезет в следующий раз!*"

    result_text += f"""
────────────────────
*Новый баланс:* `{new_balance:.2f} ₽`
"""

    await spin_msg.edit_text(result_text, parse_mode="Markdown")

    # Отправка стикера для больших выигрышей
    if win_amount > bet_amount * 10:
        try:
            await message.answer_sticker("CAACAgIAAxkBAAIBImZFg5VFcH-b9ciP_H4Zev3X83zVAAKGAwACtCYYUQ68yoyQbHwqNAQ")
        except:
            pass


@dp.message(F.text == "⚡ Авто-спины")
@dp.message(Command("auto"))
async def auto_spin_menu(message: Message):
    user = db.get_user(message.from_user.id)

    auto_spin_text = f"""
⚡ *АВТОМАТИЧЕСКИЕ СПИНЫ*

Текущая ставка: `{user.get('current_bet', MIN_BET)} ₽`
Ваш баланс: `{user['balance']:.2f} ₽`

*Доступные режимы:*
🎯 **10 спинов** - Быстрая проверка удачи
⚡ **25 спинов** - Стандартная серия
🔥 **50 спинов** - Продвинутая игра
🚀 **100 спинов** - Максимальная серия

*Особенности:*
• Автоматическое продолжение спинов
• Общая статистика в конце
• Быстрый расчет результатов
• Автостоп при недостатке средств

Выберите количество спинов:
"""

    await message.answer(
        auto_spin_text,
        parse_mode="Markdown",
        reply_markup=auto_spin_keyboard()
    )


@dp.callback_query(F.data.startswith("auto_"))
async def auto_spin_handler(callback: CallbackQuery):
    user = db.get_user(callback.from_user.id)
    action = callback.data

    logger.info(f"Auto-spin action received: {action}")

    # Если это подтверждение запуска - обрабатываем здесь же
    if action.startswith("auto_confirm_"):
        try:
            num_spins = int(action.split("_")[2])
        except (IndexError, ValueError):
            await callback.answer("❌ Ошибка в данных")
            logger.error(f"Error parsing auto_confirm data: {action}")
            return

        # КОД ДЛЯ ВЫПОЛНЕНИЯ АВТО-СПИНОВ
        user_id = user['user_id']
        settings = db.get_auto_settings(user_id)
        bet_amount = user.get('current_bet', MIN_BET)
        total_cost = bet_amount * num_spins

        # Проверка минимального баланса из настроек
        min_balance = settings.get('min_balance', 0)
        if min_balance > 0 and user['balance'] - total_cost < min_balance:
            await callback.answer(f"❌ Нельзя опускать баланс ниже {min_balance} ₽")
            return

        # Двойная проверка баланса
        if user['balance'] < total_cost:
            await callback.answer(f"❌ Недостаточно средств! Нужно {total_cost:.2f} ₽, а у вас {user['balance']:.2f} ₽")
            return

        # Снимаем деньги
        new_balance = user['balance'] - total_cost
        db.update_user(user_id, balance=new_balance)

        # Создаем сообщение о начале
        try:
            progress_msg = await callback.message.edit_text(
                f"⚡ *ЗАПУСК {num_spins} СПИНОВ*\n\n"
                f"⏳ Подготовка... 0/{num_spins}\n"
                f"💰 Ставка: {bet_amount:.2f} ₽",
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Error editing message: {e}")
            progress_msg = await callback.message.answer(
                f"⚡ *ЗАПУСК {num_spins} СПИНОВ*\n\n"
                f"⏳ Подготовка... 0/{num_spins}\n"
                f"💰 Ставка: {bet_amount:.2f} ₽",
                parse_mode="Markdown"
            )

        # Выполняем спины
        total_win = 0
        wins = 0
        losses = 0
        biggest_win = 0
        start_balance = user['balance']

        # Получаем скорость из настроек
        speed_settings = {
            'normal': 0.3,
            'fast': 0.1,
            'turbo': 0.05
        }
        speed = speed_settings.get(settings.get('speed_mode', 'normal'), 0.1)
        display_mode = settings.get('display_mode', 'full')

        for i in range(1, num_spins + 1):
            try:
                # Генерируем спин
                result = slot_machine.spin(bet_amount)
                win_amount = result["win_amount"]

                # Обновляем статистику
                if win_amount > 0:
                    total_win += win_amount
                    wins += 1
                    if win_amount > biggest_win:
                        biggest_win = win_amount
                    new_balance += win_amount
                else:
                    losses += 1

                # Сохраняем в историю
                db.add_game_record(
                    user_id,
                    bet_amount,
                    win_amount,
                    "|".join(result["symbols"]),
                    result["is_win"]
                )

                # Проверяем условия остановки
                stop_win = settings.get('stop_win', 0)
                stop_loss = settings.get('stop_loss', 0)

                current_profit = total_win - (bet_amount * i)
                if stop_win > 0 and current_profit >= stop_win:
                    # Достигли стоп-прибыли
                    await callback.answer(f"✅ Достигнута стоп-прибыль: {stop_win} ₽")
                    num_spins = i  # Обновляем количество выполненных спинов
                    break

                if stop_loss > 0 and abs(current_profit) >= stop_loss:
                    # Достигли стоп-убытка
                    await callback.answer(f"⚠️ Достигнут стоп-убыток: {stop_loss} ₽")
                    num_spins = i  # Обновляем количество выполненных спинов
                    break

                # Обновляем прогресс в зависимости от режима отображения
                if display_mode == 'full' or (display_mode == 'summary' and i % 10 == 0) or i == num_spins:
                    win_rate = (wins / i) * 100 if i > 0 else 0
                    current_profit = total_win - (bet_amount * i)

                    try:
                        progress_text = f"""
⚡ *ВЫПОЛНЕНИЕ АВТО-СПИНОВ*

⏳ Прогресс: {i}/{num_spins}
✅ Побед: {wins}
❌ Поражений: {losses}
📊 Винрейт: {win_rate:.1f}%

💰 Текущий выигрыш: {total_win:.2f} ₽
💸 Текущая прибыль: {current_profit:.2f} ₽
🏦 Баланс: {new_balance:.2f} ₽
"""

                        if i == num_spins:
                            progress_text += f"\n✅ *Завершено!*"

                        await progress_msg.edit_text(progress_text, parse_mode="Markdown")
                    except Exception as e:
                        logger.error(f"Error updating progress: {e}")

                # Пауза в зависимости от скорости
                await asyncio.sleep(speed)

            except Exception as e:
                logger.error(f"Ошибка в спин #{i}: {e}")
                continue

        # Обновляем финальный баланс
        db.update_user(user_id, balance=new_balance)

        # Финальная статистика
        win_rate = (wins / num_spins) * 100 if num_spins > 0 else 0
        total_cost_final = bet_amount * num_spins
        profit = total_win - total_cost_final

        # Формируем результат
        result_text = f"""
🎰 *РЕЗУЛЬТАТ {num_spins} СПИНОВ*

*Общая статистика:*
Выполнено спинов: `{num_spins}`
Ставка за спин: `{bet_amount:.2f} ₽`
Общая стоимость: `{total_cost_final:.2f} ₽`

*Результаты:*
✅ Побед: `{wins}`
❌ Поражений: `{losses}`
📊 Винрейт: `{win_rate:.1f}%`

*Финансы:*
💰 Общий выигрыш: `{total_win:.2f} ₽`
💸 Чистая прибыль: `{profit:.2f} ₽`
🏦 Стартовый баланс: `{start_balance:.2f} ₽`
💳 Финальный баланс: `{new_balance:.2f} ₽`

*Рекорды:*
🔥 Самый большой выигрыш: `{biggest_win:.2f} ₽`
"""

        # Добавляем заголовок в зависимости от результата
        if profit > 0:
            header = "🎉 *ВЫ В ВЫИГРЫШЕ!* 🎉\n\n"
        elif profit < 0:
            header = "😔 *ВЫ В ПРОИГРЫШЕ*\n\n"
        else:
            header = "⚖️ *НИЧЬЯ!*\n\n"

        result_text = header + result_text

        # Клавиатура для результата
        result_keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎰 Еще раз", callback_data=f"auto_{num_spins}"),
                InlineKeyboardButton(
                    text="⚡ Другое", callback_data="auto_back")
            ],
            [
                InlineKeyboardButton(
                    text="🔙 В меню", callback_data="auto_back_to_main")
            ]
        ])

        # Показываем результат
        try:
            await progress_msg.edit_text(
                result_text,
                parse_mode="Markdown",
                reply_markup=result_keyboard
            )
        except Exception as e:
            logger.error(f"Error editing final message: {e}")
            await progress_msg.answer(
                result_text,
                parse_mode="Markdown",
                reply_markup=result_keyboard
            )

        await callback.answer("✅ Авто-спины завершены!")
        return  # Важно: return после обработки подтверждения

    # Обработка основных команд
    if action == "auto_back":
        try:
            await callback.message.delete()
        except:
            pass
        await callback.answer("Возвращаемся в меню")
        return

    if action == "auto_back_to_main":
        try:
            await callback.message.delete()
        except:
            pass
        await callback.message.answer(
            "Возвращаемся в главное меню...",
            reply_markup=main_keyboard()
        )
        await callback.answer("Главное меню")
        return

    if action == "auto_settings":
        user_id = callback.from_user.id
        settings = db.get_auto_settings(user_id)

        # Текстовые обозначения
        speed_texts = {
            'normal': '🚶 Обычная',
            'fast': '🏃 Быстрая',
            'turbo': '⚡ Турбо'
        }

        display_texts = {
            'full': '📊 Подробный',
            'summary': '📈 Сводный',
            'result': '🎯 Только итог'
        }

        settings_text = f"""
⚙️ *НАСТРОЙКИ АВТО-СПИНОВ*

*Условия остановки:*
💰 Стоп-прибыль: `{settings.get('stop_win', 0)} ₽`
💸 Стоп-убыток: `{settings.get('stop_loss', 0)} ₽`
🏦 Мин. баланс: `{settings.get('min_balance', 0)} ₽`

*Режимы:*
⚡ Скорость: {speed_texts.get(settings.get('speed_mode', 'normal'))}
📊 Отображение: {display_texts.get(settings.get('display_mode', 'full'))}

Выберите что настроить:
"""

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="💰 Стоп-прибыль",
                                     callback_data="set_stop_win"),
                InlineKeyboardButton(text="💸 Стоп-убыток",
                                     callback_data="set_stop_loss")
            ],
            [
                InlineKeyboardButton(text="🏦 Мин. баланс",
                                     callback_data="set_min_balance")
            ],
            [
                InlineKeyboardButton(
                    text="⚡ Скорость", callback_data="set_speed"),
                InlineKeyboardButton(text="📊 Отображение",
                                     callback_data="set_display")
            ],
            [
                InlineKeyboardButton(text="🎯 Быстрые пресеты",
                                     callback_data="quick_presets")
            ],
            [
                InlineKeyboardButton(
                    text="✅ Готово", callback_data="auto_back"),
                InlineKeyboardButton(
                    text="🔄 Сбросить", callback_data="reset_auto_settings")
            ]
        ])

        await callback.message.edit_text(settings_text, parse_mode="Markdown", reply_markup=keyboard)
        await callback.answer()
        return

    # Определяем количество спинов (только для auto_10, auto_25, auto_50, auto_100)
    spin_mapping = {
        "auto_10": 10,
        "auto_25": 25,
        "auto_50": 50,
        "auto_100": 100
    }

    # Проверяем, есть ли такое действие
    if action not in spin_mapping:
        await callback.answer("❌ Неизвестная команда")
        logger.error(f"Unknown auto-spin action: {action}")
        return

    num_spins = spin_mapping[action]
    bet_amount = user.get('current_bet', MIN_BET)
    total_cost = bet_amount * num_spins

    # Проверка баланса
    if user['balance'] < total_cost:
        await callback.answer(f"❌ Недостаточно средств! Нужно: {total_cost:.2f} ₽")
        return

    # Создаем клавиатуру подтверждения
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=f"✅ Да, запустить {num_spins} спинов",
                callback_data=f"auto_confirm_{num_spins}"
            )
        ],
        [
            InlineKeyboardButton(text="❌ Нет, отмена",
                                 callback_data="auto_back")
        ]
    ])

    confirm_text = f"""
⚡ *ПОДТВЕРЖДЕНИЕ АВТО-СПИНОВ*

Количество спинов: `{num_spins}`
Ставка за спин: `{bet_amount:.2f} ₽`
Общая стоимость: `{total_cost:.2f} ₽`
Ваш баланс: `{user['balance']:.2f} ₽`

*Будет выполнено {num_spins} автоматических спинов подряд.*

Подтвердить запуск?
"""

    try:
        await callback.message.edit_text(
            confirm_text,
            parse_mode="Markdown",
            reply_markup=keyboard
        )
    except Exception as e:
        await callback.message.answer(
            confirm_text,
            parse_mode="Markdown",
            reply_markup=keyboard
        )

    await callback.answer()


@dp.callback_query(F.data == "set_stop_win")
async def set_stop_win_menu(callback: CallbackQuery):
    user_id = callback.from_user.id
    current = db.get_auto_settings(user_id).get('stop_win', 0)

    text = f"""
💰 *НАСТРОЙКА СТОП-ПРИБЫЛИ*

Текущее: `{current} ₽`

*Что это:*
Авто-спины остановятся при достижении этой прибыли.

Выберите значение:
"""

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="100 ₽", callback_data="stopwin_100"),
            InlineKeyboardButton(text="500 ₽", callback_data="stopwin_500")
        ],
        [
            InlineKeyboardButton(text="1000 ₽", callback_data="stopwin_1000"),
            InlineKeyboardButton(text="5000 ₽", callback_data="stopwin_5000")
        ],
        [
            InlineKeyboardButton(text="❌ Отключить", callback_data="stopwin_0")
        ],
        [
            InlineKeyboardButton(text="🔙 Назад", callback_data="auto_settings")
        ]
    ])

    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
    await callback.answer()


@dp.callback_query(F.data.startswith("stopwin_"))
async def set_stop_win_value(callback: CallbackQuery):
    value_str = callback.data.split("_")[1]

    try:
        value = float(value_str)
        db.update_auto_setting(callback.from_user.id, 'stop_win', value)

        if value == 0:
            msg = "✅ Стоп-прибыль отключена"
        else:
            msg = f"✅ Установлена: {value} ₽"

        await callback.answer(msg)
        # Возвращаемся в меню настроек
        await auto_spin_handler(callback)
    except Exception as e:
        logger.error(f"Error setting stop_win: {e}")
        await callback.answer("❌ Ошибка")


@dp.callback_query(F.data == "set_stop_loss")
async def set_stop_loss_menu(callback: CallbackQuery):
    user_id = callback.from_user.id
    current = db.get_auto_settings(user_id).get('stop_loss', 0)

    text = f"""
💸 *НАСТРОЙКА СТОП-УБЫТКА*

Текущее: `{current} ₽`

*Что это:*
Авто-спины остановятся при достижении этого убытка.

Выберите значение:
"""

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="100 ₽", callback_data="stoploss_100"),
            InlineKeyboardButton(text="300 ₽", callback_data="stoploss_300")
        ],
        [
            InlineKeyboardButton(text="500 ₽", callback_data="stoploss_500"),
            InlineKeyboardButton(text="1000 ₽", callback_data="stoploss_1000")
        ],
        [
            InlineKeyboardButton(text="❌ Отключить",
                                 callback_data="stoploss_0")
        ],
        [
            InlineKeyboardButton(text="🔙 Назад", callback_data="auto_settings")
        ]
    ])

    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
    await callback.answer()


@dp.callback_query(F.data.startswith("stoploss_"))
async def set_stop_loss_value(callback: CallbackQuery):
    value_str = callback.data.split("_")[1]

    try:
        value = float(value_str)
        db.update_auto_setting(callback.from_user.id, 'stop_loss', value)

        if value == 0:
            msg = "✅ Стоп-убыток отключен"
        else:
            msg = f"✅ Установлен: {value} ₽"

        await callback.answer(msg)
        # Возвращаемся в меню настроек
        await auto_spin_handler(callback)
    except Exception as e:
        logger.error(f"Error setting stop_loss: {e}")
        await callback.answer("❌ Ошибка")


@dp.callback_query(F.data == "set_min_balance")
async def set_min_balance_menu(callback: CallbackQuery):
    user_id = callback.from_user.id
    current = db.get_auto_settings(user_id).get('min_balance', 0)

    text = f"""
🏦 *НАСТРОЙКА МИНИМАЛЬНОГО БАЛАНСА*

Текущее: `{current} ₽`

*Что это:*
Авто-спины остановятся если баланс упадет ниже этой суммы.

Выберите значение:
"""

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="50 ₽", callback_data="minbal_50"),
            InlineKeyboardButton(text="100 ₽", callback_data="minbal_100")
        ],
        [
            InlineKeyboardButton(text="200 ₽", callback_data="minbal_200"),
            InlineKeyboardButton(text="500 ₽", callback_data="minbal_500")
        ],
        [
            InlineKeyboardButton(text="❌ Отключить", callback_data="minbal_0")
        ],
        [
            InlineKeyboardButton(text="🔙 Назад", callback_data="auto_settings")
        ]
    ])

    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
    await callback.answer()


@dp.callback_query(F.data.startswith("minbal_"))
async def set_min_balance_value(callback: CallbackQuery):
    value_str = callback.data.split("_")[1]

    try:
        value = float(value_str)
        db.update_auto_setting(callback.from_user.id, 'min_balance', value)

        if value == 0:
            msg = "✅ Минимальный баланс отключен"
        else:
            msg = f"✅ Установлен: {value} ₽"

        await callback.answer(msg)
        # Возвращаемся в меню настроек
        await auto_spin_handler(callback)
    except Exception as e:
        logger.error(f"Error setting min_balance: {e}")
        await callback.answer("❌ Ошибка")


@dp.callback_query(F.data == "set_speed")
async def set_speed_menu(callback: CallbackQuery):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="🚶 Обычная", callback_data="speed_normal"),
            InlineKeyboardButton(text="🏃 Быстрая", callback_data="speed_fast")
        ],
        [
            InlineKeyboardButton(text="⚡ Турбо", callback_data="speed_turbo")
        ],
        [
            InlineKeyboardButton(text="🔙 Назад", callback_data="auto_settings")
        ]
    ])

    text = """
⚡ *НАСТРОЙКА СКОРОСТИ*

*🚶 Обычная* - Полная анимация (0.3с/спин)
*🏃 Быстрая* - Ускоренная (0.1с/спин)
*⚡ Турбо* - Максимальная скорость (0.05с/спин)

Выберите режим:
"""

    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
    await callback.answer()


@dp.callback_query(F.data.startswith("speed_"))
async def set_speed_value(callback: CallbackQuery):
    speed = callback.data[6:]  # speed_normal → normal
    db.update_auto_setting(callback.from_user.id, 'speed_mode', speed)

    speed_names = {
        'normal': '🚶 Обычная',
        'fast': '🏃 Быстрая',
        'turbo': '⚡ Турбо'
    }

    await callback.answer(f"✅ Скорость: {speed_names.get(speed, speed)}")
    # Возвращаемся в меню настроек
    await auto_spin_handler(callback)


@dp.callback_query(F.data == "set_display")
async def set_display_menu(callback: CallbackQuery):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📊 Подробный",
                                 callback_data="display_full"),
            InlineKeyboardButton(
                text="📈 Сводный", callback_data="display_summary")
        ],
        [
            InlineKeyboardButton(text="🎯 Только итог",
                                 callback_data="display_result")
        ],
        [
            InlineKeyboardButton(text="🔙 Назад", callback_data="auto_settings")
        ]
    ])

    text = """
📊 *НАСТРОЙКА ОТОБРАЖЕНИЯ*

*📊 Подробный* - Показывать каждый спин
*📈 Сводный* - Обновлять каждые 10 спинов
*🎯 Только итог* - Показать только финальный результат

Выберите режим:
"""

    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
    await callback.answer()


@dp.callback_query(F.data.startswith("display_"))
async def set_display_value(callback: CallbackQuery):
    display = callback.data[8:]  # display_full → full
    db.update_auto_setting(callback.from_user.id, 'display_mode', display)

    display_names = {
        'full': '📊 Подробный',
        'summary': '📈 Сводный',
        'result': '🎯 Только итог'
    }

    await callback.answer(f"✅ Отображение: {display_names.get(display, display)}")
    # Возвращаемся в меню настроек
    await auto_spin_handler(callback)


@dp.callback_query(F.data == "quick_presets")
async def quick_presets_menu(callback: CallbackQuery):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🎯 Безопасный",
                                 callback_data="preset_safe"),
            InlineKeyboardButton(text="⚡ Агрессивный",
                                 callback_data="preset_aggressive")
        ],
        [
            InlineKeyboardButton(text="🚀 Максимальный",
                                 callback_data="preset_max"),
            InlineKeyboardButton(text="❌ Отключить все",
                                 callback_data="preset_off")
        ],
        [
            InlineKeyboardButton(text="🔙 Назад", callback_data="auto_settings")
        ]
    ])

    text = """
🎯 *БЫСТРЫЕ ПРЕСЕТЫ*

*🎯 Безопасный* - 500/200/100 (стопы активированы)
*⚡ Агрессивный* - 1000/500/50 (риск выше)
*🚀 Максимальный* - 5000/1000/10 (для опытных)
*❌ Отключить все* - 0/0/0 (без ограничений)

Выберите пресет:
"""

    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
    await callback.answer()


@dp.callback_query(F.data.startswith("preset_"))
async def apply_preset(callback: CallbackQuery):
    preset = callback.data[7:]  # preset_safe → safe

    presets = {
        'safe': {'stop_win': 500, 'stop_loss': 200, 'min_balance': 100},
        'aggressive': {'stop_win': 1000, 'stop_loss': 500, 'min_balance': 50},
        'max': {'stop_win': 5000, 'stop_loss': 1000, 'min_balance': 10},
        'off': {'stop_win': 0, 'stop_loss': 0, 'min_balance': 0}
    }

    if preset in presets:
        settings = presets[preset]
        db.save_auto_settings(callback.from_user.id, settings)

        preset_names = {
            'safe': '🎯 Безопасный',
            'aggressive': '⚡ Агрессивный',
            'max': '🚀 Максимальный',
            'off': '❌ Все отключено'
        }

        await callback.answer(f"✅ Применен пресет: {preset_names[preset]}")
        # Возвращаемся в меню настроек
        await auto_spin_handler(callback)
    else:
        await callback.answer("❌ Неизвестный пресет")


@dp.callback_query(F.data == "reset_auto_settings")
async def reset_auto_settings_handler(callback: CallbackQuery):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, сбросить",
                                 callback_data="confirm_reset"),
            InlineKeyboardButton(text="❌ Нет, отмена",
                                 callback_data="auto_settings")
        ]
    ])

    text = """
🔄 *СБРОС НАСТРОЕК*

Вы уверены что хотите сбросить все настройки авто-спинов?

*Будут установлены значения по умолчанию:*
💰 Стоп-прибыль: 0 ₽
💸 Стоп-убыток: 0 ₽
🏦 Мин. баланс: 0 ₽
⚡ Скорость: Обычная
📊 Отображение: Подробный
"""

    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
    await callback.answer()


@dp.callback_query(F.data == "confirm_reset")
async def confirm_reset_settings(callback: CallbackQuery):
    db.reset_auto_settings(callback.from_user.id)
    await callback.answer("✅ Настройки сброшены к значениям по умолчанию")
    # Возвращаемся в меню настроек
    await auto_spin_handler(callback)


@dp.message(F.text == "💰 Мой баланс")
@dp.message(Command("balance"))
async def show_balance(message: Message):
    user = db.get_user(message.from_user.id)
    user_stats = db.get_statistics(user['user_id'])

    total_wagered = user_stats.get(
        'total_wagered', 0) or user.get('total_wagered', 0)
    total_won = user_stats.get('total_won', 0) or user.get('total_won', 0)
    total_spins = user_stats.get(
        'total_spins', 0) or user.get('total_spins', 0)
    total_wins = user.get('total_wins', 0)

    rtp = (total_won / total_wagered * 100) if total_wagered > 0 else 0
    win_rate = (total_wins / total_spins * 100) if total_spins > 0 else 0

    balance_text = f"""
💳 *ВАШ СЧЕТ И СТАТИСТИКА*

*Баланс:* `{user['balance']:.2f} ₽`
*Текущая ставка:* `{user.get('current_bet', MIN_BET)} ₽`

*📊 Статистика:*
Всего спинов: `{total_spins}`
Выигрышей: `{total_wins}`
Процент побед: `{win_rate:.1f}%`

Общая сумма ставок: `{total_wagered:.2f} ₽`
Общий выигрыш: `{total_won:.2f} ₽`
Уровень RTP: `{rtp:.1f}%`

*🏆 Рекорды:*
Самый большой выигрыш: `{user.get('biggest_win', 0):.2f} ₽`
Текущая серия побед: `{user.get('win_streak', 0)}`
Макс. серия побед: `{user.get('max_win_streak', 0)}`
    """

    await message.answer(balance_text, parse_mode="Markdown")


@dp.message(F.text == "📊 Статистика")
@dp.message(Command("stats"))
async def personal_stats(message: Message):
    user = db.get_user(message.from_user.id)
    stats = db.get_statistics(user['user_id'])

    total_spins = stats.get('total_spins', 0) or user.get('total_spins', 0)
    total_wagered = stats.get(
        'total_wagered', 0) or user.get('total_wagered', 0)
    total_won = stats.get('total_won', 0) or user.get('total_won', 0)
    avg_rtp = stats.get('avg_rtp', 0) or 0

    win_rate = (user.get('total_wins', 0) / total_spins *
                100) if total_spins > 0 else 0

    # Создаем текстовый график
    bar_length = 20
    filled = int(win_rate / 5)
    bar = "█" * filled + "░" * (bar_length - filled)

    stats_text = f"""
📈 *ВАША ПОДРОБНАЯ СТАТИСТИКА*

*Основное:*
Всего спинов: `{total_spins}`
Выигрышей: `{user.get('total_wins', 0)}`
Процент побед: `{win_rate:.1f}%`

*Финансы:*
Общая сумма ставок: `{total_wagered:.2f} ₽`
Общий выигрыш: `{total_won:.2f} ₽`
Средний RTP: `{avg_rtp:.1f}%`
Доход/убыток: `{(total_won - total_wagered):.2f} ₽`

*График винрейта:*
`{bar}`
`{win_rate:.1f}% побед`

*Рекорды:*
Самый большой выигрыш: `{user.get('biggest_win', 0):.2f} ₽`
Текущая серия побед: `{user.get('win_streak', 0)}`
Макс. серия побед: `{user.get('max_win_streak', 0)}`

*Активность:*
Зарегистрирован: `{user.get('created_at', '')[:10] if user.get('created_at') else 'Неизвестно'}`
Последняя активность: `{user.get('last_active', '')[:16] if user.get('last_active') else 'Неизвестно'}`
    """

    await message.answer(stats_text, parse_mode="Markdown")


@dp.message(F.text == "🎁 Бонус")
@dp.message(Command("bonus"))
async def daily_bonus(message: Message):
    user = db.get_user(message.from_user.id)

    # Простой бонус без проверки времени
    bonus_amount = random.randint(50, 200)
    new_balance = user['balance'] + bonus_amount

    # Обновляем баланс с ретраями
    try:
        db.update_user(message.from_user.id, balance=new_balance)

        bonus_text = f"""
🎁 *ЕЖЕДНЕВНЫЙ БОНУС!* 🎁

*Вы получили:* `{bonus_amount:.2f} ₽`
*Новый баланс:* `{new_balance:.2f} ₽`

Заходите каждый день за новым бонусом! 📈
        """

        await message.answer(bonus_text, parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Error giving bonus: {e}")
        await message.answer(
            "🎁 *БОНУС*\n\n"
            "Извините, возникла техническая ошибка при начислении бонуса.\n"
            "Попробуйте позже или напишите /help",
            parse_mode="Markdown"
        )


@dp.message(F.text == "🏆 Топ игроков")
@dp.message(Command("top"))
async def top_players(message: Message):
    top = db.get_top_players(10)

    if not top:
        await message.answer("🏆 *Топ игроков*\n\nПока никто не играл!")
        return

    top_text = "🏆 *ТОП-10 ИГРОКОВ* 🏆\n\n"

    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]

    for i, player in enumerate(top[:10]):
        username = player.get('username', f"Игрок #{
                              player['user_id'] % 10000:04d}")
        if not username or username == "None":
            username = f"Игрок #{player['user_id'] % 10000:04d}"

        total_won = player.get('total_won', 0) or 0
        total_spins = player.get('total_spins', 0) or 0
        balance = player.get('balance', 0) or 0

        win_rate = (player.get('total_wins', 0) /
                    total_spins * 100) if total_spins > 0 else 0

        medal = medals[i] if i < len(medals) else f"{i+1}."

        top_text += f"{medal} *{username}*\n"
        top_text += f"   Выигрыш: `{total_won:.2f} ₽`\n"
        top_text += f"   Баланс: `{balance:.2f} ₽`\n"
        top_text += f"   Спинов: `{total_spins}`\n"
        top_text += f"   Винрейт: `{win_rate:.1f}%`\n"

        if i < len(top) - 1 and i < 9:
            top_text += "   ─────────────────\n"

    await message.answer(top_text, parse_mode="Markdown")


@dp.message(F.text == "⚙️ Настройки ставки")
@dp.message(Command("settings"))
async def settings_menu(message: Message):
    user = db.get_user(message.from_user.id)
    current_bet = user.get('current_bet', MIN_BET)

    keyboard = InlineKeyboardBuilder()
    keyboard.row(
        InlineKeyboardButton(text="-100", callback_data="bet_-100"),
        InlineKeyboardButton(text="-10", callback_data="bet_-10"),
        InlineKeyboardButton(text="-1", callback_data="bet_-1"),
    )
    keyboard.row(
        InlineKeyboardButton(text="+1", callback_data="bet_+1"),
        InlineKeyboardButton(text="+10", callback_data="bet_+10"),
        InlineKeyboardButton(text="+100", callback_data="bet_+100"),
    )
    keyboard.row(
        InlineKeyboardButton(text="10", callback_data="bet_set_10"),
        InlineKeyboardButton(text="50", callback_data="bet_set_50"),
        InlineKeyboardButton(text="100", callback_data="bet_set_100"),
    )
    keyboard.row(
        InlineKeyboardButton(text="500", callback_data="bet_set_500"),
        InlineKeyboardButton(text="1000", callback_data="bet_set_1000"),
        InlineKeyboardButton(text="5000", callback_data="bet_set_5000"),
    )
    keyboard.row(
        InlineKeyboardButton(text="✅ Сохранить", callback_data="bet_save"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="bet_cancel"),
    )

    settings_text = f"""
⚙️ *НАСТРОЙКИ СТАВКИ*

Текущая ставка: `{current_bet:.2f} ₽`
Ваш баланс: `{user['balance']:.2f} ₽`

*Лимиты:*
Минимальная ставка: `{MIN_BET} ₽`
Максимальная ставка: `{MAX_BET} ₽`

Выберите новую ставку:
"""

    await message.answer(
        settings_text,
        parse_mode="Markdown",
        reply_markup=keyboard.as_markup()
    )


@dp.callback_query(F.data.startswith("bet_"))
async def bet_callback_handler(callback: CallbackQuery):
    user = db.get_user(callback.from_user.id)
    current_bet = user.get('current_bet', MIN_BET)

    action = callback.data[4:]  # Убираем "bet_"

    if action == "save":
        db.update_user(callback.from_user.id, current_bet=current_bet)
        await callback.message.edit_text(
            f"✅ Ставка сохранена: `{current_bet:.2f} ₽`",
            parse_mode="Markdown"
        )
        await callback.answer(f"Ставка: {current_bet:.2f} ₽")
        return

    if action == "cancel":
        await callback.message.delete()
        await callback.answer("Отменено")
        return

    # Изменение ставки
    new_bet = current_bet
    try:
        if action.startswith("set_"):
            new_bet = float(action[4:])
        elif action.startswith("-") or action.startswith("+"):
            change = float(action)
            new_bet = current_bet + change
    except:
        new_bet = current_bet

    # Проверка лимитов
    if new_bet < MIN_BET:
        new_bet = MIN_BET
        await callback.answer(f"Минимум: {MIN_BET} ₽")
    elif new_bet > MAX_BET:
        new_bet = MAX_BET
        await callback.answer(f"Максимум: {MAX_BET} ₽")
    elif new_bet > user['balance']:
        await callback.answer("Недостаточно средств!")
        return

    # Обновляем сообщение
    keyboard = InlineKeyboardBuilder()
    keyboard.row(
        InlineKeyboardButton(text="-100", callback_data="bet_-100"),
        InlineKeyboardButton(text="-10", callback_data="bet_-10"),
        InlineKeyboardButton(text="-1", callback_data="bet_-1"),
    )
    keyboard.row(
        InlineKeyboardButton(text="+1", callback_data="bet_+1"),
        InlineKeyboardButton(text="+10", callback_data="bet_+10"),
        InlineKeyboardButton(text="+100", callback_data="bet_+100"),
    )
    keyboard.row(
        InlineKeyboardButton(text="10", callback_data="bet_set_10"),
        InlineKeyboardButton(text="50", callback_data="bet_set_50"),
        InlineKeyboardButton(text="100", callback_data="bet_set_100"),
    )
    keyboard.row(
        InlineKeyboardButton(text="500", callback_data="bet_set_500"),
        InlineKeyboardButton(text="1000", callback_data="bet_set_1000"),
        InlineKeyboardButton(text="5000", callback_data="bet_set_5000"),
    )
    keyboard.row(
        InlineKeyboardButton(text="✅ Сохранить", callback_data="bet_save"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="bet_cancel"),
    )

    settings_text = f"""
⚙️ *НАСТРОЙКИ СТАВКИ*

Текущая ставка: `{new_bet:.2f} ₽`
Ваш баланс: `{user['balance']:.2f} ₽`

*Лимиты:*
Минимальная ставка: `{MIN_BET} ₽`
Максимальная ставка: `{MAX_BET} ₽`

Выберите новую ставку:
"""

    # Обновляем ставку в памяти
    db.update_user(callback.from_user.id, current_bet=new_bet)

    await callback.message.edit_text(
        settings_text,
        parse_mode="Markdown",
        reply_markup=keyboard.as_markup()
    )
    await callback.answer(f"Ставка: {new_bet:.2f} ₽")

# АДМИН ПАНЕЛЬ


@dp.message(F.text == "👑 Админ панель")
@dp.message(Command("admin"))
async def admin_panel(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ У вас нет доступа к админ панели!")
        return

    admin_text = """
👑 *АДМИНИСТРАТИВНАЯ ПАНЕЛЬ*

Выберите действие:
"""

    await message.answer(
        admin_text,
        parse_mode="Markdown",
        reply_markup=admin_keyboard()
    )


@dp.callback_query(F.data.startswith("admin_"))
async def admin_callback_handler(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔ Нет доступа!")
        return

    action = callback.data

    if action == "admin_stats":
        stats = db.get_statistics()
        all_users = db.get_all_users()

        stats_text = f"""
📊 *СТАТИСТИКА СИСТЕМЫ*

*Общее:*
Всего игроков: `{len(all_users)}`
Всего спинов: `{stats.get('total_spins', 0)}`
Уникальных игроков: `{stats.get('total_players', 0)}`

*Финансы:*
Общая сумма ставок: `{stats.get('total_wagered', 0):.2f} ₽`
Общий выигрыш: `{stats.get('total_won', 0):.2f} ₽`
Доход казино: `{(stats.get('total_wagered', 0) - stats.get('total_won', 0)):.2f} ₽`

*Производительность:*
Средний RTP: `{stats.get('avg_rtp', 0):.1f}%`
        """

        await callback.message.edit_text(stats_text, parse_mode="Markdown")
        await callback.answer("Статистика загружена")

    elif action == "admin_users":
        all_users = db.get_all_users()
        user_count = len(all_users)

        users_text = f"👥 *ВСЕ ПОЛЬЗОВАТЕЛИ*\n\nВсего: `{user_count}`\n\n"

        # Показываем только первые 20 ID
        for i, user_id in enumerate(all_users[:20]):
            users_text += f"`{user_id}`\n"

        if user_count > 20:
            users_text += f"\n... и еще {user_count - 20} пользователей"

        keyboard = InlineKeyboardBuilder()
        keyboard.row(
            InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back"),
            InlineKeyboardButton(text="📊 Статистика",
                                 callback_data="admin_stats")
        )

        await callback.message.edit_text(
            users_text,
            parse_mode="Markdown",
            reply_markup=keyboard.as_markup()
        )
        await callback.answer(f"Найдено {user_count} пользователей")

    elif action == "admin_change_balance":
        await callback.message.edit_text(
            "💰 *ИЗМЕНЕНИЕ БАЛАНСА*\n\n"
            "Отправьте сообщение в формате:\n"
            "`ID_пользователя сумма`\n\n"
            "Пример: `123456789 1000`\n\n"
            "Для отмены отправьте /cancel",
            parse_mode="Markdown"
        )
        await callback.answer("Введите данные")

    elif action == "admin_broadcast":
        await callback.message.edit_text(
            "📢 *РАССЫЛКА СООБЩЕНИЙ*\n\n"
            "Отправьте сообщение для рассылки всем пользователям.\n\n"
            "Можно использовать Markdown разметку.\n"
            "Для отмены отправьте /cancel",
            parse_mode="Markdown"
        )
        await callback.answer("Введите сообщение")

    elif action == "admin_reset_stats":
        keyboard = InlineKeyboardBuilder()
        keyboard.row(
            InlineKeyboardButton(text="✅ Да, сбросить",
                                 callback_data="admin_reset_confirm"),
            InlineKeyboardButton(text="❌ Нет, отмена",
                                 callback_data="admin_back")
        )

        await callback.message.edit_text(
            "🔄 *СБРОС СТАТИСТИКИ*\n\n"
            "Вы уверены, что хотите сбросить всю статистику?\n"
            "Это действие нельзя отменить!\n\n"
            "Будут удалены:\n"
            "- История всех игр\n"
            "- Статистика пользователей\n"
            "- Таблица рекордов",
            parse_mode="Markdown",
            reply_markup=keyboard.as_markup()
        )
        await callback.answer("Подтверждение")

    elif action == "admin_back":
        await callback.message.edit_text(
            "👑 *АДМИНИСТРАТИВНАЯ ПАНЕЛЬ*",
            parse_mode="Markdown",
            reply_markup=admin_keyboard()
        )
        await callback.answer("Вернулись в меню")

    elif action == "admin_back_to_main":
        await callback.message.delete()
        await callback.message.answer(
            "Возвращаюсь в главное меню...",
            reply_markup=main_keyboard()
        )
        await callback.answer("Главное меню")


@dp.message(F.text.regexp(r'^\d+\s+\d+'))
async def admin_set_balance(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    try:
        parts = message.text.split()
        if len(parts) != 2:
            await message.answer("❌ Неверный формат. Используйте: `ID сумма`")
            return

        user_id_str, amount_str = parts
        user_id = int(user_id_str)
        amount = float(amount_str)

        user = db.get_user_by_id(user_id)
        if not user:
            await message.answer(f"❌ Пользователь {user_id} не найден!")
            return

        old_balance = user['balance']
        db.update_user(user_id, balance=amount)
        db.log_admin_action(message.from_user.id, "change_balance",
                            f"User {user_id}: {old_balance} -> {amount}")

        await message.answer(
            f"✅ *Баланс изменен!*\n\n"
            f"Пользователь: `{user_id}`\n"
            f"Старый баланс: `{old_balance:.2f} ₽`\n"
            f"Новый баланс: `{amount:.2f} ₽`\n\n"
            f"Изменение: `{amount - old_balance:.2f} ₽`",
            parse_mode="Markdown"
        )

        # Уведомляем пользователя
        try:
            await bot.send_message(
                user_id,
                f"👑 *УВЕДОМЛЕНИЕ ОТ АДМИНИСТРАТОРА*\n\n"
                f"Ваш баланс был изменен:\n"
                f"Новый баланс: `{amount:.2f} ₽`",
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Failed to notify user {user_id}: {e}")

    except ValueError:
        await message.answer("❌ Ошибка: ID должен быть числом, сумма - числом с плавающей точкой")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}")

# Рассылка сообщений


@dp.message(F.text & ~F.text.startswith('/'))
async def admin_broadcast_message(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    # Проверяем, достаточно ли длинное сообщение для рассылки
    if len(message.text) < 10:
        return

    # Проверяем, не является ли это ответом на другую команду
    if message.text.isdigit() or message.text.replace('.', '', 1).isdigit():
        return

    # Подтверждение рассылки
    keyboard = InlineKeyboardBuilder()
    keyboard.row(
        InlineKeyboardButton(text="✅ Да, отправить", callback_data=f"broadcast_confirm_{
                             message.message_id}"),
        InlineKeyboardButton(text="❌ Нет, отмена", callback_data="admin_back")
    )

    preview = message.text[:200] + ("..." if len(message.text) > 200 else "")

    await message.answer(
        f"📢 *ПОДТВЕРЖДЕНИЕ РАССЫЛКИ*\n\n"
        f"Сообщение для рассылки:\n\n"
        f"{preview}\n\n"
        f"Отправить это сообщение всем пользователям?",
        parse_mode="Markdown",
        reply_markup=keyboard.as_markup()
    )


@dp.callback_query(F.data.startswith("broadcast_confirm_"))
async def broadcast_confirm(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return

    message_id = int(callback.data.split("_")[2])

    try:
        # Получаем всех пользователей
        all_users = db.get_all_users()

        await callback.message.edit_text(f"📤 Начинаю рассылку для {len(all_users)} пользователей...")

        # Получаем оригинальное сообщение
        original_message = await bot.forward_message(
            chat_id=callback.from_user.id,
            from_chat_id=callback.from_user.id,
            message_id=message_id
        )

        broadcast_text = original_message.text

        sent = 0
        failed = 0

        for user_id in all_users:
            try:
                await bot.send_message(
                    user_id,
                    f"📢 *ОБЪЯВЛЕНИЕ ОТ АДМИНИСТРАЦИИ*\n\n{broadcast_text}",
                    parse_mode="Markdown"
                )
                sent += 1
                await asyncio.sleep(0.05)  # Задержка чтобы не превысить лимиты
            except Exception as e:
                failed += 1
                logger.error(f"Failed to send to {user_id}: {e}")

        db.log_admin_action(callback.from_user.id, "broadcast",
                            f"Sent to {sent} users, failed: {failed}")

        await callback.message.edit_text(
            f"✅ *Рассылка завершена!*\n\n"
            f"Отправлено: `{sent}`\n"
            f"Не отправлено: `{failed}`\n"
            f"Всего пользователей: `{len(all_users)}`",
            parse_mode="Markdown"
        )

    except Exception as e:
        await callback.message.edit_text(f"❌ Ошибка рассылки: {str(e)}")

    await callback.answer()

# Обработка неизвестных команд


@dp.message()
async def unknown_command(message: Message):
    if message.text:
        await message.answer(
            "🤔 Я не понимаю эту команду.\n"
            "Используйте /help для списка доступных команд."
        )

# Запуск бота


async def main():
    logger.info("🎰 Запуск Vegas Slots Bot...")

    # Установка команд бота
    await bot.set_my_commands([
        types.BotCommand(command="start", description="Запустить бота"),
        types.BotCommand(command="spin", description="Крутить барабан"),
        types.BotCommand(command="auto", description="Автоматические спины"),
        types.BotCommand(command="balance", description="Мой баланс"),
        types.BotCommand(command="stats", description="Моя статистика"),
        types.BotCommand(command="settings", description="Настройки ставки"),
        types.BotCommand(command="bonus", description="Ежедневный бонус"),
        types.BotCommand(command="top", description="Топ игроков"),
        types.BotCommand(command="help", description="Помощь"),
        types.BotCommand(command="admin", description="Админ панель"),
    ])

    logger.info("✅ Бот запущен и готов к работе!")
    logger.info(f"👑 Администраторы: {ADMIN_IDS}")

    # Проверка базы данных
    try:
        test_user = db.get_user(ADMIN_IDS[0] if ADMIN_IDS else 1)
        logger.info(f"✅ База данных работает. Тестовый пользователь: {
                    test_user.get('user_id')}")
    except Exception as e:
        logger.error(f"❌ Ошибка базы данных: {e}")

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
