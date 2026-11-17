"""Helpers for handling an expired or revoked Matrix access token."""
from __future__ import annotations

from collections.abc import Awaitable
from typing import TypeVar

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .matrix_client import MatrixAuthError

_T = TypeVar("_T")


async def run_with_reauth(
    hass: HomeAssistant, entry: ConfigEntry, awaitable: Awaitable[_T]
) -> _T:
    """Await a client call; on a rejected token start reauth and raise a clear error."""
    try:
        return await awaitable
    except MatrixAuthError as err:
        entry.async_start_reauth(hass)
        raise HomeAssistantError(
            "Die Matrix-Anmeldung ist abgelaufen. Bitte in den Integrationseinstellungen "
            "unter 'Matrix Messenger' neu authentifizieren."
        ) from err
