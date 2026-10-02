import asyncio
import logging
import os
import re
import time
from datetime import datetime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import (TelegramBadRequest, TelegramForbiddenError,
                                TelegramMigrateToChat, TelegramRetryAfter)
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BufferedInputFile, CallbackQuery, ChatMemberUpdated,
                           InlineKeyboardButton as Btn, LinkPreviewOptions, Message)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()
import db  # noqa: E402
import stats  # noqa: E402

TOKEN = os.environ["BOT_TOKEN"]
OWNER = int(os.environ["OWNER_ID"])
CHANNEL = os.environ["CHANNEL"]
channel_id = None

router = Router()
owner_msg = router.message.filter(F.chat.type == "private", F.from_user.id == OWNER)
owner_cb = router.callback_query.filter(F.from_user.id == OWNER)
PRESETS = [15, 30, 60, 120, 240, 360, 720, 1440]


class Form(StatesGroup):
    interval = State()
    quiet = State()


def hm(m):
    return f"{m // 60:02d}:{m % 60:02d}"


def quiet_range():
    return int(db.get_setting("quiet_start", 23 * 60)), int(db.get_setting("quiet_end", 8 * 60))


def quiet_label():
    if db.get_setting("quiet_on") != "1":
        return "выкл"
    a, b = quiet_range()
    return f"{hm(a)}–{hm(b)}"


def is_quiet():
    if db.get_setting("quiet_on") != "1":
        return False
    a, b = quiet_range()
    now = datetime.now(stats.TZ)
    m = now.hour * 60 + now.minute
    return a <= m < b if a < b else (m >= a or m < b)


def parse_quiet(text):
    m = re.fullmatch(r"\s*(\d{1,2})(?:[:.](\d{2}))?\s*[-–—]\s*(\d{1,2})(?:[:.](\d{2}))?\s*", text)
    if not m:
        return None
    a = int(m[1]) * 60 + int(m[2] or 0)
    b = int(m[3]) * 60 + int(m[4] or 0)
    if a >= 1440 or b >= 1440 or a == b or int(m[2] or 0) > 59 or int(m[4] or 0) > 59:
        return None
    return a, b


def fmt(m):
    if m % 1440 == 0:
        return f"{m // 1440} д"
    if m % 60 == 0:
        return f"{m // 60} ч"
    return f"{m} мин"


def parse_interval(text):
    m = re.fullmatch(r"\s*(\d+)\s*(м|мин|m|ч|h|д|d)?\s*", text.lower())
    if not m:
        return None
    n = int(m[1]) * {"ч": 60, "h": 60, "д": 1440, "d": 1440}.get(m[2], 1)
    return n if n >= 1 else None


# ---------- screens ----------

def main_kb():
    paused = db.get_setting("paused") == "1"
    kb = InlineKeyboardBuilder()
    kb.button(text=f"💬 Чаты ({len(db.list_chats())})", callback_data="chats")
    kb.button(text=f"🗂 Посты ({db.units_count()})", callback_data="posts:0")
    kb.button(text=f"⏱ Интервал по умолчанию: {fmt(db.default_interval())}", callback_data="ivpick:0")
    kb.button(text=f"🌙 Тихие часы: {quiet_label()}", callback_data="quiet")
    kb.button(text="▶️ Возобновить всё" if paused else "⏸ Пауза всего", callback_data="pause")
    kb.button(text="📊 Excel-статистика", callback_data="xlsx")
    kb.adjust(1)
    return kb.as_markup()


def main_text():
    state = "⏸ на паузе" if db.get_setting("paused") == "1" else "▶️ работает"
    return (f"Автопостинг: {state}\nПостов в базе: {db.units_count()}\n"
            "Порядок: случайный, без повторов до конца круга.")


def chats_screen():
    kb = InlineKeyboardBuilder()
    for c in db.list_chats():
        mark = "✅" if c["enabled"] else "⏸"
        own = "" if c["interval"] else " (общ.)"
        kb.button(text=f"{mark} {c['title']} · {fmt(db.chat_interval(c))}{own}",
                  callback_data=f"chat:{c['chat_id']}")
    kb.button(text="« Назад", callback_data="main")
    kb.adjust(1)
    text = "Чаты, где бот добавлен." if db.list_chats() else (
        "Чатов пока нет. Добавьте бота в группу и назначьте администратором.")
    return text, kb.as_markup()


def chat_screen(chat_id):
    c = db.get_chat(chat_id)
    nxt = datetime.fromtimestamp(c["next_at"], stats.TZ).strftime("%d.%m %H:%M")
    text = (f"{c['title']}\nСтатус: {'включён' if c['enabled'] else 'выключен'}\n"
            f"Интервал: {fmt(db.chat_interval(c))}{'' if c['interval'] else ' (общий)'}\n"
            f"Следующий пост: {nxt}")
    kb = InlineKeyboardBuilder()
    kb.button(text="⏸ Выключить" if c["enabled"] else "▶️ Включить", callback_data=f"tg:{chat_id}")
    kb.button(text="⏱ Интервал", callback_data=f"ivpick:{chat_id}")
    kb.button(text="🚀 Отправить сейчас", callback_data=f"now:{chat_id}")
    kb.button(text="🗑 Удалить чат", callback_data=f"cdel:{chat_id}")
    kb.button(text="« Назад", callback_data="chats")
    kb.adjust(1)
    return text, kb.as_markup()


def interval_screen(chat_id):
    kb = InlineKeyboardBuilder()
    for m in PRESETS:
        kb.button(text=fmt(m), callback_data=f"iv:{chat_id}:{m}")
    kb.adjust(4)
    kb.row(Btn(text="✏️ Свой интервал", callback_data=f"ivc:{chat_id}"))
    if chat_id:
        kb.row(Btn(text="↩️ Как общий", callback_data=f"iv:{chat_id}:0"))
    kb.row(Btn(text="« Назад", callback_data=f"chat:{chat_id}" if chat_id else "main"))
    where = db.get_chat(chat_id)["title"] if chat_id else "всех чатов (по умолчанию)"
    return f"Интервал для: {where}", kb.as_markup()


PAGE = 8


def posts_screen(page):
    total = db.units_count()
    page = max(0, min(page, (max(total, 1) - 1) // PAGE))
    units = db.list_units(page * PAGE, PAGE)
    lines = [f"Посты в памяти бота: {total}. Нажмите 🗑, чтобы убрать пост из рассылки.", ""]
    kb = InlineKeyboardBuilder()
    for i, u in enumerate(units, 1):
        album = f" (альбом, {u['n']} шт.)" if u["n"] > 1 else ""
        lines.append(f"{i}. https://t.me/{stats.CHANNEL}/{u['first']}{album}")
        kb.button(text=f"🗑 {i}", callback_data=f"pd:{u['u']}:{page}")
    kb.adjust(4)
    nav = []
    if page > 0:
        nav.append(Btn(text="«", callback_data=f"posts:{page - 1}"))
    if (page + 1) * PAGE < total:
        nav.append(Btn(text="»", callback_data=f"posts:{page + 1}"))
    if nav:
        kb.row(*nav)
    if total:
        kb.row(Btn(text="🧹 Удалить все", callback_data="pclear"))
    kb.row(Btn(text="« Назад", callback_data="main"))
    return "\n".join(lines), kb.as_markup()


async def show(cb: CallbackQuery, text, kb):
    try:
        await cb.message.edit_text(text, reply_markup=kb,
                                   link_preview_options=LinkPreviewOptions(is_disabled=True))
    except TelegramBadRequest:
        pass


# ---------- owner handlers ----------

@router.message(Command("start", "menu"))
async def start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(main_text(), reply_markup=main_kb())


@router.callback_query(F.data == "main")
async def cb_main(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(cb, main_text(), main_kb())
    await cb.answer()


@router.callback_query(F.data == "chats")
async def cb_chats(cb: CallbackQuery):
    await show(cb, *chats_screen())
    await cb.answer()


@router.callback_query(F.data.startswith("chat:"))
async def cb_chat(cb: CallbackQuery):
    await show(cb, *chat_screen(int(cb.data.split(":")[1])))
    await cb.answer()


@router.callback_query(F.data.startswith("cdel:"))
async def cb_chat_delete(cb: CallbackQuery):
    cid = int(cb.data.split(":")[1])
    c = db.get_chat(cid)
    kb = InlineKeyboardBuilder()
    kb.button(text="Удалить из списка", callback_data=f"cdy:{cid}:0")
    kb.button(text="Удалить и выйти из чата", callback_data=f"cdy:{cid}:1")
    kb.button(text="Отмена", callback_data=f"chat:{cid}")
    kb.adjust(1)
    await show(cb, f"Убрать «{c['title']}» из списка? Статистика в Excel сохранится.", kb.as_markup())
    await cb.answer()


@router.callback_query(F.data.startswith("cdy:"))
async def cb_chat_delete_yes(cb: CallbackQuery, bot: Bot):
    _, cid, leave = cb.data.split(":")
    cid = int(cid)
    if leave == "1":
        try:
            await bot.leave_chat(cid)
        except Exception as ex:  # noqa: BLE001
            await cb.message.answer(f"Не удалось выйти из чата: {ex}")
    db.delete_chat(cid)
    await show(cb, *chats_screen())
    await cb.answer("Удалено")


@router.callback_query(F.data == "quiet")
async def cb_quiet(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    on = db.get_setting("quiet_on") == "1"
    a, b = quiet_range()
    kb = InlineKeyboardBuilder()
    kb.button(text="Выключить" if on else "Включить", callback_data="quiet_tg")
    kb.button(text="✏️ Задать часы", callback_data="quiet_set")
    kb.button(text="« Назад", callback_data="main")
    kb.adjust(1)
    await show(cb, f"Тихие часы: {'включены' if on else 'выключены'}\n"
                   f"Не постить с {hm(a)} до {hm(b)} ({stats.TZ.key}).\n"
                   "После окончания тихих часов каждый чат получит один пост.", kb.as_markup())
    await cb.answer()


@router.callback_query(F.data == "quiet_tg")
async def cb_quiet_tg(cb: CallbackQuery, state: FSMContext):
    db.set_setting("quiet_on", "0" if db.get_setting("quiet_on") == "1" else "1")
    await cb_quiet(cb, state)


@router.callback_query(F.data == "quiet_set")
async def cb_quiet_set(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Form.quiet)
    await cb.message.answer("Введите часы в формате 23:00-08:00 (или 23-8).")
    await cb.answer()


@router.message(StateFilter(Form.quiet))
async def got_quiet(m: Message, state: FSMContext):
    r = parse_quiet(m.text or "")
    if not r:
        await m.answer("Не понял. Пример: 23:00-08:00")
        return
    await state.clear()
    db.set_setting("quiet_start", r[0])
    db.set_setting("quiet_end", r[1])
    db.set_setting("quiet_on", "1")
    await m.answer(f"Тихие часы включены: {hm(r[0])}–{hm(r[1])}", reply_markup=main_kb())


@router.callback_query(F.data == "pause")
async def cb_pause(cb: CallbackQuery):
    db.set_setting("paused", "0" if db.get_setting("paused") == "1" else "1")
    await show(cb, main_text(), main_kb())
    await cb.answer()


@router.callback_query(F.data.startswith("tg:"))
async def cb_toggle(cb: CallbackQuery):
    cid = int(cb.data.split(":")[1])
    c = db.get_chat(cid)
    db.update_chat(cid, enabled=0 if c["enabled"] else 1,
                   next_at=time.time() + db.chat_interval(c) * 60)
    await show(cb, *chat_screen(cid))
    await cb.answer()


@router.callback_query(F.data.startswith("now:"))
async def cb_now(cb: CallbackQuery):
    cid = int(cb.data.split(":")[1])
    db.update_chat(cid, next_at=0)
    await cb.answer("Отправка в ближайшие секунды", show_alert=False)


@router.callback_query(F.data.startswith("ivpick:"))
async def cb_ivpick(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(cb, *interval_screen(int(cb.data.split(":")[1])))
    await cb.answer()


@router.callback_query(F.data.startswith("iv:"))
async def cb_iv(cb: CallbackQuery):
    _, cid, minutes = cb.data.split(":")
    await apply_interval(int(cid), int(minutes))
    await show(cb, *(chat_screen(int(cid)) if int(cid) else (main_text(), main_kb())))
    await cb.answer("Сохранено")


async def apply_interval(cid, minutes):
    if cid:
        c = db.get_chat(cid)
        db.update_chat(cid, interval=minutes or None)
        db.update_chat(cid, next_at=time.time() + db.chat_interval(db.get_chat(cid)) * 60)
    else:
        db.set_setting("default_interval", minutes)


@router.callback_query(F.data.startswith("ivc:"))
async def cb_ivc(cb: CallbackQuery, state: FSMContext):
    cid = int(cb.data.split(":")[1])
    await state.set_state(Form.interval)
    await state.update_data(cid=cid)
    await cb.message.answer("Введите интервал: число минут или с суффиксом, например 45, 3ч, 1д.")
    await cb.answer()


@router.message(StateFilter(Form.interval))
async def got_interval(m: Message, state: FSMContext):
    minutes = parse_interval(m.text or "")
    if not minutes:
        await m.answer("Не понял. Пример: 45, 3ч, 1д.")
        return
    cid = (await state.get_data())["cid"]
    await state.clear()
    await apply_interval(cid, minutes)
    await m.answer(f"Интервал: {fmt(minutes)}", reply_markup=main_kb())


@router.callback_query(F.data.startswith("posts:"))
async def cb_posts(cb: CallbackQuery):
    await show(cb, *posts_screen(int(cb.data.split(":")[1])))
    await cb.answer()


@router.callback_query(F.data.startswith("pd:"))
async def cb_post_delete(cb: CallbackQuery):
    _, unit, page = cb.data.split(":")
    db.delete_unit(unit)
    await show(cb, *posts_screen(int(page)))
    await cb.answer("Удалено")


@router.callback_query(F.data == "pclear")
async def cb_pclear(cb: CallbackQuery):
    kb = InlineKeyboardBuilder()
    kb.button(text="Да, удалить все", callback_data="pclear_yes")
    kb.button(text="Отмена", callback_data="posts:0")
    kb.adjust(1)
    await show(cb, "Удалить все посты из памяти бота? Новые посты канала добавятся заново.", kb.as_markup())
    await cb.answer()


@router.callback_query(F.data == "pclear_yes")
async def cb_pclear_yes(cb: CallbackQuery):
    db.clear_posts()
    await show(cb, *posts_screen(0))
    await cb.answer("Очищено")


@router.callback_query(F.data == "xlsx")
async def cb_xlsx(cb: CallbackQuery):
    await cb.answer("Готовлю файл…")
    name = f"stats_{datetime.now(stats.TZ):%Y-%m-%d_%H%M}.xlsx"
    await cb.message.answer_document(BufferedInputFile(stats.build_xlsx(), name))


# ---------- events (not owner-restricted) ----------

@router.channel_post()
async def on_channel_post(m: Message):
    if m.chat.id == channel_id:
        db.add_post(m.message_id, m.media_group_id)


@router.my_chat_member()
async def on_member(e: ChatMemberUpdated, bot: Bot):
    if e.chat.type not in ("group", "supergroup"):
        return
    status = e.new_chat_member.status
    if status in ("administrator", "member"):
        db.upsert_chat(e.chat.id, e.chat.title, e.chat.type)
        note = "администратором" if status == "administrator" else "участником (без прав админа)"
        await bot.send_message(OWNER, f"Бот добавлен в «{e.chat.title}» {note}.")
    elif status in ("left", "kicked"):
        db.upsert_chat(e.chat.id, e.chat.title, e.chat.type, active=0)
        await bot.send_message(OWNER, f"Бот удалён из «{e.chat.title}».")


# ---------- scheduler ----------

async def post_to_chat(bot: Bot, chat):
    cid, title = chat["chat_id"], chat["title"]
    next_at = time.time() + db.chat_interval(chat) * 60
    try:
        for _ in range(5):
            unit = db.pick_unit(cid)
            if unit is None:
                return
            ids = db.unit_ids(unit)
            try:
                await bot.forward_messages(cid, channel_id, ids)
            except TelegramBadRequest as ex:
                err = str(ex)
                if "not found" in err or "can't be forwarded" in err or "MESSAGE_ID_INVALID" in err:
                    db.deactivate_unit(unit)
                    db.add_log(cid, title, unit, "post_unavailable", err)
                    continue
                db.add_log(cid, title, unit, "error", err)
                return
            db.mark_sent(cid, unit)
            db.add_log(cid, title, unit, "ok")
            return
    except TelegramRetryAfter as ex:
        next_at = time.time() + ex.retry_after + 1
        db.add_log(cid, title, "", "flood_wait", str(ex))
    except TelegramMigrateToChat as ex:
        db.migrate_chat(cid, ex.migrate_to_chat_id)
    except TelegramForbiddenError as ex:
        db.update_chat(cid, active=0)
        db.add_log(cid, title, "", "forbidden", str(ex))
        await bot.send_message(OWNER, f"Нет доступа к «{title}», чат отключён: {ex}")
    except Exception as ex:  # noqa: BLE001
        logging.exception("post failed")
        db.add_log(cid, title, "", "error", str(ex))
    finally:
        if db.get_chat(cid):
            db.update_chat(cid, next_at=next_at)


async def scheduler(bot: Bot):
    while True:
        await asyncio.sleep(10)
        if db.get_setting("paused") == "1" or is_quiet():
            continue
        for chat in db.due_chats():
            await post_to_chat(bot, chat)
            await asyncio.sleep(1)


async def main():
    global channel_id
    logging.basicConfig(level=logging.INFO)
    bot = Bot(TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    channel_id = (await bot.get_chat(CHANNEL)).id
    logging.info("channel %s -> %s", CHANNEL, channel_id)
    asyncio.create_task(scheduler(bot))
    await dp.start_polling(bot, allowed_updates=["message", "callback_query",
                                                 "channel_post", "my_chat_member"])


if __name__ == "__main__":
    asyncio.run(main())
