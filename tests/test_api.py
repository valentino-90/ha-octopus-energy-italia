"""Test del client Kraken che non richiedono rete (dati inventati)."""

from __future__ import annotations

import pytest

pytest.importorskip("aiohttp")

from oei_pure.api import (  # noqa: E402
    ELECTRICITY_READINGS_QUERY,
    ELECTRICITY_READINGS_QUERY_BASIC,
    KrakenClient,
    _parse_errors,
    _validate_url,
)
from oei_pure.const import GRAPHQL_URL  # noqa: E402


def test_only_the_kraken_endpoint_is_allowed() -> None:
    assert _validate_url(GRAPHQL_URL) == GRAPHQL_URL
    for url in (
        "http://api.oeit-kraken.energy/v1/graphql/",
        "https://example.com/v1/graphql/",
        "https://api.oeit-kraken.energy.example.com/v1/graphql/",
    ):
        with pytest.raises(ValueError):
            _validate_url(url)


def test_client_never_shows_credentials() -> None:
    client = KrakenClient(None, "persona@example.invalid", "segreta")  # type: ignore[arg-type]
    assert repr(client) == "<KrakenClient>"
    assert "segreta" not in repr(client)


def test_parse_errors() -> None:
    errors = _parse_errors(
        {
            "errors": [
                {"message": "Token scaduto", "extensions": {"errorCode": "KT-CT-1124"}, "path": ["account"]},
                {"message": "x" * 500},
                "non un oggetto",
            ]
        }
    )
    assert [e.code for e in errors] == ["KT-CT-1124", None]
    assert str(errors[0]) == "KT-CT-1124 su account: Token scaduto"
    assert len(errors[1].message) == 300


def test_electricity_queries_differ_only_by_metadata() -> None:
    assert "%METADATA%" not in ELECTRICITY_READINGS_QUERY
    assert "registerId" in ELECTRICITY_READINGS_QUERY
    assert "registerId" not in ELECTRICITY_READINGS_QUERY_BASIC
    assert "POINT_IN_TIME" in ELECTRICITY_READINGS_QUERY_BASIC
