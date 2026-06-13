"""Cross-engine idempotency tests for the Bybit orderLinkId.

Double-buy protection for a single listing relies on Bybit treating a repeated
``orderLinkId`` as a duplicate. The Python poller and the C++ ultra engine
(``cpp/listing_ultra_engine.cpp``) can both fire for the same (exchange,
message_id, ticker). If they build different orderLinkIds, Bybit will not dedup
the second request.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.poller import ExchangeListingPoller


ORDER_LINK_ID_MAX_LEN = 36


def cpp_order_link_id(exchange: str, message_id: int, ticker: str) -> str:
    return f"ls-{exchange}-{message_id}-{ticker}"[:ORDER_LINK_ID_MAX_LEN]


def python_order_link_id(exchange: str, message_id: int, ticker: str) -> str:
    poller = ExchangeListingPoller.__new__(ExchangeListingPoller)
    prefix = f"ls-{ExchangeListingPoller._exchange_code(exchange)}-"
    return poller._make_order_link_id(
        prefix=prefix,
        message_id=message_id,
        ticker=ticker,
    )


CASES = [
    ("upbit", 12345, "ABC"),
    ("bithumb", 999999, "WLD"),
    ("upbit", 1, "X"),
    ("bithumb", 70000000, "DOGE"),
    ("upbit", 88888888888888, "SUPERLONGTICKERNAME1234567890"),
    ("bithumb", 123456789012345, "ANOTHERVERYLONGTICKERSYMBOLXYZ"),
]


@pytest.mark.parametrize("exchange,message_id,ticker", CASES)
def test_python_matches_cpp_order_link_id(exchange, message_id, ticker):
    assert python_order_link_id(exchange, message_id, ticker) == cpp_order_link_id(
        exchange,
        message_id,
        ticker,
    )


@pytest.mark.parametrize("exchange,message_id,ticker", CASES)
def test_order_link_id_canonical_form(exchange, message_id, ticker):
    expected = f"ls-{exchange}-{message_id}-{ticker}"[:ORDER_LINK_ID_MAX_LEN]
    assert python_order_link_id(exchange, message_id, ticker) == expected


def test_exchange_code_is_full_name():
    assert ExchangeListingPoller._exchange_code("upbit") == "upbit"
    assert ExchangeListingPoller._exchange_code("bithumb") == "bithumb"


def test_order_link_id_truncated_to_36():
    value = python_order_link_id("upbit", 88888888888888, "SUPERLONGTICKERNAME1234567890")
    assert len(value) == ORDER_LINK_ID_MAX_LEN
