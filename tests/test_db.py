from datetime import datetime, timedelta, timezone

import pytest

from taskbot.db import DB, now_utc


@pytest.fixture
def db(tmp_path):
    return DB(tmp_path / "t.sqlite3")


def test_add_done_reopen(db):
    a = db.add_task("Lampadina", author_id=1, author_name="Anna")
    b = db.add_task("Idraulico", "mattina", 2, "Bruno")
    assert [t.title for t in db.open_tasks()] == ["Lampadina", "Idraulico"]

    done = db.mark_done(a.id, 2, "Bruno")
    assert done.status == "done" and done.done_by_name == "Bruno" and done.done_at is not None
    assert db.mark_done(a.id, 1, "Anna") is None  # already done
    assert [t.title for t in db.open_tasks()] == ["Idraulico"]
    assert db.last_done_by(2, timedelta(minutes=5)).id == a.id
    assert db.last_done_by(1, timedelta(minutes=5)) is None

    assert db.reopen(a.id).status == "open"
    assert db.reopen(a.id) is None
    assert db.task(b.id).notes == "mattina"


def test_done_since(db):
    now = now_utc()
    old = db.add_task("Vecchia")
    new = db.add_task("Nuova")
    db.mark_done(old.id, 1, "A", at=now - timedelta(days=10))
    db.mark_done(new.id, 1, "A", at=now - timedelta(hours=1))
    assert [t.title for t in db.done_since(now - timedelta(days=7))] == ["Nuova"]
    assert [t.title for t in db.done_since(now - timedelta(days=30))] == ["Nuova", "Vecchia"]


def test_photos_and_details(db):
    t = db.add_task("Mensola")
    db.add_photo(t.id, "F1")
    pid = db.add_photo(t.id, "F2")
    assert db.task(t.id).photo_count == 2
    db.remove_photo(pid)
    assert db.photos(t.id) == ["F1"]
    db.update_text(t.id, append_notes="in sala")
    db.update_text(t.id, append_notes="altezza 1,5 m")
    assert db.task(t.id).notes == "in sala\naltezza 1,5 m"


def test_delete_restore(db):
    t = db.add_task("X")
    db.delete(t.id)
    assert db.open_tasks() == []
    db.restore(t.id)
    assert len(db.open_tasks()) == 1


def test_last_added_by(db):
    db.add_task("Vecchia", author_id=1, at=datetime.now(timezone.utc) - timedelta(minutes=10))
    assert db.last_added_by(1, timedelta(minutes=3)) is None
    t = db.add_task("Nuova", author_id=1)
    assert db.last_added_by(1, timedelta(minutes=3)).id == t.id
    assert db.last_added_by(2, timedelta(minutes=3)) is None


def test_views(db):
    assert db.set_view(10, 100, "list") is None
    assert db.set_view(10, 101, "done") == 100
    assert db.set_view(10, 101, "list", 2) is None
    assert [(v.chat_id, v.page) for v in db.list_views()] == [(10, 2)]
