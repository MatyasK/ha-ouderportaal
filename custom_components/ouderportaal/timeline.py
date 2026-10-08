"""Parse timeline cards into moments (day rhythm entries) and photos.

Kept free of Home Assistant imports so it can be tested standalone.

A day's journal card holds the day rhythm as HTML, one table per moment:

    <table class='day-rythm-moment'>...
      <div class='icon dagritme-flesje day-rythm-journal-icon'></div>
      <span class='moment-title-span'>09:00 (Fles)voeding</span>
      <div class='moment-entry-description'>130 cc (Fles)voeding</div>
      <div class='moment-remarks'>20cc over</div>
    </table>

The daycare keeps adding moments to the same card during the day.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime, time, tzinfo
from html.parser import HTMLParser
from pathlib import PurePosixPath
from typing import Any, ClassVar
from urllib.parse import urlsplit

ICON_PREFIX = "dagritme-"
ICON_IGNORE = {"dagritme-journal-icon"}

# Icon name prefix -> category. First match wins, so 'brood-drinken' is food.
_CATEGORY_PREFIXES: tuple[tuple[str, str], ...] = (
    ("flesje", "bottle"),
    ("slapen", "sleep"),
    ("luier", "diaper"),
    ("veiligheidsspeld", "diaper"),
    ("toilet", "toilet"),
    ("medicatie", "medication"),
    ("thermometer", "temperature"),
    ("zonnebrandcreme", "sunscreen"),
    ("brood", "food"),
    ("appel", "food"),
    ("banaan", "food"),
    ("peer", "food"),
    ("groentehap", "food"),
    ("warmeten", "food"),
    ("koekje", "food"),
    ("toetje", "food"),
    ("drinken", "drink"),
)
CATEGORY_NOTE = "note"
CATEGORY_ACTIVITY = "activity"

_TITLE_RE = re.compile(r"^(\d{1,2}):(\d{2})(?:\s*-\s*(\d{1,2}):(\d{2}))?\s*(.*)$")
_AMOUNT_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:cc|ml)\b", re.IGNORECASE)


def category_for_icon(icon: str) -> str:
    for prefix, category in _CATEGORY_PREFIXES:
        if icon.startswith(prefix):
            return category
    return CATEGORY_ACTIVITY


@dataclass(frozen=True, slots=True)
class Moment:
    """One entry of a child's day rhythm, or the free-text daily note."""

    child_id: str
    day: date
    category: str
    icon: str
    label: str
    description: str
    remarks: str
    start: datetime | None
    end: datetime | None
    amount_ml: float | None

    @property
    def key(self) -> str:
        """Stable identity: a sleep that gets its end time later keeps its key."""
        anchor = self.start.strftime("%H:%M") if self.start else _digest(self.label + self.description)
        return f"{self.child_id}:{self.day.isoformat()}:{self.icon}:{anchor}"

    @property
    def fingerprint(self) -> str:
        """Changes whenever the visible content changes."""
        end = self.end.isoformat() if self.end else ""
        return _digest("|".join((self.label, self.description, self.remarks, end)))

    @property
    def duration_minutes(self) -> int | None:
        if self.start and self.end:
            return round((self.end - self.start).total_seconds() / 60)
        return None

    @property
    def title(self) -> str:
        """Human readable one-liner, e.g. '10:10-10:48 Slapen'."""
        when = ""
        if self.start:
            when = self.start.strftime("%H:%M")
            if self.end:
                when += "-" + self.end.strftime("%H:%M")
        return " ".join(part for part in (when, self.label) if part)

    @property
    def text(self) -> str:
        """Title plus details, for notifications."""
        details = [part for part in (self.description, self.remarks) if part and part != self.label]
        return " - ".join([self.title, *details])

    def to_dict(self) -> dict[str, Any]:
        return {
            "child_id": self.child_id,
            "day": self.day.isoformat(),
            "category": self.category,
            "icon": self.icon,
            "label": self.label,
            "description": self.description,
            "remarks": self.remarks,
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "amount_ml": self.amount_ml,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Moment:
        return cls(
            child_id=data["child_id"],
            day=date.fromisoformat(data["day"]),
            category=data["category"],
            icon=data["icon"],
            label=data["label"],
            description=data["description"],
            remarks=data["remarks"],
            start=datetime.fromisoformat(data["start"]) if data["start"] else None,
            end=datetime.fromisoformat(data["end"]) if data["end"] else None,
            amount_ml=data["amount_ml"],
        )


_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SAFE_SUFFIX_RE = re.compile(r"^\.[a-z0-9]{1,5}$")


@dataclass(frozen=True, slots=True)
class Photo:
    id: str
    child_ids: tuple[str, ...]
    day: date
    description: str
    media_type: str
    extension: str
    full_url: str = ""
    medium_url: str = ""
    thumb_url: str = ""

    @property
    def filename(self) -> str:
        """Relative path for the saved copy, e.g. '2026-10-07/14a54e71.jpeg'.

        The id comes from the portal, so it is checked before going near the file system.
        """
        name = self.id if _SAFE_ID_RE.match(self.id) else _digest(self.id)
        return f"{self.day.isoformat()}/{name}{self.extension}"

    def to_dict(self) -> dict[str, Any]:
        """Without the signed URLs: they expire within hours, so they are not worth keeping."""
        return {
            "id": self.id,
            "child_ids": list(self.child_ids),
            "day": self.day.isoformat(),
            "description": self.description,
            "media_type": self.media_type,
            "extension": self.extension,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Photo:
        return cls(
            id=data["id"],
            child_ids=tuple(data["child_ids"]),
            day=date.fromisoformat(data["day"]),
            description=data["description"],
            media_type=data["media_type"],
            extension=data["extension"],
        )


@dataclass(slots=True)
class Timeline:
    moments: list[Moment]
    photos: list[Photo]

    def to_dict(self) -> dict[str, Any]:
        return {
            "moments": [m.to_dict() for m in self.moments],
            "photos": [p.to_dict() for p in self.photos],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Timeline:
        return cls(
            moments=[Moment.from_dict(m) for m in data.get("moments", [])],
            photos=[Photo.from_dict(p) for p in data.get("photos", [])],
        )


def merge_timelines(history: Timeline, fresh: Timeline, *, since: date | None = None) -> Timeline:
    """Add freshly fetched entries to the history; fresh data wins for entries seen before.

    Entries older than 'since' are left out of 'fresh' (used to limit the initial import).
    """
    moments = {m.key: m for m in history.moments}
    moments.update((m.key, m) for m in fresh.moments if since is None or m.day >= since)
    photos = {p.id: p for p in fresh.photos if since is None or p.day >= since}
    photos.update((p.id, p) for p in history.photos if p.id not in photos)
    return Timeline(
        moments=sorted(moments.values(), key=_moment_sort_key),
        photos=sorted(photos.values(), key=lambda p: p.day, reverse=True),
    )


def _extension(url: str, media_type: str) -> str:
    suffix = PurePosixPath(urlsplit(url).path).suffix.lower()
    if _SAFE_SUFFIX_RE.match(suffix):
        return suffix
    return ".mp4" if media_type == "video" else ".jpg"


def _moment_sort_key(moment: Moment) -> tuple[date, bool, time]:
    # Untimed moments (notes) go last within their day.
    return (moment.day, moment.start is None, moment.start.time() if moment.start else time.min)


def _digest(value: str) -> str:
    return hashlib.sha1(value.encode()).hexdigest()[:12]


def _clean(value: str) -> str:
    return " ".join(value.split())


class _DayRhythmParser(HTMLParser):
    """Collects the raw fields of each 'day-rythm-moment' table."""

    _FIELDS: ClassVar[dict[str, str]] = {
        "moment-title-span": "title",
        "moment-subtitle": "subtitle",
        "moment-entry-description": "description",
        "moment-remarks": "remarks",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.moments: list[dict[str, str]] = []
        self._current: dict[str, str] | None = None
        self._field: str | None = None
        self._field_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = (dict(attrs).get("class") or "").split()
        if tag == "table" and "day-rythm-moment" in classes:
            self._current = {"icon": "", "title": "", "subtitle": "", "description": "", "remarks": ""}
            self.moments.append(self._current)
            return
        if self._current is None:
            return
        if self._field:
            self._field_depth += 1
            return
        for cls in classes:
            if cls.startswith(ICON_PREFIX) and cls not in ICON_IGNORE:
                self._current["icon"] = cls.removeprefix(ICON_PREFIX)
            if field := self._FIELDS.get(cls):
                self._field = field
                self._field_depth = 1

    def handle_endtag(self, tag: str) -> None:
        if self._field:
            self._field_depth -= 1
            if self._field_depth == 0:
                self._field = None
        elif tag == "table":
            self._current = None

    def handle_data(self, data: str) -> None:
        if self._current is not None and self._field:
            self._current[self._field] += data


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("br", "p", "div", "li"):
            self.parts.append("\n")


def html_to_text(html: str) -> str:
    parser = _TextParser()
    parser.feed(html)
    lines = (_clean(line) for line in "".join(parser.parts).splitlines())
    return "\n".join(line for line in lines if line)


def parse_day_rhythm(html: str, child_id: str, day: date, tz: tzinfo) -> list[Moment]:
    parser = _DayRhythmParser()
    parser.feed(html)
    moments = []
    for raw in parser.moments:
        title = _clean(raw["title"])
        start = end = None
        label = title
        if match := _TITLE_RE.match(title):
            h1, m1, h2, m2, label = match.groups()
            start = datetime.combine(day, time(int(h1), int(m1)), tz)
            if h2 is not None:
                end = datetime.combine(day, time(int(h2), int(m2)), tz)
        description = _clean(raw["description"])
        remarks = _clean(raw["remarks"])
        amount = None
        if match := _AMOUNT_RE.search(description):
            amount = float(match.group(1).replace(",", "."))
        moments.append(
            Moment(
                child_id=child_id,
                day=day,
                category=category_for_icon(raw["icon"]),
                icon=raw["icon"],
                label=_clean(label),
                description=description,
                remarks=remarks,
                start=start,
                end=end,
                amount_ml=amount,
            )
        )
    return moments


def _card_day(card: dict[str, Any], tz: tzinfo) -> date:
    # Card dates are local midnight in epoch milliseconds.
    return datetime.fromtimestamp(card["date"] / 1000, tz).date()


def _card_child_ids(card: dict[str, Any]) -> tuple[str, ...]:
    return tuple(child["id"] for child in card.get("children") or [] if child.get("id"))


def parse_timeline(cards: list[dict[str, Any]], tz: tzinfo) -> Timeline:
    """Moments sorted oldest first; photos in timeline order (newest day first)."""
    moments: list[Moment] = []
    photos: list[Photo] = []
    for card in cards:
        card_type = card.get("type")
        if "date" not in card:
            continue
        day = _card_day(card, tz)
        if card_type == "journal" and (journal := card.get("journal")):
            child_ids = (journal["childId"],) if journal.get("childId") else _card_child_ids(card)
            for child_id in child_ids:
                moments.extend(parse_day_rhythm(journal.get("dayRythmContent") or "", child_id, day, tz))
                if note := html_to_text(journal.get("journalContent") or ""):
                    moments.append(
                        Moment(
                            child_id=child_id,
                            day=day,
                            category=CATEGORY_NOTE,
                            icon="note",
                            label=f"Note from {journal['writtenByName']}" if journal.get("writtenByName") else "Note",
                            description=note,
                            remarks="",
                            start=None,
                            end=None,
                            amount_ml=None,
                        )
                    )
        elif card_type == "photo":
            for photo in card.get("photos") or []:
                tagged = tuple(tag["personId"] for tag in photo.get("childTags") or [] if tag.get("ownChild"))
                media_type = photo.get("mediaType") or "photo"
                full_url = photo.get("fullSizeUrl") or ""
                photos.append(
                    Photo(
                        id=photo["id"],
                        child_ids=tagged or _card_child_ids(card),
                        day=day,
                        description=photo.get("description") or "",
                        media_type=media_type,
                        extension=_extension(full_url, media_type),
                        full_url=full_url,
                        medium_url=photo.get("mediumUrl") or "",
                        thumb_url=photo.get("thumbUrl") or "",
                    )
                )
    moments.sort(key=_moment_sort_key)
    return Timeline(moments=moments, photos=photos)
