"""Совместимость со старым запуском: python bot.py == python -m app"""

from app.main import run

if __name__ == "__main__":
    run()
