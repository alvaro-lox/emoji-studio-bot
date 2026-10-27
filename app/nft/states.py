"""Состояния ввода (FSM) оформления подарка."""

from aiogram.fsm.state import State, StatesGroup


class GiftStyleForm(StatesGroup):
    background = State()
    text = State()
