"""End-to-end flows against a fake Telegram API (no network, no token)."""

import json
from datetime import timedelta
from itertools import count
from pathlib import Path

import pytest
from telegram import Update
from telegram.request import BaseRequest

from taskbot.bot import create_app
from taskbot.config import Config
from taskbot.db import DB, now_utc

ANNA, BRUNO, STRANGER = 11, 22, 99
BOT_ID = 777
NAMES = {ANNA: "Anna", BRUNO: "Bruno", STRANGER: "Tizio"}


class FakeTelegram(BaseRequest):
    def __init__(self):
        self.calls = []
        self._ids = count(1000)

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    @property
    def read_timeout(self):
        return 5

    async def do_request(self, url, method, request_data=None, **kw):
        name = url.rsplit("/", 1)[-1]
        p = request_data.parameters if request_data else {}
        if "/file/" in url:  # file download
            return 200, b"OggS-fake-audio"
        self.calls.append((name, p))
        chat_id = int(p.get("chat_id", 0) or 0)
        msg = {"message_id": next(self._ids), "date": 0, "chat": {"id": chat_id, "type": "private"},
               "from": {"id": BOT_ID, "is_bot": True, "first_name": "Bot"}}
        if name == "getMe":
            r = {"id": BOT_ID, "is_bot": True, "first_name": "Bot", "username": "task_bot"}
        elif name == "getFile":
            r = {"file_id": p["file_id"], "file_unique_id": "u", "file_path": "voice/file_1.oga"}
        elif name in ("sendMessage", "editMessageText"):
            r = {**msg, "text": p.get("text", "")}
        elif name in ("sendPhoto", "editMessageMedia"):
            r = {**msg, "photo": [{"file_id": "x", "file_unique_id": "x", "width": 1, "height": 1}]}
        elif name == "sendVoice":
            r = {**msg, "voice": {"file_id": "v", "file_unique_id": "v", "duration": 1}}
        else:
            r = True
        return 200, json.dumps({"ok": True, "result": r}).encode()

    def last(self, name):
        found = [p for n, p in self.calls if n == name]
        return found[-1] if found else None

    def all(self, name):
        return [p for n, p in self.calls if n == name]


class FakeTranscriber:
    def __init__(self):
        self.text = ""

    async def transcribe(self, path: Path) -> str:
        assert path.read_bytes() == b"OggS-fake-audio"
        return self.text


_ids = count(1)


def message(text=None, user=ANNA, photo=None, caption=None, voice=None, media_group=None):
    m = {"message_id": next(_ids), "date": 0, "chat": {"id": user, "type": "private"},
         "from": {"id": user, "is_bot": False, "first_name": NAMES[user]}}
    if text is not None:
        m["text"] = text
        if text.startswith("/"):
            m["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    if photo:
        m["photo"] = [{"file_id": photo, "file_unique_id": photo, "width": 1, "height": 1}]
        if caption:
            m["caption"] = caption
        if media_group:
            m["media_group_id"] = media_group
    if voice:
        m["voice"] = {"file_id": voice, "file_unique_id": voice, "duration": 3}
    return {"update_id": next(_ids), "message": m}


def tap(data, message_id, user=ANNA, photo=False):
    msg = {"message_id": message_id, "date": 0, "chat": {"id": user, "type": "private"},
           "from": {"id": BOT_ID, "is_bot": True, "first_name": "Bot"}}
    if photo:
        msg["photo"] = [{"file_id": "x", "file_unique_id": "x", "width": 1, "height": 1}]
    else:
        msg["text"] = "x"
    return {"update_id": next(_ids), "callback_query": {
        "id": str(next(_ids)), "chat_instance": "ci", "data": data, "message": msg,
        "from": {"id": user, "is_bot": False, "first_name": NAMES[user]}}}


def buttons(params):
    return [b for row in params["reply_markup"]["inline_keyboard"] for b in row]


def callbacks(params):
    return [b["callback_data"] for b in buttons(params)]


@pytest.fixture
async def env(tmp_path):
    fake = FakeTelegram()
    db = DB(tmp_path / "t.sqlite3")
    tr = FakeTranscriber()
    cfg = Config(token="1:FAKE", admin_ids=frozenset({ANNA}), allowed_users=frozenset({BRUNO}))
    app = create_app(cfg, db, transcriber=tr)
    app.bot._request = (fake, fake)
    await app.initialize()

    async def send(u):
        await app.process_update(Update.de_json(u, app.bot))

    yield send, fake, db, tr
    await app.shutdown()


async def open_list(send, fake, db, user=ANNA):
    await send(message("📋 Da fare", user=user))
    return db.view(user).message_id, fake.last("sendMessage")


async def test_add_view_done_undo(env):
    send, fake, db, _ = env
    await send(message("Cambiare lampadina bagno"))
    assert "Aggiunta: 🟡 <b>Cambiare lampadina bagno</b>" in fake.last("sendMessage")["text"]
    await send(message("- chiamare idraulico\n- pagare bolletta luce", user=BRUNO))
    assert "Aggiunte 2 attività" in fake.last("sendMessage")["text"]

    menu_msg_count = len(fake.all("deleteMessage"))
    view_id, view = await open_list(send, fake, db)
    assert len(fake.all("deleteMessage")) == menu_msg_count + 1  # the "📋 Da fare" text is tidied away
    assert "Da fare</b> · 3" in view["text"]
    lamp, idraulico, bolletta = db.open_tasks()
    assert f"D:{lamp.id}:0" in callbacks(view)

    await send(tap(f"D:{lamp.id}:0", view_id))
    edited = fake.last("editMessageText")
    assert "Da fare</b> · 2" in edited["text"] and "1 fatta oggi" in edited["text"]
    assert any(b["text"].startswith("↩️ Annulla «Cambiare") for b in buttons(edited))
    t = db.task(lamp.id)
    assert t.done_by_name == "Anna" and (now_utc() - t.done_at) < timedelta(seconds=5)

    # Bruno doesn't see Anna's undo button, but Anna can undo
    await send(tap(f"U:{lamp.id}:0", view_id))
    assert db.task(lamp.id).status == "open"


async def test_live_refresh_is_silent_edit(env):
    send, fake, db, _ = env
    await send(message("Tende"))
    bruno_view, _ = await open_list(send, fake, db, user=BRUNO)
    fake.calls.clear()
    await send(message("Mensola"))  # Anna adds a task
    edits = [p for p in fake.all("editMessageText") if int(p["chat_id"]) == BRUNO]
    assert edits and "Mensola" in json.dumps(edits[-1]["reply_markup"], ensure_ascii=False)
    assert not [p for p in fake.all("sendMessage") if int(p["chat_id"]) == BRUNO]  # nothing new pushed


async def test_detail_photos_and_done_from_photo(env):
    send, fake, db, _ = env
    await send(message(photo="P1", caption="Mensola sala\nquella vicino alla finestra"))
    t = db.open_tasks()[0]
    assert t.title == "Mensola sala" and t.notes == "quella vicino alla finestra" and t.photo_count == 1

    await send(message(photo="P2"))  # no caption, right after: attaches to the same task
    assert "Foto aggiunta a <b>Mensola sala</b>" in fake.last("sendMessage")["text"]
    assert db.task(t.id).photo_count == 2

    view_id, _ = await open_list(send, fake, db)
    await send(tap(f"O:{t.id}", view_id))
    detail = fake.last("editMessageText")
    assert "📝 <b>Mensola sala</b>" in detail["text"] and "Aggiunta da Anna" in detail["text"]
    assert f"P:{t.id}:0" in callbacks(detail)
    assert db.view(ANNA).kind == "detail"

    await send(tap(f"P:{t.id}:0", view_id))
    photo = fake.last("sendPhoto")
    assert photo["photo"] == "P1" and f"P:{t.id}:1" in callbacks(photo)
    await send(tap(f"P:{t.id}:1", 5000, photo=True))
    assert "P2" in json.dumps(fake.last("editMessageMedia"))

    await send(tap(f"PD:{t.id}", 5000, photo=True))
    assert fake.last("deleteMessage")["message_id"] == 5000
    assert db.task(t.id).status == "done"
    assert db.view(ANNA).kind == "list"  # the view went back to the list


async def test_album_is_one_task(env):
    send, fake, db, _ = env
    await send(message(photo="A1", caption="Rubinetto cucina", media_group="G1"))
    await send(message(photo="A2", media_group="G1"))
    await send(message(photo="A3", media_group="G1"))
    tasks = db.open_tasks()
    assert len(tasks) == 1 and tasks[0].photo_count == 3
    assert len([p for p in fake.all("sendMessage") if "Aggiunta" in p["text"]]) == 1


async def test_photo_alone_then_details(env):
    send, fake, db, _ = env
    await send(message(photo="Z1"))
    t = db.open_tasks()[0]
    assert t.title == "📷 Foto"
    await send(tap(f"E:{t.id}", 1))
    assert "Aggiungi dettagli" in fake.last("sendMessage")["text"]
    await send(message("Riparare questa sedia\nla gamba dondola"))
    t = db.task(t.id)
    assert t.title == "Riparare questa sedia" and t.notes == "la gamba dondola"
    assert len(db.open_tasks()) == 1  # details, not a new task
    await send(tap("N", 2))
    await send(message("Altra cosa"))
    assert len(db.open_tasks()) == 2


async def test_voice_note(env):
    send, fake, db, tr = env
    tr.text = "comprare il filtro per la cappa della cucina"
    await send(message(voice="V1"))
    t = db.open_tasks()[0]
    assert t.title == "Comprare il filtro per la cappa della cucina"
    assert t.voice_file_id == "V1" and t.from_voice
    final = fake.last("editMessageText")  # "Trascrivo…" becomes the confirmation
    assert "Aggiunta" in final["text"] and "🎙" in final["text"]

    view_id, _ = await open_list(send, fake, db)
    await send(tap(f"O:{t.id}", view_id))
    assert f"A:{t.id}" in callbacks(fake.last("editMessageText"))
    await send(tap(f"A:{t.id}", view_id))
    assert fake.last("sendVoice")["voice"] == "V1"


async def test_voice_not_understood_is_kept(env):
    send, fake, db, tr = env
    tr.text = ""
    await send(message(voice="V2"))
    t = db.open_tasks()[0]
    assert t.title == "🎙️ Nota vocale" and t.voice_file_id == "V2"
    assert "Non sono riuscito" in fake.last("editMessageText")["text"]


async def test_undo_added(env):
    send, fake, db, _ = env
    await send(message("- uno\n- due"))
    ua = next(c for c in callbacks(fake.last("sendMessage")) if c.startswith("UA:"))
    await send(tap(ua, 1))
    assert db.open_tasks() == []


async def test_done_view(env):
    send, fake, db, _ = env
    a = db.add_task("Vecchia")
    b = db.add_task("Recente")
    db.add_task("Aperta")
    db.mark_done(a.id, BRUNO, "Bruno", at=now_utc() - timedelta(days=12))
    db.mark_done(b.id, BRUNO, "Bruno")
    await send(message("✅ Fatte"))
    v = fake.last("sendMessage")
    assert "Fatte negli ultimi 7 giorni</b> · 1" in v["text"]
    assert "Ancora da fare: 1" in v["text"] and "Recente — <i>Bruno</i>" in v["text"]
    assert "<b>Oggi</b>" in v["text"]
    await send(tap("F:30", db.view(ANNA).message_id))
    assert "ultimi 30 giorni</b> · 2" in fake.last("editMessageText")["text"]


async def test_delete_with_confirm(env):
    send, fake, db, _ = env
    t = db.add_task("Sbagliata")
    view_id, _ = await open_list(send, fake, db)
    await send(tap(f"R:{t.id}", view_id))
    assert f"RY:{t.id}" in callbacks(fake.last("editMessageReplyMarkup"))
    await send(tap(f"RY:{t.id}", view_id))
    assert db.open_tasks() == []


async def test_stranger_requests_access_and_admin_approves(env):
    send, fake, db, _ = env
    await send(message("ciao", user=STRANGER))
    assert any("Ho chiesto l'accesso" in p["text"] for p in fake.all("sendMessage") if int(p["chat_id"]) == STRANGER)
    assert db.open_tasks() == []  # the message did not become a task
    request = next(p for p in fake.all("sendMessage") if int(p["chat_id"]) == ANNA)
    assert "Tizio" in request["text"] and callbacks(request) == [f"UOK:{STRANGER}:n", f"UNO:{STRANGER}:n"]

    # writing again doesn't spam the admin
    fake.calls.clear()
    await send(message("ci sei?", user=STRANGER))
    assert "in attesa" in fake.last("sendMessage")["text"]
    assert not [p for p in fake.all("sendMessage") if int(p["chat_id"]) == ANNA]

    # a stranger can't press buttons
    t = db.add_task("X")
    await send(tap(f"D:{t.id}:0", 1, user=STRANGER))
    assert db.task(t.id).status == "open"
    # a non-admin can't approve
    await send(tap(f"UOK:{STRANGER}:n", 1, user=BRUNO))
    assert db.user(STRANGER).status == "pending"

    # the admin approves: the request is updated and the new user is welcomed
    fake.calls.clear()
    await send(tap(f"UOK:{STRANGER}:n", 4000))
    assert "abilitato da Anna" in fake.last("editMessageText")["text"]
    welcome = next(p for p in fake.all("sendMessage") if int(p["chat_id"]) == STRANGER)
    assert "Sei stato abilitato" in welcome["text"] and "keyboard" in welcome["reply_markup"]
    await send(message("Pulire grondaie", user=STRANGER))
    assert [x.title for x in db.open_tasks()] == ["X", "Pulire grondaie"]


async def test_rejected_user_stays_out(env):
    send, fake, db, _ = env
    await send(message("ciao", user=STRANGER))
    await send(tap(f"UNO:{STRANGER}:n", 4000))
    assert db.user(STRANGER).status == "blocked"
    fake.calls.clear()
    await send(message("ciao", user=STRANGER))
    assert "Non sei abilitato" in fake.last("sendMessage")["text"]
    assert not [p for p in fake.all("sendMessage") if int(p["chat_id"]) == ANNA]


async def test_users_command_admin_only(env):
    send, fake, db, _ = env
    await send(message("ciao", user=STRANGER))
    await send(message("/utenti", user=BRUNO))
    assert "Solo un amministratore" in fake.last("sendMessage")["text"]
    await send(message("/utenti"))
    view = fake.last("sendMessage")
    assert "⏳ Tizio" in view["text"] and f"UOK:{STRANGER}" in callbacks(view)
    await send(tap(f"UOK:{STRANGER}", 4001))
    assert "✅ Tizio" in fake.last("editMessageText")["text"]
    assert f"UADM:{STRANGER}" in callbacks(fake.last("editMessageText"))


async def test_paging(env):
    send, fake, db, _ = env
    for i in range(11):
        db.add_task(f"Task {i}")
    view_id, view = await open_list(send, fake, db)
    assert "L:1" in callbacks(view) and len([c for c in callbacks(view) if c.startswith("O:")]) == 8
    await send(tap("L:1", view_id))
    assert len([c for c in callbacks(fake.last("editMessageText")) if c.startswith("O:")]) == 3


async def test_first_user_becomes_admin(tmp_path):
    fake = FakeTelegram()
    db = DB(tmp_path / "t.sqlite3")
    app = create_app(Config(token="1:FAKE"), db, transcriber=FakeTranscriber())
    app.bot._request = (fake, fake)
    await app.initialize()
    try:
        await app.process_update(Update.de_json(message("/start", user=BRUNO), app.bot))
        assert "ora sei <b>amministratore</b>" in fake.last("sendMessage")["text"]
        assert db.user(BRUNO).is_admin and db.user(BRUNO).status == "allowed"
        await app.process_update(Update.de_json(message("ciao", user=STRANGER), app.bot))
        assert any(int(p["chat_id"]) == BRUNO and "vuole usare il bot" in p["text"]
                   for p in fake.all("sendMessage"))
        assert not db.user(STRANGER).is_admin
    finally:
        await app.shutdown()


def test_cli(tmp_path, monkeypatch, capsys):
    import sys

    from taskbot.__main__ import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "cli.sqlite3"))
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    for argv in (["users", "add", "123", "Mario", "--admin"], ["users", "add", "456"],
                 ["users", "remove", "456"], ["users", "list"]):
        monkeypatch.setattr(sys, "argv", ["taskbot", *argv])
        main()
    out = capsys.readouterr().out
    assert "Mario (123) allowed as admin" in out and "removed" in out
    db = DB(tmp_path / "cli.sqlite3")
    assert db.user(123).is_admin and db.user(456).status == "blocked"


async def test_priority_buttons_and_order(env):
    send, fake, db, _ = env
    await send(message("Sistemare cantina"))
    await send(message("Riparare tapparella"))
    confirm = fake.last("sendMessage")
    tapparella = db.open_tasks()[-1]
    assert callbacks(confirm)[:3] == [f"PR:{tapparella.id}:1:c", f"PR:{tapparella.id}:2:c", f"PR:{tapparella.id}:3:c"]
    assert buttons(confirm)[1]["text"] == "✓ 🟡 Normale"

    # one tap on the confirmation makes it urgent; the confirmation shows it
    await send(tap(f"PR:{tapparella.id}:1:c", 3000))
    assert db.task(tapparella.id).priority == 1
    edited = fake.last("editMessageText")
    assert "🔴 <b>Riparare tapparella</b>" in edited["text"] and buttons(edited)[0]["text"] == "✓ 🔴 Alta"

    await send(message("con calma: riordinare garage"))
    await send(message("! perdita lavandino"))

    view_id, view = await open_list(send, fake, db)
    rows = [b["text"] for b in buttons(view) if b["callback_data"].startswith("O:")]
    assert rows == ["🔴 Riparare tapparella", "🔴 Perdita lavandino", "🟡 Sistemare cantina", "⚪ Riordinare garage"]
    assert "🔴 2 · 🟡 1 · ⚪ 1" in view["text"]

    # change from the detail view
    cantina = next(t for t in db.open_tasks() if t.title == "Sistemare cantina")
    await send(tap(f"O:{cantina.id}", view_id))
    detail = fake.last("editMessageText")
    assert "🟡 Priorità normale" in detail["text"] and f"PR:{cantina.id}:3" in callbacks(detail)
    await send(tap(f"PR:{cantina.id}:3", view_id))
    assert db.task(cantina.id).priority == 3
    assert "⚪ Priorità bassa" in fake.last("editMessageText")["text"]


async def test_voice_urgent(env):
    send, fake, db, tr = env
    tr.text = "chiamare il tecnico della caldaia, è urgente"
    await send(message(voice="V9"))
    assert db.open_tasks()[0].priority == 1


def test_old_database_gets_priority_column(tmp_path):
    import sqlite3

    path = tmp_path / "old.sqlite3"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE tasks (id INTEGER PRIMARY KEY, title TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '',
            author_id INTEGER, author_name TEXT, created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open', done_at TEXT, done_by_id INTEGER, done_by_name TEXT,
            voice_file_id TEXT, from_voice INTEGER NOT NULL DEFAULT 0);
        INSERT INTO tasks(title, created_at) VALUES ('Vecchia', '2026-10-01T10:00:00+00:00');
    """)
    old.commit()
    old.close()
    db = DB(path)
    t = db.open_tasks()[0]
    assert t.title == "Vecchia" and t.priority == 2
    db.set_priority(t.id, 1)
    assert db.task(t.id).priority == 1
