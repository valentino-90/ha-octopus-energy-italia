"""Statistiche a lungo termine (dashboard Energia) dalle letture del contatore.

Idea in breve
-------------
Kraken fornisce letture *cumulative* del registro (kWh per la luce, m³ per il
gas). Il consumo di un intervallo è la differenza fra due letture successive e
viene attribuito al giorno locale in cui l'intervallo termina (per letture
giornaliere a mezzanotte: il giorno appena concluso; per una lettura gas datata
D: il giorno D).

Si producono righe *giornaliere e continue* (una per giorno, alle 00:00 locali)
con `sum` = consumo cumulato e `state` = valore del registro. Per Home Assistant
la `sum` di una riga è il totale alla *fine* del suo periodo, quindi il consumo
fra la lettura del 30/09 e quella del 31/10 sta sulla riga del 31/10 e la
dashboard lo mostra in ottobre. Le righe sono
"statistiche esterne" (`octopus_energy_italia:...`), separate dai sensori:
così i consumi finiscono nel giorno giusto anche se arrivano con giorni di
ritardo, e lo storico può essere importato a posteriori.

Stabilità: dopo il primo import completo, ad ogni aggiornamento si riparte da
una riga "ancora" già scritta (STATS_REWRITE_DAYS giorni prima dell'ultima) e si
riscrivono solo i giorni successivi. Le righe più vecchie non vengono mai
toccate, quindi lo storico non può "saltare" anche se Kraken in futuro dovesse
restituire meno storico di oggi; le rettifiche recenti invece vengono recepite.
Se fra le letture scaricate non c'è quella che precede la finestra (letture
rade, per esempio mensili), il consumo della prima lettura nuova si calcola dal
registro salvato nell'ancora, anche con più fasce (F1/F2/F3).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo

from .models import Reading

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Anchor:
    """Ultima riga già scritta da cui ripartire (fine del giorno `day`)."""

    day: date
    sum: float
    state: float | None


@dataclass(frozen=True, slots=True)
class DailyRow:
    day: date
    sum: float
    state: float | None


@dataclass(frozen=True, slots=True)
class PeriodConsumption:
    start: datetime
    end: datetime
    value: float


def statistic_id_for(domain: str, kind: str, identifier: str) -> str:
    """ID valido per HA: solo [a-z0-9_], niente '__' né '_' agli estremi."""
    object_id = re.sub(r"[^a-z0-9]+", "_", f"{kind}_{identifier}".lower()).strip("_")
    return f"{domain}:{object_id}"


def day_start(day: date, tz: tzinfo) -> datetime:
    """Mezzanotte locale del giorno, allineata all'ora intera in UTC."""
    start = datetime.combine(day, time(0), tzinfo=tz).astimezone(UTC)
    return start.replace(minute=0, second=0, microsecond=0)


def booking_day(ts: datetime, tz: tzinfo) -> date:
    """Giorno locale a cui attribuire il consumo che termina all'istante ts."""
    return (ts.astimezone(tz) - timedelta(microseconds=1)).date()


def _step(previous: float, value: float) -> float:
    """Consumo fra due valori del registro; mai negativo."""
    step = value - previous
    if step < 0:
        # debug: la stessa coppia viene rielaborata ad ogni aggiornamento
        _LOGGER.debug(
            "Lettura del contatore inferiore alla precedente (%.3f -> %.3f): "
            "considerata come azzeramento, nessun consumo negativo registrato",
            previous,
            value,
        )
        return 0.0
    return step


def _group_by_instant(readings: Iterable[Reading]) -> list[tuple[datetime, dict[str | None, float]]]:
    grouped: dict[datetime, dict[str | None, float]] = {}
    for reading in sorted(readings, key=lambda r: r.ts):
        grouped.setdefault(reading.ts, {})[reading.register] = reading.value
    return sorted(grouped.items(), key=lambda item: item[0])


def compute_daily_rows(
    readings: Iterable[Reading],
    tz: tzinfo,
    anchor: Anchor | None,
    today: date,
) -> list[DailyRow]:
    """Calcola le righe giornaliere da scrivere.

    - senza `anchor`: ricostruzione completa dalla prima lettura (sum parte da 0);
    - con `anchor`: righe dal giorno successivo all'ancora, sum riparte da lì.
      Le letture fino alla fine del giorno ancora servono solo come base.

    Un registro che cala (sostituzione contatore, rettifica) non produce consumi
    negativi: si riparte dal nuovo valore, come fa HA con `total_increasing`.
    Le righe arrivano fino a ieri (o all'ultimo giorno con letture, se oggi).
    """
    boundary = day_start(anchor.day + timedelta(days=1), tz) if anchor else None
    last: dict[str | None, float] = {}
    booked: dict[date, float] = {}
    state_by_day: dict[date, float] = {}
    first_day: date | None = None

    for ts, values in _group_by_instant(readings):
        if boundary is not None and ts <= boundary:
            last.update(values)
            continue
        day = booking_day(ts, tz)
        if day > today:
            # Lettura "nel futuro" (orologi sfasati): la consideriamo al prossimo giro.
            continue
        if first_day is None:
            first_day = day
        if not last and anchor is not None and anchor.state is not None:
            # Nessuna lettura di appoggio fra quelle scaricate (letture rade, per
            # esempio mensili): si parte dal registro salvato nell'ancora, che è
            # la somma di tutti i registri. Così vale anche con più fasce, che
            # altrimenti perderebbero il consumo di questa lettura.
            delta = _step(anchor.state, sum(values.values()))
            last.update(values)
        else:
            delta = 0.0
            for register, value in values.items():
                previous = last.get(register)
                if previous is not None:
                    delta += _step(previous, value)
                last[register] = value
        booked[day] = booked.get(day, 0.0) + delta
        state_by_day[day] = sum(last.values())

    if anchor is not None:
        current_day = anchor.day + timedelta(days=1)
        total = anchor.sum
        state = anchor.state
    elif first_day is not None:
        current_day = first_day
        total = 0.0
        state = None
    else:
        return []

    last_day = max([today - timedelta(days=1), *booked])
    rows: list[DailyRow] = []
    while current_day <= last_day:
        total += booked.get(current_day, 0.0)
        state = state_by_day.get(current_day, state)
        rows.append(
            DailyRow(
                day=current_day,
                sum=round(total, 6),
                state=round(state, 6) if state is not None else None,
            )
        )
        current_day += timedelta(days=1)
    return rows


def latest_period(readings: Iterable[Reading]) -> PeriodConsumption | None:
    """Consumo fra le ultime due letture disponibili (per il sensore)."""
    groups = _group_by_instant(readings)
    if len(groups) < 2:
        return None
    (prev_ts, prev_values), (last_ts, last_values) = groups[-2], groups[-1]
    common = set(prev_values) & set(last_values)
    if not common:
        return None
    value = sum(last_values[r] - prev_values[r] for r in common)
    if value < 0:
        return None
    return PeriodConsumption(start=prev_ts, end=last_ts, value=round(value, 6))


def latest_total(readings: Iterable[Reading]) -> tuple[datetime, float] | None:
    """Ultimo valore complessivo del registro (somma dei registri, se più d'uno)."""
    last: dict[str | None, float] = {}
    last_ts: datetime | None = None
    for ts, values in _group_by_instant(readings):
        last.update(values)
        last_ts = ts
    if last_ts is None:
        return None
    return last_ts, round(sum(last.values()), 6)
