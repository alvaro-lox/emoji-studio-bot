"""Состояния ввода (FSM) перекраски."""

from aiogram.fsm.state import State, StatesGroup


class RecolorForm(StatesGroup):
    hex = State()
