"""Matrix Messenger – Home Assistant Integration.

Ermöglicht das Senden von Nachrichten an Matrix-Räume sowie das
Stellen von Fragen mit Antwortwartezeit (Text oder Emoji-Reaktion).
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall

from .config_flow import _effective_rooms, _effective_sync
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_DEVICE_ID,
    CONF_HOMESERVER,
    CONF_USERNAME,
    DEFAULT_QUESTION_TIMEOUT,
    DEFAULT_SYNC_INTERVAL,
    DOMAIN,
    EVENT_MATRIX_RESPONSE,
)
from .matrix_client import MatrixClient

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["notify"]


# ------------------------------------------------------------------
# Runtime data structures
# ------------------------------------------------------------------


@dataclass
class PendingQuestion:
    question_id: str
    room_id: str
    options: list[str]
    expires_at: float


@dataclass
class MatrixEntryData:
    client: MatrixClient
    pending_questions: dict[str, PendingQuestion] = field(default_factory=dict)
    sync_task: asyncio.Task | None = None


# ------------------------------------------------------------------
# Integration setup / teardown
# ------------------------------------------------------------------


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Matrix Messenger from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    store_path = hass.config.path(f".storage/{DOMAIN}")
    client = MatrixClient(
        homeserver=entry.data[CONF_HOMESERVER],
        user_id=entry.data[CONF_USERNAME],
        store_path=store_path,
    )
    await client.async_setup()
    await client.async_restore_login(
        access_token=entry.data[CONF_ACCESS_TOKEN],
        device_id=entry.data.get(CONF_DEVICE_ID, ""),
    )

    data = MatrixEntryData(client=client)
    hass.data[DOMAIN][entry.entry_id] = data

    async def on_message(room: Any, event: Any) -> None:
        await _handle_message(hass, data, room, event)

    async def on_reaction(room: Any, event: Any) -> None:
        await _handle_reaction(hass, data, room, event)

    client.add_message_callback(on_message)
    client.add_reaction_callback(on_reaction)

    if _effective_sync(entry):
        data.sync_task = hass.async_create_background_task(
            _sync_loop(hass, entry, data, stop_when_idle=False),
            name=f"{DOMAIN}_sync_{entry.entry_id}",
        )

    _register_services(hass, entry, data)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    data: MatrixEntryData | None = hass.data[DOMAIN].pop(entry.entry_id, None)
    if data:
        _cancel_sync(data)
        await data.client.async_close()

    for service_name in ("send_message", "ask_question"):
        if hass.services.has_service(DOMAIN, service_name):
            hass.services.async_remove(DOMAIN, service_name)

    await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


# ------------------------------------------------------------------
# Service registration
# ------------------------------------------------------------------


def _register_services(
    hass: HomeAssistant, entry: ConfigEntry, data: MatrixEntryData
) -> None:
    rooms = _effective_rooms(entry)
    room_ids = list(rooms.keys())
    room_validator = vol.In(room_ids) if room_ids else str

    async def handle_send_message(call: ServiceCall) -> None:
        room_id: str = call.data["room_id"]
        message: str = call.data["message"]
        success = await data.client.async_send_message(room_id, message)
        if not success:
            _LOGGER.error("Nachricht an %s konnte nicht gesendet werden", room_id)

    async def handle_ask_question(call: ServiceCall) -> None:
        room_id: str = call.data["room_id"]
        question: str = call.data["question"]
        options: list[str] = call.data.get("options", [])
        timeout: int = call.data.get("timeout", DEFAULT_QUESTION_TIMEOUT)

        # Assemble full message text
        text = question
        if options:
            text = f"{question}\n\nMögliche Antworten: {' / '.join(options)}"

        await data.client.async_send_message(room_id, text)

        qid = str(uuid.uuid4())
        data.pending_questions[qid] = PendingQuestion(
            question_id=qid,
            room_id=room_id,
            options=options,
            expires_at=time.monotonic() + timeout,
        )
        _LOGGER.debug("Frage %s wartet auf Antwort in Raum %s", qid, room_id)

        # Start a temporary sync loop if none is running
        if data.sync_task is None or data.sync_task.done():
            data.sync_task = hass.async_create_background_task(
                _sync_loop(hass, entry, data, stop_when_idle=True),
                name=f"{DOMAIN}_sync_{entry.entry_id}",
            )

    hass.services.async_register(
        DOMAIN,
        "send_message",
        handle_send_message,
        schema=vol.Schema(
            {
                vol.Required("room_id"): room_validator,
                vol.Required("message"): str,
            }
        ),
    )

    hass.services.async_register(
        DOMAIN,
        "ask_question",
        handle_ask_question,
        schema=vol.Schema(
            {
                vol.Required("room_id"): room_validator,
                vol.Required("question"): str,
                vol.Optional("options", default=[]): [str],
                vol.Optional("timeout", default=DEFAULT_QUESTION_TIMEOUT): vol.All(
                    int, vol.Range(min=60, max=7200)
                ),
            }
        ),
    )


# ------------------------------------------------------------------
# Background sync loop
# ------------------------------------------------------------------


async def _sync_loop(
    hass: HomeAssistant,
    entry: ConfigEntry,
    data: MatrixEntryData,
    stop_when_idle: bool,
) -> None:
    """Poll Matrix every DEFAULT_SYNC_INTERVAL seconds.

    When stop_when_idle=True, the loop exits automatically once all
    pending questions have been answered or expired.
    """
    while True:
        try:
            await data.client.async_sync_once(timeout_ms=5000)

            # Expire old questions
            now = time.monotonic()
            expired = [qid for qid, q in data.pending_questions.items() if now > q.expires_at]
            for qid in expired:
                _LOGGER.debug("Frage %s ist abgelaufen (30 min Timeout)", qid)
                data.pending_questions.pop(qid, None)

            if stop_when_idle and not data.pending_questions and not _effective_sync(entry):
                _LOGGER.debug("Keine offenen Fragen – Sync-Loop wird beendet")
                return

        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("Fehler im Matrix Sync-Loop")

        await asyncio.sleep(DEFAULT_SYNC_INTERVAL)


def _cancel_sync(data: MatrixEntryData) -> None:
    if data.sync_task and not data.sync_task.done():
        data.sync_task.cancel()


# ------------------------------------------------------------------
# Incoming event handlers
# ------------------------------------------------------------------


async def _handle_message(
    hass: HomeAssistant,
    data: MatrixEntryData,
    room: Any,
    event: Any,
) -> None:
    """Match an incoming text message against open questions."""
    if not data.pending_questions:
        return

    response_text: str = getattr(event, "body", "")

    for qid, question in list(data.pending_questions.items()):
        if question.room_id != room.room_id:
            continue
        if question.options and response_text not in question.options:
            continue

        data.pending_questions.pop(qid, None)
        hass.bus.async_fire(
            EVENT_MATRIX_RESPONSE,
            {
                "question_id": qid,
                "room_id": room.room_id,
                "response": response_text,
                "response_type": "text",
                "sender": event.sender,
            },
        )
        _LOGGER.debug("Antwort auf Frage %s empfangen: %s", qid, response_text)
        break


async def _handle_reaction(
    hass: HomeAssistant,
    data: MatrixEntryData,
    room: Any,
    event: Any,
) -> None:
    """Match an incoming m.reaction against open questions."""
    if not data.pending_questions:
        return

    content: dict = getattr(event, "source", {}).get("content", {})
    relates_to: dict = content.get("m.relates_to", {})
    if relates_to.get("rel_type") != "m.annotation":
        return

    emoji: str = relates_to.get("key", "")

    for qid, question in list(data.pending_questions.items()):
        if question.room_id != room.room_id:
            continue
        if question.options and emoji not in question.options:
            continue

        data.pending_questions.pop(qid, None)
        hass.bus.async_fire(
            EVENT_MATRIX_RESPONSE,
            {
                "question_id": qid,
                "room_id": room.room_id,
                "response": emoji,
                "response_type": "emoji",
                "sender": event.sender,
            },
        )
        _LOGGER.debug("Emoji-Reaktion auf Frage %s empfangen: %s", qid, emoji)
        break
