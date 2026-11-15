"""Diagnostica scaricabile dall'interfaccia, con i dati personali oscurati."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_EMAIL, CONF_PASSWORD
from .coordinator import OctopusConfigEntry

TO_REDACT = {CONF_EMAIL, CONF_PASSWORD, "identifier", "account_number", "statistic_id", "number", "supply_point_id", "property_id"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: OctopusConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    accounts: list[dict[str, Any]] = []
    for account in (coordinator.data or {}).values():
        meters = []
        for meter in account.meters.values():
            point = meter.point
            meters.append(
                {
                    "kind": point.kind,
                    "identifier": point.identifier,
                    "status": point.status,
                    "enrolment_status": point.enrolment_status,
                    "supply_start": point.supply_start.isoformat() if point.supply_start else None,
                    "is_smart_meter": point.is_smart_meter,
                    "has_tariff": point.tariff is not None,
                    "readings_ok": meter.readings_ok,
                    "latest_reading_at": meter.latest_total[0].isoformat() if meter.latest_total else None,
                    "history_start": meter.history_start.isoformat() if meter.history_start else None,
                    "statistic_id": meter.statistic_id,
                }
            )
        accounts.append(
            {
                "number": account.info.number,
                "ledgers": len(account.info.ledgers),
                "meters": meters,
            }
        )
    return async_redact_data(
        {
            "entry": dict(entry.data),
            "last_update_success": coordinator.last_update_success,
            "accounts": accounts,
        },
        TO_REDACT,
    )
