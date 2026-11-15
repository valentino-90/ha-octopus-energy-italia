"""Test con Home Assistant vero: configurazione, sensori e statistiche.

Il test sui mesi usa il recorder vero (database in memoria): il coordinatore
scarica letture finte, scrive le statistiche esterne e poi le leggiamo come fa
la dashboard Energia, con `statistics_during_period` per mese e tipo `change`.

Richiede pytest-homeassistant-custom-component (vedi requirements_test.txt).
Tutti i dati sono inventati.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.components.recorder import Recorder, get_instance  # noqa: E402
from homeassistant.components.recorder.statistics import (  # noqa: E402
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntryState  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.data_entry_flow import FlowResultType  # noqa: E402
from homeassistant.util import dt as dt_util  # noqa: E402
from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402
from pytest_homeassistant_custom_component.components.recorder.common import (  # noqa: E402
    async_wait_recording_done,
)

from custom_components.octopus_energy_italia.api import KrakenAuthError  # noqa: E402
from custom_components.octopus_energy_italia.const import DOMAIN  # noqa: E402
from custom_components.octopus_energy_italia.coordinator import (  # noqa: E402
    OctopusCoordinator,
)
from custom_components.octopus_energy_italia.energy_stats import (  # noqa: E402
    statistic_id_for,
)

TZ = ZoneInfo("Europe/Rome")
ACCOUNT = "A-TEST0001"
POD = "POD-TEST-LUCE"
PDR = "PDR-TEST-GAS"
PROPERTY = "property-test-1"

ELECTRICITY_ID = statistic_id_for(DOMAIN, "electricity", POD)
GAS_ID = statistic_id_for(DOMAIN, "gas", PDR)

# Letture di fine mese inventate: (giorno, F1, F2, F3, gas m³).
READINGS = [
    (date(2026, 8, 31), 2060.0, 3150.0, 2470.0, 830.0),
    (date(2026, 9, 30), 2080.0, 3180.0, 2480.0, 838.0),  # settembre: 60 kWh, 8 m³
    (date(2026, 10, 31), 2120.0, 3230.0, 2500.0, 850.0),  # ottobre: 110 kWh, 12 m³
    (date(2026, 11, 30), 2150.0, 3290.0, 2530.0, 870.0),  # novembre: 120 kWh, 20 m³
]


def _electricity_nodes(day: date, f1: float, f2: float, f3: float) -> list[dict[str, Any]]:
    # Lettura "al giorno D": mezzanotte alla fine del giorno, ora locale.
    read_at = datetime(day.year, day.month, day.day, tzinfo=TZ) + timedelta(days=1)
    return [
        {
            "value": str(value),
            "unit": "kWh",
            "readAt": read_at.isoformat(),
            "source": "TEST",
            "metaData": {"utilityFilters": {"registerId": register, "readingQuality": "ACTUAL"}},
        }
        for register, value in (("F1", f1), ("F2", f2), ("F3", f3))
    ]


class FakeKraken:
    """Client finto: risponde come KrakenClient, con i filtri per data."""

    def __init__(self, *, valid: bool = True) -> None:
        self.available_until = date.min
        self.valid = valid

    async def authenticate(self) -> None:
        if not self.valid:
            raise KrakenAuthError("credenziali rifiutate")

    def _visible(self) -> list[tuple[date, float, float, float, float]]:
        return [r for r in READINGS if r[0] <= self.available_until]

    async def async_get_account_numbers(self) -> list[str]:
        return [ACCOUNT]

    async def async_get_account(self, number: str) -> tuple[dict[str, Any], list]:
        return (
            {
                "number": ACCOUNT,
                "ledgers": [],
                "properties": [
                    {
                        "id": PROPERTY,
                        "electricitySupplyPoints": [{"id": "sp-e", "pod": POD}],
                        "gasSupplyPoints": [{"id": "sp-g", "pdr": PDR}],
                    }
                ],
            },
            [],
        )

    async def async_get_electricity_readings(
        self, property_id: str, pod: str, start_on: date | None
    ) -> list[dict[str, Any]]:
        nodes = [n for r in self._visible() for n in _electricity_nodes(*r[:4])]
        return [
            n for n in nodes if start_on is None or date.fromisoformat(n["readAt"][:10]) >= start_on
        ]

    async def async_get_gas_readings(
        self, account_number: str, pdr: str, date_from: date | None
    ) -> list[dict[str, Any]]:
        return [
            {"readingDate": r[0].isoformat(), "readingType": "ACTUAL", "consumptionValue": str(r[4])}
            for r in self._visible()
            if date_from is None or r[0] >= date_from
        ]


def _monthly_change(hass: HomeAssistant, statistic_id: str) -> dict[str, float]:
    stats = statistics_during_period(
        hass,
        dt_util.as_utc(datetime(2026, 1, 1, tzinfo=TZ)),
        dt_util.as_utc(datetime(2027, 1, 1, tzinfo=TZ)),
        {statistic_id},
        "month",
        None,
        {"change"},
    )
    return {
        dt_util.utc_from_timestamp(row["start"]).astimezone(TZ).strftime("%Y-%m"): round(
            row["change"], 6
        )
        for row in stats.get(statistic_id, [])
    }


async def _update(hass: HomeAssistant, coordinator: OctopusCoordinator) -> dict[str, dict]:
    await coordinator._async_update_data()
    await async_wait_recording_done(hass)
    instance = get_instance(hass)
    return {
        "luce": await instance.async_add_executor_job(_monthly_change, hass, ELECTRICITY_ID),
        "gas": await instance.async_add_executor_job(_monthly_change, hass, GAS_ID),
    }


async def test_months_in_recorder(recorder_mock: Recorder, hass: HomeAssistant, freezer) -> None:
    """Primo import, poi aggiornamenti successivi: ogni consumo resta nel suo mese."""
    await hass.config.async_set_time_zone("Europe/Rome")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"email": "test@example.invalid", "password": "test"},
        unique_id=ACCOUNT,
    )
    entry.add_to_hass(hass)
    client = FakeKraken()
    coordinator = OctopusCoordinator(hass, entry, client)  # type: ignore[arg-type]
    coordinator.account_numbers = [ACCOUNT]

    # 1) Primo import (nessuna statistica): letture fino al 31/10.
    freezer.move_to(datetime(2026, 11, 15, 10, tzinfo=TZ))
    client.available_until = date(2026, 10, 31)
    months = await _update(hass, coordinator)
    assert months["luce"]["2026-09"] == pytest.approx(60.0)
    assert months["luce"]["2026-10"] == pytest.approx(110.0)
    assert months["luce"].get("2026-11", 0.0) == pytest.approx(0.0)
    assert months["gas"]["2026-09"] == pytest.approx(8.0)
    assert months["gas"]["2026-10"] == pytest.approx(12.0)

    # 2) Aggiornamento con ancora: arriva la lettura del 30/11.
    freezer.move_to(datetime(2026, 12, 28, 10, tzinfo=TZ))
    client.available_until = date(2026, 11, 30)
    months = await _update(hass, coordinator)
    assert months["luce"]["2026-10"] == pytest.approx(110.0)
    assert months["luce"]["2026-11"] == pytest.approx(120.0)
    assert months["gas"]["2026-11"] == pytest.approx(20.0)

    # 3) Aggiornamento in cui la lettura d'appoggio (31/10) è fuori dalla
    #    finestra scaricata: novembre deve restare com'è.
    freezer.move_to(datetime(2027, 1, 5, 10, tzinfo=TZ))
    months = await _update(hass, coordinator)
    assert months["luce"]["2026-09"] == pytest.approx(60.0)
    assert months["luce"]["2026-10"] == pytest.approx(110.0)
    assert months["luce"]["2026-11"] == pytest.approx(120.0)
    assert months["luce"].get("2026-12", 0.0) == pytest.approx(0.0)
    assert months["gas"]["2026-11"] == pytest.approx(20.0)


async def test_setup_creates_sensors(
    recorder_mock: Recorder, hass: HomeAssistant, enable_custom_integrations, freezer
) -> None:
    """L'integrazione si carica, crea i sensori e si scarica."""
    await hass.config.async_set_time_zone("Europe/Rome")
    freezer.move_to(datetime(2026, 11, 15, 10, tzinfo=TZ))
    client = FakeKraken()
    client.available_until = date(2026, 10, 31)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"email": "test@example.invalid", "password": "test"},
        unique_id=ACCOUNT,
    )
    entry.add_to_hass(hass)

    with patch("custom_components.octopus_energy_italia.KrakenClient", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    by_statistic = {
        state.attributes.get("id_statistica"): state
        for state in hass.states.async_all("sensor")
        if state.attributes.get("id_statistica")
    }
    assert float(by_statistic[ELECTRICITY_ID].state) == pytest.approx(2120.0 + 3230.0 + 2500.0)
    assert by_statistic[ELECTRICITY_ID].attributes["unit_of_measurement"] == "kWh"
    assert float(by_statistic[GAS_ID].state) == pytest.approx(850.0)

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


@pytest.mark.parametrize(
    ("valid", "expected"),
    [(True, FlowResultType.CREATE_ENTRY), (False, FlowResultType.FORM)],
)
async def test_config_flow(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    enable_custom_integrations,
    valid: bool,
    expected: FlowResultType,
) -> None:
    """Accesso riuscito: voce creata col numero di conto; rifiutato: errore nel form."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.FORM

    with (
        patch(
            "custom_components.octopus_energy_italia.config_flow.KrakenClient",
            return_value=FakeKraken(valid=valid),
        ),
        patch("custom_components.octopus_energy_italia.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"email": " test@example.invalid ", "password": "test"}
        )

    assert result["type"] is expected
    if valid:
        assert result["title"] == f"Octopus Energy Italia {ACCOUNT}"
        assert result["data"]["email"] == "test@example.invalid"
    else:
        assert result["errors"] == {"base": "invalid_auth"}
