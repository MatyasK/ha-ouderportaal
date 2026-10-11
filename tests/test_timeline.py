"""Parsing the day rhythm HTML and photo cards."""

from __future__ import annotations

import copy
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


def _moment(icon: str, title: str, description: str = "", remarks: str = "") -> str:
    return (
        f"<table class='day-rythm-moment'><tr><td><div class='icon dagritme-{icon} day-rythm-journal-icon'>"
        f"</div></td><td><div class='moment-title'><span class='moment-title-span'>{title}</span></div>"
        f"<div class='moment-entry-description'>{description}</div><div class='moment-remarks'>{remarks}</div>"
        "</td></tr></table>"
    )


def _sleep(title: str) -> str:
    return _moment("slapen-bedje", title)


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


@pytest.mark.parametrize("line_break", ["<br>", "<br/>", "<br />", "<BR>"])
def test_line_break_in_a_field_stays_in_that_field(line_break: str) -> None:
    html = _moment("flesje", "09:00 (Fles)voeding", f"130 cc{line_break}warm", "20cc over")

    (moment,) = parse_day_rhythm(html, CHILD_ID, DAY, TZ)

    assert moment.description == "130 cc\nwarm"
    assert moment.remarks == "20cc over"
    assert moment.amount_ml == 130


def test_markup_in_a_field_stays_in_that_field() -> None:
    html = _moment(
        "flesje", "<b>09:00</b> (Fles)voeding", "130 cc <img src='bottle.png'><i>warm", "20cc<br>over"
    ) + _moment("luier", "10:00 Luier", "Nat")

    bottle, diaper = parse_day_rhythm(html, CHILD_ID, DAY, TZ)

    assert bottle.title == "09:00 (Fles)voeding"
    assert bottle.description == "130 cc warm"
    assert bottle.remarks == "20cc\nover"
    assert diaper.title == "10:00 Luier"
    assert diaper.description == "Nat"


@pytest.mark.parametrize(
    ("title", "start", "end", "label"),
    [
        ("12:00 - 24:00 Slapen", datetime(2026, 10, 7, 12, tzinfo=TZ), datetime(2026, 10, 8, tzinfo=TZ), "Slapen"),
        ("12:00 - 25:00 Slapen", datetime(2026, 10, 7, 12, tzinfo=TZ), None, "Slapen"),
        ("25:00 Slapen", None, None, "25:00 Slapen"),
        ("09:60 - 10:00 Slapen", None, None, "09:60 - 10:00 Slapen"),
    ],
)
def test_times_that_do_not_exist_are_left_out(
    title: str, start: datetime | None, end: datetime | None, label: str
) -> None:
    (moment,) = parse_day_rhythm(_sleep(title), CHILD_ID, DAY, TZ)

    assert (moment.start, moment.end, moment.label) == (start, end, label)


def test_unusable_cards_and_fields_are_skipped(timeline_cards) -> None:
    journal, photo_card = timeline_cards
    unusable = [
        "not a card",
        journal | {"date": None},
        journal | {"date": "2026-10-07"},
        journal | {"date": 10**20},
        journal | {"journal": "not a journal"},
        journal | {"journal": {"childId": CHILD_ID, "dayRythmContent": {"html": 1}, "journalContent": 5}},
        photo_card | {"photos": "not a list"},
        photo_card | {"photos": ["not a photo", {"description": "no id"}, {"id": {"nested": 1}}]},
        {"type": "journal", "date": journal["date"], "children": [7, {"id": None}], "journal": {"dayRythmContent": ""}},
    ]
    odd_photo = {"id": 9, "description": 5, "childTags": ["x", {"ownChild": True}], "fullSizeUrl": 7}

    timeline = parse_timeline([*unusable, photo_card | {"photos": [odd_photo]}, *timeline_cards], TZ)

    assert [m.title for m in timeline.moments] == ["09:00 (Fles)voeding", "10:10-10:48 Slapen", "12:00 (Fles)voeding"]
    assert [p.id for p in timeline.photos] == ["9", "photo3", "photo2", "photo1"]
    assert timeline.photos[0].child_ids == (CHILD_ID,)
    assert timeline.photos[0].description == ""
    assert timeline.photos[0].extension == ".jpg"


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


def _journal_card(cards: list[dict], day: date, day_rhythm: str, note: str = "") -> dict:
    """The fixture's journal card for another day, with this day rhythm and note."""
    card = copy.deepcopy(cards[0])
    card["date"] = int(datetime(day.year, day.month, day.day, tzinfo=TZ).timestamp() * 1000)
    card["journal"] |= {"dayRythmContent": day_rhythm, "journalContent": note}
    return card


def test_journal_card_replaces_its_day_in_the_history(timeline_cards) -> None:
    yesterday = _journal_card(timeline_cards, date(2026, 10, 6), _moment("luier", "10:00 Luier"))
    today = (_moment("flesje", "09:00 (Fles)voeding", "130 cc"), _moment("luier", "10:00 Luier", "Nat"))
    history = parse_timeline(
        [_journal_card(timeline_cards, DAY, "".join(today), "<p>Lekker gespeeld</p>"), yesterday], TZ
    )
    # Later that day the daycare moves the bottle, removes the diaper and adds to the note.
    corrected = _journal_card(
        timeline_cards,
        DAY,
        _moment("flesje", "09:15 (Fles)voeding", "130 cc"),
        "<p>Lekker gespeeld</p><p>Tot morgen!</p>",
    )
    fresh = parse_timeline([corrected], TZ)

    merged = merge_timelines(history, fresh)

    assert [(m.day.isoformat(), m.title, m.description) for m in merged.moments] == [
        ("2026-10-06", "10:00 Luier", ""),
        ("2026-10-07", "09:15 (Fles)voeding", "130 cc"),
        ("2026-10-07", "Note from Juf", "Lekker gespeeld\nTot morgen!"),
    ]
    # The note is the same note, with more text.
    assert history.moments[-1].key == merged.moments[-1].key
    assert history.moments[-1].fingerprint != merged.moments[-1].fingerprint
    # A card from before 'since' is left out, so it replaces nothing either.
    assert merge_timelines(history, fresh, since=date(2026, 10, 8)) == history


def test_merge_leaves_out_entries_before_since(timeline_cards) -> None:
    fresh = parse_timeline(timeline_cards, TZ)

    merged = merge_timelines(Timeline([], []), fresh, since=date(2026, 10, 8))

    assert merged.moments == []
    assert merged.photos == []
