"""Coordinatore: un solo giro di richieste all'ora per tutti i sensori e le statistiche."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter, VolumeConverter

from .api import (
    KrakenApiError,
    KrakenAuthError,
    KrakenClient,
    KrakenConnectionError,
    KrakenError,
    KrakenRateLimitError,
)
from .const import (
    CONF_PRICE_ELECTRICITY,
    CONF_PRICE_GAS,
    DOMAIN,
    PRICE_HISTORY_SUFFIX,
    SEED_DAYS,
    GAS_READING_PRIORITY,
    GAS_READING_TYPES_FOR_STATS,
    KIND_ELECTRICITY,
    STATS_REWRITE_DAYS,
    UPDATE_INTERVAL,
)
from .energy_stats import (
    Anchor,
    DailyRow,
    PeriodConsumption,
    compute_cost_rows,
    compute_daily_rows,
    day_start,
    latest_period,
    latest_total,
    statistic_id_for,
)
from .models import (
    AccountInfo,
    Reading,
    SupplyPoint,
    dedupe_gas_readings,
    parse_account,
    parse_electricity_readings,
    parse_gas_readings,
)

_LOGGER = logging.getLogger(__name__)

type OctopusConfigEntry = ConfigEntry[OctopusCoordinator]


@dataclass(slots=True)
class MeterState:
    """Stato di un contatore (POD o PDR) per i sensori."""

    account_number: str
    point: SupplyPoint
    statistic_id: str
    latest_reading: Reading | None = None
    latest_total: tuple[datetime, float] | None = None
    last_period: PeriodConsumption | None = None
    history_start: date | None = None  # primo giorno nelle statistiche (se noto)
    readings_ok: bool = False


@dataclass(slots=True)
class AccountState:
    info: AccountInfo
    meters: dict[str, MeterState] = field(default_factory=dict)


class OctopusCoordinator(DataUpdateCoordinator[dict[str, AccountState]]):
    """Scarica conto, tariffe e letture e aggiorna le statistiche esterne."""

    config_entry: OctopusConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: OctopusConfigEntry, client: KrakenClient
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self.client = client
        self.account_numbers: list[str] = []
        # Ultime righe scritte per statistica: evita di riscrivere dati identici.
        self._written: dict[str, tuple[DailyRow, ...]] = {}
        # Ultimo stato noto per contatore: se le letture falliscono si tiene questo.
        self._meters: dict[str, MeterState] = {}

    async def _async_setup(self) -> None:
        try:
            self.account_numbers = await self.client.async_get_account_numbers()
        except KrakenAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except KrakenError as err:
            raise UpdateFailed(str(err)) from err
        if not self.account_numbers:
            raise UpdateFailed("Nessun conto Octopus associato a queste credenziali")

    async def _async_update_data(self) -> dict[str, AccountState]:
        tz = dt_util.get_default_time_zone()
        today = dt_util.now(tz).date()
        previous = self.data or {}
        result: dict[str, AccountState] = {}
        failures: list[str] = []

        for number in self.account_numbers:
            try:
                raw, errors = await self.client.async_get_account(number)
            except KrakenAuthError as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            except (KrakenRateLimitError, KrakenConnectionError, KrakenApiError) as err:
                failures.append(str(err))
                if number in previous:
                    result[number] = previous[number]
                continue
            if errors:
                # Errori parziali (es. un prodotto non risolvibile): i dati restanti
                # sono comunque utilizzabili.
                _LOGGER.debug("Conto: %d errori parziali, es. %s", len(errors), errors[0])

            info = parse_account(raw, number)
            account = AccountState(info=info)
            for point in info.supply_points:
                meter = await self._async_update_meter(number, point, tz, today)
                account.meters[point.key] = meter
            result[number] = account

        if not result:
            raise UpdateFailed(f"Aggiornamento non riuscito: {failures[0] if failures else '?'}")
        if failures:
            _LOGGER.warning("Alcuni conti non sono stati aggiornati: %s", failures[0])
        return result

    # ------------------------------------------------------------------

    async def _async_update_meter(
        self, account_number: str, point: SupplyPoint, tz: tzinfo, today: date
    ) -> MeterState:
        statistic_id = statistic_id_for(DOMAIN, point.kind, point.identifier)
        cache_key = f"{account_number}_{point.key}"
        old = self._meters.get(cache_key)
        meter = MeterState(
            account_number=account_number, point=point, statistic_id=statistic_id
        )
        if old is not None:
            meter.latest_reading = old.latest_reading
            meter.latest_total = old.latest_total
            meter.last_period = old.last_period
            meter.history_start = old.history_start

        try:
            anchor = await self._async_get_anchor(statistic_id, point.kind, tz)
            fetch_from = (
                anchor.day - timedelta(days=SEED_DAYS) if anchor else None
            )
            readings = await self._async_fetch_readings(account_number, point, fetch_from, tz)
            if not readings and fetch_from is not None and old is None:
                # Dopo un riavvio senza letture recenti: scarica tutto una volta,
                # così i sensori mostrano comunque l'ultima lettura disponibile.
                readings = await self._async_fetch_readings(account_number, point, None, tz)
        except KrakenAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except KrakenError as err:
            _LOGGER.warning(
                "Letture %s non aggiornate: %s",
                "luce" if point.kind == KIND_ELECTRICITY else "gas",
                err,
            )
            self._meters[cache_key] = meter
            return meter

        meter.readings_ok = True
        if readings:
            meter.latest_reading = readings[-1]
            meter.latest_total = latest_total(readings)
            meter.last_period = latest_period(readings) or meter.last_period

        rows = compute_daily_rows(readings, tz, anchor, today)
        if anchor is None and rows:
            meter.history_start = rows[0].day
            # Una sola volta (primo import): utile per sapere quanto storico c'è.
            _LOGGER.info(
                "Storico %s importato: %d letture dal %s al %s",
                "luce" if point.kind == KIND_ELECTRICITY else "gas",
                len(readings),
                readings[0].ts.astimezone(tz).date() if readings else "-",
                readings[-1].ts.astimezone(tz).date() if readings else "-",
            )
        self._async_write_statistics(point, statistic_id, rows, tz)
        await self._async_write_costs(point, statistic_id, rows, anchor, tz)
        self._meters[cache_key] = meter
        return meter

    async def _async_fetch_readings(
        self,
        account_number: str,
        point: SupplyPoint,
        fetch_from: date | None,
        tz: tzinfo,
    ) -> list[Reading]:
        """Letture valide per statistiche e sensori (stime escluse)."""
        if point.kind == KIND_ELECTRICITY:
            if not point.property_id:
                return []
            nodes = await self.client.async_get_electricity_readings(
                point.property_id, point.identifier, fetch_from
            )
            readings = parse_electricity_readings(nodes)
            return [r for r in readings if r.quality != "ESTIMATE"]

        nodes = await self.client.async_get_gas_readings(
            account_number, point.identifier, fetch_from
        )
        readings = dedupe_gas_readings(parse_gas_readings(nodes, tz), GAS_READING_PRIORITY)
        return [r for r in readings if r.quality in GAS_READING_TYPES_FOR_STATS]

    @staticmethod
    def _units(kind: str) -> dict[str, str]:
        # Valori sempre nell'unità con cui li scriviamo (kWh / m³), anche se
        # l'utente ha cambiato l'unità di visualizzazione della statistica.
        return (
            {EnergyConverter.UNIT_CLASS: UnitOfEnergy.KILO_WATT_HOUR}
            if kind == KIND_ELECTRICITY
            else {VolumeConverter.UNIT_CLASS: UnitOfVolume.CUBIC_METERS}
        )

    async def _async_get_anchor(
        self, statistic_id: str, kind: str, tz: tzinfo
    ) -> Anchor | None:
        """Riga da cui ripartire: STATS_REWRITE_DAYS giorni prima dell'ultima scritta."""
        recorder = get_instance(self.hass)
        units = self._units(kind)
        last = await recorder.async_add_executor_job(
            get_last_statistics, self.hass, 1, statistic_id, False, {"sum", "state"}
        )
        rows = last.get(statistic_id) if last else None
        if not rows:
            return None
        last_day = dt_util.utc_from_timestamp(rows[0]["start"]).astimezone(tz).date()
        anchor_day = last_day - timedelta(days=STATS_REWRITE_DAYS)
        found = await recorder.async_add_executor_job(
            statistics_during_period,
            self.hass,
            day_start(anchor_day - timedelta(days=31), tz),
            day_start(anchor_day + timedelta(days=1), tz),
            {statistic_id},
            "hour",
            units,
            {"sum", "state"},
        )
        candidates = found.get(statistic_id) if found else None
        if not candidates:
            # Storico più corto della finestra: si ricostruisce tutto da capo
            # (stessa prima lettura, quindi stesse somme di prima).
            return None
        row = candidates[-1]
        if row.get("sum") is None:
            return None
        return Anchor(
            day=dt_util.utc_from_timestamp(row["start"]).astimezone(tz).date(),
            sum=float(row["sum"]),
            state=float(row["state"]) if row.get("state") is not None else None,
        )

    def _prices(self, kind: str) -> list[tuple[date, float]] | None:
        """I prezzi delle opzioni, in ordine di data; None senza prezzo."""
        key = CONF_PRICE_ELECTRICITY if kind == KIND_ELECTRICITY else CONF_PRICE_GAS
        options = self.config_entry.options
        current = options.get(key)
        if current in (None, "") or float(current) <= 0:
            return None
        history = [
            (date.fromisoformat(since), float(value))
            for since, value in options.get(key + PRICE_HISTORY_SUFFIX) or []
        ]
        return sorted(history) or [(date.min, float(current))]

    async def _async_rows_until(
        self, statistic_id: str, kind: str, last_day: date, tz: tzinfo
    ) -> list[DailyRow]:
        """Le righe dei consumi già scritte, fino a `last_day` compreso."""
        found = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            day_start(date(2000, 1, 1), tz),
            day_start(last_day + timedelta(days=1), tz),
            {statistic_id},
            "hour",
            self._units(kind),
            {"sum"},
        )
        return [
            DailyRow(
                day=dt_util.utc_from_timestamp(r["start"]).astimezone(tz).date(),
                sum=float(r["sum"]),
                state=None,
            )
            for r in (found.get(statistic_id) or [] if found else [])
            if r.get("sum") is not None
        ]

    async def _async_cost_on(self, cost_id: str, day: date, tz: tzinfo) -> float | None:
        """La somma dei costi già scritta nel giorno, se c'è."""
        found = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            day_start(day, tz),
            day_start(day + timedelta(days=1), tz),
            {cost_id},
            "hour",
            None,
            {"sum"},
        )
        rows = found.get(cost_id) if found else None
        return float(rows[0]["sum"]) if rows and rows[0].get("sum") is not None else None

    async def _async_write_costs(
        self,
        point: SupplyPoint,
        statistic_id: str,
        rows: list[DailyRow],
        anchor: Anchor | None,
        tz: tzinfo,
    ) -> None:
        """Il costo dei consumi, giorno per giorno, ai prezzi delle opzioni.

        Riparte dalla stessa ancora dei consumi. Se i costi non ci sono ancora
        (prezzo appena impostato su uno storico già scritto), si calcola tutto
        lo storico, col primo prezzo per i giorni prima dei cambi.
        """
        prices = self._prices(point.kind)
        if prices is None or not rows:
            return
        cost_id = statistic_id_for(DOMAIN, f"{point.kind}_cost", point.identifier)
        history, base = rows, None
        if anchor is not None:
            cost = await self._async_cost_on(cost_id, anchor.day, tz)
            if cost is not None:
                base = (anchor.sum, cost)
            else:
                history = [
                    *await self._async_rows_until(statistic_id, point.kind, anchor.day, tz),
                    *rows,
                ]
        cost_rows = compute_cost_rows(history, prices, base)
        frozen = (tuple(prices), tuple(cost_rows))
        if not cost_rows or self._written.get(cost_id) == frozen:
            return
        electricity = point.kind == KIND_ELECTRICITY
        metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=(
                f"Octopus costo luce {point.identifier}"
                if electricity
                else f"Octopus costo gas {point.identifier}"
            ),
            source=DOMAIN,
            statistic_id=cost_id,
            unit_class=None,
            unit_of_measurement=self.hass.config.currency,
        )
        async_add_external_statistics(
            self.hass,
            metadata,
            [StatisticData(start=day_start(day, tz), sum=total) for day, total in cost_rows],
        )
        self._written[cost_id] = frozen
        _LOGGER.debug("Statistiche %s: scritte %d righe giornaliere", cost_id, len(cost_rows))

    def _async_write_statistics(
        self, point: SupplyPoint, statistic_id: str, rows: list[DailyRow], tz: tzinfo
    ) -> None:
        if not rows:
            return
        frozen = tuple(rows)
        if self._written.get(statistic_id) == frozen:
            return
        electricity = point.kind == KIND_ELECTRICITY
        metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=(
                f"Octopus luce {point.identifier}"
                if electricity
                else f"Octopus gas {point.identifier}"
            ),
            source=DOMAIN,
            statistic_id=statistic_id,
            unit_class=EnergyConverter.UNIT_CLASS if electricity else VolumeConverter.UNIT_CLASS,
            unit_of_measurement=(
                UnitOfEnergy.KILO_WATT_HOUR if electricity else UnitOfVolume.CUBIC_METERS
            ),
        )
        statistics: list[StatisticData] = []
        for row in rows:
            data = StatisticData(start=day_start(row.day, tz), sum=row.sum)
            if row.state is not None:
                data["state"] = row.state
            statistics.append(data)
        async_add_external_statistics(self.hass, metadata, statistics)
        self._written[statistic_id] = frozen
        _LOGGER.debug("Statistiche %s: scritte %d righe giornaliere", statistic_id, len(rows))
