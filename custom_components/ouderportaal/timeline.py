"""Parse timeline cards into moments (day rhythm entries) and photos, and the portal's Messages page into messages.

Kept free of Home Assistant imports so it can be tested standalone.

A day's journal card holds the day rhythm as HTML, one table per moment:

    <table class='day-rythm-moment'>...
      <div class='icon dagritme-flesje day-rythm-journal-icon'></div>
      <span class='moment-title-span'>09:00 (Fles)voeding</span>
      <div class='moment-entry-description'>130 cc (Fles)voeding</div>
      <div class='moment-remarks'>20cc over</div>
    </table>

The daycare keeps adding moments to the same card during the day, and can correct or remove them.

A field of an unexpected type counts as missing, so a surprise from the portal costs at most that card, photo or item.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, tzinfo
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

# The kinds of item on the portal's Messages page.
MESSAGE = "message"
NEWS = "news"
NEWSLETTER = "newsletter"
SERVICE_MESSAGE = "service_message"

_TITLE_RE = re.compile(r"^(\d{1,2}):(\d{2})(?:\s*-\s*(\d{1,2}):(\d{2}))?\s*(.*)$")
_WHITESPACE_RE = re.compile(r"\s+")
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
        """Stable identity: a sleep that gets its end time later keeps its key, and so does a note that gets longer."""
        if self.start:
            anchor = self.start.strftime("%H:%M")
        elif self.category == CATEGORY_NOTE:
            # A journal card has one note.
            anchor = CATEGORY_NOTE
        else:
            anchor = _digest(self.label + self.description)
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
    # Signed, and expired within hours: not part of what the photo is, so not compared or stored.
    full_url: str = field(default="", compare=False)
    medium_url: str = field(default="", compare=False)
    thumb_url: str = field(default="", compare=False)

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


@dataclass(frozen=True, slots=True)
class Message:
    """An item on the portal's Messages page: a conversation, news item, newsletter or service message.

    A conversation keeps its id when replies are added; its text, author and date are those of the latest message.
    News and newsletters only come with a snippet of their text.
    """

    id: str
    subject: str
    preview: str
    sent: datetime
    unread: bool
    child_ids: tuple[str, ...]
    message_type: str = MESSAGE
    author: str = ""
    # The latest message in the conversation was written by a parent: you, or the other parent.
    from_parent: bool = False
    has_attachment: bool = False

    @property
    def key(self) -> str:
        """Unique across the kinds of item, like the portal's own 'message-<id>' and 'news-<id>'."""
        return f"{self.message_type}-{self.id}"

    @property
    def day(self) -> date:
        return self.sent.date()

    @property
    def fingerprint(self) -> str:
        """Changes when a reply arrives; not when it is read, so reading does not trigger notifications."""
        return _digest(f"{self.sent.isoformat()}|{self.preview}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "message_type": self.message_type,
            "subject": self.subject,
            "preview": self.preview,
            "sent": self.sent.isoformat(),
            "unread": self.unread,
            "child_ids": list(self.child_ids),
            "author": self.author,
            "from_parent": self.from_parent,
            "has_attachment": self.has_attachment,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Message:
        # Stores from before news and newsletters only hold conversations, without the newer fields.
        return cls(
            id=data["id"],
            subject=data["subject"],
            preview=data["preview"],
            sent=datetime.fromisoformat(data["sent"]),
            unread=data["unread"],
            child_ids=tuple(data["child_ids"]),
            message_type=data.get("message_type", MESSAGE),
            author=data.get("author", ""),
            from_parent=data.get("from_parent", False),
            has_attachment=data.get("has_attachment", False),
        )


@dataclass(slots=True)
class Timeline:
    moments: list[Moment]
    photos: list[Photo]
    messages: list[Message] = field(default_factory=list)
    # The child and day of each journal card read: its moments are that day's whole day rhythm. Not stored.
    journal_days: set[tuple[str, date]] = field(default_factory=set, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "moments": [m.to_dict() for m in self.moments],
            "photos": [p.to_dict() for p in self.photos],
            "messages": [m.to_dict() for m in self.messages],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Timeline:
        return cls(
            moments=[Moment.from_dict(m) for m in data.get("moments", [])],
            photos=[Photo.from_dict(p) for p in data.get("photos", [])],
            messages=[Message.from_dict(m) for m in data.get("messages", [])],
        )


def merge_timelines(history: Timeline, fresh: Timeline, *, since: date | None = None) -> Timeline:
    """Add freshly fetched entries to the history; fresh data wins for entries seen before.

    A fresh journal card replaces the moments the history has for its child and day, so an entry the daycare
    corrected or removed is corrected or removed here too. Photos and messages stay, also when the portal drops them.
    Entries older than 'since' are left out of 'fresh' (used to limit the initial import).
    """

    def wanted(day: date) -> bool:
        return since is None or day >= since

    replaced = {(child_id, day) for child_id, day in fresh.journal_days if wanted(day)}
    moments = {m.key: m for m in history.moments if (m.child_id, m.day) not in replaced}
    moments.update((m.key, m) for m in fresh.moments if wanted(m.day))
    photos = {p.id: p for p in fresh.photos if wanted(p.day)}
    photos.update((p.id, p) for p in history.photos if p.id not in photos)
    messages = {m.key: m for m in history.messages}
    messages.update((m.key, m) for m in fresh.messages if wanted(m.day))
    return Timeline(
        moments=sorted(moments.values(), key=_moment_sort_key),
        photos=sorted(photos.values(), key=lambda p: p.day, reverse=True),
        messages=sorted(messages.values(), key=lambda m: m.sent, reverse=True),
    )


def _parse_sent(value: Any, tz: tzinfo) -> datetime | None:
    """The portal sends dates as epoch milliseconds or ISO strings; accept both."""
    if isinstance(value, int | float):
        return _epoch(value, tz)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)
    return None


def parse_messages(items: list[dict[str, Any]], tz: tzinfo) -> list[Message]:
    """Conversations from /logbook/overview, newest first. Items without a usable date are skipped."""
    return _parse_items(items, tz, "date", _conversation)


def parse_news(items: list[dict[str, Any]], tz: tzinfo) -> list[Message]:
    """News items from /htmlnews/view, newest first."""
    return _parse_items(items, tz, "publishDate", _news_item)


def parse_newsletters(items: list[dict[str, Any]], tz: tzinfo) -> list[Message]:
    """Newsletters and service messages from /newsletter, newest first."""
    return _parse_items(items, tz, "sendDate", _newsletter)


def _parse_items(
    items: list[dict[str, Any]], tz: tzinfo, date_field: str, build: Callable[[dict[str, Any], datetime], Message]
) -> list[Message]:
    messages = []
    for item in items:
        if isinstance(item, dict) and (sent := _parse_sent(item.get(date_field), tz)) is not None:
            messages.append(build(item, sent))
    messages.sort(key=lambda m: m.sent, reverse=True)
    return messages


def _source_id(item: dict[str, Any], fields: tuple[str, ...], sent: datetime, subject: str) -> str:
    """The item's id, with the same fallbacks as the portal's own app."""
    for name in fields:
        if source_id := _id(item.get(name)):
            return source_id
    return f"{sent.isoformat()}-{subject}"


def _conversation(item: dict[str, Any], sent: datetime) -> Message:
    subject = _str(item.get("subject"))
    return Message(
        id=_source_id(item, ("logMessageMessageId", "logbookMessageId", "logMessageId", "id"), sent, subject),
        subject=subject,
        # The whole latest message, as HTML.
        preview=html_to_text(_raw_str(item.get("message"))),
        sent=sent,
        # null rather than false when the latest message is from a parent.
        unread=bool(item.get("unread")),
        child_ids=_child_ids(item),
        message_type=MESSAGE,
        # The group for messages from the daycare, the parent's name for theirs.
        author=_str(item.get("lastWritten")),
        from_parent=bool(item.get("lastFromParent")),
    )


def _news_item(item: dict[str, Any], sent: datetime) -> Message:
    subject = _str(item.get("title"))
    return Message(
        id=_source_id(item, ("id", "htmlNewsId", "htmlNewsItemId", "newsItemId"), sent, subject),
        subject=subject,
        preview=_str(item.get("contentSnippet")),
        sent=sent,
        unread=bool(item.get("isNew")),
        child_ids=(),
        message_type=NEWS,
        has_attachment=bool(item.get("containsAttachment")),
    )


def _newsletter(item: dict[str, Any], sent: datetime) -> Message:
    subject = _str(item.get("mailSubject"))
    service = _str(item.get("newsLetterType")).lower() == "servicemessage"
    return Message(
        id=_source_id(item, ("generatedHtmlNewsLetterId", "id"), sent, subject),
        subject=subject,
        # Plain text, cut off after about 500 characters.
        preview=_str(item.get("contentSnippet")),
        sent=sent,
        unread=bool(item.get("unread")),
        child_ids=(),
        message_type=SERVICE_MESSAGE if service else NEWSLETTER,
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


def _lines(value: str) -> str:
    """Each line cleaned up, without the empty ones."""
    return "\n".join(line for line in (_clean(line) for line in value.splitlines()) if line)


def _raw_str(value: Any) -> str:
    """A string as the portal sent it, such as HTML."""
    return value if isinstance(value, str) else ""


def _str(value: Any) -> str:
    return _clean(_raw_str(value))


def _id(value: Any) -> str:
    """An id from the portal, as text; empty when there is none."""
    if isinstance(value, bool) or not isinstance(value, str | int) or not value:
        return ""
    return str(value)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _dicts(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _epoch(value: Any, tz: tzinfo) -> datetime | None:
    """Epoch milliseconds, as the portal sends times."""
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz)
    except (OverflowError, OSError, ValueError):
        return None


# Tags that start a new line on screen.
_BREAKS = frozenset({"br", "p", "div", "li"})


class _DayRhythmParser(HTMLParser):
    """Collects the raw fields of each 'day-rythm-moment' table.

    A field ends with the end tag of the element that holds it, so other markup in it, such as a <br>, which has no
    end tag, cannot keep it open.
    """

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
        # The tag of the element holding the field, and how many elements with that tag are open.
        self._field_tag = ""
        self._field_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = (dict(attrs).get("class") or "").split()
        if tag == "table" and "day-rythm-moment" in classes:
            self._current = {"icon": "", "title": "", "subtitle": "", "description": "", "remarks": ""}
            self.moments.append(self._current)
            self._field = None
            return
        if self._current is None:
            return
        if self._field:
            self._break(tag)
            if tag == self._field_tag:
                self._field_depth += 1
            return
        for cls in classes:
            if cls.startswith(ICON_PREFIX) and cls not in ICON_IGNORE:
                self._current["icon"] = cls.removeprefix(ICON_PREFIX)
            if field := self._FIELDS.get(cls):
                self._field, self._field_tag, self._field_depth = field, tag, 1

    def handle_endtag(self, tag: str) -> None:
        if self._field:
            self._break(tag)
            if tag == self._field_tag:
                self._field_depth -= 1
                if self._field_depth == 0:
                    self._field = None
        elif tag == "table":
            self._current = None

    def handle_data(self, data: str) -> None:
        if self._current is not None and self._field:
            # Line breaks in the HTML source are spaces on screen.
            self._current[self._field] += _WHITESPACE_RE.sub(" ", data)

    def _break(self, tag: str) -> None:
        if self._current is not None and self._field and tag in _BREAKS:
            self._current[self._field] += "\n"


class _TextParser(HTMLParser):
    """Text as the portal shows it: line breaks in the HTML source are spaces; <br> and blocks start a new line."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(_WHITESPACE_RE.sub(" ", data))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BREAKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _BREAKS:
            self.parts.append("\n")


def html_to_text(html: str) -> str:
    parser = _TextParser()
    parser.feed(html)
    return _lines("".join(parser.parts))


def _at(day: date, hours: str, minutes: str, tz: tzinfo) -> datetime | None:
    """That time of the day; None for a time the clock does not have. '24:00' is the end of the day."""
    if (hours, minutes) == ("24", "00"):
        return datetime.combine(day + timedelta(days=1), time.min, tz)
    if int(hours) > 23 or int(minutes) > 59:
        return None
    return datetime.combine(day, time(int(hours), int(minutes)), tz)


def parse_day_rhythm(html: str, child_id: str, day: date, tz: tzinfo) -> list[Moment]:
    parser = _DayRhythmParser()
    parser.feed(html)
    moments = []
    for raw in parser.moments:
        title = _clean(raw["title"])
        start = end = None
        label = title
        # A title whose start time does not exist stays whole, as an untimed moment.
        if (match := _TITLE_RE.match(title)) and (start := _at(day, *match.group(1, 2), tz)):
            h2, m2, label = match.group(3, 4, 5)
            if h2 is not None:
                end = _at(day, h2, m2, tz)
        description = _lines(raw["description"])
        remarks = _lines(raw["remarks"])
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


def _card_day(card: dict[str, Any], tz: tzinfo) -> date | None:
    # Card dates are local midnight in epoch milliseconds.
    when = _epoch(card.get("date"), tz)
    return when.date() if when else None


def _child_ids(item: dict[str, Any]) -> tuple[str, ...]:
    """The 'children' of a timeline card or conversation."""
    return tuple(child_id for child in _dicts(item.get("children")) if (child_id := _id(child.get("id"))))


def _note(journal: dict[str, Any], child_id: str, day: date) -> Moment | None:
    """The journal's free text, as an untimed moment."""
    if not (text := html_to_text(_raw_str(journal.get("journalContent")))):
        return None
    writer = _str(journal.get("writtenByName"))
    return Moment(
        child_id=child_id,
        day=day,
        category=CATEGORY_NOTE,
        icon="note",
        label=f"Note from {writer}" if writer else "Note",
        description=text,
        remarks="",
        start=None,
        end=None,
        amount_ml=None,
    )


def _photo(photo: dict[str, Any], card: dict[str, Any], day: date) -> Photo | None:
    if not (photo_id := _id(photo.get("id"))):
        return None
    tagged = tuple(
        child_id
        for tag in _dicts(photo.get("childTags"))
        if tag.get("ownChild") and (child_id := _id(tag.get("personId")))
    )
    media_type = _str(photo.get("mediaType")) or "photo"
    full_url = _str(photo.get("fullSizeUrl"))
    return Photo(
        id=photo_id,
        child_ids=tagged or _child_ids(card),
        day=day,
        description=_raw_str(photo.get("description")),
        media_type=media_type,
        extension=_extension(full_url, media_type),
        full_url=full_url,
        medium_url=_str(photo.get("mediumUrl")),
        thumb_url=_str(photo.get("thumbUrl")),
    )


def parse_timeline(cards: list[dict[str, Any]], tz: tzinfo) -> Timeline:
    """Moments sorted oldest first; photos in timeline order (newest day first)."""
    timeline = Timeline(moments=[], photos=[])
    for card in _dicts(cards):
        if (day := _card_day(card, tz)) is None:
            continue
        card_type = card.get("type")
        if card_type == "journal" and (journal := _dict(card.get("journal"))):
            written_for = _id(journal.get("childId"))
            for child_id in (written_for,) if written_for else _child_ids(card):
                timeline.journal_days.add((child_id, day))
                timeline.moments += parse_day_rhythm(_raw_str(journal.get("dayRythmContent")), child_id, day, tz)
                if note := _note(journal, child_id, day):
                    timeline.moments.append(note)
        elif card_type == "photo":
            timeline.photos += [p for photo in _dicts(card.get("photos")) if (p := _photo(photo, card, day))]
    timeline.moments.sort(key=_moment_sort_key)
    return timeline
