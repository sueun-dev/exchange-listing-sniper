from __future__ import annotations

import os
import sys
from pathlib import Path


os.environ["LISTING_CLASSIFIER_BACKEND"] = "python"

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))
for module_name in list(sys.modules):
    if module_name == "src" or module_name.startswith("src."):
        sys.modules.pop(module_name, None)

from src.poller import ExchangeListingPoller  # noqa: E402
from src.state_store import StateStore  # noqa: E402


class FakeBybitClient:
    def lookup_ticker(self, ticker: str) -> dict:
        return {"ticker": ticker, "spot": True, "perp": True}


class FakeSpotBuyer:
    def __init__(self):
        self.calls: list[dict] = []

    def buy_market(self, *, ticker: str, order_link_id: str) -> dict:
        self.calls.append({"ticker": ticker, "order_link_id": order_link_id})
        return {
            "enabled": True,
            "attempted": True,
            "executed": False,
            "reason": "test_buyer",
            "order_link_id": order_link_id,
        }


class FakeSignalEmitter:
    def __init__(self):
        self.persisted: list[dict] = []
        self.trade_proofs: list[dict] = []

    def build(self, *, post: dict, listing: dict, bybit: dict, trade: dict, latency=None) -> dict:
        return {
            "message_id": post["message_id"],
            "ticker": listing["ticker"],
            "asset_name": listing["asset_name"],
            "markets": listing["markets"],
            "trade": trade,
        }

    def persist(self, signal: dict):
        self.persisted.append(signal)

    def persist_trade_proof(self, *, post: dict, listing: dict, trade: dict):
        self.trade_proofs.append(
            {
                "message_id": post["message_id"],
                "ticker": listing["ticker"],
                "trade": trade,
            }
        )


class DisabledUltraEngine:
    def is_enabled(self) -> bool:
        return False


def make_poller(tmp_path, buyer: FakeSpotBuyer, emitter: FakeSignalEmitter):
    return ExchangeListingPoller(
        state_store=StateStore(tmp_path / "state.json"),
        bybit_client=FakeBybitClient(),
        spot_buyer=buyer,
        signal_emitter=emitter,
        cpp_ultra_engine=DisabledUltraEngine(),
        enable_bybit_warmup=False,
        enable_channel_client=False,
        enable_cpp_ultra_warmup=False,
    )


def test_repeated_bithumb_listing_ticker_does_not_buy_twice(tmp_path):
    buyer = FakeSpotBuyer()
    emitter = FakeSignalEmitter()
    poller = make_poller(tmp_path, buyer, emitter)

    first = poller.process_post(
        "bithumb",
        {
            "channel_handle": "BithumbExchange",
            "message_id": 11403,
            "published_at": "2025-10-01T06:28:00+00:00",
            "title": "[마켓 추가/수수료 이벤트] 솜니아(SOMI) 원화 마켓 추가 (거래 수수료 무료)",
            "text": "[마켓 추가/수수료 이벤트] 솜니아(SOMI) 원화 마켓 추가 (거래 수수료 무료)",
            "post_url": "https://t.me/BithumbExchange/11403",
        },
    )
    second = poller.process_post(
        "bithumb",
        {
            "channel_handle": "BithumbExchange",
            "message_id": 11404,
            "published_at": "2025-10-01T06:31:00+00:00",
            "title": "[마켓 추가/수수료 이벤트] 솜니아(SOMI) 원화 마켓 추가 (거래 수수료 무료) (거래 오픈 3시 30분 )",
            "text": "[마켓 추가/수수료 이벤트] 솜니아(SOMI) 원화 마켓 추가 (거래 수수료 무료) (거래 오픈 3시 30분 )",
            "post_url": "https://t.me/BithumbExchange/11404",
        },
    )

    assert first is not None
    assert second is None
    assert [call["ticker"] for call in buyer.calls] == ["SOMI"]
    assert [signal["ticker"] for signal in emitter.persisted] == ["SOMI"]


def test_out_of_order_lower_listing_after_non_listing_is_not_dropped(tmp_path):
    buyer = FakeSpotBuyer()
    emitter = FakeSignalEmitter()
    poller = make_poller(tmp_path, buyer, emitter)

    non_listing = poller.process_post(
        "bithumb",
        {
            "channel_handle": "BithumbExchange",
            "message_id": 200,
            "published_at": "2026-06-11T13:00:00+00:00",
            "title": "[빗썸 시세알림] *전일 23:59 기준 대비",
            "text": "[빗썸 시세알림] *전일 23:59 기준 대비",
            "post_url": "https://t.me/BithumbExchange/200",
        },
    )
    listing = poller.process_post(
        "bithumb",
        {
            "channel_handle": "BithumbExchange",
            "message_id": 199,
            "published_at": "2026-06-11T12:59:58+00:00",
            "title": "[마켓 추가] 자마(ZAMA) 원화 마켓 추가(거래 오픈 오후 5시 예정)",
            "text": "[마켓 추가] 자마(ZAMA) 원화 마켓 추가(거래 오픈 오후 5시 예정)",
            "post_url": "https://t.me/BithumbExchange/199",
        },
    )

    assert non_listing is None
    assert listing is not None
    assert listing["ticker"] == "ZAMA"
    assert [call["ticker"] for call in buyer.calls] == ["ZAMA"]


def test_native_trades_align_to_fresh_tickers_after_listing_dedupe(tmp_path):
    buyer = FakeSpotBuyer()
    emitter = FakeSignalEmitter()
    state_store = StateStore(tmp_path / "state.json")
    state_store.mark_listing_seen("bithumb", "WLFI", 991019)
    poller = ExchangeListingPoller(
        state_store=state_store,
        bybit_client=FakeBybitClient(),
        spot_buyer=buyer,
        signal_emitter=emitter,
        cpp_ultra_engine=DisabledUltraEngine(),
        enable_bybit_warmup=False,
        enable_channel_client=False,
        enable_cpp_ultra_warmup=False,
    )

    signal = poller.process_post(
        "bithumb",
        {
            "channel_handle": "BithumbExchange",
            "message_id": 991020,
            "published_at": "2026-06-11T13:00:00+00:00",
            "title": "[마켓 추가] 월드 리버티 파이낸셜(WLFI), 밈코어(M) 원화 마켓 추가",
            "text": "[마켓 추가] 월드 리버티 파이낸셜(WLFI), 밈코어(M) 원화 마켓 추가",
            "post_url": "https://t.me/BithumbExchange/991020",
            "native_listing": {
                "signal_type": "market_add",
                "ticker": "WLFI",
                "tickers": ["WLFI", "M"],
                "asset_name": "월드 리버티 파이낸셜",
                "markets": ["KRW"],
                "assets": [
                    {"ticker": "WLFI", "asset_name": "월드 리버티 파이낸셜"},
                    {"ticker": "M", "asset_name": "밈코어"},
                ],
            },
            "native_trades": [
                {
                    "ticker": "WLFI",
                    "symbol": "WLFIUSDT",
                    "order_link_id": "native-WLFI",
                    "reason": "native_preflight",
                },
                {
                    "ticker": "M",
                    "symbol": "MUSDT",
                    "order_link_id": "native-M",
                    "reason": "native_preflight",
                },
            ],
        },
    )

    assert signal is not None
    assert signal["ticker"] == "M"
    assert signal["trade"]["ticker"] == "M"
    assert signal["trade"]["order_link_id"] == "native-M"
    assert [item["ticker"] for item in emitter.persisted] == ["M"]
    assert [item["trade"]["ticker"] for item in emitter.persisted] == ["M"]
    assert buyer.calls == []


def test_missing_native_trade_for_fresh_ticker_uses_python_buy_fallback(tmp_path):
    buyer = FakeSpotBuyer()
    emitter = FakeSignalEmitter()
    state_store = StateStore(tmp_path / "state.json")
    state_store.mark_listing_seen("bithumb", "WLFI", 991021)
    poller = ExchangeListingPoller(
        state_store=state_store,
        bybit_client=FakeBybitClient(),
        spot_buyer=buyer,
        signal_emitter=emitter,
        cpp_ultra_engine=DisabledUltraEngine(),
        enable_bybit_warmup=False,
        enable_channel_client=False,
        enable_cpp_ultra_warmup=False,
    )

    signal = poller.process_post(
        "bithumb",
        {
            "channel_handle": "BithumbExchange",
            "message_id": 991022,
            "published_at": "2026-06-11T13:00:00+00:00",
            "title": "[마켓 추가] 월드 리버티 파이낸셜(WLFI), 밈코어(M) 원화 마켓 추가",
            "text": "[마켓 추가] 월드 리버티 파이낸셜(WLFI), 밈코어(M) 원화 마켓 추가",
            "post_url": "https://t.me/BithumbExchange/991022",
            "native_listing": {
                "signal_type": "market_add",
                "ticker": "WLFI",
                "tickers": ["WLFI", "M"],
                "asset_name": "월드 리버티 파이낸셜",
                "markets": ["KRW"],
                "assets": [
                    {"ticker": "WLFI", "asset_name": "월드 리버티 파이낸셜"},
                    {"ticker": "M", "asset_name": "밈코어"},
                ],
            },
            "native_trades": [
                {
                    "ticker": "WLFI",
                    "symbol": "WLFIUSDT",
                    "order_link_id": "native-WLFI",
                    "reason": "native_preflight",
                },
            ],
        },
    )

    assert signal is not None
    assert signal["ticker"] == "M"
    assert signal["trade"]["reason"] == "test_buyer"
    assert signal["trade"]["ticker"] == "M"
    assert signal["trade"]["order_link_id"] == "ls-bithumb-991022-M"
    assert [call["ticker"] for call in buyer.calls] == ["M"]
    assert [item["ticker"] for item in emitter.persisted] == ["M"]


def test_short_bulk_buy_result_keeps_listing_aligned_disabled_trade(tmp_path):
    class ShortBulkBuyer(FakeSpotBuyer):
        def buy_markets(self, orders: list[dict]) -> list[dict]:
            self.calls.extend(orders)
            return [{"reason": "first_only"}]

    buyer = ShortBulkBuyer()
    poller = ExchangeListingPoller(
        state_store=StateStore(tmp_path / "state.json"),
        bybit_client=FakeBybitClient(),
        spot_buyer=buyer,
        signal_emitter=FakeSignalEmitter(),
        cpp_ultra_engine=DisabledUltraEngine(),
        enable_bybit_warmup=False,
        enable_channel_client=False,
        enable_cpp_ultra_warmup=False,
    )

    trades = poller._maybe_buy_spots(
        channel=poller._channel_runtime_by_id["bithumb"],
        post={"message_id": 991025},
        listings=[{"ticker": "AAA"}, {"ticker": "BBB"}],
    )

    assert trades == [
        {
            "reason": "first_only",
            "ticker": "AAA",
            "order_link_id": "ls-bithumb-991025-AAA",
        },
        {
            "enabled": False,
            "attempted": False,
            "executed": False,
            "ticker": "BBB",
            "order_link_id": "ls-bithumb-991025-BBB",
            "reason": "python_spot_buyer_missing_result",
        },
    ]


def test_out_of_order_bulk_buy_result_aligns_by_ticker(tmp_path):
    class OutOfOrderBulkBuyer(FakeSpotBuyer):
        def buy_markets(self, orders: list[dict]) -> list[dict]:
            self.calls.extend(orders)
            return [
                {"ticker": "BBB", "reason": "bbb_result"},
                {"ticker": "AAA", "reason": "aaa_result"},
            ]

    buyer = OutOfOrderBulkBuyer()
    poller = ExchangeListingPoller(
        state_store=StateStore(tmp_path / "state.json"),
        bybit_client=FakeBybitClient(),
        spot_buyer=buyer,
        signal_emitter=FakeSignalEmitter(),
        cpp_ultra_engine=DisabledUltraEngine(),
        enable_bybit_warmup=False,
        enable_channel_client=False,
        enable_cpp_ultra_warmup=False,
    )

    trades = poller._maybe_buy_spots(
        channel=poller._channel_runtime_by_id["bithumb"],
        post={"message_id": 991027},
        listings=[{"ticker": "AAA"}, {"ticker": "BBB"}],
    )

    assert [call["ticker"] for call in buyer.calls] == ["AAA", "BBB"]
    assert trades == [
        {
            "ticker": "AAA",
            "reason": "aaa_result",
            "order_link_id": "ls-bithumb-991027-AAA",
        },
        {
            "ticker": "BBB",
            "reason": "bbb_result",
            "order_link_id": "ls-bithumb-991027-BBB",
        },
    ]


def test_empty_bulk_buy_result_keeps_disabled_trade_for_every_listing(tmp_path):
    class EmptyBulkBuyer(FakeSpotBuyer):
        def buy_markets(self, orders: list[dict]):
            self.calls.extend(orders)
            return None

    buyer = EmptyBulkBuyer()
    poller = ExchangeListingPoller(
        state_store=StateStore(tmp_path / "state.json"),
        bybit_client=FakeBybitClient(),
        spot_buyer=buyer,
        signal_emitter=FakeSignalEmitter(),
        cpp_ultra_engine=DisabledUltraEngine(),
        enable_bybit_warmup=False,
        enable_channel_client=False,
        enable_cpp_ultra_warmup=False,
    )

    trades = poller._maybe_buy_spots(
        channel=poller._channel_runtime_by_id["bithumb"],
        post={"message_id": 991026},
        listings=[{"ticker": "AAA"}, {"ticker": "BBB"}],
    )

    assert [trade["ticker"] for trade in trades] == ["AAA", "BBB"]
    assert [trade["reason"] for trade in trades] == [
        "python_spot_buyer_missing_result",
        "python_spot_buyer_missing_result",
    ]
    assert [trade["order_link_id"] for trade in trades] == [
        "ls-bithumb-991026-AAA",
        "ls-bithumb-991026-BBB",
    ]


def test_native_trade_without_ticker_keeps_positional_fallback():
    listings = [{"ticker": "WLFI"}, {"ticker": "M"}]
    native_trades = [
        {"order_link_id": "first-positional"},
        {"order_link_id": "second-positional"},
    ]

    aligned = ExchangeListingPoller._align_native_trades_to_listings(
        listings=listings,
        native_trades=native_trades,
    )

    assert [trade["order_link_id"] for trade in aligned] == [
        "first-positional",
        "second-positional",
    ]


def test_native_listing_string_tickers_are_not_split_into_characters(tmp_path):
    buyer = FakeSpotBuyer()
    emitter = FakeSignalEmitter()
    poller = make_poller(tmp_path, buyer, emitter)

    signal = poller.process_post(
        "bithumb",
        {
            "channel_handle": "BithumbExchange",
            "message_id": 991011,
            "published_at": "2026-06-11T13:00:00+00:00",
            "title": "native matched payload",
            "text": "native matched payload",
            "post_url": "https://t.me/BithumbExchange/991011",
            "native_listing": {
                "signal_type": "market_add",
                "ticker": "WLFI",
                "tickers": "WLFI",
                "asset_name": "월드 리버티 파이낸셜",
                "markets": "KRW",
            },
        },
    )

    assert signal is not None
    assert signal["ticker"] == "WLFI"
    assert signal["markets"] == ["KRW"]
    assert [call["ticker"] for call in buyer.calls] == ["WLFI"]
    assert [item["ticker"] for item in emitter.persisted] == ["WLFI"]


def test_duplicate_native_trade_payload_is_persisted_without_rebuy(tmp_path):
    buyer = FakeSpotBuyer()
    emitter = FakeSignalEmitter()
    poller = make_poller(tmp_path, buyer, emitter)
    post = {
        "channel_handle": "BithumbExchange",
        "message_id": 991012,
        "published_at": "2026-06-11T13:00:00+00:00",
        "title": "[마켓 추가] 월드 리버티 파이낸셜(WLFI) 원화 마켓 추가",
        "text": "[마켓 추가] 월드 리버티 파이낸셜(WLFI) 원화 마켓 추가",
        "post_url": "https://t.me/BithumbExchange/991012",
        "native_listing": {
            "signal_type": "market_add",
            "ticker": "WLFI",
            "tickers": ["WLFI"],
            "asset_name": "월드 리버티 파이낸셜",
            "markets": ["KRW"],
        },
    }

    first = poller.process_post("bithumb", dict(post))
    duplicate = poller.process_post(
        "bithumb",
        {
            **post,
            "native_trades": [
                {
                    "ticker": "WLFI",
                    "attempted": True,
                    "executed": True,
                    "reason": "tdlib_native_rest_preflight",
                }
            ],
        },
    )

    assert first is not None
    assert duplicate is None
    assert [call["ticker"] for call in buyer.calls] == ["WLFI"]
    assert [item["ticker"] for item in emitter.persisted] == ["WLFI", "WLFI"]
    assert emitter.persisted[-1]["trade"]["executed"] is True
    assert emitter.persisted[-1]["trade"]["reason"] == "tdlib_native_rest_preflight"
    assert [item["ticker"] for item in emitter.trade_proofs] == ["WLFI"]
    assert emitter.trade_proofs[-1]["trade"]["executed"] is True


def test_invalid_native_listing_falls_back_to_title_classifier(tmp_path):
    buyer = FakeSpotBuyer()
    emitter = FakeSignalEmitter()
    poller = make_poller(tmp_path, buyer, emitter)

    signal = poller.process_post(
        "bithumb",
        {
            "channel_handle": "BithumbExchange",
            "message_id": 991013,
            "published_at": "2026-06-11T13:00:00+00:00",
            "title": "[마켓 추가] 밈코어(M) 원화 마켓 추가",
            "text": "[마켓 추가] 밈코어(M) 원화 마켓 추가",
            "post_url": "https://t.me/BithumbExchange/991013",
            "native_listing": "malformed",
        },
    )

    assert signal is not None
    assert signal["ticker"] == "M"
    assert [call["ticker"] for call in buyer.calls] == ["M"]
