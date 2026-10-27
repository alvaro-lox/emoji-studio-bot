"""Состояния ввода (FSM) админ-панели."""

from aiogram.fsm.state import State, StatesGroup


class AdminForm(StatesGroup):
    broadcast = State()
    ban = State()
    unban = State()
    add_admin = State()
