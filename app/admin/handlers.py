"""Админ-панель: статистика, рассылка, бан. Доступ — ADMIN_IDS в .env."""

import asyncio
import logging
import time
from contextlib import suppress

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, Filter, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.admin.states import AdminForm
from app.common.i18n import t
from app.common.icons import icons
from app.common.storage import EVENT_KINDS, Storage
from app.common.ui import keyboard, plain
from app.config import Config

log = logging.getLogger(__name__)
router = Router()

BROADCAST_BATCH = 25  # Telegram пропускает ~30 сообщений в секунду — шлём пачками по 25
ALBUM_WAIT = 1.2  # части альбома приходят отдельными апдейтами — ждём, пока придут все
_albums: dict[str, list[int]] = {}
_broadcast_tasks: set[asyncio.Task] = set()


class IsAdmin(Filter):
    async def __call__(self, event, is_admin: bool = False) -> bool:
        return is_admin


router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


def panel_kb(lang: str, is_owner: bool = False) -> InlineKeyboardMarkup:
    return keyboard(
        [
            icons.button(t(lang, "btn_stats"), "stats", "primary", callback_data="adm:stats"),
            icons.button(t(lang, "btn_broadcast"), "megaphone", "primary", callback_data="adm:bc"),
        ],
        [
            icons.button(t(lang, "btn_ban"), "lock", "danger", callback_data="adm:ban"),
            icons.button(t(lang, "btn_unban"), "unlock", "danger", callback_data="adm:unban"),
        ],
        [
            icons.button(t(lang, "btn_banned_list"), "users", "primary", callback_data="adm:banned"),
            *(
                [icons.button(t(lang, "btn_admins"), "admin", "primary", callback_data="adm:admins")]
                if is_owner
                else []
            ),
        ],
        [icons.button(t(lang, "btn_menu"), "home", callback_data="menu")],
    )


def back_kb(lang: str) -> InlineKeyboardMarkup:
    return keyboard([icons.button(t(lang, "btn_back"), "back", callback_data="admin")])


def stats_text(lang: str, stats: dict) -> str:
    langs = " · ".join(f"{code} {n}" for code, n in stats["langs"].items()) or "—"
    lines = [
        t(lang, "stats_head"),
        "",
        t(lang, "stats_users", **{key: stats[key] for key in ("users", "new_day", "new_week")}),
        t(lang, "stats_active", day=stats["active_day"], week=stats["active_week"]),
        t(lang, "stats_banned", banned=stats["banned"], blocked=stats["blocked"]),
        t(lang, "stats_langs", langs=langs),
        "",
    ]
    for kind in EVENT_KINDS:
        day, week, total = stats["events"][kind]
        lines.append(t(lang, f"stats_{kind}", day=day, week=week, total=total))
    lines.append(t(lang, "stats_watches", n=stats["watches"]))
    return "\n".join(lines)


async def _edit(call: CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    with suppress(TelegramBadRequest):
        await call.message.edit_text(text, reply_markup=markup)


@router.message(Command("admin"))
async def cmd_admin(message: Message, lang: str, is_owner: bool, state: FSMContext, db: Storage) -> None:
    await state.clear()
    stats = await db.stats()
    await message.answer(
        t(lang, "admin_panel", users=stats["users"], active=stats["active_day"]), reply_markup=panel_kb(lang, is_owner)
    )


@router.callback_query(F.data == "admin")
async def cb_admin(call: CallbackQuery, lang: str, is_owner: bool, state: FSMContext, db: Storage) -> None:
    await state.clear()
    stats = await db.stats()
    await _edit(
        call, t(lang, "admin_panel", users=stats["users"], active=stats["active_day"]), panel_kb(lang, is_owner)
    )
    await call.answer()


@router.message(Command("stats"))
async def cmd_stats(message: Message, lang: str, db: Storage) -> None:
    await message.answer(stats_text(lang, await db.stats()), reply_markup=back_kb(lang))


@router.callback_query(F.data == "adm:stats")
async def cb_stats(call: CallbackQuery, lang: str, db: Storage) -> None:
    markup = keyboard(
        [icons.button(t(lang, "btn_refresh"), "refresh", "primary", callback_data="adm:stats")],
        [icons.button(t(lang, "btn_back"), "back", callback_data="admin")],
    )
    await _edit(call, stats_text(lang, await db.stats()), markup)
    await call.answer()


# ---------- рассылка ----------


@router.callback_query(F.data == "adm:bc")
async def cb_broadcast(call: CallbackQuery, lang: str, state: FSMContext) -> None:
    await state.set_state(AdminForm.broadcast)
    await _edit(call, t(lang, "bc_ask"), back_kb(lang))
    await call.answer()


@router.message(StateFilter(AdminForm.broadcast), ~F.text.startswith("/"))
async def on_broadcast_message(message: Message, bot: Bot, lang: str, state: FSMContext, db: Storage) -> None:
    """Любое сообщение как есть: текст с форматированием и премиум-эмодзи, фото, GIF, видео,
    альбом. Рассылаем копию (copy_message), поэтому всё сохраняется, а пометки «переслано» нет."""
    if message.media_group_id:
        album = _albums.setdefault(message.media_group_id, [])
        album.append(message.message_id)
        if len(album) > 1:
            return  # альбом соберёт обработчик первой части
        await asyncio.sleep(ALBUM_WAIT)
        message_ids = sorted(_albums.pop(message.media_group_id, album))
    else:
        message_ids = [message.message_id]

    await state.update_data(chat_id=message.chat.id, message_ids=message_ids)
    targets = len(await db.broadcast_targets())
    await bot.copy_messages(message.chat.id, message.chat.id, message_ids)  # превью ровно как у всех
    await message.answer(
        t(lang, "bc_confirm", n=targets),
        reply_markup=keyboard(
            [icons.button(t(lang, "btn_bc_send", n=targets), "megaphone", "success", callback_data="adm:bc_go")],
            [icons.button(t(lang, "btn_cancel"), "cross", "danger", callback_data="admin")],
        ),
    )


async def _deliver(bot: Bot, db: Storage, user_id: int, from_chat: int, message_ids: list[int]) -> bool:
    for _ in range(3):
        try:
            if len(message_ids) == 1:
                await bot.copy_message(user_id, from_chat, message_ids[0])
            else:
                await bot.copy_messages(user_id, from_chat, message_ids)
            return True
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except TelegramForbiddenError:
            await db.mark_blocked(user_id)
            return False
        except Exception as e:
            log.warning("Рассылка: %s не получил сообщение: %s", user_id, e)
            return False
    return False


async def broadcast(bot: Bot, db: Storage, lang: str, admin_chat: int, from_chat: int, message_ids: list[int]) -> None:
    targets = await db.broadcast_targets()
    status = await bot.send_message(admin_chat, t(lang, "bc_progress", done=0, total=len(targets), ok=0, fail=0))
    delivered = failed = 0
    last_update = time.monotonic()
    for start in range(0, len(targets), BROADCAST_BATCH):
        batch = targets[start : start + BROADCAST_BATCH]
        started = time.monotonic()
        results = await asyncio.gather(*(_deliver(bot, db, user_id, from_chat, message_ids) for user_id in batch))
        delivered += sum(results)
        failed += len(results) - sum(results)
        if time.monotonic() - last_update > 3:
            last_update = time.monotonic()
            with suppress(TelegramBadRequest):
                done = start + len(batch)
                await status.edit_text(t(lang, "bc_progress", done=done, total=len(targets), ok=delivered, fail=failed))
        await asyncio.sleep(max(0.0, 1.0 - (time.monotonic() - started)))  # не больше пачки в секунду
    with suppress(TelegramBadRequest):
        await status.edit_text(t(lang, "bc_done", ok=delivered, fail=failed))


@router.callback_query(F.data == "adm:bc_go")
async def cb_broadcast_go(call: CallbackQuery, bot: Bot, lang: str, state: FSMContext, db: Storage) -> None:
    data = await state.get_data()
    await state.clear()
    if "message_ids" not in data:
        await call.answer(t(lang, "expired"), show_alert=True)
        return
    await call.answer()
    with suppress(TelegramBadRequest):
        await call.message.edit_reply_markup(reply_markup=None)
    task = asyncio.create_task(broadcast(bot, db, lang, call.message.chat.id, data["chat_id"], data["message_ids"]))
    _broadcast_tasks.add(task)
    task.add_done_callback(_broadcast_tasks.discard)


# ---------- бан ----------


@router.callback_query(F.data.in_({"adm:ban", "adm:unban"}))
async def cb_ban_ask(call: CallbackQuery, lang: str, state: FSMContext) -> None:
    await state.set_state(AdminForm.ban if call.data == "adm:ban" else AdminForm.unban)
    await _edit(call, t(lang, "ban_ask"), back_kb(lang))
    await call.answer()


async def _target_id(message: Message, db: Storage) -> int | None:
    """ID из пересланного сообщения, числа или @username (если человек уже писал боту)."""
    origin = message.forward_origin
    if origin is not None and getattr(origin, "sender_user", None):
        return origin.sender_user.id
    text = (message.text or "").strip()
    if text.lstrip("-").isdigit():
        return int(text)
    if text.startswith("@") or text.replace("_", "").isalnum():
        await db.find_by_username(text)
        return None
    return None


@router.message(StateFilter(AdminForm.ban, AdminForm.unban), ~F.text.startswith("/"))
async def on_ban_target(message: Message, lang: str, state: FSMContext, db: Storage, config: Config) -> None:
    user_id = await _target_id(message, db)
    if user_id is None:
        await message.answer(t(lang, "target_not_found"))
        return
    banning = await state.get_state() == AdminForm.ban.state
    await state.clear()
    if banning and (user_id in config.admin_ids or await db.is_admin(user_id)):
        await message.answer(t(lang, "ban_admin"), reply_markup=back_kb(lang))
        return
    if not await db.set_banned(user_id, banning):
        await message.answer(t(lang, "ban_unknown", id=user_id), reply_markup=back_kb(lang))
        return
    await message.answer(t(lang, "ban_done" if banning else "unban_done", id=user_id), reply_markup=back_kb(lang))


async def _show_banned(call: CallbackQuery, lang: str, db: Storage) -> None:
    ids = await db.banned_users()
    rows = [
        [icons.button(f"{t(lang, 'btn_unban')} {user_id}", "unlock", callback_data=f"adm:ub:{user_id}")]
        for user_id in ids[:30]
    ]
    rows.append([icons.button(t(lang, "btn_back"), "back", callback_data="admin")])
    text = t(lang, "banned_list", n=len(ids)) if ids else t(lang, "banned_empty")
    await _edit(call, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == "adm:banned")
async def cb_banned(call: CallbackQuery, lang: str, db: Storage) -> None:
    await _show_banned(call, lang, db)
    await call.answer()


@router.callback_query(F.data.startswith("adm:ub:"))
async def cb_unban_one(call: CallbackQuery, lang: str, db: Storage) -> None:
    user_id = int(call.data.removeprefix("adm:ub:"))
    await db.set_banned(user_id, False)
    await call.answer(plain(t(lang, "unban_done", id=user_id)))
    await _show_banned(call, lang, db)


# ---------- управление админами (только владельцы из ADMIN_IDS) ----------


def _who(user_id: int, username: str | None) -> str:
    return f"@{username} ({user_id})" if username else str(user_id)


async def _show_admins(call: CallbackQuery, lang: str, db: Storage, config: Config) -> None:
    lines = [t(lang, "admins_title"), ""]
    for owner in sorted(config.admin_ids):
        lines.append(f"👑 {_who(owner, await db.username_of(owner))}")
    admins = await db.admins()
    lines += [f"🛡 {_who(user_id, name)}" for user_id, name in admins] or [t(lang, "admins_empty")]
    rows = [
        [
            icons.button(
                f"{t(lang, 'btn_remove')} {_who(user_id, name)}", "trash", "danger", callback_data=f"adm:rm:{user_id}"
            )
        ]
        for user_id, name in admins
    ]
    rows.append([icons.button(t(lang, "btn_add_admin"), "add", "success", callback_data="adm:add")])
    rows.append([icons.button(t(lang, "btn_back"), "back", callback_data="admin")])
    await _edit(call, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == "adm:admins")
async def cb_admins(call: CallbackQuery, lang: str, is_owner: bool, db: Storage, config: Config) -> None:
    if not is_owner:
        await call.answer(t(lang, "owner_only"), show_alert=True)
        return
    await _show_admins(call, lang, db, config)
    await call.answer()


@router.callback_query(F.data == "adm:add")
async def cb_add_admin(call: CallbackQuery, lang: str, is_owner: bool, state: FSMContext) -> None:
    if not is_owner:
        await call.answer(t(lang, "owner_only"), show_alert=True)
        return
    await state.set_state(AdminForm.add_admin)
    await _edit(call, t(lang, "admin_add_ask"), back_kb(lang))
    await call.answer()


@router.message(StateFilter(AdminForm.add_admin), ~F.text.startswith("/"))
async def on_add_admin(
    message: Message, bot: Bot, lang: str, is_owner: bool, state: FSMContext, db: Storage, config: Config
) -> None:
    if not is_owner:
        await state.clear()
        return
    user_id = await _target_id(message, db)
    if user_id is None:
        await message.answer(t(lang, "target_not_found"))
        return
    await state.clear()
    if user_id in config.admin_ids or await db.is_admin(user_id):
        await message.answer(t(lang, "admin_exists"), reply_markup=back_kb(lang))
        return
    await db.add_admin(user_id, message.from_user.id)
    await db.set_banned(user_id, False)
    who = _who(user_id, await db.username_of(user_id))
    await message.answer(t(lang, "admin_added", who=who), reply_markup=back_kb(lang))
    with suppress(Exception):  # человек мог ещё не писать боту
        await bot.send_message(user_id, t(lang, "admin_granted"))


@router.callback_query(F.data.startswith("adm:rm:"))
async def cb_remove_admin(call: CallbackQuery, lang: str, is_owner: bool, db: Storage, config: Config) -> None:
    if not is_owner:
        await call.answer(t(lang, "owner_only"), show_alert=True)
        return
    await db.remove_admin(int(call.data.removeprefix("adm:rm:")))
    await call.answer(t(lang, "admin_removed"))
    await _show_admins(call, lang, db, config)
