"""Conversione delle risposte Kraken in strutture dati semplici e tipizzate.

Qui non c'è I/O: solo funzioni pure, facili da verificare nei test.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from decimal import Decimal, InvalidOperation
from typing import Any

from .const import KIND_ELECTRICITY, KIND_GAS

_LOGGER = logging.getLogger(__name__)


def to_float(value: Any) -> float | None:
    """Converte un Decimal GraphQL (stringa o numero) in float, o None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(Decimal(str(value)))
    except (InvalidOperation, ValueError):
        return None


def parse_datetime(value: Any) -> datetime | None:
    """ISO 8601 con fuso -> datetime consapevole del fuso (None se non valido)."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # Kraken restituisce sempre il fuso; se mancasse assumiamo UTC.
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def parse_date(value: Any) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Strutture
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Reading:
    """Lettura cumulativa del contatore (registro), non un consumo."""

    ts: datetime  # istante a cui si riferisce il valore del registro
    value: float  # kWh (luce) o m³ (gas)
    register: str | None = None  # registro/fascia se Kraken lo indica
    quality: str | None = None  # ACTUAL/ESTIMATE/... (luce) o readingType (gas)
    source: str | None = None
    reading_date: date | None = None  # solo gas: data della lettura


@dataclass(frozen=True, slots=True)
class Tariff:
    code: str | None
    name: str | None
    product_type: str | None
    consumption_charge: float | None  # luce: F0/F1 o monoraria; gas: €/Smc
    consumption_charge_f2: float | None
    consumption_charge_f3: float | None
    annual_standing_charge: float | None
    consumption_units: str | None
    standing_charge_units: str | None
    product_valid_to: datetime | None
    agreement_valid_from: datetime | None
    agreement_valid_to: datetime | None


@dataclass(frozen=True, slots=True)
class SupplyPoint:
    kind: str  # KIND_ELECTRICITY o KIND_GAS
    identifier: str  # POD o PDR
    supply_point_id: str | None
    property_id: str | None
    status: str | None
    enrolment_status: str | None
    supply_start: date | None
    is_smart_meter: bool | None
    tariff: Tariff | None

    @property
    def key(self) -> str:
        return f"{self.kind}_{self.identifier}"


@dataclass(frozen=True, slots=True)
class Ledger:
    number: str | None
    name: str | None
    ledger_type: str | None
    balance: float | None  # euro (Kraken fornisce centesimi)
    amount_owed: float | None  # euro; positivo = il cliente deve pagare


@dataclass(slots=True)
class AccountInfo:
    number: str
    ledgers: list[Ledger] = field(default_factory=list)
    supply_points: list[SupplyPoint] = field(default_factory=list)


# --------------------------------------------------------------------------
# Parsing conto
# --------------------------------------------------------------------------


def _cents_to_eur(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return round(value / 100.0, 2)


def _active_agreement(supply_point: dict[str, Any]) -> dict[str, Any] | None:
    agreements = []
    conn = supply_point.get("agreements")
    if isinstance(conn, dict):
        for edge in conn.get("edges") or []:
            node = edge.get("node") if isinstance(edge, dict) else None
            if isinstance(node, dict):
                agreements.append(node)
    active = [a for a in agreements if a.get("isActive")]
    if not active:
        return None
    # Se più contratti risultano attivi prendiamo quello iniziato più di recente.
    return max(
        active,
        key=lambda a: parse_datetime(a.get("validFrom")) or datetime.min.replace(tzinfo=UTC),
    )


def _parse_tariff(supply_point: dict[str, Any]) -> Tariff | None:
    product = supply_point.get("product")
    if not isinstance(product, dict):
        return None
    prices = product.get("prices") if isinstance(product.get("prices"), dict) else {}
    agreement = _active_agreement(supply_point) or {}
    return Tariff(
        code=product.get("code"),
        name=product.get("displayName") or product.get("fullName"),
        product_type=prices.get("productType"),
        consumption_charge=to_float(prices.get("consumptionCharge")),
        consumption_charge_f2=to_float(prices.get("consumptionChargeF2")),
        consumption_charge_f3=to_float(prices.get("consumptionChargeF3")),
        annual_standing_charge=to_float(prices.get("annualStandingCharge")),
        consumption_units=prices.get("consumptionChargeUnits"),
        standing_charge_units=prices.get("annualStandingChargeUnits"),
        product_valid_to=parse_datetime(product.get("validTo")),
        agreement_valid_from=parse_datetime(agreement.get("validFrom")),
        agreement_valid_to=parse_datetime(agreement.get("validTo")),
    )


def parse_account(raw: dict[str, Any], account_number: str) -> AccountInfo:
    """Estrae saldo, POD, PDR e tariffe dalla risposta della query Account."""
    info = AccountInfo(number=raw.get("number") or account_number)

    for ledger in raw.get("ledgers") or []:
        if not isinstance(ledger, dict):
            continue
        info.ledgers.append(
            Ledger(
                number=ledger.get("number"),
                name=ledger.get("name"),
                ledger_type=ledger.get("ledgerType"),
                balance=_cents_to_eur(ledger.get("balance")),
                amount_owed=_cents_to_eur(ledger.get("amountOwedByCustomer")),
            )
        )

    seen: set[str] = set()
    for prop in raw.get("properties") or []:
        if not isinstance(prop, dict):
            continue
        property_id = prop.get("id")
        for kind, list_key, id_key in (
            (KIND_ELECTRICITY, "electricitySupplyPoints", "pod"),
            (KIND_GAS, "gasSupplyPoints", "pdr"),
        ):
            for sp in prop.get(list_key) or []:
                if not isinstance(sp, dict):
                    continue
                identifier = sp.get(id_key)
                if not isinstance(identifier, str) or not identifier:
                    continue
                point = SupplyPoint(
                    kind=kind,
                    identifier=identifier,
                    supply_point_id=sp.get("id"),
                    property_id=str(property_id) if property_id is not None else None,
                    status=sp.get("status"),
                    enrolment_status=sp.get("enrolmentStatus"),
                    supply_start=parse_date(sp.get("supplyStartDate")),
                    is_smart_meter=sp.get("isSmartMeter"),
                    tariff=_parse_tariff(sp),
                )
                if point.key in seen:
                    continue
                seen.add(point.key)
                info.supply_points.append(point)
    return info


# --------------------------------------------------------------------------
# Parsing letture
# --------------------------------------------------------------------------


def parse_electricity_readings(nodes: list[dict[str, Any]]) -> list[Reading]:
    """Letture POINT_IN_TIME del registro luce, ordinate per istante."""
    readings: list[Reading] = []
    for node in nodes:
        ts = parse_datetime(node.get("readAt"))
        value = to_float(node.get("value"))
        if ts is None or value is None:
            continue
        unit = str(node.get("unit") or "kWh").strip().lower()
        if unit == "wh":
            value = value / 1000.0
        elif unit != "kwh":
            _LOGGER.warning("Unità inattesa per la lettura luce (%s): lettura ignorata", unit)
            continue
        register = None
        quality = None
        meta = node.get("metaData")
        if isinstance(meta, dict):
            filters = meta.get("utilityFilters")
            if isinstance(filters, dict):
                register = filters.get("registerId") or None
                quality = filters.get("readingQuality") or None
        readings.append(
            Reading(
                ts=ts,
                value=value,
                register=register,
                quality=quality,
                source=node.get("source"),
            )
        )
    readings.sort(key=lambda r: (r.ts, r.register or ""))
    return readings


def gas_reading_ts(reading_date: date, tz: tzinfo) -> datetime:
    """La lettura gas del giorno D è attribuita alla fine del giorno D (ora locale)."""
    return datetime.combine(reading_date + timedelta(days=1), time(0), tzinfo=tz)


def parse_gas_readings(nodes: list[dict[str, Any]], tz: tzinfo) -> list[Reading]:
    """Letture del contatore gas (m³ cumulativi), ordinate per data."""
    readings: list[Reading] = []
    for node in nodes:
        day = parse_date(node.get("readingDate"))
        value = to_float(node.get("consumptionValue"))
        if day is None or value is None:
            continue
        readings.append(
            Reading(
                ts=gas_reading_ts(day, tz),
                value=value,
                quality=node.get("readingType"),
                source=node.get("readingSource"),
                reading_date=day,
            )
        )
    readings.sort(key=lambda r: r.ts)
    return readings


def dedupe_gas_readings(
    readings: list[Reading], priority: dict[str, int]
) -> list[Reading]:
    """Una sola lettura per giorno: vince il tipo con priorità più alta."""
    best: dict[date, Reading] = {}
    for reading in readings:
        day = reading.reading_date or reading.ts.date()
        current = best.get(day)
        if current is None or priority.get(reading.quality or "", 0) >= priority.get(
            current.quality or "", 0
        ):
            best[day] = reading
    return sorted(best.values(), key=lambda r: r.ts)
