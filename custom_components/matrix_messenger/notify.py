"""Notify platform – creates one notify entity per configured Matrix room.

Ergebnis: notify.matrix_<raumname> Entitäten, die im HA-Benachrichtigungsdialog
und in Automationen als Ziel auswählbar sind.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.notify import NotifyEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .config_flow import _effective_rooms
from .const import CONF_USERNAME, DOMAIN

if TYPE_CHECKING:
    from . import MatrixEntryData

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Registriert für jeden konfigurierten Raum eine NotifyEntity."""
    data: MatrixEntryData = hass.data[DOMAIN][entry.entry_id]
    rooms = _effective_rooms(entry)
    async_add_entities(
        MatrixRoomNotifyEntity(entry, data, room_id, room_name)
        for room_id, room_name in rooms.items()
    )


class MatrixRoomNotifyEntity(NotifyEntity):
    """Eine Benachrichtigungsentität für einen einzelnen Matrix-Raum."""

    _attr_has_entity_name = True

    def __init__(
        self,
        entry: ConfigEntry,
        data: Any,
        room_id: str,
        room_name: str,
    ) -> None:
        self._entry = entry
        self._data = data
        self._room_id = room_id
        self._attr_name = room_name
        # Unique ID: entry-ID + Raum-ID verhindert Kollisionen bei umbenahnten Räumen
        self._attr_unique_id = f"{entry.entry_id}_{room_id}"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=f"Matrix ({self._entry.data.get(CONF_USERNAME, '')})",
            manufacturer="Matrix.org",
            model="Matrix Messenger",
        )

    async def async_send_message(self, message: str, title: str | None = None) -> None:
        """Sendet eine Nachricht. Optionaler Titel wird fett vorangestellt."""
        text = f"**{title}**\n{message}" if title else message
        success = await self._data.client.async_send_message(self._room_id, text)
        if not success:
            _LOGGER.error(
                "notify: Nachricht an Raum %s konnte nicht gesendet werden", self._room_id
            )
