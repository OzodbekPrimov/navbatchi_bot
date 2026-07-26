from aiogram.fsm.state import State, StatesGroup


class TransferStates(StatesGroup):
    waiting_for_comment = State()
