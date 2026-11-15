"""Sensori: letture e consumi di POD/PDR, tariffe e saldo."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, KIND_ELECTRICITY, KIND_GAS
from .coordinator import AccountState, MeterState, OctopusConfigEntry, OctopusCoordinator
from .models import Ledger, Tariff

PARALLEL_UPDATES = 0

EUR = "EUR"


@dataclass(frozen=True, kw_only=True)
class MeterSensorDescription(SensorEntityDescription):
    value_fn: Callable[[MeterState], Any]
    attrs_fn: Callable[[MeterState], dict[str, Any]] | None = None
    exists_fn: Callable[[MeterState], bool] = lambda _m: True
    kinds: frozenset[str] = frozenset({KIND_ELECTRICITY, KIND_GAS})


def _tariff(meter: MeterState) -> Tariff | None:
    return meter.point.tariff


def _iso(value: datetime | date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _reading_attrs(meter: MeterState) -> dict[str, Any]:
    reading = meter.latest_reading
    attrs: dict[str, Any] = {
        "id_statistica": meter.statistic_id,
    }
    if reading is not None:
        attrs["tipo_lettura"] = reading.quality
        attrs["fonte"] = reading.source
        if reading.reading_date is not None:
            attrs["data_lettura"] = reading.reading_date.isoformat()
    if meter.history_start is not None:
        attrs["storico_statistiche_dal"] = meter.history_start.isoformat()
    return attrs


def _period_attrs(meter: MeterState) -> dict[str, Any]:
    period = meter.last_period
    if period is None:
        return {}
    return {"inizio": _iso(period.start), "fine": _iso(period.end)}


def _reading_time(meter: MeterState) -> datetime | date | None:
    reading = meter.latest_reading
    if reading is None:
        return None
    if reading.reading_date is not None:
        return reading.reading_date
    return meter.latest_total[0] if meter.latest_total else reading.ts


def _tariff_attrs(meter: MeterState) -> dict[str, Any]:
    tariff = _tariff(meter)
    if tariff is None:
        return {}
    return {
        "codice_prodotto": tariff.code,
        "tipo_prodotto": tariff.product_type,
        "unita_kraken": tariff.consumption_units,
        "contratto_dal": _iso(tariff.agreement_valid_from),
        "contratto_al": _iso(tariff.agreement_valid_to),
    }


METER_SENSORS: tuple[MeterSensorDescription, ...] = (
    MeterSensorDescription(
        key="meter_reading_electricity",
        translation_key="meter_reading_electricity",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=3,
        kinds=frozenset({KIND_ELECTRICITY}),
        value_fn=lambda m: m.latest_total[1] if m.latest_total else None,
        attrs_fn=_reading_attrs,
    ),
    MeterSensorDescription(
        key="meter_reading_gas",
        translation_key="meter_reading_gas",
        device_class=SensorDeviceClass.GAS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        suggested_display_precision=0,
        kinds=frozenset({KIND_GAS}),
        value_fn=lambda m: m.latest_total[1] if m.latest_total else None,
        attrs_fn=_reading_attrs,
    ),
    MeterSensorDescription(
        key="reading_time",
        translation_key="reading_time",
        device_class=SensorDeviceClass.TIMESTAMP,
        kinds=frozenset({KIND_ELECTRICITY}),
        value_fn=_reading_time,
    ),
    MeterSensorDescription(
        key="reading_date",
        translation_key="reading_date",
        device_class=SensorDeviceClass.DATE,
        kinds=frozenset({KIND_GAS}),
        value_fn=_reading_time,
    ),
    MeterSensorDescription(
        key="last_period_electricity",
        translation_key="last_period_electricity",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=3,
        kinds=frozenset({KIND_ELECTRICITY}),
        value_fn=lambda m: m.last_period.value if m.last_period else None,
        attrs_fn=_period_attrs,
    ),
    MeterSensorDescription(
        key="last_period_gas",
        translation_key="last_period_gas",
        device_class=SensorDeviceClass.GAS,
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        suggested_display_precision=0,
        kinds=frozenset({KIND_GAS}),
        value_fn=lambda m: m.last_period.value if m.last_period else None,
        attrs_fn=_period_attrs,
    ),
    MeterSensorDescription(
        key="price_electricity",
        translation_key="price_electricity",
        native_unit_of_measurement=f"{EUR}/{UnitOfEnergy.KILO_WATT_HOUR}",
        suggested_display_precision=4,
        kinds=frozenset({KIND_ELECTRICITY}),
        exists_fn=lambda m: m.point.tariff is not None,
        value_fn=lambda m: _tariff(m).consumption_charge if _tariff(m) else None,
        attrs_fn=_tariff_attrs,
    ),
    MeterSensorDescription(
        key="price_electricity_f2",
        translation_key="price_electricity_f2",
        native_unit_of_measurement=f"{EUR}/{UnitOfEnergy.KILO_WATT_HOUR}",
        suggested_display_precision=4,
        kinds=frozenset({KIND_ELECTRICITY}),
        exists_fn=lambda m: bool(m.point.tariff and m.point.tariff.consumption_charge_f2 is not None),
        value_fn=lambda m: _tariff(m).consumption_charge_f2 if _tariff(m) else None,
    ),
    MeterSensorDescription(
        key="price_electricity_f3",
        translation_key="price_electricity_f3",
        native_unit_of_measurement=f"{EUR}/{UnitOfEnergy.KILO_WATT_HOUR}",
        suggested_display_precision=4,
        kinds=frozenset({KIND_ELECTRICITY}),
        exists_fn=lambda m: bool(m.point.tariff and m.point.tariff.consumption_charge_f3 is not None),
        value_fn=lambda m: _tariff(m).consumption_charge_f3 if _tariff(m) else None,
    ),
    MeterSensorDescription(
        key="price_gas",
        translation_key="price_gas",
        native_unit_of_measurement=f"{EUR}/{UnitOfVolume.CUBIC_METERS}",
        suggested_display_precision=4,
        kinds=frozenset({KIND_GAS}),
        exists_fn=lambda m: m.point.tariff is not None,
        value_fn=lambda m: _tariff(m).consumption_charge if _tariff(m) else None,
        attrs_fn=_tariff_attrs,
    ),
    MeterSensorDescription(
        key="standing_charge",
        translation_key="standing_charge",
        native_unit_of_measurement=f"{EUR}/anno",
        suggested_display_precision=2,
        exists_fn=lambda m: m.point.tariff is not None,
        value_fn=lambda m: _tariff(m).annual_standing_charge if _tariff(m) else None,
        attrs_fn=lambda m: {"unita_kraken": _tariff(m).standing_charge_units} if _tariff(m) else {},
    ),
    MeterSensorDescription(
        key="product",
        translation_key="product",
        exists_fn=lambda m: m.point.tariff is not None,
        value_fn=lambda m: (_tariff(m).name or _tariff(m).code) if _tariff(m) else None,
        attrs_fn=_tariff_attrs,
    ),
    MeterSensorDescription(
        key="supply_status",
        translation_key="supply_status",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda m: m.point.status,
        attrs_fn=lambda m: {
            "stato_attivazione": m.point.enrolment_status,
            "fornitura_dal": _iso(m.point.supply_start),
            "contatore_elettronico": m.point.is_smart_meter,
        },
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OctopusConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = []
    for number, account in (coordinator.data or {}).items():
        for index, ledger in enumerate(account.info.ledgers):
            entities.append(LedgerBalanceSensor(coordinator, number, index, ledger))
        for key, meter in account.meters.items():
            for description in METER_SENSORS:
                if meter.point.kind in description.kinds and description.exists_fn(meter):
                    entities.append(MeterSensor(coordinator, number, key, description))
    async_add_entities(entities)


def _account_device(number: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, f"account_{number}")},
        name=f"Octopus Energy conto {number}",
        manufacturer="Octopus Energy Italia",
        model="Conto cliente",
        entry_type=DeviceEntryType.SERVICE,
    )


class MeterSensor(CoordinatorEntity[OctopusCoordinator], SensorEntity):
    """Sensore legato a un POD o PDR."""

    _attr_has_entity_name = True
    entity_description: MeterSensorDescription

    def __init__(
        self,
        coordinator: OctopusCoordinator,
        account_number: str,
        meter_key: str,
        description: MeterSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._account_number = account_number
        self._meter_key = meter_key
        account = (coordinator.data or {})[account_number]
        point = account.meters[meter_key].point  # creato solo per contatori esistenti
        label = "Luce POD" if point.kind == KIND_ELECTRICITY else "Gas PDR"
        self._attr_unique_id = f"{account_number}_{meter_key}_{description.key}".lower()
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, meter_key)},
            name=f"{label} {point.identifier}",
            manufacturer="Octopus Energy Italia",
            model="Contatore elettronico" if point.is_smart_meter else "Contatore",
            serial_number=point.identifier,
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def _meter(self) -> MeterState | None:
        account: AccountState | None = (self.coordinator.data or {}).get(self._account_number)
        return account.meters.get(self._meter_key) if account else None

    @property
    def available(self) -> bool:
        return super().available and self._meter is not None

    @property
    def native_value(self) -> Any:
        meter = self._meter
        return self.entity_description.value_fn(meter) if meter else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        meter = self._meter
        if meter is None or self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(meter)


class LedgerBalanceSensor(CoordinatorEntity[OctopusCoordinator], SensorEntity):
    """Saldo di un ledger del conto (Kraken: centesimi -> euro)."""

    _attr_has_entity_name = True
    _attr_translation_key = "balance"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = EUR
    _attr_suggested_display_precision = 2

    def __init__(
        self, coordinator: OctopusCoordinator, account_number: str, index: int, ledger: Ledger
    ) -> None:
        super().__init__(coordinator)
        self._account_number = account_number
        self._ledger_key = ledger.number or ledger.ledger_type or str(index)
        label = ledger.name or ledger.ledger_type or self._ledger_key
        self._attr_translation_placeholders = {"ledger": label}
        self._attr_unique_id = f"{account_number}_ledger_{self._ledger_key}".lower()
        self._attr_device_info = _account_device(account_number)

    @property
    def _ledger(self) -> Ledger | None:
        account = (self.coordinator.data or {}).get(self._account_number)
        if account is None:
            return None
        for index, ledger in enumerate(account.info.ledgers):
            if (ledger.number or ledger.ledger_type or str(index)) == self._ledger_key:
                return ledger
        return None

    @property
    def available(self) -> bool:
        return super().available and self._ledger is not None

    @property
    def native_value(self) -> float | None:
        ledger = self._ledger
        return ledger.balance if ledger else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        ledger = self._ledger
        if ledger is None:
            return None
        return {"tipo_ledger": ledger.ledger_type, "importo_dovuto": ledger.amount_owed}
