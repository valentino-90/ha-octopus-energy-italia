"""Client GraphQL minimale per le API Kraken di Octopus Energy Italia.

Scelte di progetto:
- parla SOLO con GRAPHQL_URL (https, host fisso); il costruttore rifiuta altri URL
  e i redirect HTTP non vengono seguiti;
- usa la sessione aiohttp fornita da Home Assistant: nessuna libreria esterna;
- email, password e token non finiscono mai nei log né nei messaggi d'errore;
- i token restano solo in memoria (mai scritti su disco).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from .const import (
    GRAPHQL_HOST,
    GRAPHQL_URL,
    MAX_PAGES,
    PAGE_SIZE,
    REQUEST_TIMEOUT,
    TOKEN_FALLBACK_LIFETIME,
    TOKEN_REFRESH_MARGIN,
)

_LOGGER = logging.getLogger(__name__)

# Codici d'errore Kraken rilevanti (dallo schema e dal comportamento osservato
# in HomeAssistant-OctopusEnergyIT, vedi la sezione Crediti del README).
TOKEN_EXPIRED_CODES = frozenset({"KT-CT-1124", "KT-CT-1120"})
UNAUTHORIZED_CODES = frozenset({"KT-CT-1111", "KT-CT-1112"})
RATE_LIMIT_CODES = frozenset({"KT-CT-1199"})
# Errori di obtainKrakenToken che NON indicano credenziali errate.
LOGIN_NON_AUTH_CODES = frozenset({"KT-CT-1113"})

# --------------------------------------------------------------------------
# Query e mutation GraphQL. Le query delle letture sono adattate da
# HomeAssistant-OctopusEnergyIT (licenza MIT, vedi LICENSE).
# --------------------------------------------------------------------------

LOGIN_MUTATION = """
mutation ObtainToken($input: ObtainJSONWebTokenInput!) {
  obtainKrakenToken(input: $input) {
    token
    payload
    refreshToken
    refreshExpiresIn
  }
}
"""

ACCOUNTS_QUERY = """
query Accounts {
  viewer {
    accounts {
      number
    }
  }
}
"""

ACCOUNT_QUERY = """
query Account($accountNumber: String!) {
  account(accountNumber: $accountNumber) {
    number
    ledgers {
      number
      name
      ledgerType
      balance
      amountOwedByCustomer
    }
    properties {
      id
      electricitySupplyPoints {
        id
        pod
        status
        enrolmentStatus
        supplyStartDate
        isSmartMeter
        product {
          code
          displayName
          fullName
          validTo
          prices {
            productType
            annualStandingCharge
            annualStandingChargeUnits
            consumptionCharge
            consumptionChargeF2
            consumptionChargeF3
            consumptionChargeUnits
          }
        }
        agreements(first: 20) {
          edges {
            node {
              id
              validFrom
              validTo
              isActive
            }
          }
        }
      }
      gasSupplyPoints {
        id
        pdr
        status
        enrolmentStatus
        supplyStartDate
        isSmartMeter
        product {
          code
          displayName
          fullName
          validTo
          prices {
            annualStandingCharge
            consumptionCharge
          }
        }
        agreements(first: 20) {
          edges {
            node {
              id
              validFrom
              validTo
              isActive
            }
          }
        }
      }
    }
  }
}
"""

_ELECTRICITY_READINGS_TEMPLATE = """
query ElectricityReadings(
  $propertyId: ID!
  $pod: String!
  $startOn: Date
  $first: Int!
  $after: String
) {
  property(id: $propertyId) {
    measurements(
      startOn: $startOn
      first: $first
      after: $after
      utilityFilters: [
        {
          electricityFilters: {
            marketSupplyPointId: $pod
            readingFrequencyType: POINT_IN_TIME
            readingDirection: CONSUMPTION
          }
        }
      ]
    ) {
      pageInfo {
        hasNextPage
        endCursor
      }
      edges {
        node {
          value
          unit
          readAt
          source
          %METADATA%
        }
      }
    }
  }
}
"""

_ELECTRICITY_METADATA = """metaData {
            utilityFilters {
              ... on ElectricityFiltersOutput {
                registerId
                readingQuality
              }
            }
          }"""

ELECTRICITY_READINGS_QUERY = _ELECTRICITY_READINGS_TEMPLATE.replace(
    "%METADATA%", _ELECTRICITY_METADATA
)
ELECTRICITY_READINGS_QUERY_BASIC = _ELECTRICITY_READINGS_TEMPLATE.replace(
    "%METADATA%", ""
)

GAS_READINGS_QUERY = """
query GasReadings(
  $accountNumber: String!
  $pdr: String!
  $dateFrom: Date
  $first: Int!
  $after: String
) {
  gasMeterReadings(
    accountNumber: $accountNumber
    pdr: $pdr
    dateFrom: $dateFrom
    first: $first
    after: $after
  ) {
    pageInfo {
      hasNextPage
      endCursor
    }
    edges {
      node {
        readingDate
        readingType
        readingSource
        consumptionValue
      }
    }
  }
}
"""


# --------------------------------------------------------------------------
# Eccezioni
# --------------------------------------------------------------------------


class KrakenError(Exception):
    """Errore generico del client Kraken."""


class KrakenAuthError(KrakenError):
    """Credenziali rifiutate: serve una nuova autenticazione."""


class KrakenConnectionError(KrakenError):
    """Problema di rete, timeout o errore HTTP lato server."""


class KrakenRateLimitError(KrakenError):
    """Kraken ha segnalato troppe richieste."""


class KrakenApiError(KrakenError):
    """Errore GraphQL non legato all'autenticazione."""


@dataclass(frozen=True, slots=True)
class GraphQLError:
    """Errore GraphQL ridotto ai campi utili (niente dati personali)."""

    code: str | None
    message: str
    path: tuple[str | int, ...]

    def __str__(self) -> str:
        where = "/".join(str(p) for p in self.path) or "-"
        return f"{self.code or 'senza codice'} su {where}: {self.message}"


def _parse_errors(payload: dict[str, Any]) -> list[GraphQLError]:
    errors: list[GraphQLError] = []
    for raw in payload.get("errors") or []:
        if not isinstance(raw, dict):
            continue
        extensions = raw.get("extensions") or {}
        code = extensions.get("errorCode") if isinstance(extensions, dict) else None
        path = raw.get("path") or ()
        errors.append(
            GraphQLError(
                code=code if isinstance(code, str) else None,
                message=str(raw.get("message") or "errore sconosciuto")[:300],
                path=tuple(path) if isinstance(path, list) else (),
            )
        )
    return errors


def _validate_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname != GRAPHQL_HOST:
        raise ValueError("Indirizzo non ammesso: è consentito solo l'endpoint Kraken")
    return url


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------


class KrakenClient:
    """Client asincrono per l'API GraphQL Kraken di Octopus Energy Italia."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        email: str,
        password: str,
        *,
        url: str = GRAPHQL_URL,
    ) -> None:
        self._session = session
        self._email = email
        self._password = password
        self._url = _validate_url(url)
        self._token: str | None = None
        self._token_expiry: float = 0.0
        self._refresh_token: str | None = None
        self._refresh_expiry: float = 0.0
        self._lock = asyncio.Lock()
        self._electricity_metadata_supported = True

    def __repr__(self) -> str:
        # Mai mostrare credenziali o token, nemmeno nei traceback.
        return "<KrakenClient>"

    # ---------------- trasporto ----------------

    async def _post(
        self, query: str, variables: dict[str, Any], *, token: str | None
    ) -> dict[str, Any]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if token:
            headers["Authorization"] = token
        try:
            async with self._session.post(
                self._url,
                json={"query": query, "variables": variables},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                allow_redirects=False,
            ) as resp:
                status = resp.status
                if status == 429:
                    raise KrakenRateLimitError("Kraken: troppe richieste (HTTP 429)")
                if status >= 500:
                    raise KrakenConnectionError(f"Kraken: errore del server (HTTP {status})")
                if 300 <= status < 400:
                    raise KrakenConnectionError(
                        f"Kraken: redirect inatteso (HTTP {status}) non seguito"
                    )
                try:
                    payload = await resp.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError) as err:
                    raise KrakenConnectionError(
                        f"Kraken: risposta non JSON (HTTP {status})"
                    ) from err
        except TimeoutError as err:
            raise KrakenConnectionError("Kraken: timeout della richiesta") from err
        except aiohttp.ClientError as err:
            # Il messaggio aiohttp contiene al massimo l'URL pubblico dell'API.
            raise KrakenConnectionError(f"Kraken: errore di rete ({type(err).__name__})") from err

        if not isinstance(payload, dict):
            raise KrakenConnectionError(f"Kraken: risposta inattesa (HTTP {status})")
        if status in (401, 403) and not payload.get("errors"):
            payload = {
                "errors": [
                    {"message": f"HTTP {status}", "extensions": {"errorCode": "KT-CT-1111"}}
                ]
            }
        elif status >= 400 and not payload.get("errors"):
            raise KrakenApiError(f"Kraken: richiesta rifiutata (HTTP {status})")
        return payload

    # ---------------- autenticazione ----------------

    def _store_token(self, result: dict[str, Any]) -> None:
        token = result.get("token")
        if not isinstance(token, str) or not token:
            raise KrakenAuthError("Kraken non ha restituito un token")
        now = time.time()
        expiry: float | None = None
        payload = result.get("payload")
        if isinstance(payload, dict):
            try:
                expiry = float(payload["exp"])
            except (KeyError, TypeError, ValueError):
                expiry = None
        self._token = token
        self._token_expiry = expiry if expiry and expiry > now else now + TOKEN_FALLBACK_LIFETIME

        refresh = result.get("refreshToken")
        if isinstance(refresh, str) and refresh:
            self._refresh_token = refresh
            refresh_exp = result.get("refreshExpiresIn")
            try:
                value = float(refresh_exp)
            except (TypeError, ValueError):
                value = 0.0
            # Lo schema lo descrive come timestamp Unix; per prudenza se è un
            # numero piccolo lo trattiamo come "secondi da adesso".
            if 0 < value < 1_000_000_000:
                value = now + value
            self._refresh_expiry = value

    async def _obtain_token(self, token_input: dict[str, str]) -> None:
        payload = await self._post(LOGIN_MUTATION, {"input": token_input}, token=None)
        errors = _parse_errors(payload)
        data = payload.get("data") or {}
        result = data.get("obtainKrakenToken") if isinstance(data, dict) else None
        if errors or not isinstance(result, dict):
            codes = {e.code for e in errors}
            if codes & RATE_LIMIT_CODES:
                raise KrakenRateLimitError("Kraken: troppi tentativi di accesso")
            if errors and codes <= LOGIN_NON_AUTH_CODES:
                raise KrakenApiError(f"Kraken: accesso non riuscito ({errors[0]})")
            detail = errors[0].code if errors else "risposta vuota"
            raise KrakenAuthError(f"Kraken: accesso rifiutato ({detail})")
        self._store_token(result)

    async def authenticate(self) -> None:
        """Accesso con email e password (usato da config flow e al bisogno)."""
        async with self._lock:
            await self._obtain_token({"email": self._email, "password": self._password})

    def _invalidate_token(self) -> None:
        self._token = None
        self._token_expiry = 0.0

    async def _ensure_token(self) -> str:
        async with self._lock:
            now = time.time()
            if self._token and now < self._token_expiry - TOKEN_REFRESH_MARGIN:
                return self._token
            if self._refresh_token and now < self._refresh_expiry - TOKEN_REFRESH_MARGIN:
                try:
                    await self._obtain_token({"refreshToken": self._refresh_token})
                except (KrakenRateLimitError, KrakenConnectionError):
                    # Problema temporaneo: il refresh token resta buono.
                    raise
                except KrakenError as err:
                    _LOGGER.debug("Rinnovo con refresh token fallito (%s), nuovo accesso", type(err).__name__)
                    self._refresh_token = None
                    self._refresh_expiry = 0.0
                else:
                    return self._current_token()
            await self._obtain_token({"email": self._email, "password": self._password})
            return self._current_token()

    def _current_token(self) -> str:
        if not self._token:
            raise KrakenAuthError("Kraken non ha restituito un token")
        return self._token

    # ---------------- esecuzione query ----------------

    async def execute(
        self, query: str, variables: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any], list[GraphQLError]]:
        """Esegue una query autenticata. Restituisce (data, errori parziali).

        Solleva un'eccezione se non c'è nessun dato utilizzabile.
        """
        variables = variables or {}
        for attempt in (1, 2):
            token = await self._ensure_token()
            payload = await self._post(query, variables, token=token)
            errors = _parse_errors(payload)
            codes = {e.code for e in errors}
            if attempt == 1 and codes & (TOKEN_EXPIRED_CODES | UNAUTHORIZED_CODES):
                _LOGGER.debug("Token non accettato (%s): nuovo token e riprovo", sorted(c for c in codes if c))
                self._invalidate_token()
                if codes & UNAUTHORIZED_CODES:
                    # Anche il refresh token potrebbe non valere più.
                    self._refresh_token = None
                    self._refresh_expiry = 0.0
                continue
            if codes & RATE_LIMIT_CODES:
                raise KrakenRateLimitError("Kraken: troppe richieste")
            data = payload.get("data")
            if not isinstance(data, dict) or not any(v is not None for v in data.values()):
                if errors:
                    raise KrakenApiError(f"Kraken: {errors[0]}")
                raise KrakenApiError("Kraken: risposta senza dati")
            return data, errors
        raise KrakenApiError("Kraken: richiesta non autorizzata anche dopo un nuovo accesso")

    async def _paginate(
        self,
        query: str,
        variables: dict[str, Any],
        extract: Callable[[dict[str, Any]], dict[str, Any] | None],
    ) -> list[dict[str, Any]]:
        nodes: list[dict[str, Any]] = []
        after: str | None = None
        seen: set[str] = set()
        for _ in range(MAX_PAGES):
            data, errors = await self.execute(
                query, {**variables, "first": PAGE_SIZE, "after": after}
            )
            connection = extract(data)
            if not isinstance(connection, dict):
                if errors:
                    raise KrakenApiError(f"Kraken: {errors[0]}")
                break
            for edge in connection.get("edges") or []:
                node = (edge or {}).get("node") if isinstance(edge, dict) else None
                if isinstance(node, dict):
                    nodes.append(node)
            info = connection.get("pageInfo") or {}
            cursor = info.get("endCursor")
            if not info.get("hasNextPage") or not cursor or cursor in seen:
                break
            seen.add(cursor)
            after = cursor
        else:
            _LOGGER.warning("Paginazione interrotta dopo %s pagine", MAX_PAGES)
        return nodes

    # ---------------- operazioni di alto livello ----------------

    async def async_get_account_numbers(self) -> list[str]:
        data, _ = await self.execute(ACCOUNTS_QUERY)
        viewer = data.get("viewer") or {}
        numbers = [
            acc["number"]
            for acc in viewer.get("accounts") or []
            if isinstance(acc, dict) and isinstance(acc.get("number"), str)
        ]
        return sorted(set(numbers))

    async def async_get_account(
        self, account_number: str
    ) -> tuple[dict[str, Any], list[GraphQLError]]:
        data, errors = await self.execute(ACCOUNT_QUERY, {"accountNumber": account_number})
        account = data.get("account")
        if not isinstance(account, dict):
            raise KrakenApiError("Kraken: conto non disponibile")
        return account, errors

    async def async_get_electricity_readings(
        self, property_id: str, pod: str, start_on: date | None
    ) -> list[dict[str, Any]]:
        variables = {
            "propertyId": property_id,
            "pod": pod,
            "startOn": start_on.isoformat() if start_on else None,
        }

        def extract(data: dict[str, Any]) -> dict[str, Any] | None:
            prop = data.get("property")
            return prop.get("measurements") if isinstance(prop, dict) else None

        if self._electricity_metadata_supported:
            try:
                return await self._paginate(ELECTRICITY_READINGS_QUERY, variables, extract)
            except KrakenApiError as err:
                _LOGGER.info(
                    "Metadati delle letture luce non disponibili (%s): uso la query semplice",
                    err,
                )
                self._electricity_metadata_supported = False
        return await self._paginate(ELECTRICITY_READINGS_QUERY_BASIC, variables, extract)

    async def async_get_gas_readings(
        self, account_number: str, pdr: str, date_from: date | None
    ) -> list[dict[str, Any]]:
        variables = {
            "accountNumber": account_number,
            "pdr": pdr,
            "dateFrom": date_from.isoformat() if date_from else None,
        }
        return await self._paginate(
            GAS_READINGS_QUERY, variables, lambda data: data.get("gasMeterReadings")
        )
