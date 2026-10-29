from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from taskbot.text import fmt_age, fmt_day_heading, fmt_when, parse_tasks, split_title

TZ = ZoneInfo("Europe/Rome")


def test_single_line():
    assert parse_tasks("cambiare lampadina bagno") == [("Cambiare lampadina bagno", "")]


def test_title_and_details():
    assert parse_tasks("Chiamare idraulico\nil numero è sul frigo\nmeglio la mattina") == [
        ("Chiamare idraulico", "il numero è sul frigo\nmeglio la mattina")]


def test_bulleted_list_is_many_tasks():
    assert [t for t, _ in parse_tasks("- lampadina bagno\n- chiamare idraulico\n• pagare bolletta")] == [
        "Lampadina bagno", "Chiamare idraulico", "Pagare bolletta"]


def test_list_with_heading():
    assert [t for t, _ in parse_tasks("Casa:\n1. tende\n2) mensola")] == ["Tende", "Mensola"]


def test_single_bullet_is_one_task():
    assert parse_tasks("- tende\nquelle della sala") == [("Tende", "quelle della sala")]


def test_long_dictation_split_at_sentence():
    title, notes = split_title(
        "Bisogna portare la macchina dal meccanico. Fa un rumore strano quando freno, "
        "e la spia dell'olio si accende ogni tanto, quindi meglio questa settimana.")
    assert title == "Bisogna portare la macchina dal meccanico"
    assert notes.startswith("Fa un rumore")


def test_long_line_without_sentence_cut_at_word():
    title, notes = split_title("parola " * 30)
    assert title.endswith("…") and len(title) <= 81 and notes


@pytest.mark.parametrize("dt,expected", [
    (datetime(2026, 10, 29, 14, 32, tzinfo=TZ), "oggi 14:32"),
    (datetime(2026, 10, 28, 9, 5, tzinfo=TZ), "ieri 09:05"),
    (datetime(2026, 10, 26, 18, 0, tzinfo=TZ), "lun 26 ott 18:00"),
])
def test_fmt_when(dt, expected):
    assert fmt_when(dt, TZ, datetime(2026, 10, 29, 19, 0, tzinfo=TZ)) == expected


def test_day_heading_and_age():
    today = date(2026, 10, 29)
    assert fmt_day_heading(today, today) == "Oggi"
    assert fmt_day_heading(date(2026, 10, 27), today) == "Martedì 27 ottobre"
    now = datetime(2026, 10, 29, tzinfo=TZ)
    assert fmt_age(datetime(2026, 10, 28, 23, tzinfo=TZ), now) == ""
    assert fmt_age(datetime(2026, 10, 26, tzinfo=TZ), now) == "3g"
    assert fmt_age(datetime(2026, 10, 1, tzinfo=TZ), now) == "4 sett"
