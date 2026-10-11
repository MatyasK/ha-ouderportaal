"""Sensors summarising each child's day rhythm."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import UnitOfTime, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import OuderportaalConfigEntry, OuderportaalCoordinator
from .entity import OuderportaalEntity
from .timeline import Message, Moment

MAX_STATE_LENGTH = 255


def _latest(moments: list[Moment], category: str | None = None) -> Moment | None:
    timed = [m for m in moments if m.start and (category is None or m.category == category)]
    return timed[-1] if timed else None


def _moment_attrs(moment: Moment | None) -> dict[str, Any]:
    if moment is None:
        return {}
    return {
        "category": moment.category,
        "label": moment.label,
        "description": moment.description,
        "remarks": moment.remarks,
        "start": moment.start,
        "end": moment.end,
        "duration_minutes": moment.duration_minutes,
        "amount_ml": moment.amount_ml,
    }


def _last_activity(moments: list[Moment], _today: date) -> str | None:
    moment = _latest(moments)
    return moment.title[:MAX_STATE_LENGTH] if moment else None


def _last_sleep(moments: list[Moment], _today: date) -> Any:
    # While asleep (no end yet) this is the time they fell asleep.
    moment = _latest(moments, "sleep")
    return (moment.end or moment.start) if moment else None


def _last_sleep_attrs(moments: list[Moment]) -> dict[str, Any]:
    moment = _latest(moments, "sleep")
    return _moment_attrs(moment) | ({"sleeping": moment.end is None} if moment else {})


def _today_total(category: str, value: Callable[[Moment], float | None]) -> Callable[[list[Moment], date], float]:
    def total(moments: list[Moment], today: date) -> float:
        return sum(value(m) or 0 for m in moments if m.day == today and m.category == category)

    return total


@dataclass(frozen=True, kw_only=True)
class OuderportaalSensorDescription(SensorEntityDescription):
    value_fn: Callable[[list[Moment], date], Any]
    attrs_fn: Callable[[list[Moment]], dict[str, Any]] = lambda _moments: {}


SENSORS: tuple[OuderportaalSensorDescription, ...] = (
    OuderportaalSensorDescription(
        key="last_activity",
        value_fn=_last_activity,
        attrs_fn=lambda moments: _moment_attrs(_latest(moments)),
    ),
    OuderportaalSensorDescription(
        key="last_bottle",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda moments, _today: (m := _latest(moments, "bottle")) and m.start,
        attrs_fn=lambda moments: _moment_attrs(_latest(moments, "bottle")),
    ),
    OuderportaalSensorDescription(
        key="last_diaper",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda moments, _today: (m := _latest(moments, "diaper")) and m.start,
        attrs_fn=lambda moments: _moment_attrs(_latest(moments, "diaper")),
    ),
    OuderportaalSensorDescription(
        key="last_sleep",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_last_sleep,
        attrs_fn=_last_sleep_attrs,
    ),
    OuderportaalSensorDescription(
        key="milk_today",
        device_class=SensorDeviceClass.VOLUME,
        native_unit_of_measurement=UnitOfVolume.MILLILITERS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=0,
        value_fn=_today_total("bottle", lambda m: m.amount_ml),
    ),
    OuderportaalSensorDescription(
        key="sleep_today",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=_today_total("sleep", lambda m: m.duration_minutes),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OuderportaalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        OuderportaalSensor(coordinator, child_id, description)
        for child_id in coordinator.children
        for description in SENSORS
    )
    async_add_entities([UnreadMessagesSensor(coordinator), LatestMessageSensor(coordinator)])


class OuderportaalSensor(OuderportaalEntity, SensorEntity):
    entity_description: OuderportaalSensorDescription

    def __init__(
        self,
        coordinator: OuderportaalCoordinator,
        child_id: str,
        description: OuderportaalSensorDescription,
    ) -> None:
        super().__init__(coordinator, child_id, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.moments, dt_util.now().date())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self.entity_description.attrs_fn(self.moments)


class OuderportaalMailboxSensor(CoordinatorEntity[OuderportaalCoordinator], SensorEntity):
    """Messages belong to the account, not to one child: they get their own 'Mailbox' device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: OuderportaalCoordinator, key: str) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_mailbox")},
            translation_key="mailbox",
            manufacturer="Konnect",
            model="Ouderportaal",
            configuration_url=f"{coordinator.client.base_url}/parent/#/tabs/mailbox",
            entry_type=DeviceEntryType.SERVICE,
        )


class UnreadMessagesSensor(OuderportaalMailboxSensor):
    """The badge on the portal's Messages tab: unread conversations, news and newsletters together."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: OuderportaalCoordinator) -> None:
        super().__init__(coordinator, "unread_messages")

    @property
    def native_value(self) -> int | None:
        counts = self.coordinator.unread_counts
        return sum(counts.values()) if counts is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return dict(self.coordinator.unread_counts or {})


class LatestMessageSensor(OuderportaalMailboxSensor):
    """The top of the portal's Messages page, whatever kind of item it is."""

    def __init__(self, coordinator: OuderportaalCoordinator) -> None:
        super().__init__(coordinator, "latest_message")

    @property
    def _latest(self) -> Message | None:
        return self.coordinator.data.messages[0] if self.coordinator.data.messages else None

    @property
    def native_value(self) -> str | None:
        message = self._latest
        if message is None:
            return None
        return (message.subject or message.preview)[:MAX_STATE_LENGTH] or None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        message = self._latest
        if message is None:
            return {}
        return {
            "message_type": message.message_type,
            "message_id": message.id,
            "subject": message.subject,
            "preview": message.preview,
            "author": message.author,
            "from_parent": message.from_parent,
            "has_attachment": message.has_attachment,
            "sent": message.sent,
            "unread": message.unread,
            "child_names": [self.coordinator.child_name(c) for c in message.child_ids],
        }
