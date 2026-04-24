"""Async Matrix client wrapper with E2EE support via matrix-nio."""
from __future__ import annotations

import logging
import os
from typing import Any, Awaitable, Callable

from nio import (
    AsyncClient,
    AsyncClientConfig,
    KeysUploadResponse,
    LoginResponse,
    MegolmEvent,
    RoomMessageText,
    RoomSendResponse,
    UnknownEvent,
    WhoamiResponse,
)

_LOGGER = logging.getLogger(__name__)

MessageCallback = Callable[[Any, Any], Awaitable[None]]


class MatrixClientError(Exception):
    """Raised on unrecoverable Matrix client errors."""


class MatrixClient:
    """Async wrapper around nio.AsyncClient with E2EE and callback support."""

    def __init__(self, homeserver: str, user_id: str, store_path: str) -> None:
        self._homeserver = homeserver
        self._user_id = user_id
        self._store_path = store_path
        self._client: AsyncClient | None = None
        self._message_callbacks: list[MessageCallback] = []
        self._reaction_callbacks: list[MessageCallback] = []

    async def async_setup(self) -> None:
        """Create the nio client. Must be called before any other method."""
        os.makedirs(self._store_path, exist_ok=True)
        config = AsyncClientConfig(
            max_limit_exceeded=0,
            max_timeouts=0,
            store_sync_tokens=True,
            encryption_enabled=True,
        )
        self._client = AsyncClient(
            homeserver=self._homeserver,
            user=self._user_id,
            store_path=self._store_path,
            config=config,
        )
        self._client.add_event_callback(self._on_message, RoomMessageText)
        self._client.add_event_callback(self._on_unknown_event, UnknownEvent)
        self._client.add_event_callback(self._on_megolm, MegolmEvent)

    # ------------------------------------------------------------------
    # Login helpers
    # ------------------------------------------------------------------

    async def async_login_password(self, password: str, device_name: str) -> tuple[str, str]:
        """Login with username/password. Returns (access_token, device_id)."""
        resp = await self._client.login(password, device_name=device_name)
        if not isinstance(resp, LoginResponse):
            raise MatrixClientError(f"Login fehlgeschlagen: {resp}")
        await self._upload_keys_if_needed()
        return resp.access_token, resp.device_id

    async def async_restore_login(self, access_token: str, device_id: str) -> str:
        """Restore an existing session. Returns the device_id (fetched if empty)."""
        self._client.access_token = access_token
        self._client.user_id = self._user_id

        if not device_id:
            resp = await self._client.whoami()
            if isinstance(resp, WhoamiResponse):
                device_id = resp.device_id or ""
            else:
                _LOGGER.warning("whoami() failed: %s", resp)

        if device_id:
            self._client.restore_login(
                user_id=self._user_id,
                device_id=device_id,
                access_token=access_token,
            )
            self._client.load_store()

        await self._upload_keys_if_needed()
        return device_id

    async def async_whoami_device_id(self, access_token: str) -> tuple[str, str]:
        """Fetch (user_id, device_id) for a given access token (used during config flow)."""
        self._client.access_token = access_token
        resp = await self._client.whoami()
        if isinstance(resp, WhoamiResponse):
            return resp.user_id or self._user_id, resp.device_id or ""
        raise MatrixClientError(f"Konnte Gerätedaten nicht abrufen: {resp}")

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------

    async def async_get_joined_rooms(self) -> dict[str, str]:
        """Return {room_id: display_name} for all joined rooms."""
        await self._client.sync(timeout=10000)
        return {
            room_id: room.display_name or room_id
            for room_id, room in self._client.rooms.items()
        }

    async def async_send_message(self, room_id: str, message: str) -> bool:
        """Send a plain-text message. Returns True on success."""
        resp = await self._client.room_send(
            room_id=room_id,
            message_type="m.room.message",
            content={"msgtype": "m.text", "body": message},
        )
        if not isinstance(resp, RoomSendResponse):
            _LOGGER.error("send_message fehlgeschlagen (%s): %s", room_id, resp)
            return False
        return True

    async def async_sync_once(self, timeout_ms: int = 5000) -> None:
        """Perform one Matrix /sync call."""
        await self._client.sync(timeout=timeout_ms)

    async def async_close(self) -> None:
        """Close the HTTP session."""
        if self._client:
            await self._client.close()
            self._client = None

    # ------------------------------------------------------------------
    # Callback registration
    # ------------------------------------------------------------------

    def add_message_callback(self, cb: MessageCallback) -> None:
        self._message_callbacks.append(cb)

    def add_reaction_callback(self, cb: MessageCallback) -> None:
        self._reaction_callbacks.append(cb)

    # ------------------------------------------------------------------
    # Internal nio callbacks
    # ------------------------------------------------------------------

    async def _on_message(self, room: Any, event: RoomMessageText) -> None:
        for cb in self._message_callbacks:
            try:
                await cb(room, event)
            except Exception:
                _LOGGER.exception("Fehler im Nachrichten-Callback")

    async def _on_unknown_event(self, room: Any, event: UnknownEvent) -> None:
        if event.type == "m.reaction":
            for cb in self._reaction_callbacks:
                try:
                    await cb(room, event)
                except Exception:
                    _LOGGER.exception("Fehler im Reaktions-Callback")

    async def _on_megolm(self, room: Any, event: MegolmEvent) -> None:
        _LOGGER.debug("Undecryptable MegolmEvent in %s (fehlende Session-Keys?)", room.room_id)

    async def _upload_keys_if_needed(self) -> None:
        if self._client.should_upload_keys:
            resp = await self._client.keys_upload()
            if not isinstance(resp, KeysUploadResponse):
                _LOGGER.warning("E2EE Key-Upload fehlgeschlagen: %s", resp)
        await self._client.keys_query()
