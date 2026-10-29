"""Turning what a user writes (or says) into tasks, and formatting dates in Italian."""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

TITLE_MAX = 80

_BULLET = re.compile(r"^\s*(?:[-•*·▪–—]|\d{1,2}[.)]|☐|\[ \])\s+")


def _clean(s: str) -> str:
    s = re.sub(r"[ \t]+", " ", s).strip()
    return s[:1].upper() + s[1:] if s else s


def split_title(text: str) -> tuple[str, str]:
    """First line (or first sentence, if the text is one long line) is the title, the rest notes."""
    text = text.strip()
    if not text:
        return "", ""
    first, _, rest = text.partition("\n")
    first, rest = first.strip(), rest.strip()
    if len(first) > TITLE_MAX:
        m = re.match(r"^(.{10,%d}?[.!?;])\s+(.+)$" % TITLE_MAX, first, re.DOTALL)
        if m:
            first, rest = m.group(1), f"{m.group(2)}\n{rest}".strip()
        else:  # cut at a word boundary
            cut = first.rfind(" ", 0, TITLE_MAX)
            cut = cut if cut > 20 else TITLE_MAX
            first, rest = first[:cut] + "…", (first[cut:].strip() + "\n" + rest).strip()
    first = first.rstrip(".")
    return _clean(first), rest


HIGH, NORMAL, LOW = 1, 2, 3

_SEP = r"(?:\s*[:,.\-–—]\s*|\s+|$)"
_PRIO_START = [
    (HIGH, re.compile(r"^\s*!+\s*")),
    (HIGH, re.compile(rf"^\s*(?:urgente|urgentissimo|importante|subito|priorit[aà] alta|alta priorit[aà]){_SEP}",
                      re.IGNORECASE)),
    (LOW, re.compile(rf"^\s*(?:con calma|quando capita|quando puoi|non urgente|priorit[aà] bassa|"
                     rf"bassa priorit[aà]){_SEP}", re.IGNORECASE)),
]
_PRIO_ANYWHERE = [  # for dictation, where people say "...è urgente" at the end
    (LOW, re.compile(r"\b(?:non (?:è )?urgente|con calma|quando capita|priorit[aà] bassa)\b", re.IGNORECASE)),
    (HIGH, re.compile(r"\b(?:urgente|urgentissimo|priorit[aà] alta)\b", re.IGNORECASE)),
]


def take_priority(text: str) -> tuple[int | None, str]:
    """A leading "!" or "urgente:" means high, "con calma:" low. Returns (priority or None, rest)."""
    for prio, rx in _PRIO_START:
        m = rx.match(text)
        if m and text[m.end():].strip():
            return prio, text[m.end():]
    return None, text


def spoken_priority(text: str) -> int | None:
    for prio, rx in _PRIO_ANYWHERE:
        if rx.search(text):
            return prio
    return None


def parse_tasks(text: str) -> list[tuple[str, str, int]]:
    """A message is one task (first line = title, other lines = details) -> (title, notes, priority).

    Exception: a bulleted/numbered list ("- buy bulbs\\n- call plumber") becomes one task per item.
    A priority word at the start ("urgente: ...", "! ...", "con calma: ...") applies to the whole
    message; one at the start of a list item applies to that item.
    """
    msg_prio, text = take_priority(text.strip())
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    if not lines:
        return []
    bulleted = [ln for ln in lines if _BULLET.match(ln)]
    if len(bulleted) >= 2 and len(bulleted) >= len(lines) - 1:
        # Optional heading line ("Casa:") followed by a list: the heading is dropped.
        out = []
        for ln in bulleted:
            item_prio, item = take_priority(_BULLET.sub("", ln))
            if item.strip():
                out.append((*split_title(item), item_prio or msg_prio or NORMAL))
        return out
    return [(*split_title(_BULLET.sub("", text.strip(), count=1)), msg_prio or NORMAL)]


def clean_transcript(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text[:1].upper() + text[1:] if text else text


# ------------------------------------------------------------------- dates

GIORNI = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
GIORNI_BREVI = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"]
MESI = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio",
        "agosto", "settembre", "ottobre", "novembre", "dicembre"]
MESI_BREVI = ["gen", "feb", "mar", "apr", "mag", "giu", "lug", "ago", "set", "ott", "nov", "dic"]


def local(dt: datetime, tz: ZoneInfo) -> datetime:
    return dt.astimezone(tz)


def fmt_when(dt: datetime, tz: ZoneInfo, now: datetime) -> str:
    """'oggi 14:32', 'ieri 09:10', 'lun 26 ott 18:00'."""
    d, n = dt.astimezone(tz), now.astimezone(tz)
    hm = d.strftime("%H:%M")
    delta = (n.date() - d.date()).days
    if delta == 0:
        return f"oggi {hm}"
    if delta == 1:
        return f"ieri {hm}"
    s = f"{GIORNI_BREVI[d.weekday()]} {d.day} {MESI_BREVI[d.month - 1]}"
    if d.year != n.year:
        s += f" {d.year}"
    return f"{s} {hm}"


def fmt_day_heading(day, today) -> str:
    delta = (today - day).days
    if delta == 0:
        return "Oggi"
    if delta == 1:
        return "Ieri"
    return f"{GIORNI[day.weekday()].capitalize()} {day.day} {MESI[day.month - 1]}"


def fmt_age(created: datetime, now: datetime) -> str:
    """Short 'waiting since' label: '3g', '2 sett'. Empty when recent."""
    days = (now - created).days
    if days < 1:
        return ""
    if days < 14:
        return f"{days}g"
    return f"{days // 7} sett"
