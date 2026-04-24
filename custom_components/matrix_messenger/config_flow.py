"""Config flow and options flow for Matrix Messenger."""
from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    AUTH_METHOD_PASSWORD,
    AUTH_METHOD_TOKEN,
    CONF_ACCESS_TOKEN,
    CONF_AUTH_METHOD,
    CONF_DEVICE_ID,
    CONF_DEVICE_NAME,
    CONF_ENABLE_SYNC,
    CONF_HOMESERVER,
    CONF_PASSWORD,
    CONF_ROOMS,
    CONF_USERNAME,
    DEFAULT_DEVICE_NAME,
    DOMAIN,
)
from .matrix_client import MatrixClient, MatrixClientError

_LOGGER = logging.getLogger(__name__)


class MatrixMessengerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Multi-step config flow: server → credentials → room selection."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict = {}
        self._available_rooms: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Step 1: homeserver + auth method
    # ------------------------------------------------------------------

    async def async_step_user(self, user_input=None):
        errors: dict[str, str] = {}
        if user_input is not None:
            self._data.update(user_input)
            if user_input[CONF_AUTH_METHOD] == AUTH_METHOD_PASSWORD:
                return await self.async_step_credentials_password()
            return await self.async_step_credentials_token()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOMESERVER): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.URL)
                    ),
                    vol.Required(CONF_AUTH_METHOD, default=AUTH_METHOD_PASSWORD): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(
                                    value=AUTH_METHOD_PASSWORD,
                                    label="Benutzername + Passwort",
                                ),
                                selector.SelectOptionDict(
                                    value=AUTH_METHOD_TOKEN,
                                    label="Access Token",
                                ),
                            ],
                            mode=selector.SelectSelectorMode.LIST,
                        )
                    ),
                }
            ),
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Step 2a: password login
    # ------------------------------------------------------------------

    async def async_step_credentials_password(self, user_input=None):
        errors: dict[str, str] = {}
        if user_input is not None:
            store_path = self.hass.config.path(f".storage/{DOMAIN}")
            client = MatrixClient(
                homeserver=self._data[CONF_HOMESERVER],
                user_id=user_input[CONF_USERNAME],
                store_path=store_path,
            )
            try:
                await client.async_setup()
                token, device_id = await client.async_login_password(
                    user_input[CONF_PASSWORD],
                    user_input.get(CONF_DEVICE_NAME, DEFAULT_DEVICE_NAME),
                )
                self._available_rooms = await client.async_get_joined_rooms()
            except MatrixClientError as err:
                _LOGGER.error("Login fehlgeschlagen: %s", err)
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unerwarteter Fehler beim Login")
                errors["base"] = "unknown"
            else:
                self._data.update(
                    {
                        CONF_USERNAME: user_input[CONF_USERNAME],
                        CONF_ACCESS_TOKEN: token,
                        CONF_DEVICE_ID: device_id,
                        CONF_DEVICE_NAME: user_input.get(CONF_DEVICE_NAME, DEFAULT_DEVICE_NAME),
                    }
                )
                return await self.async_step_rooms()
            finally:
                await client.async_close()

        return self.async_show_form(
            step_id="credentials_password",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.TEXT,
                            autocomplete="username",
                        )
                    ),
                    vol.Required(CONF_PASSWORD): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.PASSWORD,
                            autocomplete="current-password",
                        )
                    ),
                    vol.Optional(CONF_DEVICE_NAME, default=DEFAULT_DEVICE_NAME): selector.TextSelector(),
                }
            ),
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Step 2b: token login
    # ------------------------------------------------------------------

    async def async_step_credentials_token(self, user_input=None):
        errors: dict[str, str] = {}
        if user_input is not None:
            store_path = self.hass.config.path(f".storage/{DOMAIN}")
            client = MatrixClient(
                homeserver=self._data[CONF_HOMESERVER],
                user_id=user_input[CONF_USERNAME],
                store_path=store_path,
            )
            try:
                await client.async_setup()
                # Fetch device_id from server using the provided token
                _, device_id = await client.async_whoami_device_id(
                    user_input[CONF_ACCESS_TOKEN]
                )
                device_id = await client.async_restore_login(
                    access_token=user_input[CONF_ACCESS_TOKEN],
                    device_id=device_id,
                )
                self._available_rooms = await client.async_get_joined_rooms()
            except MatrixClientError as err:
                _LOGGER.error("Token-Login fehlgeschlagen: %s", err)
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unerwarteter Fehler beim Token-Login")
                errors["base"] = "unknown"
            else:
                self._data.update(
                    {
                        CONF_USERNAME: user_input[CONF_USERNAME],
                        CONF_ACCESS_TOKEN: user_input[CONF_ACCESS_TOKEN],
                        CONF_DEVICE_ID: device_id,
                    }
                )
                return await self.async_step_rooms()
            finally:
                await client.async_close()

        return self.async_show_form(
            step_id="credentials_token",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
                    ),
                    vol.Required(CONF_ACCESS_TOKEN): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                    ),
                }
            ),
            errors=errors,
            description_placeholders={
                "token_help": "Den Access Token findest du in deinem Matrix-Client unter Einstellungen → Sicherheit → Sitzungen."
            },
        )

    # ------------------------------------------------------------------
    # Step 3: room selection
    # ------------------------------------------------------------------

    async def async_step_rooms(self, user_input=None):
        errors: dict[str, str] = {}
        if user_input is not None:
            selected = user_input.get(CONF_ROOMS, [])
            self._data[CONF_ROOMS] = {
                rid: self._available_rooms[rid]
                for rid in selected
                if rid in self._available_rooms
            }
            self._data[CONF_ENABLE_SYNC] = user_input.get(CONF_ENABLE_SYNC, False)
            return self.async_create_entry(
                title=self._data.get(CONF_USERNAME, self._data[CONF_HOMESERVER]),
                data=self._data,
            )

        room_options = [
            selector.SelectOptionDict(value=rid, label=f"{name}  ({rid})")
            for rid, name in self._available_rooms.items()
        ]

        return self.async_show_form(
            step_id="rooms",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ROOMS): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=room_options,
                            multiple=True,
                            mode=selector.SelectSelectorMode.LIST,
                        )
                    ),
                    vol.Optional(CONF_ENABLE_SYNC, default=False): selector.BooleanSelector(),
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return MatrixMessengerOptionsFlow(config_entry)


# ----------------------------------------------------------------------
# Options flow – reconfigure rooms and sync after initial setup
# ----------------------------------------------------------------------


class MatrixMessengerOptionsFlow(config_entries.OptionsFlow):
    """Allow re-selection of rooms and sync toggle without re-authentication."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry
        self._available_rooms: dict[str, str] = {}

    async def async_step_init(self, user_input=None):
        errors: dict[str, str] = {}

        if user_input is not None:
            selected = user_input.get(CONF_ROOMS, [])
            new_rooms = {
                rid: self._available_rooms.get(rid, rid)
                for rid in selected
            }
            return self.async_create_entry(
                title="",
                data={
                    CONF_ROOMS: new_rooms,
                    CONF_ENABLE_SYNC: user_input.get(CONF_ENABLE_SYNC, False),
                },
            )

        # Try to load fresh room list from the running client
        domain_data = self.hass.data.get(DOMAIN, {})
        entry_data = domain_data.get(self._entry.entry_id)
        if entry_data is not None:
            try:
                self._available_rooms = await entry_data.client.async_get_joined_rooms()
            except Exception:
                _LOGGER.warning("Konnte Räume nicht neu laden, zeige gespeicherte Auswahl.")

        if not self._available_rooms:
            self._available_rooms = _effective_rooms(self._entry)

        current_rooms = list(_effective_rooms(self._entry).keys())

        room_options = [
            selector.SelectOptionDict(value=rid, label=f"{name}  ({rid})")
            for rid, name in self._available_rooms.items()
        ]

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ROOMS, default=current_rooms): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=room_options,
                            multiple=True,
                            mode=selector.SelectSelectorMode.LIST,
                        )
                    ),
                    vol.Optional(
                        CONF_ENABLE_SYNC,
                        default=_effective_sync(self._entry),
                    ): selector.BooleanSelector(),
                }
            ),
            errors=errors,
        )


# ------------------------------------------------------------------
# Helpers shared between flows
# ------------------------------------------------------------------


def _effective_rooms(entry: config_entries.ConfigEntry) -> dict[str, str]:
    return entry.options.get(CONF_ROOMS, entry.data.get(CONF_ROOMS, {}))


def _effective_sync(entry: config_entries.ConfigEntry) -> bool:
    return entry.options.get(CONF_ENABLE_SYNC, entry.data.get(CONF_ENABLE_SYNC, False))
