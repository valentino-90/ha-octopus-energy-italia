"""Test della conversione delle risposte Kraken (funzioni pure, dati inventati)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from oei_pure.const import GAS_READING_PRIORITY
from oei_pure.models import (
    dedupe_gas_readings,
    gas_reading_ts,
    parse_account,
    parse_datetime,
    parse_electricity_readings,
    parse_gas_readings,
    to_float,
)

TZ = ZoneInfo("Europe/Rome")


def test_to_float() -> None:
    assert to_float("12.345") == pytest.approx(12.345)
    assert to_float(7) == 7.0
    assert to_float(None) is None
    assert to_float(True) is None
    assert to_float("abc") is None


def test_parse_datetime() -> None:
    assert parse_datetime("2026-10-31T23:00:00Z") == datetime(2026, 10, 31, 23, tzinfo=UTC)
    naive = parse_datetime("2026-10-31T23:00:00")
    assert naive is not None and naive.tzinfo is UTC
    assert parse_datetime("") is None
    assert parse_datetime("ieri") is None


def test_parse_electricity_readings_registers_and_units() -> None:
    nodes = [
        {
            "value": "1500",
            "unit": "Wh",
            "readAt": "2026-11-01T00:00:00+01:00",
            "metaData": {"utilityFilters": {"registerId": "F2", "readingQuality": "ACTUAL"}},
        },
        {
            "value": "10.5",
            "unit": "kWh",
            "readAt": "2026-11-01T00:00:00+01:00",
            "metaData": {"utilityFilters": {"registerId": "F1", "readingQuality": "ESTIMATE"}},
        },
        {"value": "3", "unit": "MWh", "readAt": "2026-11-01T00:00:00+01:00"},
        {"value": None, "unit": "kWh", "readAt": "2026-11-01T00:00:00+01:00"},
        {"value": "4", "unit": "kWh", "readAt": "non una data"},
    ]
    readings = parse_electricity_readings(nodes)

    assert [(r.register, r.value, r.quality) for r in readings] == [
        ("F1", 10.5, "ESTIMATE"),
        ("F2", 1.5, "ACTUAL"),
    ]


def test_gas_reading_ts_is_end_of_reading_day() -> None:
    assert gas_reading_ts(date(2026, 10, 31), TZ) == datetime(2026, 11, 1, tzinfo=TZ)


def test_parse_and_dedupe_gas_readings() -> None:
    nodes = [
        {"readingDate": "2026-10-31", "readingType": "ESTIMATE", "consumptionValue": "900"},
        {"readingDate": "2026-10-31", "readingType": "ACTUAL", "consumptionValue": "850"},
        {"readingDate": "2026-09-30", "readingType": "CUSTOMER_SELF_READING", "consumptionValue": "838"},
        {"readingDate": "2026-09-30", "readingType": "RECOMPUTED", "consumptionValue": "839"},
        {"readingDate": None, "readingType": "ACTUAL", "consumptionValue": "1"},
    ]
    readings = dedupe_gas_readings(parse_gas_readings(nodes, TZ), GAS_READING_PRIORITY)

    assert [(r.reading_date, r.quality, r.value) for r in readings] == [
        (date(2026, 9, 30), "RECOMPUTED", 839.0),
        (date(2026, 10, 31), "ACTUAL", 850.0),
    ]


def test_parse_account() -> None:
    raw = {
        "number": "A-TEST0001",
        "ledgers": [
            {
                "number": "L-TEST-1",
                "name": "Luce e gas",
                "ledgerType": "ITA_ENERGY",
                "balance": -12345,
                "amountOwedByCustomer": 4200,
            }
        ],
        "properties": [
            {
                "id": 77,
                "electricitySupplyPoints": [
                    {
                        "id": "sp-1",
                        "pod": "POD-TEST-LUCE",
                        "status": "ON_SUPPLY",
                        "supplyStartDate": "2025-05-01",
                        "isSmartMeter": True,
                        "product": {
                            "code": "PROD-TEST",
                            "displayName": "Offerta di prova",
                            "prices": {
                                "productType": "FIXED",
                                "consumptionCharge": "0.1234",
                                "consumptionChargeF2": "0.1111",
                                "consumptionChargeF3": None,
                                "annualStandingCharge": "60",
                            },
                        },
                        "agreements": {
                            "edges": [
                                {"node": {"id": "1", "isActive": False, "validFrom": "2025-05-01T00:00:00+02:00"}},
                                {"node": {"id": "2", "isActive": True, "validFrom": "2026-05-01T00:00:00+02:00"}},
                            ]
                        },
                    },
                    {"id": "sp-dup", "pod": "POD-TEST-LUCE"},
                ],
                "gasSupplyPoints": [{"id": "sp-2", "pdr": "PDR-TEST-GAS"}, {"id": "sp-3", "pdr": ""}],
            }
        ],
    }
    info = parse_account(raw, "A-TEST0001")

    assert info.number == "A-TEST0001"
    assert info.ledgers[0].balance == pytest.approx(-123.45)
    assert info.ledgers[0].amount_owed == pytest.approx(42.0)
    assert [p.key for p in info.supply_points] == [
        "electricity_POD-TEST-LUCE",
        "gas_PDR-TEST-GAS",
    ]
    electricity = info.supply_points[0]
    assert electricity.property_id == "77"
    assert electricity.supply_start == date(2025, 5, 1)
    assert electricity.tariff is not None
    assert electricity.tariff.consumption_charge == pytest.approx(0.1234)
    assert electricity.tariff.consumption_charge_f3 is None
    assert electricity.tariff.agreement_valid_from == datetime(2026, 5, 1, tzinfo=ZoneInfo("Etc/GMT-2"))
    assert info.supply_points[1].tariff is None
