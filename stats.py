import io
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

import db

TZ = ZoneInfo(os.getenv("TZ", "Europe/Moscow"))
CHANNEL = os.getenv("CHANNEL", "").lstrip("@")


def post_link(unit):
    first = unit[1:] if unit.startswith("m") else None
    if first is None:
        ids = db.unit_ids(unit)
        first = ids[0] if ids else None
    return f"https://t.me/{CHANNEL}/{first}" if first else ""


def _sheet(ws, header, rows):
    ws.append(header)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="305496")
    for r in rows:
        ws.append(r)
    for i, h in enumerate(header, 1):
        width = max([len(str(h))] + [len(str(r[i - 1])) for r in rows[:200]]) + 2
        ws.column_dimensions[get_column_letter(i)].width = min(width, 60)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions


def build_xlsx() -> bytes:
    log = db.conn.execute("SELECT * FROM log ORDER BY ts DESC").fetchall()
    wb = Workbook()

    ws = wb.active
    ws.title = "Журнал"
    _sheet(ws, ["Дата и время", "Чат", "ID чата", "Пост", "Ссылка на пост", "Статус", "Ошибка"],
           [[datetime.fromtimestamp(r["ts"], TZ).replace(tzinfo=None), r["chat_title"],
             r["chat_id"], r["unit"], post_link(r["unit"]) if r["unit"] else "",
             r["status"], r["error"]] for r in log])

    ws = wb.create_sheet("По чатам")
    rows = db.conn.execute(
        "SELECT chat_id, MAX(chat_title) t, SUM(status='ok') ok, SUM(status!='ok') err, "
        "MAX(CASE WHEN status='ok' THEN ts END) last FROM log GROUP BY chat_id").fetchall()
    _sheet(ws, ["Чат", "ID чата", "Успешно", "Ошибки", "Последний пост"],
           [[r["t"], r["chat_id"], r["ok"], r["err"],
             datetime.fromtimestamp(r["last"], TZ).replace(tzinfo=None) if r["last"] else ""]
            for r in rows])

    ws = wb.create_sheet("По постам")
    rows = db.conn.execute(
        "SELECT unit, SUM(status='ok') ok, COUNT(DISTINCT CASE WHEN status='ok' THEN chat_id END) chats "
        "FROM log WHERE unit!='' GROUP BY unit ORDER BY ok DESC").fetchall()
    _sheet(ws, ["Пост", "Ссылка", "Отправок", "Чатов"],
           [[r["unit"], post_link(r["unit"]), r["ok"], r["chats"]] for r in rows])

    ws = wb.create_sheet("По дням")
    rows = db.conn.execute(
        "SELECT ts, status FROM log").fetchall()
    days = {}
    for r in rows:
        d = datetime.fromtimestamp(r["ts"], TZ).date()
        ok, err = days.get(d, (0, 0))
        days[d] = (ok + (r["status"] == "ok"), err + (r["status"] != "ok"))
    _sheet(ws, ["Дата", "Успешно", "Ошибки"],
           [[d, *v] for d, v in sorted(days.items(), reverse=True)])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
