"""Config flow: accesso, ri-autenticazione e riconfigurazione."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    KrakenAuthError,
    KrakenClient,
    KrakenConnectionError,
    KrakenError,
    KrakenRateLimitError,
)
from .const import CONF_EMAIL, CONF_PASSWORD, DOMAIN

_LOGGER = logging.getLogger(__name__)

EMAIL_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username"))
PASSWORD_SELECTOR = TextSelector(
    TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
)


class OctopusEnergyItaliaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Configurazione tramite interfaccia di Home Assistant."""

    VERSION = 1

    async def _async_validate(self, email: str, password: str) -> tuple[list[str], dict[str, str]]:
        """Prova l'accesso. Restituisce (numeri di conto, errori per il form)."""
        client = KrakenClient(async_get_clientsession(self.hass), email, password)
        try:
            await client.authenticate()
            accounts = await client.async_get_account_numbers()
        except KrakenAuthError:
            return [], {"base": "invalid_auth"}
        except KrakenRateLimitError:
            return [], {"base": "rate_limited"}
        except KrakenConnectionError:
            return [], {"base": "cannot_connect"}
        except KrakenError:
            return [], {"base": "unknown"}
        except Exception:  # noqa: BLE001 - qualunque imprevisto non deve rompere il form
            _LOGGER.exception("Errore imprevisto durante la verifica dell'accesso")
            return [], {"base": "unknown"}
        if not accounts:
            return [], {"base": "no_accounts"}
        return accounts, {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            accounts, errors = await self._async_validate(email, user_input[CONF_PASSWORD])
            if not errors:
                await self.async_set_unique_id(accounts[0])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Octopus Energy Italia {accounts[0]}",
                    data={CONF_EMAIL: email, CONF_PASSWORD: user_input[CONF_PASSWORD]},
                )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(
                    {
                        vol.Required(CONF_EMAIL): EMAIL_SELECTOR,
                        vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
                    }
                ),
                {CONF_EMAIL: user_input[CONF_EMAIL]} if user_input else {},
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            accounts, errors = await self._async_validate(email, user_input[CONF_PASSWORD])
            if not errors:
                if entry.unique_id not in accounts:
                    return self.async_abort(reason="wrong_account")
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={CONF_EMAIL: email, CONF_PASSWORD: user_input[CONF_PASSWORD]},
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(
                    {
                        vol.Required(CONF_EMAIL): EMAIL_SELECTOR,
                        vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
                    }
                ),
                {CONF_EMAIL: entry.data.get(CONF_EMAIL, "")},
            ),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            accounts, errors = await self._async_validate(email, user_input[CONF_PASSWORD])
            if not errors:
                if entry.unique_id not in accounts:
                    return self.async_abort(reason="wrong_account")
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={CONF_EMAIL: email, CONF_PASSWORD: user_input[CONF_PASSWORD]},
                )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(
                    {
                        vol.Required(CONF_EMAIL): EMAIL_SELECTOR,
                        vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
                    }
                ),
                {CONF_EMAIL: entry.data.get(CONF_EMAIL, "")},
            ),
            errors=errors,
        )
