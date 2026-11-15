<p align="center">
  <img src="https://raw.githubusercontent.com/valentino-90/ha-octopus-energy-italia/main/custom_components/octopus_energy_italia/brand/icon@2x.png" alt="" width="128" height="128">
</p>

# Octopus Energy Italia per Home Assistant

[![HACS](https://img.shields.io/badge/HACS-repository%20personalizzato-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories/)
[![Validate](https://github.com/valentino-90/ha-octopus-energy-italia/actions/workflows/validate.yml/badge.svg)](https://github.com/valentino-90/ha-octopus-energy-italia/actions/workflows/validate.yml)
[![Tests](https://github.com/valentino-90/ha-octopus-energy-italia/actions/workflows/tests.yml/badge.svg)](https://github.com/valentino-90/ha-octopus-energy-italia/actions/workflows/tests.yml)

Integrazione non ufficiale che legge dall'area clienti di **Octopus Energy Italia**
i consumi di luce e gas, le tariffe e il saldo, e porta lo storico dei consumi
nella **dashboard Energia** di Home Assistant.

*English summary [below](#english).*

## Cosa fa

- **Statistiche per la dashboard Energia**, una per contatore:
  `Octopus luce <POD>` (kWh) e `Octopus gas <PDR>` (m³). Sono statistiche
  esterne (`octopus_energy_italia:electricity_…`, `octopus_energy_italia:gas_…`),
  con una riga al giorno:
  - al primo avvio viene importato tutto lo storico che Octopus rende disponibile;
  - il consumo fra due letture va nel giorno della lettura **che chiude**
    l'intervallo: il consumo fra la lettura del 30/09 e quella del 31/10 sta in
    ottobre. Con letture mensili quindi il consumo del mese compare tutto
    sull'ultimo giorno del mese;
  - a ogni aggiornamento si riscrivono gli ultimi 45 giorni, così le letture
    arrivate in ritardo o rettificate finiscono al posto giusto;
  - le letture stimate non vengono usate (verrebbero poi corrette).
- **Sensori** per ogni POD e PDR: lettura del contatore, data dell'ultima
  lettura, consumo dell'ultimo periodo, prezzo dell'energia (F1 o monoraria, F2,
  F3) o del gas, quota fissa annua, offerta attiva, stato della fornitura.
- **Saldo** del conto, un sensore per ogni ledger.
- Aggiornamento ogni ora (le letture arrivano al massimo una volta al giorno,
  spesso con uno o due giorni di ritardo).
- Riautenticazione e cambio delle credenziali dall'interfaccia, diagnostica
  scaricabile con i dati personali oscurati.

## Installazione da HACS

1. In Home Assistant apri **HACS**, menu **⋮** in alto a destra,
   **Repository personalizzati**.
2. Incolla `https://github.com/valentino-90/ha-octopus-energy-italia`, tipo
   **Integrazione**, e conferma.
3. Cerca **Octopus Energy Italia** in HACS e scaricalo.
4. Riavvia Home Assistant.

Gli aggiornamenti successivi compaiono in HACS come per le altre integrazioni.

### Installazione manuale

Copia la cartella `custom_components/octopus_energy_italia` nella cartella
`custom_components` della configurazione di Home Assistant e riavvia.

Serve Home Assistant 2025.11 o successivo.

## Configurazione

1. **Impostazioni → Dispositivi e servizi → Aggiungi integrazione**, cerca
   **Octopus Energy Italia**.
2. Inserisci email e password dell'area clienti Octopus Energy Italia.

Vengono creati un dispositivo per il conto (saldo) e uno per ogni POD e PDR.

### Dashboard Energia

In **Impostazioni → Dashboard → Energia**:

- **Rete elettrica → Aggiungi consumo**: scegli la statistica `Octopus luce …`;
- **Consumo di gas → Aggiungi fonte di gas**: scegli la statistica `Octopus gas …`.

Usa le statistiche `Octopus luce/gas …`, non i sensori «Lettura contatore»: le
statistiche mettono i consumi nel giorno a cui si riferiscono, i sensori nel
momento in cui la lettura arriva in Home Assistant.

## Cosa non fa

- Non comanda nulla: niente Intelligent Octopus, ricariche, wallbox o dispositivi.
- Non calcola il costo in bolletta e non scarica le bollette.
- Non mostra consumi orari: usa le letture del contatore che Octopus espone
  (giornaliere o mensili, a seconda del contatore).
- Funziona solo con Octopus Energy **Italia** (non con le API di altri paesi).
- Non è un prodotto di Octopus Energy e non è supportata da Octopus Energy.

## Privacy

- L'integrazione parla **solo** con `https://api.oeit-kraken.energy/v1/graphql/`,
  l'API ufficiale Kraken di Octopus Energy Italia. L'indirizzo non è
  configurabile, altri indirizzi vengono rifiutati e i redirect non vengono
  seguiti.
- **Nessuna dipendenza esterna**: usa solo la sessione HTTP di Home Assistant.
- Email e password restano nella configurazione di Home Assistant; i token di
  accesso restano solo in memoria e non vengono mai scritti né registrati nei log.
- Nessuna telemetria. La diagnostica oscura email, password, numeri di conto,
  POD e PDR.

## Sviluppo e test

```bash
# test puri, senza Home Assistant
pip install pytest aiohttp
python -m pytest

# tutti i test, compreso quello con il recorder di Home Assistant (Python 3.14)
pip install -r requirements_test.txt
python -m pytest
```

I test usano solo dati inventati.

## Crediti

Le query GraphQL delle letture e la gestione dei codici d'errore Kraken sono
adattate da [HomeAssistant-OctopusEnergyIT](https://github.com/samuelebistoletti/HomeAssistant-OctopusEnergyIT)
di Samuele Bistoletti, distribuito con licenza MIT: l'avviso di copyright
originale è riportato in [LICENSE](LICENSE). Se ti servono funzioni che qui
mancano (dispatch di Intelligent Octopus, dispositivi, tariffe pubbliche), dai
un'occhiata a quel progetto.

«Octopus Energy» è un marchio dei rispettivi titolari. L'icona di questa
integrazione è un disegno originale e non è il logo di Octopus Energy.

## Licenza

[MIT](LICENSE).

---

## English

Unofficial Home Assistant integration for **Octopus Energy Italy** customers.
It reads electricity and gas meter readings, tariffs and account balance from
the Kraken GraphQL API of Octopus Energy Italy and imports the consumption
history as long-term statistics for the **Energy dashboard**.

- **Install with HACS**: HACS → ⋮ → *Custom repositories* → add
  `https://github.com/valentino-90/ha-octopus-energy-italia` as *Integration*,
  download **Octopus Energy Italia**, restart Home Assistant. Requires
  Home Assistant 2025.11 or newer.
- **Configure**: *Settings → Devices & services → Add integration →
  Octopus Energy Italia*, then enter the email and password of your Octopus
  Energy Italy account.
- **Energy dashboard**: add the external statistics `Octopus luce …` (grid
  consumption) and `Octopus gas …` (gas consumption). The consumption between
  two readings is booked on the day of the reading that closes the interval, so
  a reading on 30 September followed by one on 31 October puts that energy in
  October. The last 45 days are rewritten on every update to pick up late or
  corrected readings; estimated readings are ignored.
- **What it does not do**: no device control (no Intelligent Octopus), no bill
  or cost calculation, no hourly data, Italy only. Not affiliated with Octopus
  Energy.
- **Privacy**: it only talks to `https://api.oeit-kraken.energy/v1/graphql/`, has
  no third-party dependencies, keeps tokens in memory only and redacts personal
  data in diagnostics.
- **Credits**: the GraphQL queries and Kraken error handling are adapted from
  [HomeAssistant-OctopusEnergyIT](https://github.com/samuelebistoletti/HomeAssistant-OctopusEnergyIT)
  (MIT); its copyright notice is kept in [LICENSE](LICENSE).
