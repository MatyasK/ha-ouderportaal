"""Parsing the day rhythm HTML and photo cards."""

from __future__ import annotations

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from custom_components.ouderportaal.api import normalize_portal
from custom_components.ouderportaal.timeline import (
    Photo,
    Timeline,
    _extension,
    category_for_icon,
    merge_timelines,
    parse_day_rhythm,
    parse_timeline,
)

from .conftest import CHILD_ID

TZ = ZoneInfo("Europe/Amsterdam")
DAY = date(2026, 10, 7)


def test_parses_moments(timeline_cards) -> None:
    moments = parse_timeline(timeline_cards, TZ).moments

    assert [(m.category, m.title) for m in moments] == [
        ("bottle", "09:00 (Fles)voeding"),
        ("sleep", "10:10-10:48 Slapen"),
        ("bottle", "12:00 (Fles)voeding"),
    ]
    bottle, sleep, _ = moments
    assert bottle.day == DAY
    assert bottle.child_id == CHILD_ID
    assert bottle.start == datetime(2026, 10, 7, 9, 0, tzinfo=TZ)
    assert bottle.amount_ml == 130
    assert bottle.description == "130 cc (Fles)voeding"
    assert bottle.remarks == "20cc over"
    assert bottle.text == "09:00 (Fles)voeding - 130 cc (Fles)voeding - 20cc over"
    assert sleep.duration_minutes == 38
    assert sleep.amount_ml is None


def test_parses_photos_newest_first(timeline_cards) -> None:
    photos = parse_timeline(timeline_cards, TZ).photos

    assert [p.id for p in photos] == ["photo3", "photo2", "photo1"]
    assert photos[0].child_ids == (CHILD_ID,)
    assert photos[0].day == DAY
    assert photos[0].full_url.endswith("Signature=test")


def _sleep(title: str) -> str:
    return (
        "<table class='day-rythm-moment'><tr><td><div class='icon dagritme-slapen-bedje day-rythm-journal-icon'>"
        f"</div></td><td><div class='moment-title'><span class='moment-title-span'>{title}</span></div>"
        "<div class='moment-entry-description'></div><div class='moment-remarks'></div></td></tr></table>"
    )


def test_ongoing_sleep_keeps_its_key_when_it_ends() -> None:
    (ongoing,) = parse_day_rhythm(_sleep("10:10 Slapen"), CHILD_ID, DAY, TZ)
    (ended,) = parse_day_rhythm(_sleep("10:10 - 10:48 Slapen"), CHILD_ID, DAY, TZ)

    assert ongoing.end is None
    assert ongoing.key == ended.key
    assert ongoing.fingerprint != ended.fingerprint


def test_html_entities_and_untimed_titles() -> None:
    html = (
        "<table class='day-rythm-moment'><tr><td><div class='icon dagritme-appel day-rythm-journal-icon'></div></td>"
        "<td><div class='moment-title'><span class='moment-title-span'>Fruit &amp; koekje</span></div>"
        "<div class='moment-entry-description'>Alles   op</div><div class='moment-remarks'></div></td></tr></table>"
    )
    (moment,) = parse_day_rhythm(html, CHILD_ID, DAY, TZ)

    assert moment.category == "food"
    assert moment.start is None
    assert moment.label == "Fruit & koekje"
    assert moment.description == "Alles op"


def test_journal_note_becomes_untimed_moment_at_end_of_day(timeline_cards) -> None:
    timeline_cards[0]["journal"]["journalContent"] = "<p>Lekker gespeeld</p><p>Tot morgen!</p>"

    moments = parse_timeline(timeline_cards, TZ).moments

    assert moments[-1].category == "note"
    assert moments[-1].label == "Note from Juf"
    assert moments[-1].description == "Lekker gespeeld\nTot morgen!"


@pytest.mark.parametrize(
    ("icon", "category"),
    [
        ("flesje", "bottle"),
        ("slapen-wolk-zzz", "sleep"),
        ("luier", "diaper"),
        ("brood-drinken-licht", "food"),
        ("drinken-warm1", "drink"),
        ("medicatie-zwart-01", "medication"),
        ("buitensport", "activity"),
        ("", "activity"),
    ],
)
def test_category_for_icon(icon: str, category: str) -> None:
    assert category_for_icon(icon) == category


@pytest.mark.parametrize(
    "value",
    [
        "mijnopvang",
        " Mijnopvang ",
        "mijnopvang.ouderportaal.nl",
        "https://mijnopvang.ouderportaal.nl/parent/#/tabs/mychild",
    ],
)
def test_normalize_portal(value: str) -> None:
    assert normalize_portal(value) == "mijnopvang"


@pytest.mark.parametrize("value", ["", "https://", "not a portal", "evil.com/x"])
def test_normalize_portal_rejects_garbage(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_portal(value)


def _photo(photo_id: str = "abc123", url: str = "https://cdn/x/IMG-1.jpeg?Signature=s", day: date = DAY) -> Photo:
    return Photo(
        id=photo_id,
        child_ids=(CHILD_ID,),
        day=day,
        description="",
        media_type="photo",
        extension=_extension(url, "photo"),
        full_url=url,
    )


def test_photo_filename_uses_day_id_and_extension() -> None:
    assert _photo().filename == "2026-10-07/abc123.jpeg"


@pytest.mark.parametrize(
    ("photo_id", "url"),
    [
        ("../../etc/passwd", "https://cdn/x/IMG-1.jpeg"),
        ("abc123", "https://cdn/x/evil.jpeg%2F..%2F..%2Fx"),
        ("abc123", "https://cdn/x/no-extension"),
    ],
)
def test_photo_filename_cannot_escape_the_archive(photo_id: str, url: str) -> None:
    filename = _photo(photo_id, url).filename

    assert filename.startswith("2026-10-07/")
    assert ".." not in filename
    assert filename.count("/") == 1


def test_timeline_survives_storage_round_trip(timeline_cards) -> None:
    timeline = parse_timeline(timeline_cards, TZ)

    restored = Timeline.from_dict(json.loads(json.dumps(timeline.to_dict())))

    assert restored.moments == timeline.moments
    assert [p.filename for p in restored.photos] == [p.filename for p in timeline.photos]
    # Signed URLs expire, so they are not kept.
    assert all(p.full_url == "" for p in restored.photos)


def test_merge_keeps_history_and_prefers_fresh_entries(timeline_cards) -> None:
    yesterday = date(2026, 10, 6)
    old_photo = _photo("old", day=yesterday)
    history = Timeline(moments=[], photos=[old_photo, _photo("abc123")])
    fresh_photo = _photo("abc123", url="https://cdn/x/IMG-1.jpeg?Signature=new")
    fresh = Timeline(moments=parse_timeline(timeline_cards, TZ).moments, photos=[fresh_photo])

    merged = merge_timelines(history, fresh)

    assert [p.id for p in merged.photos] == ["abc123", "old"]
    assert merged.photos[0].full_url.endswith("Signature=new")
    assert len(merged.moments) == 3
    # Merging the same data again changes nothing.
    assert merge_timelines(merged, fresh).to_dict() == merged.to_dict()


def test_merge_leaves_out_entries_before_since(timeline_cards) -> None:
    fresh = parse_timeline(timeline_cards, TZ)

    merged = merge_timelines(Timeline([], []), fresh, since=date(2026, 10, 8))

    assert merged.moments == []
    assert merged.photos == []
