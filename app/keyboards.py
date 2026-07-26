from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

TODAY = "🍽 Bugungi navbat"
STATUS = "📋 Bugungi holat"
MY_DUTY = "📌 Mening navbatlarim"
TRANSFER = "🔄 Navbatni o‘tkazish"
HISTORY = "📜 Tarix"
ADMIN = "⚙️ Admin"
BREAD_EMPTY = "🥖 Non tugadi"
WATER_EMPTY = "💧 Suv tugadi"


def main_menu(is_admin: bool) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=STATUS), KeyboardButton(text=MY_DUTY)],
        [KeyboardButton(text=TODAY)],
        [KeyboardButton(text=BREAD_EMPTY), KeyboardButton(text=WATER_EMPTY)],
        [KeyboardButton(text=TRANSFER), KeyboardButton(text=HISTORY)],
    ]
    if is_admin:
        rows.append([KeyboardButton(text=ADMIN)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)
