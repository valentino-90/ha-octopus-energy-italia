"""Costanti dell'integrazione Octopus Energy Italia."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "octopus_energy_italia"

# Unico indirizzo esterno contattato dall'integrazione. Non è configurabile di
# proposito: il client rifiuta qualsiasi altro URL (vedi api.py).
GRAPHQL_URL: Final = "https://api.oeit-kraken.energy/v1/graphql/"
GRAPHQL_HOST: Final = "api.oeit-kraken.energy"

# Ogni quanto interrogare le API. Le letture arrivano al più una volta al giorno
# (spesso con 1-2 giorni di ritardo), tariffe e saldo cambiano raramente:
# un'ora è più che sufficiente e resta lontana dai limiti di frequenza Kraken.
UPDATE_INTERVAL: Final = timedelta(hours=1)

# Timeout di ogni singola richiesta HTTP verso Kraken (secondi).
REQUEST_TIMEOUT: Final = 30

# Rinnova il token se scade entro questo margine (secondi).
TOKEN_REFRESH_MARGIN: Final = 300
# Durata ipotizzata del token se la risposta non riporta la scadenza (secondi).
TOKEN_FALLBACK_LIFETIME: Final = 50 * 60

# Paginazione: elementi per pagina e numero massimo di pagine per una singola
# serie (protezione contro cicli infiniti se il cursore non avanza).
PAGE_SIZE: Final = 100
MAX_PAGES: Final = 500

# Statistiche a lungo termine.
# Ad ogni aggiornamento si riscrivono gli ultimi STATS_REWRITE_DAYS giorni, così
# eventuali rettifiche recenti delle letture vengono recepite.
STATS_REWRITE_DAYS: Final = 45
# Oltre alla finestra riscritta si scaricano altri giorni "di appoggio" per
# conoscere la lettura precedente; se non ce ne sono (gas con letture rade) si
# usa il valore del registro salvato nella riga di partenza.
SEED_DAYS: Final = 10

# Tipi di lettura gas usati nelle statistiche (le stime sono escluse: verrebbero
# poi rettificate e falserebbero lo storico). Ordine = priorità se nello stesso
# giorno ci sono più letture.
GAS_READING_PRIORITY: Final = {
    "RECOMPUTED": 4,
    "ACTUAL": 3,
    "CUSTOMER_SELF_READING": 2,
    "ESTIMATE": 1,
}
GAS_READING_TYPES_FOR_STATS: Final = frozenset(
    {"RECOMPUTED", "ACTUAL", "CUSTOMER_SELF_READING"}
)

CONF_EMAIL: Final = "email"
CONF_PASSWORD: Final = "password"  # nome del campo, non una credenziale

KIND_ELECTRICITY: Final = "electricity"
KIND_GAS: Final = "gas"
