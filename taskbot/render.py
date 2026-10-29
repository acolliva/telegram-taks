"""Texts and keyboards of the bot (Italian UI)."""

from __future__ import annotations

from datetime import datetime, timedelta
from html import escape
from itertools import groupby
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton as B
from telegram import InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from .db import Task, User
from .text import fmt_age, fmt_day_heading, fmt_when

PAGE_SIZE = 8
BTN_TODO = "📋 Da fare"
BTN_DONE = "✅ Fatte"
MENU = ReplyKeyboardMarkup([[KeyboardButton(BTN_TODO), KeyboardButton(BTN_DONE)]],
                           resize_keyboard=True, is_persistent=True,
                           input_field_placeholder="Scrivi o detta una cosa da fare…")

PH_PHOTO = "📷 Foto"
PH_VOICE = "🎙️ Nota vocale"
PLACEHOLDERS = (PH_PHOTO, PH_VOICE)


def _short(s: str, n: int = 48) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _icons(t: Task) -> str:
    return (" 📷" if t.photo_count else "") + (" 🎙" if t.voice_file_id else "") + (" 📝" if t.notes else "")


# ------------------------------------------------------------------- to-do list


def todo_list(tasks: list[Task], page: int, now: datetime, undo: Task | None,
              done_today: int) -> tuple[str, InlineKeyboardMarkup, int]:
    pages = max(1, -(-len(tasks) // PAGE_SIZE))
    page = min(max(page, 0), pages - 1)
    shown = tasks[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

    if tasks:
        lines = [f"📋 <b>Da fare</b> · {len(tasks)}"]
        if done_today:
            lines.append(f"✅ {done_today} {'fatta' if done_today == 1 else 'fatte'} oggi")
        lines += ["", "Tocca un'attività per i dettagli, ☑️ quando è fatta."]
    else:
        lines = ["📋 <b>Niente da fare</b> 🎉"]
        if done_today:
            lines.append(f"✅ {done_today} {'fatta' if done_today == 1 else 'fatte'} oggi")
        lines += ["", "Scrivimi o mandami un vocale per aggiungere qualcosa."]

    rows = []
    for t in shown:
        age = fmt_age(t.created_at, now)
        label = _short(t.title) + _icons(t) + (f" · {age}" if age else "")
        rows.append([B(label, callback_data=f"O:{t.id}"), B("☑️", callback_data=f"D:{t.id}:{page}")])
    if pages > 1:
        rows.append([
            B("◀️", callback_data=f"L:{page - 1}") if page > 0 else B(" ", callback_data="noop"),
            B(f"{page + 1}/{pages}", callback_data="noop"),
            B("▶️", callback_data=f"L:{page + 1}") if page < pages - 1 else B(" ", callback_data="noop"),
        ])
    if undo:
        rows.append([B(f"↩️ Annulla «{_short(undo.title, 28)}»", callback_data=f"U:{undo.id}:{page}")])
    rows.append([B("✅ Fatte", callback_data="F"), B("🔄 Aggiorna", callback_data=f"L:{page}")])
    return "\n".join(lines), InlineKeyboardMarkup(rows), page


# ------------------------------------------------------------------ task detail


def task_detail(t: Task, tz: ZoneInfo, now: datetime) -> tuple[str, InlineKeyboardMarkup]:
    lines = [f"📝 <b>{escape(t.title)}</b>"]
    if t.notes:
        lines += ["", escape(t.notes)]
    lines.append("")
    meta = f"Aggiunta da {escape(t.author_name or '?')} · {fmt_when(t.created_at, tz, now)}"
    if t.from_voice:
        meta += " · 🎙 dettata"
    lines.append(f"<i>{meta}</i>")
    if t.status == "done" and t.done_at:
        lines.append(f"✅ <b>Fatta</b> da {escape(t.done_by_name or '?')} · {fmt_when(t.done_at, tz, now)}")

    rows = []
    if t.status == "open":
        rows.append([B("☑️ Fatta", callback_data=f"DD:{t.id}")])
    elif t.status == "done":
        rows.append([B("↩️ Riapri", callback_data=f"RO:{t.id}")])
    media = []
    if t.photo_count:
        media.append(B(f"📷 Foto ({t.photo_count})" if t.photo_count > 1 else "📷 Foto", callback_data=f"P:{t.id}:0"))
    if t.voice_file_id:
        media.append(B("🔊 Ascolta", callback_data=f"A:{t.id}"))
    if media:
        rows.append(media)
    if t.status == "open":
        rows.append([B("➕ Dettagli", callback_data=f"E:{t.id}"), B("🗑 Elimina", callback_data=f"R:{t.id}")])
    rows.append([B("⬅️ Torna alla lista", callback_data="L:0")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def confirm_delete(t: Task) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[B("🗑 Sì, elimina", callback_data=f"RY:{t.id}"),
                                  B("No", callback_data=f"O:{t.id}")]])


def photo_buttons(t: Task, idx: int, total: int) -> InlineKeyboardMarkup:
    rows = []
    if total > 1:
        rows.append([
            B("◀️", callback_data=f"P:{t.id}:{(idx - 1) % total}"),
            B(f"{idx + 1}/{total}", callback_data="noop"),
            B("▶️", callback_data=f"P:{t.id}:{(idx + 1) % total}"),
        ])
    bottom = [B("✖ Chiudi", callback_data="X")]
    if t.status == "open":
        bottom.insert(0, B("☑️ Fatta", callback_data=f"PD:{t.id}"))
    rows.append(bottom)
    return InlineKeyboardMarkup(rows)


def close_button() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[B("✖ Chiudi", callback_data="X")]])


# -------------------------------------------------------------------- done view

DONE_PERIODS = [(1, "Oggi"), (7, "7 giorni"), (30, "30 giorni")]
MAX_TEXT = 3800


def done_view(done: list[Task], open_count: int, days: int, tz: ZoneInfo,
              now: datetime) -> tuple[str, InlineKeyboardMarkup]:
    period = "oggi" if days == 1 else f"negli ultimi {days} giorni"
    lines = [f"✅ <b>Fatte {period}</b> · {len(done)}", f"📋 Ancora da fare: {open_count}", ""]
    if not done:
        lines.append("<i>Ancora niente.</i>")
    today = now.astimezone(tz).date()
    length = sum(len(x) for x in lines)
    shown = 0
    for day, items in groupby(done, key=lambda t: t.done_at.astimezone(tz).date()):
        block = [f"<b>{fmt_day_heading(day, today)}</b>"]
        for t in items:
            block.append(f"☑️ {t.done_at.astimezone(tz):%H:%M} {escape(t.title)} — <i>{escape(t.done_by_name or '?')}</i>")
        block.append("")
        size = sum(len(x) + 1 for x in block)
        if length + size > MAX_TEXT:
            break
        lines += block
        length += size
        shown += len(block) - 2
    if shown < len(done):
        lines.append(f"<i>…e altre {len(done) - shown}</i>")

    period_row = [B(("• " if d == days else "") + label, callback_data=f"F:{d}") for d, label in DONE_PERIODS]
    if days not in [d for d, _ in DONE_PERIODS]:
        period_row.append(B(f"• {days} giorni", callback_data=f"F:{days}"))
    kb = InlineKeyboardMarkup([period_row, [B("📋 Da fare", callback_data="L:0")]])
    return "\n".join(lines).rstrip(), kb


def since_for(days: int, tz: ZoneInfo, now: datetime) -> datetime:
    """Start of the period: midnight (local) of (days - 1) days ago."""
    start = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    return start - timedelta(days=days - 1)


# --------------------------------------------------------------- confirmations


def added(tasks: list[Task], transcript: str | None = None) -> tuple[str, InlineKeyboardMarkup]:
    if len(tasks) == 1:
        t = tasks[0]
        text = f"➕ Aggiunta: <b>{escape(t.title)}</b>"
        if transcript:
            text += f"\n\n🎙 <i>«{escape(_short(transcript, 600))}»</i>"
        elif t.title in PLACEHOLDERS:
            text += "\nTocca ➕ Dettagli per descriverla."
        kb = InlineKeyboardMarkup([[B("↩️ Annulla", callback_data=f"UA:{t.id}"),
                                    B("➕ Dettagli", callback_data=f"E:{t.id}")]])
        return text, kb
    text = f"➕ Aggiunte {len(tasks)} attività:\n" + "\n".join(f"• {escape(t.title)}" for t in tasks)
    ids = ",".join(str(t.id) for t in tasks)
    kb = InlineKeyboardMarkup([[B("↩️ Annulla tutte", callback_data=f"UA:{ids}")]]) if len(ids) <= 58 else None
    return text, kb


def details_prompt(t: Task) -> tuple[str, InlineKeyboardMarkup]:
    return (f"✏️ Aggiungi dettagli a <b>{escape(t.title)}</b>: scrivi, manda foto o un vocale.",
            InlineKeyboardMarkup([[B("✔️ Fine", callback_data="N")]]))


# ------------------------------------------------------------------- users


def access_request(u: User) -> tuple[str, InlineKeyboardMarkup]:
    text = f"🔔 <b>{escape(u.label)}</b> vuole usare il bot (id <code>{u.id}</code>)."
    # ":n" = pressed on a request notification (the message becomes the verdict)
    return text, InlineKeyboardMarkup([[B("✅ Abilita", callback_data=f"UOK:{u.id}:n"),
                                        B("🚫 Rifiuta", callback_data=f"UNO:{u.id}:n")]])


def users_view(users: list[User], me: int, cfg) -> tuple[str, InlineKeyboardMarkup]:
    lines = ["👥 <b>Utenti</b>", ""]
    rows = []
    fixed = set(cfg.admin_ids) | set(cfg.allowed_users)
    if fixed:
        lines.append(f"<i>Fissi nel .env: {', '.join(str(i) for i in sorted(fixed))}</i>")
    icons = {"allowed": "✅", "pending": "⏳", "blocked": "🚫"}
    for u in users:
        admin = u.is_admin or u.id in cfg.admin_ids
        lines.append(f"{icons.get(u.status, '')} {escape(u.label)}" + (" 👑" if admin else "")
                     + (" <i>(tu)</i>" if u.id == me else ""))
        if u.id == me or u.id in cfg.admin_ids:
            continue
        name = _short(u.name or str(u.id), 18)
        if u.status == "pending":
            rows.append([B(f"✅ {name}", callback_data=f"UOK:{u.id}"), B("🚫", callback_data=f"UNO:{u.id}")])
        elif u.status == "allowed":
            rows.append([B(f"🚫 Togli {name}", callback_data=f"UNO:{u.id}"),
                         B("👑 Admin" if not u.is_admin else "👑 Non admin", callback_data=f"UADM:{u.id}")])
        else:
            rows.append([B(f"✅ Riabilita {name}", callback_data=f"UOK:{u.id}")])
    if not users:
        lines.append("<i>Nessuno ancora.</i>")
    lines += ["", "Chi scrive al bot per la prima volta ti manda una richiesta da approvare."]
    return "\n".join(lines), InlineKeyboardMarkup(rows)


HELP = f"""<b>Come funziona</b>

<b>Aggiungere</b>: scrivimi una cosa da fare, nient'altro.
• La prima riga è il titolo, le altre i dettagli.
• Un elenco puntato (<code>- lampadina bagno</code> a capo <code>- chiamare idraulico</code>) diventa un'attività per riga.
• Una <b>foto</b> con didascalia diventa un'attività con foto; una foto senza didascalia subito dopo un'attività si aggiunge a quella.
• Un <b>vocale</b> viene trascritto e diventa un'attività.

<b>Vedere</b>: usa i bottoni in basso.
• <b>{BTN_TODO}</b>: cosa c'è da fare. Tocca un'attività per dettagli e foto, ☑️ quando è fatta (con ↩️ per annullare).
• <b>{BTN_DONE}</b>: cosa è stato fatto, da chi e quando, negli ultimi giorni.

Non ti scrivo mai di mia iniziativa: le liste le vedi solo quando le chiedi."""
