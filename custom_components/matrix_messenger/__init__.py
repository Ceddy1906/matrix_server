"""Matrix Messenger – Home Assistant Integration.

Ermöglicht das Senden von Nachrichten an Matrix-Räume sowie das
Stellen von Fragen mit Antwortwartezeit (Text oder Emoji-Reaktion).
"""
from __future__ import annotations

import asyncio
import logging
import re
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
    display_names: dict[str, str] = field(default_factory=dict)
    pending_questions: dict[str, PendingQuestion] = field(default_factory=dict)
    sync_task: asyncio.Task | None = None
    room_service_names: list[str] = field(default_factory=list)


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

    # Fetch room display names via direct state API (independent of sync state)
    stored_rooms = _effective_rooms(entry)
    try:
        display_names = await client.async_get_room_names(list(stored_rooms.keys()))
    except Exception:
        _LOGGER.debug("Could not fetch room display names – falling back to stored names")
        display_names = dict(stored_rooms)

    data = MatrixEntryData(client=client, display_names=display_names)
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

    all_services = ["send_message", "ask_question", "send_to_user"]
    if data:
        all_services.extend(data.room_service_names)
    for service_name in all_services:
        if hass.services.has_service(DOMAIN, service_name):
            hass.services.async_remove(DOMAIN, service_name)

    # Remove injected descriptions from cache so stale entries don't linger
    try:
        from homeassistant.loader import SERVICE_DESCRIPTION_CACHE as _cache_key
    except ImportError:
        _cache_key = "service_description_cache"
    cache = hass.data.get(_cache_key, {})
    for svc in ["send_message", "ask_question"] + (data.room_service_names if data else []):
        cache.pop((DOMAIN, svc), None)

    await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


# ------------------------------------------------------------------
# Service registration
# ------------------------------------------------------------------


def _inject_service_descriptions(
    hass: HomeAssistant,
    rooms: dict[str, str],
    room_service_names: list[str],
    display_names: dict[str, str] | None = None,
) -> None:
    """Inject dynamic service descriptions so HA shows friendly dropdowns and text fields."""
    try:
        from homeassistant.loader import SERVICE_DESCRIPTION_CACHE as _cache_key
    except ImportError:
        _cache_key = "service_description_cache"  # legacy fallback

    cache: dict = hass.data.setdefault(_cache_key, {})
    labels = display_names or {}

    msg_field = {
        "name": "Nachricht",
        "description": "Der zu sendende Text.",
        "required": True,
        "selector": {"text": {"multiline": True}},
    }
    room_options = [
        {"value": rid, "label": labels.get(rid) or stored or rid}
        for rid, stored in rooms.items()
    ]
    room_field = {
        "name": "Raum",
        "description": "Wähle einen konfigurierten Matrix-Raum.",
        "required": True,
        "selector": {"select": {"options": room_options, "mode": "dropdown"}},
    }

    cache[(DOMAIN, "send_message")] = {
        "name": "Matrix-Nachricht senden",
        "description": "Sendet eine Textnachricht an einen konfigurierten Matrix-Raum.",
        "fields": {"room_id": room_field, "message": msg_field},
    }
    cache[(DOMAIN, "ask_question")] = {
        "name": "Frage in Matrix-Raum stellen",
        "description": (
            "Sendet eine Frage und wartet auf Antwort (Text oder Emoji-Reaktion). "
            "Löst das Event 'matrix_messenger_response' aus."
        ),
        "fields": {
            "room_id": room_field,
            "question": {
                "name": "Frage",
                "description": "Der Fragetext.",
                "required": True,
                "selector": {"text": {"multiline": True}},
            },
            "options": {
                "name": "Antwortoptionen",
                "description": "Optionale Liste gültiger Antworten. Leer = jede Antwort.",
                "required": False,
                "selector": {"object": {}},
            },
            "timeout": {
                "name": "Timeout (Sekunden)",
                "description": "Wartezeit. Standard: 1800 s (30 Minuten).",
                "required": False,
                "default": DEFAULT_QUESTION_TIMEOUT,
                "selector": {
                    "number": {"min": 60, "max": 7200, "step": 60, "unit_of_measurement": "s", "mode": "box"}
                },
            },
        },
    }

    for service_name in room_service_names:
        slug = service_name[len("send_to_"):]
        room_id = next(
            (rid for rid, name in rooms.items() if _room_slug(name) == slug),
            None,
        )
        label = (labels.get(room_id) or rooms.get(room_id) or slug) if room_id else slug
        cache[(DOMAIN, service_name)] = {
            "name": f"Matrix → {label}",
            "description": f'Sendet eine Nachricht an den Matrix-Raum "{label}".',
            "fields": {"message": msg_field},
        }


def _room_slug(name: str) -> str:
    """Convert a room display name to a valid HA service name fragment."""
    slug = name.lower()
    slug = re.sub(r"[^a-z0-9]+", "_", slug).strip("_")
    return slug or "room"


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

        if data.sync_task is None or data.sync_task.done():
            data.sync_task = hass.async_create_background_task(
                _sync_loop(hass, entry, data, stop_when_idle=True),
                name=f"{DOMAIN}_sync_{entry.entry_id}",
            )

    async def handle_send_to_user(call: ServiceCall) -> None:
        user_id: str = call.data["user_id"]
        message: str = call.data["message"]
        success = await data.client.async_send_to_user(user_id, message)
        if not success:
            _LOGGER.error("Direktnachricht an %s konnte nicht gesendet werden", user_id)

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

    hass.services.async_register(
        DOMAIN,
        "send_to_user",
        handle_send_to_user,
        schema=vol.Schema(
            {
                vol.Required("user_id"): str,
                vol.Required("message"): str,
            }
        ),
    )

    # Per-room convenience services: matrix_messenger.send_to_<slug>
    # (registered first so room_service_names is populated before injection)
    used_slugs: set[str] = set()
    for room_id, room_name in rooms.items():
        base = _room_slug(room_name)
        slug = base
        counter = 2
        while slug in used_slugs:
            slug = f"{base}_{counter}"
            counter += 1
        used_slugs.add(slug)

        service_name = f"send_to_{slug}"
        data.room_service_names.append(service_name)

        def _make_handler(rid: str, rname: str):
            async def handler(call: ServiceCall) -> None:
                msg: str = call.data["message"]
                success = await data.client.async_send_message(rid, msg)
                if not success:
                    _LOGGER.error("Nachricht an %s (%s) konnte nicht gesendet werden", rname, rid)
            return handler

        hass.services.async_register(
            DOMAIN,
            service_name,
            _make_handler(room_id, room_name),
            schema=vol.Schema({vol.Required("message"): str}),
        )

    _inject_service_descriptions(hass, rooms, data.room_service_names, data.display_names)


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
