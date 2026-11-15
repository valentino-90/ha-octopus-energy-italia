"""Test del calcolo delle righe giornaliere per le statistiche (dashboard Energia).

Tutti i numeri sono inventati.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from oei_pure.energy_stats import (
    Anchor,
    DailyRow,
    booking_day,
    compute_cost_rows,
    compute_daily_rows,
    day_start,
    latest_period,
    latest_total,
    price_on,
    statistic_id_for,
)
from oei_pure.models import Reading, parse_gas_readings

TZ = ZoneInfo("Europe/Rome")
TODAY = date(2026, 11, 15)


# ---------------------------------------------------------------------------
# Strumenti
# ---------------------------------------------------------------------------


def monthly_change(rows: Iterable[DailyRow]) -> dict[str, float]:
    """Consumo per mese come lo calcola Home Assistant.

    Il recorder riduce le righe orarie per mese (ora locale) tenendo la `sum`
    dell'ultima riga del mese; il `change` di un mese è quella somma meno la
    somma dell'ultima riga precedente (0 se non ce n'è). Ogni riga giornaliera
    ha `start` alla mezzanotte locale del suo giorno, quindi appartiene al mese
    di `row.day`.
    """
    last_in_month: dict[str, float] = {}
    for row in sorted(rows, key=lambda r: r.day):
        last_in_month[row.day.strftime("%Y-%m")] = row.sum
    result: dict[str, float] = {}
    previous = 0.0
    for month in sorted(last_in_month):
        result[month] = round(last_in_month[month] - previous, 6)
        previous = last_in_month[month]
    return result


def three_bands(ts: datetime, f1: float, f2: float, f3: float) -> list[Reading]:
    return [
        Reading(ts=ts, value=f1, register="F1", quality="ACTUAL"),
        Reading(ts=ts, value=f2, register="F2", quality="ACTUAL"),
        Reading(ts=ts, value=f3, register="F3", quality="ACTUAL"),
    ]


# Letture "di fine mese" inventate (F1, F2, F3) al 31/08, 30/09 e 31/10.
# Settembre: 20 + 30 + 10 = 60 kWh. Ottobre: 40 + 50 + 20 = 110 kWh.
END_OF_MONTH_VALUES = {
    date(2026, 8, 31): (2060.0, 3150.0, 2470.0),
    date(2026, 9, 30): (2080.0, 3180.0, 2480.0),
    date(2026, 10, 31): (2120.0, 3230.0, 2500.0),
}


def _at(day: date, hour: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=TZ)


def _next_month_end(month_end: date) -> date:
    first_of_next = month_end + timedelta(days=1)
    return (first_of_next + timedelta(days=32)).replace(day=1) - timedelta(days=1)


# Possibili modi in cui Kraken può datare la lettura "al giorno D".
TIMESTAMP_CONVENTIONS: dict[str, Callable[[date], datetime]] = {
    # mezzanotte alla fine del giorno D (cioè 00:00 del giorno dopo)
    "fine_giorno_locale": lambda d: _at(d + timedelta(days=1)),
    # la stessa mezzanotte, ma scritta in UTC ("...T22:00:00Z" / "...T23:00:00Z")
    "fine_giorno_utc": lambda d: _at(d + timedelta(days=1)).astimezone(UTC),
    # a metà del giorno D
    "mezzogiorno": lambda d: _at(d, 12),
    # mezzanotte all'inizio del giorno D
    "inizio_giorno": lambda d: _at(d),
}


# ---------------------------------------------------------------------------
# Il mese giusto
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("convention", sorted(TIMESTAMP_CONVENTIONS))
def test_consumption_between_readings_lands_in_month_of_closing_reading(
    convention: str,
) -> None:
    """Il consumo fra la lettura del 30/09 e quella del 31/10 è di ottobre."""
    stamp = TIMESTAMP_CONVENTIONS[convention]
    readings = [
        r
        for day, values in END_OF_MONTH_VALUES.items()
        for r in three_bands(stamp(day), *values)
    ]

    rows = compute_daily_rows(readings, TZ, None, TODAY)
    months = monthly_change(rows)

    assert months["2026-10"] == pytest.approx(110.0)
    assert months["2026-09"] == pytest.approx(60.0)
    assert months.get("2026-08", 0.0) == pytest.approx(0.0)
    assert months.get("2026-11", 0.0) == pytest.approx(0.0)
    # Il totale non cambia: cambia solo la posizione.
    assert sum(months.values()) == pytest.approx(170.0)


def test_closing_reading_is_booked_on_its_own_day() -> None:
    """Con letture a mezzanotte, il delta va sul giorno appena concluso."""
    readings = [
        r
        for day, values in END_OF_MONTH_VALUES.items()
        for r in three_bands(_at(day + timedelta(days=1)), *values)
    ]
    rows = {row.day: row for row in compute_daily_rows(readings, TZ, None, TODAY)}

    assert rows[date(2026, 10, 30)].sum == pytest.approx(60.0)
    assert rows[date(2026, 10, 31)].sum == pytest.approx(170.0)
    assert rows[date(2026, 10, 31)].state == pytest.approx(2120.0 + 3230.0 + 2500.0)
    assert rows[date(2026, 9, 30)].sum == pytest.approx(60.0)
    assert rows[date(2026, 9, 29)].sum == pytest.approx(0.0)


def test_gas_reading_dates_land_in_their_month() -> None:
    nodes = [
        {"readingDate": "2026-08-31", "readingType": "ACTUAL", "consumptionValue": "830"},
        {"readingDate": "2026-09-30", "readingType": "ACTUAL", "consumptionValue": "838"},
        {"readingDate": "2026-10-31", "readingType": "ACTUAL", "consumptionValue": "850"},
    ]
    rows = compute_daily_rows(parse_gas_readings(nodes, TZ), TZ, None, TODAY)
    months = monthly_change(rows)

    assert months["2026-09"] == pytest.approx(8.0)
    assert months["2026-10"] == pytest.approx(12.0)
    assert months.get("2026-11", 0.0) == pytest.approx(0.0)


def test_daily_readings_each_day_gets_its_consumption() -> None:
    start = date(2026, 9, 28)
    readings = []
    value = 1000.0
    for offset in range(8):  # letture a mezzanotte dal 28/09 al 05/10
        day = start + timedelta(days=offset)
        readings.append(Reading(ts=_at(day), value=value))
        value += offset + 1  # consumo del giorno che inizia: 1, 2, 3, ...

    rows = {row.day: row for row in compute_daily_rows(readings, TZ, None, TODAY)}

    # La lettura delle 00:00 del 29/09 chiude il 28/09.
    assert rows[date(2026, 9, 27)].sum == pytest.approx(0.0)
    assert rows[date(2026, 9, 28)].sum - rows[date(2026, 9, 27)].sum == pytest.approx(1.0)
    assert rows[date(2026, 9, 30)].sum - rows[date(2026, 9, 29)].sum == pytest.approx(3.0)
    assert rows[date(2026, 10, 1)].sum - rows[date(2026, 9, 30)].sum == pytest.approx(4.0)


# ---------------------------------------------------------------------------
# Aggiornamenti successivi e reimportazione
# ---------------------------------------------------------------------------


def _anchor_from_store(store: dict[date, DailyRow], rewrite_days: int) -> Anchor | None:
    """Come OctopusCoordinator._async_get_anchor, ma su un dizionario."""
    if not store:
        return None
    anchor_day = max(store) - timedelta(days=rewrite_days)
    candidates = [d for d in store if anchor_day - timedelta(days=31) <= d <= anchor_day]
    if not candidates:
        return None
    row = store[max(candidates)]
    return Anchor(day=row.day, sum=row.sum, state=row.state)


def _local_day(reading: Reading) -> date:
    return reading.reading_date or reading.ts.astimezone(TZ).date()


def _simulate_hourly_updates(
    readings: list[tuple[Reading, date]],
    first_day: date,
    last_day: date,
    rewrite_days: int = 45,
    seed_days: int = 10,
) -> dict[date, DailyRow]:
    """Un aggiornamento al giorno, come il coordinatore, con letture in ritardo.

    `readings` contiene coppie (lettura, giorno in cui Kraken la rende
    disponibile). Le righe vengono "scritte" in un dizionario che fa la parte
    del database delle statistiche: una riga già presente viene sovrascritta.
    """
    store: dict[date, DailyRow] = {}
    today = first_day
    while today <= last_day:
        anchor = _anchor_from_store(store, rewrite_days)
        fetch_from = anchor.day - timedelta(days=seed_days) if anchor else None
        available = [
            r
            for r, available_on in readings
            if available_on <= today and (fetch_from is None or _local_day(r) >= fetch_from)
        ]
        for row in compute_daily_rows(available, TZ, anchor, today):
            store[row.day] = row
        today += timedelta(days=1)
    return store


def _monthly_three_band_readings() -> list[tuple[Reading, date]]:
    result = []
    values = [1000.0, 2000.0, 3000.0]
    day = date(2025, 12, 31)
    for month in range(11):  # fine dicembre 2025 ... fine ottobre 2026
        stamp = _at(day + timedelta(days=1))
        for reading in three_bands(stamp, *values):
            result.append((reading, day + timedelta(days=4)))  # arriva dopo 4 giorni
        values = [values[0] + 30 + month, values[1] + 45, values[2] + 25 + 2 * month]
        day = _next_month_end(day)
    return result


def _monthly_gas_readings() -> list[tuple[Reading, date]]:
    nodes = []
    value = 500.0
    day = date(2025, 12, 31)
    for month in range(11):
        nodes.append(
            {"readingDate": day.isoformat(), "readingType": "ACTUAL", "consumptionValue": str(value)}
        )
        value += 5 + month
        day = _next_month_end(day)
    return [(r, r.reading_date + timedelta(days=6)) for r in parse_gas_readings(nodes, TZ)]


def _daily_readings(bands: int) -> list[tuple[Reading, date]]:
    result = []
    values = [100.0 * (i + 1) for i in range(bands)]
    day = date(2026, 6, 1)
    for offset in range(150):
        stamp = _at(day + timedelta(days=offset + 1))
        for index in range(bands):
            register = f"F{index + 1}" if bands > 1 else None
            result.append(
                (Reading(ts=stamp, value=values[index], register=register), stamp.date() + timedelta(days=2))
            )
            values[index] += 1.0 + ((offset * (index + 3)) % 7) / 10
    return result


@pytest.mark.parametrize(
    "make_readings",
    [
        pytest.param(_monthly_three_band_readings, id="mensili_tre_fasce"),
        pytest.param(_monthly_gas_readings, id="mensili_gas"),
        pytest.param(lambda: _daily_readings(1), id="giornaliere_monoraria"),
        pytest.param(lambda: _daily_readings(3), id="giornaliere_tre_fasce"),
    ],
)
def test_incremental_updates_match_full_reimport(make_readings: Callable) -> None:
    """Gli aggiornamenti di ogni giorno portano alle stesse righe di un reimport."""
    readings = make_readings()
    first_day = min(_local_day(r) for r, _ in readings)
    last_day = max(available for _, available in readings) + timedelta(days=40)

    incremental = _simulate_hourly_updates(readings, first_day, last_day)
    full = {
        row.day: row
        for row in compute_daily_rows([r for r, _ in readings], TZ, None, last_day)
    }

    assert sorted(incremental) == sorted(full)
    for day, row in full.items():
        assert incremental[day].sum == pytest.approx(row.sum), day
        assert incremental[day].state == pytest.approx(row.state), day
    assert monthly_change(incremental.values()) == pytest.approx(monthly_change(full.values()))


def test_sparse_multi_register_update_keeps_consumption() -> None:
    """Letture mensili a fasce senza lettura d'appoggio nella finestra scaricata.

    L'ancora è a metà mese e la lettura precedente è fuori dai giorni di
    appoggio: il consumo del mese va comunque contato, a partire dal registro
    salvato nell'ancora.
    """
    previous_total = 1000.0 + 2000.0 + 3000.0
    anchor = Anchor(day=date(2026, 10, 15), sum=500.0, state=previous_total)
    readings = three_bands(_at(date(2026, 11, 1)), 1030.0, 2045.0, 3025.0)

    rows = {row.day: row for row in compute_daily_rows(readings, TZ, anchor, TODAY)}

    assert rows[date(2026, 10, 30)].sum == pytest.approx(500.0)
    assert rows[date(2026, 10, 31)].sum == pytest.approx(600.0)
    assert rows[date(2026, 10, 31)].state == pytest.approx(previous_total + 100.0)
    assert rows[date(2026, 11, 14)].sum == pytest.approx(600.0)


def test_reimport_is_idempotent() -> None:
    readings = [r for r, _ in _monthly_three_band_readings()]
    first = compute_daily_rows(readings, TZ, None, TODAY)
    second = compute_daily_rows(list(reversed(readings)), TZ, None, TODAY)
    assert first == second


def test_meter_reset_does_not_produce_negative_consumption() -> None:
    readings = [
        Reading(ts=_at(date(2026, 10, 1)), value=900.0),
        Reading(ts=_at(date(2026, 10, 2)), value=905.0),
        Reading(ts=_at(date(2026, 10, 3)), value=3.0),  # contatore sostituito
        Reading(ts=_at(date(2026, 10, 4)), value=7.0),
    ]
    rows = compute_daily_rows(readings, TZ, None, date(2026, 10, 10))
    sums = [row.sum for row in rows]
    assert sums == sorted(sums)
    assert rows[-1].sum == pytest.approx(5.0 + 0.0 + 4.0)


def test_rows_are_continuous_until_yesterday() -> None:
    readings = [
        Reading(ts=_at(date(2026, 11, 1)), value=10.0),
        Reading(ts=_at(date(2026, 11, 3)), value=14.0),
    ]
    rows = compute_daily_rows(readings, TZ, None, TODAY)
    days = [row.day for row in rows]
    assert days[0] == date(2026, 10, 31)
    assert days[-1] == TODAY - timedelta(days=1)
    assert days == [days[0] + timedelta(days=i) for i in range(len(days))]


def test_future_readings_are_skipped() -> None:
    readings = [
        Reading(ts=_at(date(2026, 11, 10)), value=10.0),
        Reading(ts=_at(date(2026, 11, 20)), value=99.0),
    ]
    rows = compute_daily_rows(readings, TZ, None, TODAY)
    assert rows[-1].day == TODAY - timedelta(days=1)
    assert rows[-1].sum == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Funzioni di supporto
# ---------------------------------------------------------------------------


def test_day_start_is_local_midnight_across_dst() -> None:
    assert day_start(date(2026, 10, 25), TZ) == datetime(2026, 10, 24, 22, tzinfo=UTC)
    assert day_start(date(2026, 10, 26), TZ) == datetime(2026, 10, 25, 23, tzinfo=UTC)
    assert day_start(date(2026, 3, 29), TZ) == datetime(2026, 3, 28, 23, tzinfo=UTC)
    assert day_start(date(2026, 3, 30), TZ) == datetime(2026, 3, 29, 22, tzinfo=UTC)


def test_booking_day_midnight_belongs_to_previous_day() -> None:
    assert booking_day(_at(date(2026, 11, 1)), TZ) == date(2026, 10, 31)
    assert booking_day(_at(date(2026, 10, 31), 12), TZ) == date(2026, 10, 31)
    assert booking_day(datetime(2026, 10, 31, 23, tzinfo=UTC), TZ) == date(2026, 10, 31)


def test_statistic_id_is_valid() -> None:
    assert (
        statistic_id_for("octopus_energy_italia", "electricity", "POD-TEST-01")
        == "octopus_energy_italia:electricity_pod_test_01"
    )
    assert statistic_id_for("octopus_energy_italia", "gas", "__X__") == "octopus_energy_italia:gas_x"


def test_latest_period_and_total() -> None:
    readings = three_bands(_at(date(2026, 10, 1)), 10.0, 20.0, 30.0) + three_bands(
        _at(date(2026, 11, 1)), 15.0, 22.0, 31.0
    )
    period = latest_period(readings)
    assert period is not None
    assert period.value == pytest.approx(8.0)
    assert period.start == _at(date(2026, 10, 1))
    assert period.end == _at(date(2026, 11, 1))
    assert latest_total(readings) == (_at(date(2026, 11, 1)), pytest.approx(68.0))


# ---------------------------------------------------------------------------
# Costi
# ---------------------------------------------------------------------------


def _rows(*sums: float, start: date = date(2026, 11, 1)) -> list[DailyRow]:
    return [DailyRow(day=start + timedelta(days=i), sum=s, state=None) for i, s in enumerate(sums)]


def test_price_on_uses_the_last_change_up_to_the_day() -> None:
    prices = [(date.min, 0.20), (date(2026, 12, 1), 0.25)]
    assert price_on(prices, date(2025, 1, 1)) == 0.20
    assert price_on(prices, date(2026, 11, 30)) == 0.20
    assert price_on(prices, date(2026, 12, 1)) == 0.25


def test_costs_from_scratch_follow_the_consumption() -> None:
    rows = _rows(0.0, 3.0, 5.0, 9.0)
    costs = compute_cost_rows(rows, [(date.min, 0.5)], None)
    assert costs == [
        (date(2026, 11, 1), 0.0),
        (date(2026, 11, 2), 1.5),
        (date(2026, 11, 3), 2.5),
        (date(2026, 11, 4), 4.5),
    ]


def test_a_new_price_counts_only_from_its_day() -> None:
    rows = _rows(10.0, 12.0, 15.0, start=date(2026, 11, 29))
    prices = [(date.min, 0.2), (date(2026, 12, 1), 0.3)]
    costs = compute_cost_rows(rows, prices, None)
    # 29/11: 10 kWh a 0,2; 30/11: 2 a 0,2; 1/12: 3 a 0,3.
    assert [c for _, c in costs] == pytest.approx([2.0, 2.4, 3.3])


def test_costs_restart_from_the_anchor() -> None:
    # Ancora: 100 kWh consumati e 20 € spesi; poi 4 kWh a 0,25.
    rows = _rows(102.0, 104.0, start=date(2026, 12, 2))
    costs = compute_cost_rows(rows, [(date.min, 0.25)], (100.0, 20.0))
    assert [c for _, c in costs] == pytest.approx([20.5, 21.0])


def test_a_lower_sum_costs_nothing() -> None:
    costs = compute_cost_rows(_rows(5.0, 4.0, 6.0), [(date.min, 1.0)], None)
    assert [c for _, c in costs] == pytest.approx([5.0, 5.0, 7.0])
