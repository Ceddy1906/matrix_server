"""Async Matrix client wrapper with E2EE support via matrix-nio."""
from __future__ import annotations

import logging
import os
from typing import Any, Awaitable, Callable

from nio import (
    AsyncClient,
    AsyncClientConfig,
    JoinedRoomsResponse,
    KeysUploadResponse,
    LoginResponse,
    MegolmEvent,
    RoomCreateResponse,
    RoomGetStateEventResponse,
    RoomMessageText,
    RoomSendResponse,
    SyncResponse,
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
        self._encryption_enabled = False

    async def async_setup(self) -> None:
        """Create the nio client. Must be called before any other method."""
        os.makedirs(self._store_path, exist_ok=True)
        try:
            config = AsyncClientConfig(
                max_limit_exceeded=0,
                max_timeouts=0,
                store_sync_tokens=True,
                encryption_enabled=True,
            )
            self._encryption_enabled = True
        except ImportWarning:
            _LOGGER.warning(
                "E2EE-Abhängigkeiten (python-olm) nicht installiert – "
                "Verschlüsselung deaktiviert. Für E2EE 'matrix-nio[e2e]' installieren."
            )
            config = AsyncClientConfig(
                max_limit_exceeded=0,
                max_timeouts=0,
                store_sync_tokens=True,
                encryption_enabled=False,
            )
        self._client = AsyncClient(
            homeserver=self._homeserver,
            user=self._user_id,
            store_path=self._store_path,
            config=config,
        )
        self._client.add_event_callback(self._on_message, RoomMessageText)
        self._client.add_event_callback(self._on_unknown_event, UnknownEvent)
        if self._encryption_enabled:
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
            if self._encryption_enabled:
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
        rooms_resp = await self._client.joined_rooms()
        if not isinstance(rooms_resp, JoinedRoomsResponse):
            _LOGGER.error("Konnte Raumliste nicht laden: %s", rooms_resp)
            return {}
        room_ids = list(rooms_resp.rooms)
        return await self.async_get_room_names(room_ids)

    async def async_get_room_names(self, room_ids: list[str]) -> dict[str, str]:
        """Fetch room display names.

        Strategy:
        1. Direct state API (m.room.name / m.room.canonical_alias) – works
           without syncing and is unaffected by incremental sync tokens.
        2. full_state sync fallback – populates client.rooms with current
           state so display_name can be calculated from room name / member list.
        """
        result: dict[str, str] = {}

        # --- Strategy 1: direct state API ---
        for room_id in room_ids:
            name = room_id
            for event_type, field in (
                ("m.room.name", "name"),
                ("m.room.canonical_alias", "alias"),
            ):
                try:
                    resp = await self._client.room_get_state_event(room_id, event_type)
                    # Use duck-typing so we work across different nio versions
                    content = getattr(resp, "content", None)
                    _LOGGER.debug(
                        "state_event(%s, %s) → %s  content=%s",
                        room_id, event_type, type(resp).__name__, content,
                    )
                    if isinstance(content, dict):
                        val = str(content.get(field, "")).strip()
                        if val:
                            name = val
                            break
                except Exception as exc:
                    _LOGGER.debug("state_event(%s, %s) exception: %s", room_id, event_type, exc)
            result[room_id] = name

        # --- Strategy 2: full_state sync for rooms still unresolved ---
        unresolved = [rid for rid in room_ids if result.get(rid) == rid]
        if unresolved:
            _LOGGER.debug(
                "%d room(s) unresolved after state API – trying full_state sync", len(unresolved)
            )
            try:
                sync_resp = await self._client.sync(timeout=12000, full_state=True)
                if isinstance(sync_resp, SyncResponse):
                    for room_id in unresolved:
                        room = self._client.rooms.get(room_id)
                        if room:
                            name = getattr(room, "display_name", None) or getattr(room, "name", None)
                            _LOGGER.debug("  full_state display_name(%s) → %r", room_id, name)
                            if name and name != room_id:
                                result[room_id] = name
            except Exception as exc:
                _LOGGER.warning("full_state sync fehlgeschlagen: %s", exc)

        return result

    def get_room_display_names(self) -> dict[str, str]:
        """Return {room_id: display_name} from currently synced room state."""
        if not self._client:
            return {}
        result = {}
        for room_id, room in self._client.rooms.items():
            name = (
                getattr(room, "display_name", None)
                or getattr(room, "name", None)
                or room_id
            )
            result[room_id] = name
        return result

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

    async def async_sync_once(self, timeout_ms: int = 5000, full_state: bool = False) -> None:
        """Perform one Matrix /sync call.

        full_state=True forces the server to return complete room state (incl.
        m.room.name) regardless of any stored sync token.
        """
        resp = await self._client.sync(timeout=timeout_ms, full_state=full_state or None)
        if not isinstance(resp, SyncResponse):
            _LOGGER.debug("Sync fehlgeschlagen: %s", resp)

    async def async_send_to_user(self, user_id: str, message: str) -> bool:
        """Send a direct message to a Matrix user. Finds or creates a DM room."""
        room_id = await self._find_or_create_dm(user_id)
        if not room_id:
            return False
        return await self.async_send_message(room_id, message)

    async def _find_or_create_dm(self, user_id: str) -> str | None:
        """Return an existing room_id for user_id, or create one.

        Bridge portal rooms (mautrix-whatsapp, -signal, -telegram) have 3+
        members (user + puppet + bridge bot), so we search all joined rooms
        for one that contains the target user and prefer the one with the
        fewest members (most likely a direct/portal room).
        """
        await self.async_sync_once(timeout_ms=5000)

        my_id = self._client.user_id
        candidates: list[tuple[int, str]] = []
        for room_id, room in self._client.rooms.items():
            joined = [
                uid for uid, member in room.users.items()
                if getattr(member, "membership", None) == "join"
            ]
            if user_id in joined and my_id in joined:
                candidates.append((len(joined), room_id))

        if candidates:
            candidates.sort()  # fewest members first → most likely the direct/portal room
            return candidates[0][1]

        # No existing room found – try to create a DM (works for native Matrix
        # users; bridge puppets usually require the bridge bot to initiate).
        resp = await self._client.room_create(is_direct=True, invite=[user_id])
        if isinstance(resp, RoomCreateResponse):
            return resp.room_id
        _LOGGER.error("Konnte Raum für %s nicht erstellen: %s", user_id, resp)
        return None

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
        if not self._encryption_enabled:
            return
        if self._client.should_upload_keys:
            resp = await self._client.keys_upload()
            if not isinstance(resp, KeysUploadResponse):
                _LOGGER.warning("E2EE Key-Upload fehlgeschlagen: %s", resp)
        await self._client.keys_query()
