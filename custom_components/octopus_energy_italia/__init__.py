"""Octopus Energy Italia: consumi di luce e gas, tariffe e saldo da Kraken."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import KrakenClient
from .const import CONF_EMAIL, CONF_PASSWORD, DOMAIN
from .coordinator import OctopusConfigEntry, OctopusCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: OctopusConfigEntry) -> bool:
    """Avvia l'integrazione per una voce di configurazione."""
    client = KrakenClient(
        async_get_clientsession(hass), entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD]
    )
    coordinator = OctopusCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    # Dispositivo "conto" (saldo) creato anche se il conto non ha ledger.
    registry = dr.async_get(hass)
    for number in coordinator.account_numbers:
        registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, f"account_{number}")},
            name=f"Octopus Energy conto {number}",
            manufacturer="Octopus Energy Italia",
            model="Conto cliente",
            entry_type=dr.DeviceEntryType.SERVICE,
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Prezzi cambiati nelle opzioni: si riparte, e i costi si ricalcolano.
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: OctopusConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: OctopusConfigEntry) -> bool:
    """Scarica l'integrazione. Le statistiche già scritte restano nel database."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
