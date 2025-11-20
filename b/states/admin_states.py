from aiogram.fsm.state import StatesGroup, State


class AdminStates(StatesGroup):
    REMOVE_DAYS = State()      # состояние ввода количества дней
