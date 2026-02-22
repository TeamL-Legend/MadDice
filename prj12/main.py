import asyncio
import logging
import aiosqlite
import random
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiogram.types import InlineQuery, InlineQueryResultArticle, InputTextMessageContent
from aiocryptopay import AioCryptoPay, Networks

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = '7678521670:AAFQjJdBfj_L4Ut3mkeMTFH46Nq-ppaboAw'
CRYPTO_TOKEN = '54008:AAFYeTdzxhPrsqHXI5UvGvUeopfMTXbRRWG'
FEE_WITHDRAW = 0.045  # 4.5% на вывод
FEE_DEPOSIT = 0.005    # 0.5% на пополнение (теперь это вычитается)
MIN_SUM = 0.5
ADMIN_ID = 7569161412
BONUS_MIN_BET_PER_DAY = 5.0  # Минимальная сумма ставок за день для получения бонуса

crypto = AioCryptoPay(token=CRYPTO_TOKEN, network=Networks.TEST_NET)
router = Router()

class BotStates(StatesGroup):
    wait_deposit_amount = State()
    wait_withdraw_amount = State()
    wait_transfer_id = State()
    wait_transfer_amount = State()
    wait_bet_solo = State()
    wait_bet_knb = State()

# --- БАЗА ДАННЫХ ---
async def init_db():
    async with aiosqlite.connect('bot_database.db') as db:
        # Создаем таблицу users если её нет
        await db.execute('''CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            balance REAL DEFAULT 0,
            total_games INTEGER DEFAULT 0,
            total_bet REAL DEFAULT 0,
            total_won REAL DEFAULT 0,
            join_date TIMESTAMP,
            last_bonus TIMESTAMP,
            bonus_claimed INTEGER DEFAULT 0,
            daily_bet_total REAL DEFAULT 0,
            last_bet_date TIMESTAMP,
            bonus_attempts INTEGER DEFAULT 0,
            last_bonus_attempt_date TIMESTAMP
        )''')
        
        # Проверяем и добавляем недостающие колонки
        cursor = await db.execute("PRAGMA table_info(users)")
        columns = await cursor.fetchall()
        column_names = [column[1] for column in columns]
        
        # Добавляем колонки если их нет
        if 'bonus_claimed' not in column_names:
            await db.execute('ALTER TABLE users ADD COLUMN bonus_claimed INTEGER DEFAULT 0')
        if 'last_bonus' not in column_names:
            await db.execute('ALTER TABLE users ADD COLUMN last_bonus TIMESTAMP')
        if 'daily_bet_total' not in column_names:
            await db.execute('ALTER TABLE users ADD COLUMN daily_bet_total REAL DEFAULT 0')
        if 'last_bet_date' not in column_names:
            await db.execute('ALTER TABLE users ADD COLUMN last_bet_date TIMESTAMP')
        if 'bonus_attempts' not in column_names:
            await db.execute('ALTER TABLE users ADD COLUMN bonus_attempts INTEGER DEFAULT 0')
        if 'last_bonus_attempt_date' not in column_names:
            await db.execute('ALTER TABLE users ADD COLUMN last_bonus_attempt_date TIMESTAMP')
        
        # Таблица для активных дуэлей
        await db.execute('''CREATE TABLE IF NOT EXISTS active_duels (
            duel_id INTEGER PRIMARY KEY,
            creator_id INTEGER,
            joiner_id INTEGER,
            game_type TEXT,
            bet REAL,
            message_id INTEGER,
            chat_id INTEGER,
            status TEXT DEFAULT 'waiting',
            created_at TIMESTAMP
        )''')
        await db.commit()
    
        await db.execute('''CREATE TABLE IF NOT EXISTS payments (
    invoice_id INTEGER PRIMARY KEY,
    user_id INTEGER,
    amount REAL,
    processed_at TIMESTAMP
)''')

async def get_user_data(user_id: int):
    async with aiosqlite.connect('bot_database.db') as db:
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT * FROM users WHERE user_id = ?', (user_id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                user_dict = dict(row)
                # Убеждаемся что все поля существуют
                if 'bonus_claimed' not in user_dict:
                    user_dict['bonus_claimed'] = 0
                if 'last_bonus' not in user_dict:
                    user_dict['last_bonus'] = None
                if 'daily_bet_total' not in user_dict:
                    user_dict['daily_bet_total'] = 0
                if 'last_bet_date' not in user_dict:
                    user_dict['last_bet_date'] = None
                if 'bonus_attempts' not in user_dict:
                    user_dict['bonus_attempts'] = 0
                if 'last_bonus_attempt_date' not in user_dict:
                    user_dict['last_bonus_attempt_date'] = None
                return user_dict
            
            # Если пользователя нет, создаем нового
            current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            await db.execute(
                'INSERT INTO users (user_id, join_date, balance, bonus_claimed, daily_bet_total, bonus_attempts) VALUES (?, ?, ?, ?, ?, ?)', 
                (user_id, current_time, 0, 0, 0, 0)
            )
            await db.commit()
            
            # Возвращаем данные нового пользователя
            return {
                'user_id': user_id,
                'balance': 0,
                'total_games': 0,
                'total_bet': 0,
                'total_won': 0,
                'join_date': current_time,
                'last_bonus': None,
                'bonus_claimed': 0,
                'daily_bet_total': 0,
                'last_bet_date': None,
                'bonus_attempts': 0,
                'last_bonus_attempt_date': None
            }

async def update_balance(user_id: int, amount: float):
    async with aiosqlite.connect('bot_database.db') as db:
        await db.execute('UPDATE users SET balance = ROUND(balance + ?, 2) WHERE user_id = ?', (amount, user_id))
        await db.commit()

async def update_daily_bet(user_id: int, bet_amount: float):
    """Обновляет дневную сумму ставок пользователя"""
    async with aiosqlite.connect('bot_database.db') as db:
        today = datetime.now().strftime("%Y-%m-%d")
        await db.execute('''
            UPDATE users 
            SET daily_bet_total = CASE 
                WHEN last_bet_date = ? THEN daily_bet_total + ?
                ELSE ?
            END,
            last_bet_date = ?
            WHERE user_id = ?
        ''', (today, bet_amount, bet_amount, today, user_id))
        await db.commit()


# --- КЛАВИАТУРЫ ---
def get_main_menu():
    builder = ReplyKeyboardBuilder()
    builder.row(types.KeyboardButton(text="🎰 ИГРОВОЙ ЗАЛ"))
    builder.row(types.KeyboardButton(text="👤 ПРОФИЛЬ"), types.KeyboardButton(text="💳 КОШЕЛЕК"))
    builder.row(types.KeyboardButton(text="🎁 БОНУС"))
    return builder.as_markup(resize_keyboard=True)

def get_wallet_menu():
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="➕ ПОПОЛНИТЬ", callback_data="deposit"),
                types.InlineKeyboardButton(text="➖ ВЫВЕСТИ", callback_data="withdraw"))
    builder.row(types.InlineKeyboardButton(text="💸 ПЕРЕВЕСТИ", callback_data="transfer"))
    return builder.as_markup()

def get_game_menu():
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="🎲 КУБИКИ", callback_data="solo_dice"),
                types.InlineKeyboardButton(text="🎳 БОУЛИНГ", callback_data="solo_bowling"))
    builder.row(types.InlineKeyboardButton(text="🎯 ДАРТС", callback_data="solo_dart"),
                # Добавлена кнопка КНБ
                types.InlineKeyboardButton(text="✂️ КНБ (x2.3)", callback_data="solo_knb"))
    builder.row(types.InlineKeyboardButton(text="⬅️ НАЗАД", callback_data="to_main_reset"))
    return builder.as_markup()

def get_cancel_kb():
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="❌ ОТМЕНА", callback_data="to_main_reset"))
    return builder.as_markup()

# --- КОМАНДА HELP ---
@router.message(Command("help"))
async def cmd_help(message: types.Message):
    help_text = """
<b>КАК ТУТ ИГРАТЬ? 🎰</b>
━━━━━━━━━━━━━━━━
🎮 <b>ИГРЫ:</b>
├ <b>🎰 Игровой зал:</b> Соло-игры (Кубики, Боулинг, Дартс, КНБ).
└ <b>⚔️ PvP Дуэли:</b> Введи в любом чате <code>@имя_бота сумма</code> и выбери игру.

💰 <b>ФИНАНСЫ:</b>
├ <b>Пополнение:</b> Через CryptoPay (USDT). Комиссия: 0.5%.
├ <b>Вывод:</b> Моментальные чеки. Комиссия: 4.5%.
└ <b>Перевод:</b> Без комиссии по ID пользователю.

🎁 <b>БОНУСЫ И РАНГИ:</b>
├ <b>Уровень:</b> Повышается от суммы ставок. Выше уровень — больше кэшбэк!
├ <b>Бонус:</b> Ежедневный слот (нужно 5$ оборота за день).
└ <b>Рефералы:</b> Приглашай друзей и получай 1% от их ставок!

🛡 <b>КОМАНДЫ:</b>
/start — Главное меню
/top — Топ 10 игроков проекта
/help — Эта справка
    """
    await message.answer(help_text, parse_mode="HTML")

# --- БОНУС ---
@router.message(F.text == "🎁 БОНУС")
@router.message(Command("bonus"))
async def get_bonus(message: types.Message):
    user = await get_user_data(message.from_user.id)
    today = datetime.now().strftime("%Y-%m-%d")
    
    # Проверяем, достаточно ли ставок за сегодня
    if user.get('last_bet_date') != today or user.get('daily_bet_total', 0) < BONUS_MIN_BET_PER_DAY:
        return await message.answer(
            f"❌ Для получения бонуса нужно сделать ставок на сумму от {BONUS_MIN_BET_PER_DAY} USDT сегодня!\n"
            f"Текущая сумма ставок за сегодня: {user.get('daily_bet_total', 0)} USDT"
        )
    
    # Проверяем попытки за сегодня
    if user.get('last_bonus_attempt_date') == today:
        if user.get('bonus_attempts', 0) >= 5:
            return await message.answer(
                f"❌ Вы исчерпали все 5 попыток на сегодня!\n"
                f"Следующие попытки будут доступны завтра."
            )
    else:
        # Сбрасываем попытки для нового дня
        async with aiosqlite.connect('bot_database.db') as db:
            await db.execute(
                'UPDATE users SET bonus_attempts = 0, last_bonus_attempt_date = ? WHERE user_id = ?',
                (today, message.from_user.id)
            )
            await db.commit()
        user['bonus_attempts'] = 0
    
    # Проверяем, получал ли бонус сегодня
    if user.get('last_bonus'):
        try:
            last_bonus = datetime.strptime(user['last_bonus'], "%Y-%m-%d %H:%M:%S")
            if datetime.now() - last_bonus < timedelta(hours=24):
                next_bonus = last_bonus + timedelta(hours=24)
                time_left = next_bonus - datetime.now()
                hours = time_left.seconds // 3600
                minutes = (time_left.seconds % 3600) // 60
                return await message.answer(
                    f"⏳ Вы уже получили бонус сегодня!\n"
                    f"Следующий бонус через: {hours}ч {minutes}м"
                )
        except:
            pass
    
    # Увеличиваем счетчик попыток
    attempts_left = 5 - user['bonus_attempts']
    
    await message.answer(
        f"🎰 <b>БОНУСНЫЙ СЛОТ</b>\n\n"
        f"Попытка {user['bonus_attempts'] + 1} из 5\n"
        f"Осталось попыток: {attempts_left - 1}\n\n"
        f"Вам нужно выбить 777, чтобы получить бонус!\n"
        f"Крутите слот...",
        parse_mode="HTML"
    )
    
    # Отправляем слот
    msg = await message.answer_dice(emoji="🎰")
    await asyncio.sleep(3.5)
    
    # Обновляем попытки в БД
    async with aiosqlite.connect('bot_database.db') as db:
        await db.execute(
            'UPDATE users SET bonus_attempts = bonus_attempts + 1, last_bonus_attempt_date = ? WHERE user_id = ?',
            (today, message.from_user.id)
        )
        await db.commit()
    
    # Проверяем результат (777)
    if msg.dice.value == 777:
        # Случайный бонус от 0.5 до 2.5 USDT
        bonus_amount = round(random.uniform(0.5, 2.5), 2)
        
        async with aiosqlite.connect('bot_database.db') as db:
            await db.execute(
                'UPDATE users SET balance = balance + ?, last_bonus = ?, bonus_claimed = bonus_claimed + 1 WHERE user_id = ?',
                (bonus_amount, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), message.from_user.id)
            )
            await db.commit()
        
        # Получаем обновленный баланс
        updated_user = await get_user_data(message.from_user.id)
        
        await message.answer(
            f"🎁 <b>ПОЗДРАВЛЯЕМ!</b>\n\n"
            f"Вы выиграли бонус: +{bonus_amount} USDT\n"
            f"💰 Текущий баланс: {updated_user['balance']} USDT\n"
            f"Всего бонусов получено: {updated_user['bonus_claimed']}",
            parse_mode="HTML"
        )
    else:
        await message.answer(
            f"😞 К сожалению, вы не выиграли бонус.\n"
            f"Выпало: {msg.dice.value}\n"
            f"Осталось попыток: {4 - user['bonus_attempts']}"
        )

# --- АДМИН КОМАНДЫ ---
@router.message(Command("add"))
async def admin_add(message: types.Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID: 
        return await message.answer("❌ У вас нет прав администратора!")
    try:
        args = command.args.split()
        if len(args) != 2:
            raise ValueError
        user_id, amount = int(args[0]), float(args[1])
        await update_balance(user_id, amount)
        
        # Уведомляем пользователя
        try:
            await message.bot.send_message(
                user_id,
                f"💰 Вам начислено {amount} USDT администратором!"
            )
        except:
            pass
            
        await message.answer(f"✅ Баланс <code>{user_id}</code> увеличен на {amount} USDT", parse_mode="HTML")
    except:
        await message.answer("Ошибка! Формат: `/add 12345 10.5`", parse_mode="HTML")

@router.message(Command("sub"))
async def admin_sub(message: types.Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID: 
        return await message.answer("❌ У вас нет прав администратора!")
    try:
        args = command.args.split()
        if len(args) != 2:
            raise ValueError
        user_id, amount = int(args[0]), float(args[1])
        
        user = await get_user_data(user_id)
        if user['balance'] < amount:
            return await message.answer(f"❌ У пользователя недостаточно средств! Баланс: {user['balance']} USDT")
        
        await update_balance(user_id, -amount)
        
        # Уведомляем пользователя
        try:
            await message.bot.send_message(
                user_id,
                f"💰 С вашего баланса списано {amount} USDT администратором!\n"
                f"Текущий баланс: {user['balance'] - amount} USDT"
            )
        except:
            pass
            
        await message.answer(f"✅ С баланса <code>{user_id}</code> списано {amount} USDT", parse_mode="HTML")
    except:
        await message.answer("Ошибка! Формат: `/sub 12345 10.5`", parse_mode="HTML")

# --- ХЕНДЛЕРЫ МЕНЮ ---
@router.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await get_user_data(message.from_user.id)
    await message.answer(
        "💎 <b>MadDice CASINO</b>\n"
        "Добро пожаловать! Используйте /help для списка команд.\n"
        "Ежедневный бонус: /bonus",
        reply_markup=get_main_menu(), 
        parse_mode="HTML"
    )

@router.callback_query(F.data == "to_main_reset")
async def back_to_main(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer("⚙️ Главное меню", reply_markup=get_main_menu())
    await callback.message.delete()

@router.message(F.text == "🎰 ИГРОВОЙ ЗАЛ")
async def games_msg(message: types.Message):
    await message.answer("<b>Выберите дисциплину:</b>", reply_markup=get_game_menu(), parse_mode="HTML")

@router.message(F.text == "👤 ПРОФИЛЬ")
async def profile_msg(message: types.Message):
    u = await get_user_data(message.from_user.id)
    win_rate = round((u['total_won'] / u['total_bet'] * 100) if u['total_bet'] > 0 else 0, 1)
    
    # Форматируем дату регистрации
    join_date = "Неизвестно"
    if u.get('join_date'):
        try:
            join_date = datetime.strptime(u['join_date'], "%Y-%m-%d %H:%M:%S").strftime("%d.%m.%Y")
        except:
            join_date = u['join_date']
    
    # Проверяем статус бонуса
    today = datetime.now().strftime("%Y-%m-%d")
    bonus_status = "✅ Доступен" if (u.get('last_bet_date') == today and u.get('daily_bet_total', 0) >= BONUS_MIN_BET_PER_DAY) else "❌ Недоступен"
    
    await message.answer(
        f"👤 <b>ПРОФИЛЬ</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"🆔 ID: <code>{u['user_id']}</code>\n"
        f"📅 Регистрация: {join_date}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"💰 Баланс: {u['balance']} USDT\n"
        f"🎲 Всего игр: {u['total_games']}\n"
        f"💸 Сумма ставок: {u['total_bet']} USDT\n"
        f"🏆 Выигрыши: {u['total_won']} USDT\n"
        f"📊 Win Rate: {win_rate}%\n"
        f"🎁 Бонусов получено: {u.get('bonus_claimed', 0)}\n"
        f"📈 Ставок сегодня: {u.get('daily_bet_total', 0)} USDT\n"
        f"🎯 Бонус: {bonus_status}\n"
        f"🎯 Любимая: Маргарита @incredible_113\n"
        f"━━━━━━━━━━━━━━━━",
        parse_mode="HTML"
    )

@router.message(F.text == "💳 КОШЕЛЕК")
async def wallet_msg(message: types.Message):
    u = await get_user_data(message.from_user.id)
    await message.answer(
        f"💳 <b>КОШЕЛЕК</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"💰 Баланс: {u['balance']} USDT\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"<b>КОМИССИИ:</b>\n"
        f"• Пополнение: -{FEE_DEPOSIT*100}% (вычитается)\n"
        f"• Вывод: +{FEE_WITHDRAW*100}%\n"
        f"━━━━━━━━━━━━━━━━",
        reply_markup=get_wallet_menu(), 
        parse_mode="HTML"
    )

# --- ИНЛАЙН РЕЖИМ (PVP ДУЭЛИ) ---
@router.inline_query()
async def inline_handler(query: InlineQuery):
    try:
        bet = float(query.query.replace(',', '.'))
        if bet < 0.1:
            return
    except ValueError:
        return

    u = await get_user_data(query.from_user.id)
    if u['balance'] < bet:
        return

    results = []
    games = [
        ("dice", "🎲 КУБИКИ", "🎲"),
        ("bowling", "🎳 БОУЛИНГ", "🎳"),
        ("dart", "🎯 ДАРТС", "🎯"),
        ("mines", "💣 МИНЫ (PvP)", "💣")
    ]

    for game_id, game_name, emoji in games:
        # Создаем уникальный ID для дуэли
        duel_id = random.randint(10000, 99999)
        
        kb = InlineKeyboardBuilder()
        kb.row(types.InlineKeyboardButton(
            text=f"✅ Принять вызов ({bet} USDT)", 
            callback_data=f"accept_{game_id}_{query.from_user.id}_{bet}_{duel_id}"
        ))

        results.append(InlineQueryResultArticle(
            id=f"pvp_{game_id}_{duel_id}",
            title=f"{game_name} на {bet} USDT",
            description=f"Нажмите для отправки вызова | Ваш баланс: {u['balance']} USDT",
            input_message_content=InputTextMessageContent(
                message_text=(
                    f"⚔️ <b>ДУЭЛЬ: {game_name}</b>\n\n"
                    f"👤 Игрок: {query.from_user.full_name}\n"
                    f"💰 Ставка: <b>{bet} USDT</b>\n"
                    f"🏆 Выигрыш: <b>{round(bet * 1.85, 2)} USDT</b>\n\n"
                    f"<i>ID дуэли: {duel_id}</i>\n"
                    f"<i>Нажмите кнопку ниже, чтобы принять вызов!</i>"
                ),
                parse_mode="HTML"
            ),
            reply_markup=kb.as_markup()
        ))
    
    await query.answer(results, cache_time=1, is_personal=True)

@router.callback_query(F.data.startswith("accept_"))
async def pvp_process(callback: types.CallbackQuery, bot: Bot):
    parts = callback.data.split("_")
    # accept, game_type, creator_id, bet, duel_id
    game_type, creator_id, bet, duel_id = parts[1], int(parts[2]), float(parts[3]), parts[4]
    joiner_id = callback.from_user.id

    if joiner_id == creator_id:
        return await callback.answer("❌ Нельзя принять свой вызов!", show_alert=True)

    c_user = await get_user_data(creator_id)
    j_user = await get_user_data(joiner_id)

    if c_user['balance'] < bet:
        return await callback.answer("❌ У создателя недостаточно средств!", show_alert=True)
    if j_user['balance'] < bet:
        return await callback.answer("❌ У вас недостаточно средств!", show_alert=True)

    # Списание средств
    await update_balance(creator_id, -bet)
    await update_balance(joiner_id, -bet)
    await update_daily_bet(creator_id, bet)
    await update_daily_bet(joiner_id, bet)

            # Внутри pvp_process, после списания баланса:
    if game_type == "mines":
     mine_index = random.randint(0, 8)
    # Поле: 9 нулей (не нажато). При нажатии 0 меняется на 2 (пусто) или игра окончена (мина)
    field = "000000000" 
    
    players = [creator_id, joiner_id]
    first_player = random.choice(players)
    second_player = joiner_id if first_player == creator_id else creator_id

    text = (
        f"💣 <b>МИНИ-ДУЭЛЬ #{duel_id}</b>\n\n"
        f"💰 Ставка: <b>{bet} USDT</b>\n"
        f"👤 Ходит: <a href='tg://user?id={first_player}'>ПЕРВЫЙ ИГРОК</a>\n\n"
        f"<i>Нажимайте на кнопки, чтобы найти пустые клетки!</i>"
    )
    
    await bot.edit_message_text(
        inline_message_id=callback.inline_message_id,
        text=text,
        reply_markup=get_mines_keyboard(field, mine_index, first_player, second_player, bet, duel_id),
        parse_mode="HTML"
    )

    await callback.answer("✅ Игра началась! Проверьте ЛС с ботом.")

    # Выбираем эмодзи
    emoji = {"dice": "🎲", "bowling": "🎳", "dart": "🎯"}[game_type]

    # Информируем в общем чате, что игра пошла
    start_text = f"⚔️ <b>ДУЭЛЬ #{duel_id} НАЧАТА!</b>\n\nБроски выполняются в ЛС с ботом..."
    if callback.inline_message_id:
        await bot.edit_message_text(inline_message_id=callback.inline_message_id, text=start_text, parse_mode="HTML")
    
    # --- САМА ИГРА (БРОСКИ) ---
    # Бросок первого игрока (создателя)
    m1 = await bot.send_dice(creator_id, emoji=emoji)
    v1 = m1.dice.value
    await bot.send_message(joiner_id, f"👤 Соперник (ID:{creator_id}) бросил {emoji}...")
    
    await asyncio.sleep(3.5) # Ждем анимацию

    # Бросок второго игрока (принявшего)
    m2 = await bot.send_dice(joiner_id, emoji=emoji)
    v2 = m2.dice.value
    await bot.send_message(creator_id, f"👤 Соперник (ID:{joiner_id}) бросил {emoji}...")

    await asyncio.sleep(3.5)

    # Определение победителя
    win_amt = round(bet * 1.85, 2)
    winner_id = None
    
    if v1 > v2:
        await update_balance(creator_id, win_amt)
        winner_text = f"🏆 Победил Игрок 1 (ID:{creator_id})!"
        result_msg = f"{winner_text}\n💰 Выигрыш: {win_amt} USDT"
        winner_id = creator_id
    elif v2 > v1:
        await update_balance(joiner_id, win_amt)
        winner_text = f"🏆 Победил Игрок 2 (ID:{joiner_id})!"
        result_msg = f"{winner_text}\n💰 Выигрыш: {win_amt} USDT"
        winner_id = joiner_id
    else:
        await update_balance(creator_id, bet)
        await update_balance(joiner_id, bet)
        winner_text = "🤝 НИЧЬЯ!"
        result_msg = "🤝 НИЧЬЯ! Ставки возвращены."

    final_text = (
        f"🏁 <b>РЕЗУЛЬТАТ ДУЭЛИ #{duel_id}</b>\n\n"
        f"👤 Игрок 1: {v1} {emoji}\n"
        f"👤 Игрок 2: {v2} {emoji}\n\n"
        f"<b>{result_msg}</b>"
    )

    # Обновляем сообщение в общем чате (результат видят все)
    if callback.inline_message_id:
        await bot.edit_message_text(inline_message_id=callback.inline_message_id, text=final_text, parse_mode="HTML")

    # Отправляем итоги в ЛС обоим
    for uid in [creator_id, joiner_id]:
        try:
            await bot.send_message(uid, final_text, parse_mode="HTML")
        except: pass

    # Запись статистики в БД
    async with aiosqlite.connect('bot_database.db') as db:
        await db.execute('UPDATE users SET total_games = total_games + 1, total_bet = total_bet + ? WHERE user_id IN (?, ?)', (bet, creator_id, joiner_id))
        if winner_id:
            await db.execute('UPDATE users SET total_won = total_won + ? WHERE user_id = ?', (win_amt, winner_id))
        await db.commit()

# Вспомогательная функция для генерации клавиатуры
def get_mines_keyboard(field, mine_idx, current_id, next_id, bet, duel_id):
    builder = InlineKeyboardBuilder()
    for i in range(9):
        state = field[i]
        if state == "0": # Не вскрыто
            builder.add(types.InlineKeyboardButton(
                text="❓", 
                callback_data=f"m_{duel_id}_{mine_idx}_{i}_{field}_{current_id}_{next_id}_{bet}"
            ))
        else: # Вскрыто (state == "2")
            builder.add(types.InlineKeyboardButton(text="⬜️", callback_data="none"))
    
    builder.adjust(3)
    return builder.as_markup()

# --- ПЕРЕВОД / ПОПОЛНЕНИЕ / ВЫВОД ---
@router.callback_query(F.data == "transfer")
async def transfer_init(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "💸 <b>ПЕРЕВОД</b>\n\n"
        "Введите ID пользователя для перевода:",
        reply_markup=get_cancel_kb(),
        parse_mode="HTML"
    )
    await state.set_state(BotStates.wait_transfer_id)

@router.message(BotStates.wait_transfer_id)
async def transfer_id(message: types.Message, state: FSMContext):
    if not message.text.isdigit():
        return await message.answer("❌ ID должен состоять только из цифр!")
    await state.update_data(target_id=int(message.text))
    await message.answer("Введите сумму перевода:", reply_markup=get_cancel_kb())
    await state.set_state(BotStates.wait_transfer_amount)

@router.message(BotStates.wait_transfer_amount)
async def transfer_proc(message: types.Message, state: FSMContext):
    u = await get_user_data(message.from_user.id)
    data = await state.get_data()
    try:
        amt = float(message.text.replace(',', '.'))
        if amt < 0.1:
            return await message.answer("❌ Минимальная сумма перевода: 0.1 USDT")
        if amt > u['balance']:
            return await message.answer("❌ Недостаточно средств!")
        
        await update_balance(message.from_user.id, -amt)
        await update_balance(data['target_id'], amt)
        
        # Уведомляем получателя
        try:
            await message.bot.send_message(
                data['target_id'],
                f"💸 Вам переведено {amt} USDT от пользователя {message.from_user.id}!"
            )
        except:
            pass
        
        await message.answer(
            f"✅ Перевод выполнен!\n"
            f"Сумма: {amt} USDT\n"
            f"Получатель: <code>{data['target_id']}</code>",
            parse_mode="HTML"
        )
        await state.clear()
    except ValueError:
        await message.answer("❌ Неверный формат суммы!")

@router.callback_query(F.data == "deposit")
async def deposit_init(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        f"💰 <b>ПОПОЛНЕНИЕ</b>\n\n"
        f"Комиссия: -{FEE_DEPOSIT*100}% (вычитается из суммы)\n"
        f"Пример: при пополнении на 100 USDT будет зачислено {round(100 * (1 - FEE_DEPOSIT), 2)} USDT\n\n"
        f"Введите сумму пополнения в USDT:",
        reply_markup=get_cancel_kb(),
        parse_mode="HTML"
    )
    await state.set_state(BotStates.wait_deposit_amount)

@router.message(BotStates.wait_deposit_amount)
async def deposit_process(message: types.Message, state: FSMContext):
    try:
        amt = float(message.text.replace(',', '.'))
        if amt < 0.1: 
            return await message.answer("❌ Минимум 0.1 USDT")
        
        final_amt = round(amt * (1 - FEE_DEPOSIT), 2)
        inv = await crypto.create_invoice(asset='USDT', amount=amt)
        
        kb = InlineKeyboardBuilder()
        kb.row(types.InlineKeyboardButton(text="💳 ОПЛАТИТЬ", url=inv.bot_invoice_url))
        # ИСПРАВЛЕНО: Префикс должен совпадать с хендлером ниже
        kb.row(types.InlineKeyboardButton(
            text="✅ ПРОВЕРИТЬ ОПЛАТУ", 
            callback_data=f"check_payment_{inv.invoice_id}_{final_amt}"
        ))
        
        await message.answer(
            f"🧾 <b>СЧЕТ НА ОПЛАТУ</b>\n\n"
            f"Сумма: {amt} USDT\n"
            f"К зачислению: {final_amt} USDT\n"
            f"ID счета: <code>{inv.invoice_id}</code>",
            reply_markup=kb.as_markup(), 
            parse_mode="HTML"
        )
        await state.clear()
    except Exception as e:
        logging.error(f"Ошибка создания счета: {e}")
        await message.answer("❌ Ошибка создания счета")

@router.callback_query(F.data.startswith("check_payment_"))
async def check_payment(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    # check_payment_{invoice_id}_{final_amt}
    try:
        invoice_id = int(parts[2])
        final_amt = float(parts[3])
        
        # Получаем список инвойсов по ID
        invoices = await crypto.get_invoices(invoice_ids=invoice_id)
        
        # Проверяем, что ответ не пустой
        if not invoices:
            return await callback.answer("❌ Счет не найден", show_alert=True)
            
        # В aiocryptopay get_invoices возвращает список. Берем первый элемент.
        invoice = invoices[0] if isinstance(invoices, list) else invoices

        if invoice.status == 'paid':
            # Дополнительная проверка, не обрабатывали ли мы этот платеж ранее
            async with aiosqlite.connect('bot_database.db') as db:
                async with db.execute('SELECT invoice_id FROM payments WHERE invoice_id = ?', (invoice_id,)) as cursor:
                    if await cursor.fetchone():
                        return await callback.answer("⚠️ Этот платеж уже был зачислен", show_alert=True)
                
                # Зачисляем баланс
                await update_balance(callback.from_user.id, final_amt)
                
                # Фиксируем платеж в базе
                await db.execute(
                    'INSERT INTO payments (invoice_id, user_id, amount, processed_at) VALUES (?, ?, ?, ?)',
                    (invoice_id, callback.from_user.id, final_amt, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                )
                await db.commit()

            await callback.message.edit_text(
                f"✅ <b>ОПЛАТА ПОДТВЕРЖДЕНА</b>\n\n"
                f"Сумма {final_amt} USDT зачислена на ваш баланс!",
                parse_mode="HTML"
            )
        elif invoice.status == 'expired':
            await callback.answer("❌ Срок оплаты счета истек", show_alert=True)
        else:
            await callback.answer("⏳ Оплата еще не поступила", show_alert=True)
            
    except Exception as e:
        logging.error(f"Ошибка проверки оплаты: {e}")
        await callback.answer("⚠️ Произошла ошибка при проверке. Попробуйте позже.", show_alert=True)

@router.message(Command("test_invoice"))
async def test_invoice(message: types.Message):
    try:
        # Создаем тестовый инвойс
        inv = await crypto.create_invoice(asset='USDT', amount=1)
        await message.answer(f"✅ Инвойс создан: {inv.invoice_id}")
        
        # Получаем информацию об инвойсе
        invoices = await crypto.get_invoices(invoice_ids=inv.invoice_id)
        if invoices:
            inv_info = invoices[0]
            # Показываем все атрибуты
            attrs = [attr for attr in dir(inv_info) if not attr.startswith('_')]
            await message.answer(f"Атрибуты объекта: {', '.join(attrs)}")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@router.callback_query(F.data == "withdraw")
async def withdraw_init(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        f"💸 <b>ВЫВОД СРЕДСТВ</b>\n\n"
        f"Минимальная сумма: {MIN_SUM} USDT\n"
        f"Комиссия: {FEE_WITHDRAW*100}%\n"
        f"Пример: при выводе 100 USDT вы получите {round(100 * (1 - FEE_WITHDRAW), 2)} USDT\n\n"
        f"Введите сумму вывода:",
        reply_markup=get_cancel_kb(),
        parse_mode="HTML"
    )
    await state.set_state(BotStates.wait_withdraw_amount)

@router.message(BotStates.wait_withdraw_amount)
async def withdraw_process(message: types.Message, state: FSMContext):
    u = await get_user_data(message.from_user.id)
    try:
        amt = float(message.text.replace(',', '.'))
        if amt < MIN_SUM:
            return await message.answer(f"❌ Минимальная сумма вывода: {MIN_SUM} USDT")
        if amt > u['balance']:
            return await message.answer("❌ Недостаточно средств!")
        
        final = round(amt * (1 - FEE_WITHDRAW), 2)
        fee = round(amt - final, 2)
        
        check = await crypto.create_check(asset='USDT', amount=final)
        await update_balance(message.from_user.id, -amt)
        
        await message.answer(
            f"✅ <b>ВЫВОД ВЫПОЛНЕН</b>\n\n"
            f"Сумма вывода: {amt} USDT\n"
            f"К получению: {final} USDT\n"
            f"Комиссия: {fee} USDT\n\n"
            f"🔗 <a href='{check.bot_check_url}'>ССЫЛКА НА ЧЕК</a>",
            parse_mode="HTML"
        )
        await state.clear()
    except ValueError:
        await message.answer("❌ Неверный формат суммы!")

# --- СОЛО ИГРЫ ---
@router.callback_query(F.data.startswith("solo_"))
async def solo_init(callback: types.CallbackQuery, state: FSMContext):
    game = callback.data.split("_")[1]
    
    # Для КНБ используем отдельный обработчик
    if game == "knb":
        await knb_init(callback, state)
        return
    
    game_names = {"dice": "КУБИКИ", "bowling": "БОУЛИНГ", "dart": "ДАРТС"}
    
    await state.update_data(game=game)
    await callback.message.edit_text(
        f"🎲 <b>ИГРА: {game_names[game]}</b>\n\n"
        f"Введите сумму ставки (мин. 0.1 USDT):\n"
        f"При победе: x1.85\n"
        f"Ничья: возврат ставки",
        reply_markup=get_cancel_kb(),
        parse_mode="HTML"
    )
    await state.set_state(BotStates.wait_bet_solo)

@router.message(BotStates.wait_bet_solo)
async def solo_play(message: types.Message, state: FSMContext):
    data = await state.get_data()
    u = await get_user_data(message.from_user.id)
    
    try:
        bet = float(message.text.replace(',', '.'))
        if bet < 0.1:
            return await message.answer("❌ Минимальная ставка: 0.1 USDT")
        if bet > u['balance']:
            return await message.answer("❌ Недостаточно средств!")
        
        # Списываем ставку
        await update_balance(message.from_user.id, -bet)
        
        # Обновляем дневные ставки
        await update_daily_bet(message.from_user.id, bet)
        
        emoji = {"dice": "🎲", "bowling": "🎳", "dart": "🎯"}[data['game']]
        
        # Отправляем первый бросок
        m1 = await message.answer_dice(emoji=emoji)
        v1 = m1.dice.value
        await asyncio.sleep(3.5)
        
        # Отправляем второй бросок
        m2 = await message.answer_dice(emoji=emoji)
        v2 = m2.dice.value
        await asyncio.sleep(3.5)
        
        # Определяем результат
        if v1 > v2:
            win = round(bet * 1.85, 2)
            result_text = f"🏆 ВЫ ВЫИГРАЛИ! +{win} USDT"
            await update_balance(message.from_user.id, win)
        elif v1 < v2:
            win = 0
            result_text = f"📉 ВЫ ПРОИГРАЛИ! -{bet} USDT"
        else:
            win = bet  # Ничья - возврат ставки
            result_text = f"🤝 НИЧЬЯ! Ставка возвращена (+{bet} USDT)"
            await update_balance(message.from_user.id, bet)
        
        # Обновляем статистику
        async with aiosqlite.connect('bot_database.db') as db:
            await db.execute(
                'UPDATE users SET total_games = total_games + 1, total_bet = total_bet + ?, total_won = total_won + ? WHERE user_id = ?',
                (bet, win if win > 0 else 0, message.from_user.id)
            )
            await db.commit()
        
        # Получаем актуальный баланс
        u = await get_user_data(message.from_user.id)
        
        await message.answer(
            f"<b>🎮 РЕЗУЛЬТАТ:</b>\n\n"
            f"{result_text}\n"
            f"🎲 Ваш бросок: {v1}\n"
            f"🎲 Бросок соперника: {v2}\n"
            f"💰 Текущий баланс: {u['balance']} USDT",
            parse_mode="HTML"
        )
        
        await state.clear()
        
    except ValueError:
        await message.answer("❌ Неверный формат суммы! Введите число.")

@router.callback_query(F.data == "solo_knb")
async def knb_init(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "👊 <b>КАМЕНЬ, НОЖНИЦЫ, БУМАГА</b>\n\n"
        "Коэффициент: <b>x2.3</b>\n"
        "Введите сумму ставки (мин. 0.1 USDT):",
        reply_markup=get_cancel_kb(), 
        parse_mode="HTML"
    )
    await state.set_state(BotStates.wait_bet_knb)

@router.message(BotStates.wait_bet_knb)
async def knb_bet(message: types.Message, state: FSMContext):
    try:
        bet = float(message.text.replace(',', '.'))
        u = await get_user_data(message.from_user.id)
        
        if bet < 0.1:
            return await message.answer("❌ Минимальная ставка: 0.1 USDT")
        if bet > u['balance']:
            return await message.answer("❌ Недостаточно средств!")
        
        # Списываем ставку сразу
        await update_balance(message.from_user.id, -bet)
        await update_daily_bet(message.from_user.id, bet)
        
        # Создаем клавиатуру для выбора
        kb = InlineKeyboardBuilder()
        kb.row(
            types.InlineKeyboardButton(text="👊 КАМЕНЬ", callback_data=f"knb_{bet}_0"),
            types.InlineKeyboardButton(text="✌️ НОЖНИЦЫ", callback_data=f"knb_{bet}_1"),
            types.InlineKeyboardButton(text="✋ БУМАГА", callback_data=f"knb_{bet}_2")
        )
        
        await message.answer(
            f"👊 <b>КАМЕНЬ, НОЖНИЦЫ, БУМАГА</b>\n\n"
            f"💰 Ставка: {bet} USDT\n"
            f"🏆 Множитель: x2.3\n\n"
            f"<b>Ваш выбор:</b>",
            reply_markup=kb.as_markup(),
            parse_mode="HTML"
        )
        
        await state.clear()
        
    except ValueError:
        await message.answer("❌ Введите число!")

@router.callback_query(F.data.startswith("knb_"))
async def knb_result(callback: types.CallbackQuery):
    try:
        # Разбираем данные: knb_ставка_выбор
        parts = callback.data.split("_")
        bet = float(parts[1])
        user_choice = int(parts[2])
        
        await callback.answer()
        
        # Удаляем сообщение с кнопками выбора
        await callback.message.delete()
        
        user_emojis = ["👊 (Камень)", "✌️ (Ножницы)", "✋ (Бумага)"]
        bot_emojis = ["👊 (Камень)", "✌️ (Ножницы)", "✋ (Бумага)"]
        
        # Бот делает случайный выбор
        bot_choice = random.randint(0, 2)
        
        # Анимация "процесса"
        msg = await callback.message.answer("👊...")
        await asyncio.sleep(0.7)
        await msg.edit_text("👊 Ножницы...")
        await asyncio.sleep(0.7)
        await msg.edit_text("👊 Ножницы, Бумага...")
        await asyncio.sleep(0.7)
        await msg.delete()

        # Определяем победителя
        # 0 - Камень, 1 - Ножницы, 2 - Бумага
        if user_choice == bot_choice:
            # Ничья — возвращаем всю ставку (или половину, если хочешь комиссию)
            win = bet/2
            result_text = f"🤝 <b>НИЧЬЯ!</b>\nСтавка {bet/2} USDT возвращена (50% комиссия при ничье)."
            await update_balance(callback.from_user.id, win)
        elif (user_choice == 0 and bot_choice == 1) or \
             (user_choice == 1 and bot_choice == 2) or \
             (user_choice == 2 and bot_choice == 0):
            # Победа юзера
            win = round(bet * 2.3, 2)
            result_text = f"🏆 <b>ВЫ ПОБЕДИЛИ!</b>\nВыигрыш: +{win} USDT"
            await update_balance(callback.from_user.id, win)
        else:
            # Проигрыш
            win = 0
            result_text = f"📉 <b>ВЫ ПРОИГРАЛИ!</b>\nУбыток: -{bet} USDT"

        # Обновляем статистику в БД
        async with aiosqlite.connect('bot_database.db') as db:
            await db.execute(
                'UPDATE users SET total_games = total_games + 1, total_bet = total_bet + ?, total_won = total_won + ? WHERE user_id = ?',
                (bet, win if win > bet else 0, callback.from_user.id)
            )
            await db.commit()
        
        updated_user = await get_user_data(callback.from_user.id)
        
        # Финальный результат
        await callback.message.answer(
            f"👊 <b>КАМЕНЬ, НОЖНИЦЫ, БУМАГА</b>\n\n"
            f"🧑 Вы: <b>{user_emojis[user_choice]}</b>\n"
            f"🤖 Бот: <b>{bot_emojis[bot_choice]}</b>\n\n"
            f"{result_text}\n"
            f"💰 Баланс: {updated_user['balance']} USDT",
            parse_mode="HTML"
        )
        
    except Exception as e:
        logging.error(f"Ошибка в КНБ: {e}")
        await callback.message.answer(f"❌ Произошла ошибка в игре.")

@router.callback_query(F.data.startswith("mplay_"))
async def mines_play_logic(callback: types.CallbackQuery, bot: Bot):
    # mplay_{mine_idx}_{clicked_idx}_{current_turn}_{next_turn}_{bet}_{duel_id}
    data = callback.data.split("_")
    mine_idx = int(data[1])
    clicked_idx = int(data[2])
    current_turn = int(data[3])
    next_turn = int(data[4])
    bet = float(data[5])
    duel_id = data[6]

    if callback.from_user.id != current_turn:
        return await callback.answer("⏳ Сейчас не ваш ход!", show_alert=True)

    # Проверяем, попал ли на мину
    if clicked_idx == mine_idx:
        # ПРОИГРЫШ текущего игрока
        win_amt = round(bet * 1.85, 2)
        await update_balance(next_turn, win_amt)
        
        # Обновляем статистику
        async with aiosqlite.connect('bot_database.db') as db:
            await db.execute('UPDATE users SET total_games = total_games + 1, total_bet = total_bet + ? WHERE user_id IN (?, ?)', (bet, current_turn, next_turn))
            await db.execute('UPDATE users SET total_won = total_won + ? WHERE user_id = ?', (win_amt, next_turn))
            await db.commit()

        final_text = (
            f"💥 <b>БАБАХ! ДУЭЛЬ #{duel_id} ОКОНЧЕНА</b>\n\n"
            f"👤 Проиграл: <a href='tg://user?id={current_turn}'>Игрок</a>\n"
            f"🏆 Победитель: <a href='tg://user?id={next_turn}'>Игрок</a>\n"
            f"💰 Выигрыш: <b>{win_amt} USDT</b>"
        )
        
        # Показываем где была мина
        builder = InlineKeyboardBuilder()
        for i in range(9):
            txt = "💣" if i == mine_idx else "⬜️"
            builder.add(types.InlineKeyboardButton(text=txt, callback_data="none"))
        builder.adjust(3)

        await bot.edit_message_text(
            inline_message_id=callback.inline_message_id,
            text=final_text,
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
    else:
        # Игра продолжается, передаем ход
        await callback.answer("✅ Чисто! Ход соперника.")
        
        # Обновляем клавиатуру: нужно пометить нажатую кнопку
        # В данном упрощенном примере мы просто меняем местами игроков в callback_data
        builder = InlineKeyboardBuilder()
        # Чтобы знать, какие кнопки уже нажаты, в реальном проекте нужно хранить состояние поля в БД.
        # Для простоты: игрок просто выбирает из тех же 9 кнопок, но мы меняем заголовок.
        
        new_text = (
            f"💣 <b>МИНИ-ДУЭЛЬ #{duel_id}</b>\n\n"
            f"💰 Ставка: {bet} USDT\n"
            f"👣 Очередной ход: <a href='tg://user?id={next_turn}'>ЖДЕМ ХОДА</a>"
        )

        def update_mines_kb(m_idx, curr_p, nxt_p):
            build = InlineKeyboardBuilder()
            for i in range(9):
                build.add(types.InlineKeyboardButton(
                    text="❓", 
                    callback_data=f"mplay_{m_idx}_{i}_{nxt_p}_{curr_p}_{bet}_{duel_id}"
                ))
            build.adjust(3)
            return build.as_markup()

        await bot.edit_message_text(
            inline_message_id=callback.inline_message_id,
            text=new_text,
            reply_markup=update_mines_kb(mine_idx, current_turn, next_turn),
            parse_mode="HTML"
        )

@router.callback_query(F.data.startswith("m_"))
async def mines_turn(callback: types.CallbackQuery, bot: Bot):
    # m_{duel_id}_{mine_idx}_{clicked_idx}_{field}_{curr_id}_{next_id}_{bet}
    data = callback.data.split("_")
    duel_id, mine_idx = data[1], int(data[2])
    clicked_idx, field = int(data[3]), list(data[4])
    curr_id, next_id, bet = int(data[5]), int(data[6]), float(data[7])

    if callback.from_user.id != curr_id:
        return await callback.answer("⏳ Сейчас ход вашего оппонента!", show_alert=True)

    if clicked_idx == mine_idx:
        # --- ПРОИГРЫШ (напоролся на мину) ---
        win_amt = round(bet * 1.85, 2)
        await update_balance(next_id, win_amt) # Деньги получает тот, кто НЕ нажимал
        
        # Статистика
        async with aiosqlite.connect('bot_database.db') as db:
            await db.execute('UPDATE users SET total_games = total_games + 1, total_bet = total_bet + ? WHERE user_id IN (?, ?)', (bet, curr_id, next_id))
            await db.execute('UPDATE users SET total_won = total_won + ? WHERE user_id = ?', (win_amt, next_id))
            await db.commit()

        # Финальное поле
        kb = InlineKeyboardBuilder()
        for i in range(9):
            icon = "💥" if i == mine_idx else ("⬜️" if field[i] == "2" else "❓")
            kb.add(types.InlineKeyboardButton(text=icon, callback_data="none"))
        kb.adjust(3)

        await bot.edit_message_text(
            inline_message_id=callback.inline_message_id,
            text=f"💥 <b>БАБАХ! ДУЭЛЬ #{duel_id}</b>\n\n"
                 f"👤 Проиграл: <a href='tg://user?id={curr_id}'>Игрок</a>\n"
                 f"🏆 Победитель: <a href='tg://user?id={next_id}'>Игрок</a>\n"
                 f"💰 Выигрыш: <b>{win_amt} USDT</b>",
            reply_markup=kb.as_markup(),
            parse_mode="HTML"
        )
    else:
        # --- УДАЧНЫЙ ХОД ---
        field[clicked_idx] = "2" # Помечаем как вскрытую
        new_field = "".join(field)
        
        await bot.edit_message_text(
            inline_message_id=callback.inline_message_id,
            text=f"💣 <b>МИНИ-ДУЭЛЬ #{duel_id}</b>\n\n"
                 f"💰 Ставка: <b>{bet} USDT</b>\n"
                 f"👤 Ходит: <a href='tg://user?id={next_id}'>СЛЕДУЮЩИЙ ИГРОК</a>\n\n"
                 f"<i>Клетка {clicked_idx + 1} пуста! Фух...</i>",
            reply_markup=get_mines_keyboard(new_field, mine_idx, next_id, curr_id, bet, duel_id),
            parse_mode="HTML"
        )

@router.message(Command("top"))
async def cmd_top(message: types.Message):
    async with aiosqlite.connect('bot_database.db') as db:
        async with db.execute('SELECT user_id, total_won FROM users ORDER BY total_won DESC LIMIT 10') as cursor:
            rows = await cursor.fetchall()
            
    text = "<b>🏆 ТОП 10 ПОБЕДИТЕЛЕЙ:</b>\n\n"
    for i, row in enumerate(rows, 1):
        text += f"{i}. ID <code>{row[0]}</code> — {row[1]} USDT\n"
    await message.answer(text, parse_mode="HTML")

async def main():
    logging.basicConfig(level=logging.INFO)
    await init_db()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    
    print("🤖 Бот запущен и готов к работе!")
    print(f"👑 Админ ID: {ADMIN_ID}")
    print(f"💰 Комиссии: пополнение -{FEE_DEPOSIT*100}%, вывод +{FEE_WITHDRAW*100}%")
    print(f"🎁 Бонус: требуется ставок от {BONUS_MIN_BET_PER_DAY} USDT в день")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())