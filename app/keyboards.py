from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

TODAY = "🍽 Bugungi navbat"
MY_DUTY = "📌 Mening navbatim"
TRANSFER = "🔄 Navbatni o‘tkazish"
HISTORY = "📜 Tarix"
ADMIN = "⚙️ Admin"


def main_menu(is_admin: bool) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=TODAY), KeyboardButton(text=MY_DUTY)],
        [KeyboardButton(text=TRANSFER), KeyboardButton(text=HISTORY)],
    ]
    if is_admin:
        rows.append([KeyboardButton(text=ADMIN)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)
