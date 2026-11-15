"""Config flow: accesso, ri-autenticazione e riconfigurazione."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import date
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.util import dt as dt_util

from .api import (
    KrakenAuthError,
    KrakenClient,
    KrakenConnectionError,
    KrakenError,
    KrakenRateLimitError,
)
from .const import (
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_PRICE_ELECTRICITY,
    CONF_PRICE_GAS,
    DOMAIN,
    PRICE_HISTORY_SUFFIX,
)

_LOGGER = logging.getLogger(__name__)

EMAIL_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username"))
PASSWORD_SELECTOR = TextSelector(
    TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
)


class OctopusEnergyItaliaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Configurazione tramite interfaccia di Home Assistant."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return OctopusOptionsFlow()

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


def _price_selector(unit: str) -> NumberSelector:
    return NumberSelector(
        # Passo libero: Home Assistant non accetta passi sotto 0,001, e un
        # prezzo ha spesso quattro decimali.
        NumberSelectorConfig(
            min=0, max=10, step="any", mode=NumberSelectorMode.BOX, unit_of_measurement=unit
        )
    )


def updated_prices(options: Mapping[str, Any], user_input: Mapping[str, Any], today: date) -> dict:
    """Le opzioni coi prezzi nuovi. Un prezzo cambiato si aggiunge alla sua
    storia da oggi; il primo vale per tutto lo storico; uno tolto ferma i costi."""
    out = dict(options)
    for key in (CONF_PRICE_ELECTRICITY, CONF_PRICE_GAS):
        history_key = key + PRICE_HISTORY_SUFFIX
        value = user_input.get(key)
        if value in (None, "") or float(value) <= 0:
            out.pop(key, None)
            out.pop(history_key, None)
            continue
        value = round(float(value), 6)
        history = [list(x) for x in out.get(history_key) or []]
        if not history:
            history = [[date.min.isoformat(), value]]
        elif history[-1][1] != value:
            if history[-1][0] == today.isoformat():
                history[-1][1] = value
            else:
                history.append([today.isoformat(), value])
        out[key] = value
        out[history_key] = history
    return out


class OctopusOptionsFlow(OptionsFlow):
    """Opzioni: i prezzi per le statistiche di costo di luce e gas."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        options = self.config_entry.options
        if user_input is not None:
            return self.async_create_entry(
                data=updated_prices(options, user_input, dt_util.now().date())
            )
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_PRICE_ELECTRICITY,
                    description={"suggested_value": options.get(CONF_PRICE_ELECTRICITY)},
                ): _price_selector("€/kWh"),
                vol.Optional(
                    CONF_PRICE_GAS,
                    description={"suggested_value": options.get(CONF_PRICE_GAS)},
                ): _price_selector("€/m³"),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
