import asyncio
import logging
import os
import re
import time
from datetime import datetime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import (TelegramBadRequest, TelegramForbiddenError,
                                TelegramMigrateToChat, TelegramRetryAfter,
                                TelegramUnauthorizedError)
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

MASTER: Bot = None  # control bot (also worker #1 for backward compatibility)
RT = {}             # worker_id -> {"bot", "channel_id", "task"} for running workers

router = Router()
router.message.filter(F.chat.type == "private", F.from_user.id == OWNER)
router.callback_query.filter(F.from_user.id == OWNER)
send_lock = asyncio.Lock()
NOPREVIEW = LinkPreviewOptions(is_disabled=True)
PRESETS = [15, 30, 60, 120, 240, 360, 720, 1440]
PAGE = 8
GONE = ("chat not found", "kicked", "not a member", "forbidden", "deactivated")


class Form(StatesGroup):
    interval = State()
    quiet = State()
    token = State()
    channel = State()


# ---------- helpers ----------

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


def norm_channel(t):
    t = t.strip()
    m = re.search(r"t\.me/([A-Za-z0-9_]{4,})", t)
    if m:
        return "@" + m[1]
    if re.fullmatch(r"-100\d+", t):
        return t
    if re.fullmatch(r"@?[A-Za-z0-9_]{4,}", t):
        return "@" + t.lstrip("@")
    return None


def chat_arg(ref):
    return int(ref) if str(ref).lstrip("-").isdigit() else ref


def chat_ref(c):
    return f"{c['title']} — {c['link']}" if c["link"] else f"{c['title']} (ссылки нет)"


def worker_state(w):
    if not w["enabled"]:
        return "⏸", "выключен"
    if w["id"] in RT:
        return "🟢", "работает"
    return "🔴", "не запущен (нет доступа к каналу или токен недействителен)"


async def refresh_link(bot: Bot, chat_id):
    chat = await bot.get_chat(chat_id)
    db.set_link(chat_id, f"https://t.me/{chat.username}" if chat.username else chat.invite_link)


async def show(cb: CallbackQuery, text, kb):
    try:
        await cb.message.edit_text(text, reply_markup=kb, link_preview_options=NOPREVIEW)
    except TelegramBadRequest:
        pass


async def notify(text):
    try:
        await MASTER.send_message(OWNER, text, link_preview_options=NOPREVIEW)
    except Exception:  # noqa: BLE001
        logging.exception("notify failed")


# ---------- worker lifecycle ----------

async def start_worker(w):
    wid = w["id"]
    if wid in RT:
        return
    is_master = w["token"] == TOKEN
    bot = MASTER if is_master else Bot(w["token"])
    try:
        ch = await bot.get_chat(chat_arg(w["channel_ref"]))
    except Exception:
        if not is_master:
            await bot.session.close()
        raise
    db.update_worker(wid, channel_id=ch.id)
    task = None if is_master else asyncio.create_task(poll_worker(wid, bot))
    RT[wid] = {"bot": bot, "channel_id": ch.id, "task": task}


async def stop_worker(wid):
    rt = RT.pop(wid, None)
    if not rt or rt["bot"] is MASTER:
        return
    rt["task"].cancel()
    await rt["bot"].session.close()


async def poll_worker(wid, bot: Bot):
    """Workers only need channel posts and membership changes; no dispatcher required."""
    offset = None
    while True:
        try:
            updates = await bot.get_updates(offset=offset, timeout=30,
                                            allowed_updates=["channel_post", "my_chat_member"])
            for u in updates:
                offset = u.update_id + 1
                try:
                    if u.channel_post:
                        await handle_channel_post(u.channel_post, bot)
                    elif u.my_chat_member:
                        await handle_member(u.my_chat_member, bot)
                except Exception:  # noqa: BLE001
                    logging.exception("worker update failed")
        except asyncio.CancelledError:
            raise
        except TelegramUnauthorizedError:
            w = db.get_worker(wid)
            db.update_worker(wid, enabled=0)
            RT.pop(wid, None)
            await notify(f"Токен бота {w['name']} недействителен, бот выключен.")
            return
        except Exception:  # noqa: BLE001
            await asyncio.sleep(5)


async def handle_channel_post(m: Message, bot: Bot):
    w = db.worker_by_bot(bot.id)
    rt = RT.get(w["id"]) if w else None
    if rt and m.chat.id == rt["channel_id"]:
        db.add_post(w["id"], m.message_id, m.media_group_id)


async def handle_member(e: ChatMemberUpdated, bot: Bot):
    if e.chat.type not in ("group", "supergroup"):
        return
    w = db.worker_by_bot(bot.id)
    if not w:
        return
    status = e.new_chat_member.status
    if status in ("administrator", "member"):
        db.upsert_chat(e.chat.id, e.chat.title, e.chat.type, w["id"])
        try:
            await refresh_link(bot, e.chat.id)
        except Exception:  # noqa: BLE001
            pass
        note = "администратором" if status == "administrator" else "участником (без прав админа)"
        await notify(f"{w['name']} добавлен в «{e.chat.title}» {note}.")
    elif status in ("left", "kicked"):
        db.delete_chat(e.chat.id)
        await notify(f"{w['name']} удалён из «{e.chat.title}», чат убран из списка.")


# ---------- screens ----------

def main_kb():
    paused = db.get_setting("paused") == "1"
    kb = InlineKeyboardBuilder()
    kb.button(text=f"🤖 Боты ({len(db.list_workers())})", callback_data="bots")
    kb.button(text=f"💬 Все чаты ({len(db.list_chats())})", callback_data="chats:0")
    kb.button(text="🚀 Отправить во все чаты", callback_data="all")
    kb.button(text="🔍 Проверить доступность чатов", callback_data="check")
    kb.button(text=f"⏱ Интервал по умолчанию: {fmt(db.default_interval())}", callback_data="ivpick:0")
    kb.button(text=f"🌙 Тихие часы: {quiet_label()}", callback_data="quiet")
    kb.button(text="▶️ Возобновить всё" if paused else "⏸ Пауза всего", callback_data="pause")
    kb.button(text="📊 Excel-статистика", callback_data="xlsx")
    kb.adjust(1)
    return kb.as_markup()


def main_text():
    state = "⏸ на паузе" if db.get_setting("paused") == "1" else "▶️ работает"
    return (f"Автопостинг: {state}\nБотов: {len(db.list_workers())} · "
            f"чатов: {len(db.list_chats())} · постов: {db.units_count()}\n"
            "Порядок: случайный, без повторов до конца круга.")


def bots_screen():
    kb = InlineKeyboardBuilder()
    lines = []
    for w in db.list_workers():
        icon, _ = worker_state(w)
        lines.append(f"{icon} {w['name']} — {stats.channel_base(w) or w['channel_ref']}")
        kb.button(text=f"{icon} {w['name']}", callback_data=f"w:{w['id']}")
    kb.button(text="➕ Добавить бота", callback_data="wadd")
    kb.button(text="« Назад", callback_data="main")
    kb.adjust(1)
    return "Боты (канал-прокладка у каждого свой):\n\n" + "\n".join(lines), kb.as_markup()


def worker_screen(wid):
    w = db.get_worker(wid)
    icon, state = worker_state(w)
    text = (f"{icon} {w['name']}\nСтатус: {state}\n"
            f"Канал: {stats.channel_base(w) or w['channel_ref']}\n"
            f"Чатов: {len(db.list_chats(worker_id=wid))} · постов: {db.units_count(wid)}")
    kb = InlineKeyboardBuilder()
    kb.button(text="⏸ Выключить" if w["enabled"] else "▶️ Включить", callback_data=f"wt:{wid}")
    kb.button(text="💬 Чаты бота", callback_data=f"chats:{wid}")
    kb.button(text="🗂 Посты бота", callback_data=f"posts:{wid}:0")
    if wid != 1:
        kb.button(text="🗑 Удалить бота", callback_data=f"wdel:{wid}")
    kb.button(text="« Назад", callback_data="bots")
    kb.adjust(1)
    return text, kb.as_markup()


def chats_screen(wid=0):
    kb = InlineKeyboardBuilder()
    chats = db.list_chats(worker_id=wid or None)
    names = {w["id"]: w["name"] for w in db.list_workers()}
    lines = []
    for i, c in enumerate(chats, 1):
        mark = "✅" if c["enabled"] else "⏸"
        own = "" if c["interval"] else " (общ.)"
        bot = f" [{names.get(c['worker_id'], '?')}]" if not wid else ""
        lines.append(f"{i}. {chat_ref(c)}{bot}")
        kb.button(text=f"{mark} {i}. {c['title']} · {fmt(db.chat_interval(c))}{own}",
                  callback_data=f"chat:{c['chat_id']}")
    kb.button(text="« Назад", callback_data=f"w:{wid}" if wid else "main")
    kb.adjust(1)
    text = ("Чаты:\n\n" + "\n".join(lines)) if chats else (
        "Чатов пока нет. Добавьте рабочего бота в группу и назначьте администратором.")
    return text, kb.as_markup()


def chat_screen(chat_id):
    c = db.get_chat(chat_id)
    w = db.get_worker(c["worker_id"])
    nxt = datetime.fromtimestamp(c["next_at"], stats.TZ).strftime("%d.%m %H:%M")
    text = (f"{chat_ref(c)}\nБот: {w['name'] if w else '?'}\n"
            f"Статус: {'включён' if c['enabled'] else 'выключен'}\n"
            f"Интервал: {fmt(db.chat_interval(c))}{'' if c['interval'] else ' (общий)'}\n"
            f"Следующий пост: {nxt}")
    kb = InlineKeyboardBuilder()
    kb.button(text="⏸ Выключить" if c["enabled"] else "▶️ Включить", callback_data=f"tg:{chat_id}")
    kb.button(text="⏱ Интервал", callback_data=f"ivpick:{chat_id}")
    kb.button(text="🚀 Отправить сейчас", callback_data=f"now:{chat_id}")
    kb.button(text="🗑 Удалить чат", callback_data=f"cdel:{chat_id}")
    kb.button(text="« Назад", callback_data=f"chats:{c['worker_id']}")
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


def posts_screen(wid, page):
    w = db.get_worker(wid)
    total = db.units_count(wid)
    page = max(0, min(page, (max(total, 1) - 1) // PAGE))
    units = db.list_units(wid, page * PAGE, PAGE)
    base = stats.channel_base(w)
    lines = [f"{w['name']}: постов в памяти {total}. Нажмите 🗑, чтобы убрать пост из рассылки.", ""]
    kb = InlineKeyboardBuilder()
    for i, u in enumerate(units, 1):
        album = f" (альбом, {u['n']} шт.)" if u["n"] > 1 else ""
        lines.append(f"{i}. {base}/{u['first']}{album}")
        kb.button(text=f"🗑 {i}", callback_data=f"pd:{wid}:{u['u']}:{page}")
    kb.adjust(4)
    nav = []
    if page > 0:
        nav.append(Btn(text="«", callback_data=f"posts:{wid}:{page - 1}"))
    if (page + 1) * PAGE < total:
        nav.append(Btn(text="»", callback_data=f"posts:{wid}:{page + 1}"))
    if nav:
        kb.row(*nav)
    if total:
        kb.row(Btn(text="🧹 Удалить все", callback_data=f"pclear:{wid}"))
    kb.row(Btn(text="« Назад", callback_data=f"w:{wid}"))
    return "\n".join(lines), kb.as_markup()


# ---------- owner handlers: navigation ----------

@router.message(Command("start", "menu"))
async def start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(main_text(), reply_markup=main_kb())


@router.callback_query(F.data == "main")
async def cb_main(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(cb, main_text(), main_kb())
    await cb.answer()


@router.callback_query(F.data == "bots")
async def cb_bots(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(cb, *bots_screen())
    await cb.answer()


@router.callback_query(F.data.startswith("w:"))
async def cb_worker(cb: CallbackQuery):
    await show(cb, *worker_screen(int(cb.data.split(":")[1])))
    await cb.answer()


@router.callback_query(F.data.startswith("chats:"))
async def cb_chats(cb: CallbackQuery):
    await show(cb, *chats_screen(int(cb.data.split(":")[1])))
    await cb.answer()


@router.callback_query(F.data.startswith("chat:"))
async def cb_chat(cb: CallbackQuery):
    await show(cb, *chat_screen(int(cb.data.split(":")[1])))
    await cb.answer()


# ---------- workers management ----------

@router.callback_query(F.data.startswith("wt:"))
async def cb_worker_toggle(cb: CallbackQuery):
    wid = int(cb.data.split(":")[1])
    w = db.get_worker(wid)
    if w["enabled"]:
        db.update_worker(wid, enabled=0)
        await stop_worker(wid)
    else:
        db.update_worker(wid, enabled=1)
        try:
            await start_worker(db.get_worker(wid))
        except Exception as ex:  # noqa: BLE001
            await cb.message.answer(f"Не удалось запустить {w['name']}: {ex}")
    await show(cb, *worker_screen(wid))
    await cb.answer()


@router.callback_query(F.data.startswith("wdel:"))
async def cb_worker_delete(cb: CallbackQuery):
    wid = int(cb.data.split(":")[1])
    w = db.get_worker(wid)
    kb = InlineKeyboardBuilder()
    kb.button(text="Да, удалить", callback_data=f"wdy:{wid}")
    kb.button(text="Отмена", callback_data=f"w:{wid}")
    kb.adjust(1)
    await show(cb, f"Удалить {w['name']}? Будут удалены его чаты и посты в памяти, "
                   "токен забудется. Статистика в Excel сохранится. Из самих групп бот не выйдет.",
               kb.as_markup())
    await cb.answer()


@router.callback_query(F.data.startswith("wdy:"))
async def cb_worker_delete_yes(cb: CallbackQuery):
    wid = int(cb.data.split(":")[1])
    if wid != 1:
        await stop_worker(wid)
        db.delete_worker(wid)
    await show(cb, *bots_screen())
    await cb.answer("Удалено")


@router.callback_query(F.data == "wadd")
async def cb_worker_add(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Form.token)
    await cb.message.answer("Пришлите токен нового рабочего бота (из @BotFather). "
                            "Сообщение с токеном я сразу удалю.")
    await cb.answer()


@router.message(StateFilter(Form.token))
async def got_token(m: Message, state: FSMContext):
    token = (m.text or "").strip()
    try:
        await m.delete()
    except Exception:  # noqa: BLE001
        pass
    if not re.fullmatch(r"\d{6,}:[\w-]{30,}", token):
        await m.answer("Это не похоже на токен. Попробуйте ещё раз или нажмите /start для отмены.")
        return
    if any(w["token"] == token for w in db.list_workers()):
        await m.answer("Такой бот уже добавлен.")
        return
    tmp = Bot(token)
    try:
        me = await tmp.get_me()
    except Exception as ex:  # noqa: BLE001
        await m.answer(f"Токен не подошёл: {ex}")
        return
    finally:
        await tmp.session.close()
    await state.update_data(token=token, bot_id=me.id, username=me.username)
    await state.set_state(Form.channel)
    await m.answer(f"Бот @{me.username} найден. Теперь пришлите канал-прокладку этого бота "
                   "(@username или ссылку t.me/...; для закрытого канала числовой ID вида -100…). "
                   "Бот должен быть администратором канала.")


@router.message(StateFilter(Form.channel))
async def got_channel(m: Message, state: FSMContext):
    ref = norm_channel(m.text or "")
    if not ref:
        await m.answer("Не понял канал. Пример: @mychannel или https://t.me/mychannel")
        return
    data = await state.get_data()
    bot = Bot(data["token"])
    try:
        ch = await bot.get_chat(chat_arg(ref))
        if ch.type != "channel":
            await m.answer("Это не канал. Пришлите канал-прокладку.")
            return
        if any(w["channel_id"] == ch.id for w in db.list_workers()):
            await m.answer("Этот канал уже используется другим ботом.")
            return
        member = await bot.get_chat_member(ch.id, bot.id)
    except Exception as ex:  # noqa: BLE001
        await m.answer(f"Нет доступа к каналу: {ex}\nДобавьте бота в канал администратором и пришлите канал снова.")
        return
    finally:
        await bot.session.close()
    wid = db.add_worker(data["token"], data["bot_id"], data["username"], ref, ch.id)
    await state.clear()
    try:
        await start_worker(db.get_worker(wid))
    except Exception as ex:  # noqa: BLE001
        await m.answer(f"Бот сохранён, но не запустился: {ex}")
        return
    warn = "" if member.status == "administrator" else (
        "\n⚠️ Бот не администратор канала: новые посты он не увидит. Сделайте его админом.")
    await m.answer(f"Готово: @{data['username']} добавлен и работает.{warn}\n"
                   "Теперь добавьте его в нужные группы администратором: чаты появятся в списке сами.",
                   reply_markup=main_kb())


# ---------- chats / intervals ----------

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
    db.update_chat(int(cb.data.split(":")[1]), next_at=0)
    await cb.answer("Отправка в ближайшие секунды")


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
async def cb_chat_delete_yes(cb: CallbackQuery):
    _, cid, leave = cb.data.split(":")
    cid = int(cid)
    c = db.get_chat(cid)
    wid = c["worker_id"] if c else 0
    if leave == "1" and c and c["worker_id"] in RT:
        try:
            await RT[c["worker_id"]]["bot"].leave_chat(cid)
        except Exception as ex:  # noqa: BLE001
            await cb.message.answer(f"Не удалось выйти из чата: {ex}")
    db.delete_chat(cid)
    await show(cb, *chats_screen(wid))
    await cb.answer("Удалено")


@router.callback_query(F.data.startswith("ivpick:"))
async def cb_ivpick(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(cb, *interval_screen(int(cb.data.split(":")[1])))
    await cb.answer()


async def apply_interval(cid, minutes):
    if cid:
        db.update_chat(cid, interval=minutes or None)
        db.update_chat(cid, next_at=time.time() + db.chat_interval(db.get_chat(cid)) * 60)
    else:
        db.set_setting("default_interval", minutes)


@router.callback_query(F.data.startswith("iv:"))
async def cb_iv(cb: CallbackQuery):
    _, cid, minutes = cb.data.split(":")
    await apply_interval(int(cid), int(minutes))
    await show(cb, *(chat_screen(int(cid)) if int(cid) else (main_text(), main_kb())))
    await cb.answer("Сохранено")


@router.callback_query(F.data.startswith("ivc:"))
async def cb_ivc(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Form.interval)
    await state.update_data(cid=int(cb.data.split(":")[1]))
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


# ---------- quiet hours ----------

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


# ---------- posts ----------

@router.callback_query(F.data.startswith("posts:"))
async def cb_posts(cb: CallbackQuery):
    _, wid, page = cb.data.split(":")
    await show(cb, *posts_screen(int(wid), int(page)))
    await cb.answer()


@router.callback_query(F.data.startswith("pd:"))
async def cb_post_delete(cb: CallbackQuery):
    _, wid, unit, page = cb.data.split(":")
    db.delete_unit(int(wid), unit)
    await show(cb, *posts_screen(int(wid), int(page)))
    await cb.answer("Удалено")


@router.callback_query(F.data.startswith("pclear:"))
async def cb_pclear(cb: CallbackQuery):
    wid = int(cb.data.split(":")[1])
    kb = InlineKeyboardBuilder()
    kb.button(text="Да, удалить все", callback_data=f"pclear_yes:{wid}")
    kb.button(text="Отмена", callback_data=f"posts:{wid}:0")
    kb.adjust(1)
    await show(cb, "Удалить все посты бота из памяти? Новые посты канала добавятся заново.", kb.as_markup())
    await cb.answer()


@router.callback_query(F.data.startswith("pclear_yes:"))
async def cb_pclear_yes(cb: CallbackQuery):
    wid = int(cb.data.split(":")[1])
    db.clear_posts(wid)
    await show(cb, *posts_screen(wid, 0))
    await cb.answer("Очищено")


# ---------- send all / check / excel ----------

def active_chats():
    return [c for c in db.list_chats() if c["enabled"] and c["worker_id"] in RT]


@router.callback_query(F.data == "all")
async def cb_all(cb: CallbackQuery):
    n = len(active_chats())
    kb = InlineKeyboardBuilder()
    kb.button(text=f"Да, отправить в {n}", callback_data="all_yes")
    kb.button(text="Отмена", callback_data="main")
    kb.adjust(1)
    await show(cb, f"Отправить по одному посту во все включённые чаты ({n}) всех ботов? "
                   "Паузы и тихие часы игнорируются, у каждого чата свой случайный пост. "
                   "Таймер следующего поста в чатах начнётся заново.", kb.as_markup())
    await cb.answer()


@router.callback_query(F.data == "all_yes")
async def cb_all_yes(cb: CallbackQuery):
    chats = active_chats()
    if not chats:
        await cb.answer("Нет включённых чатов", show_alert=True)
        return
    await show(cb, f"Отправляю в {len(chats)} чатов…", None)
    await cb.answer()
    start_ts = time.time()
    async with send_lock:
        for c in chats:
            await post_to_chat(c)
            await asyncio.sleep(1)
    ok, bad = db.conn.execute(
        "SELECT COALESCE(SUM(status='ok'),0), COALESCE(SUM(status!='ok'),0) FROM log WHERE ts>=?",
        (start_ts,)).fetchone()
    await cb.message.answer(f"Готово: успешно {ok}, ошибок {bad}.", reply_markup=main_kb())


@router.callback_query(F.data == "check")
async def cb_check(cb: CallbackQuery):
    await cb.answer("Проверяю…")
    total, removed, unknown = await check_chats()
    text = f"Проверено чатов: {total}. Удалено недоступных: {len(removed)}."
    if removed:
        text += "\n" + "\n".join(f"– {t}" for t in removed)
    if unknown:
        text += f"\nНе удалось проверить: {unknown} (временная ошибка, чаты оставлены)."
    await cb.message.answer(text, reply_markup=main_kb(), link_preview_options=NOPREVIEW)


@router.callback_query(F.data == "xlsx")
async def cb_xlsx(cb: CallbackQuery):
    await cb.answer("Готовлю файл…")
    name = f"stats_{datetime.now(stats.TZ):%Y-%m-%d_%H%M}.xlsx"
    await cb.message.answer_document(BufferedInputFile(stats.build_xlsx(), name))


# ---------- master-bot events ----------

@router.channel_post()
async def on_channel_post(m: Message, bot: Bot):
    await handle_channel_post(m, bot)


@router.my_chat_member()
async def on_member(e: ChatMemberUpdated, bot: Bot):
    await handle_member(e, bot)


# ---------- posting ----------

async def post_to_chat(chat):
    wid, cid, title = chat["worker_id"], chat["chat_id"], chat["title"]
    rt, w = RT.get(wid), db.get_worker(wid)
    if not rt or not w:
        return
    bot, channel_id = rt["bot"], rt["channel_id"]
    next_at = time.time() + db.chat_interval(chat) * 60
    try:
        for _ in range(5):
            unit = db.pick_unit(wid, cid)
            if unit is None:
                return
            ids = db.unit_ids(wid, unit)
            try:
                await bot.forward_messages(cid, channel_id, ids)
            except TelegramBadRequest as ex:
                err = str(ex)
                if "not found" in err or "can't be forwarded" in err or "MESSAGE_ID_INVALID" in err:
                    db.deactivate_unit(wid, unit)
                    db.add_log(w, cid, title, unit, "post_unavailable", err)
                    continue
                db.add_log(w, cid, title, unit, "error", err)
                return
            db.mark_sent(cid, unit)
            db.add_log(w, cid, title, unit, "ok")
            return
    except TelegramRetryAfter as ex:
        next_at = time.time() + ex.retry_after + 1
        db.add_log(w, cid, title, "", "flood_wait", str(ex))
    except TelegramMigrateToChat as ex:
        db.migrate_chat(cid, ex.migrate_to_chat_id)
    except TelegramForbiddenError as ex:
        db.delete_chat(cid)
        db.add_log(w, cid, title, "", "forbidden", str(ex))
        await notify(f"{w['name']}: нет доступа к «{title}», чат убран из списка:\n"
                     f"{chat['link'] or ''}\n{ex}")
    except Exception as ex:  # noqa: BLE001
        logging.exception("post failed")
        db.add_log(w, cid, title, "", "error", str(ex))
    finally:
        if db.get_chat(cid):
            db.update_chat(cid, next_at=next_at)


async def check_chats():
    """Returns (checked, removed, unknown). Only definitive errors remove a chat."""
    removed, unknown = [], 0
    chats = db.list_chats(active_only=False)
    for c in chats:
        cid = c["chat_id"]
        rt = RT.get(c["worker_id"])
        if not rt:
            unknown += 1
            continue
        bot = rt["bot"]
        try:
            m = await bot.get_chat_member(cid, bot.id)
            if m.status in ("left", "kicked"):
                raise TelegramForbiddenError(method=None, message="bot is not a member")
            await refresh_link(bot, cid)
        except TelegramMigrateToChat as ex:
            db.migrate_chat(cid, ex.migrate_to_chat_id)
        except TelegramForbiddenError:
            db.delete_chat(cid)
            removed.append(chat_ref(c))
        except TelegramBadRequest as ex:
            if any(g in str(ex).lower() for g in GONE):
                db.delete_chat(cid)
                removed.append(chat_ref(c))
            else:
                unknown += 1
        except TelegramRetryAfter as ex:
            await asyncio.sleep(ex.retry_after + 1)
            unknown += 1
        except Exception:  # noqa: BLE001 - network hiccup, keep the chat
            unknown += 1
        await asyncio.sleep(0.5)
    return len(chats), removed, unknown


async def checker():
    while True:
        _, removed, _ = await check_chats()
        if removed:
            await notify("Недоступные чаты убраны из списка:\n" + "\n".join(removed))
        await asyncio.sleep(6 * 3600)


async def scheduler():
    while True:
        await asyncio.sleep(10)
        if db.get_setting("paused") == "1" or is_quiet():
            continue
        async with send_lock:
            for chat in db.due_chats():
                await post_to_chat(chat)
                await asyncio.sleep(1)


async def main():
    global MASTER
    logging.basicConfig(level=logging.INFO)
    MASTER = Bot(TOKEN)
    me = await MASTER.get_me()
    db.ensure_main_worker(TOKEN, me.id, CHANNEL)
    db.update_worker(1, username=me.username, name=f"Главный @{me.username}")
    dp = Dispatcher()
    dp.include_router(router)
    for w in db.list_workers():
        if not w["enabled"]:
            continue
        try:
            await start_worker(w)
            logging.info("worker %s started", w["name"])
        except Exception as ex:  # noqa: BLE001
            logging.error("worker %s failed: %s", w["name"], ex)
            await notify(f"{w['name']} не запущен: {ex}")
    asyncio.create_task(scheduler())
    asyncio.create_task(checker())
    await dp.start_polling(MASTER, allowed_updates=["message", "callback_query",
                                                    "channel_post", "my_chat_member"])


if __name__ == "__main__":
    asyncio.run(main())
