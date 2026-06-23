"""Native listing classifier backends and winner selection."""

from __future__ import annotations

import ctypes
import json
import logging
import platform
import time
from dataclasses import dataclass
from pathlib import Path

from .env_loader import MODULE_DIR, load_env_settings

logger = logging.getLogger(__name__)

MARKET_FLAG_KRW = 1
MARKET_FLAG_BTC = 2
MARKET_FLAG_USDT = 4
MARKET_FLAG_ETH = 8

MARKET_FLAGS = (
    ("KRW", MARKET_FLAG_KRW),
    ("BTC", MARKET_FLAG_BTC),
    ("USDT", MARKET_FLAG_USDT),
    ("ETH", MARKET_FLAG_ETH),
)

BENCHMARK_TITLE_POSITIVE = "[거래] 베니스토큰(VVV) 신규 거래지원 안내 (KRW, BTC 마켓)"
BENCHMARK_TITLE_NEGATIVE = "[거래] 유통량 계획표 변경 안내 : 오르카(ORCA)"
BENCHMARK_ITERS = 20_000
SEMANTIC_CANARIES = (
    (
        "bithumb",
        "[마켓 추가/수수료 이벤트] 팔콘 파이낸스(FF) 원화 마켓 추가 (거래 수수료 무료)",
        {"signal_type": "market_add", "ticker": "FF", "markets": ["KRW"]},
    ),
    (
        "bithumb",
        "[마켓 추가] 밈코어(M) 원화 마켓 추가",
        {"signal_type": "market_add", "ticker": "M", "markets": ["KRW"]},
    ),
    (
        "bithumb",
        "[마켓 추가] 비쓰리(B3) 원화 마켓 추가 (거래 오픈 시간 변경)",
        None,
    ),
)

SETTINGS = load_env_settings(
    {
        "LISTING_CLASSIFIER_BACKEND",
        "LISTING_NATIVE_BENCHMARK_PATH",
    }
)


def _library_suffix() -> str:
    system = platform.system().lower()
    if system == "darwin":
        return ".dylib"
    if system == "windows":
        return ".dll"
    return ".so"


BENCHMARK_CACHE_PATH = Path(
    SETTINGS.get("LISTING_NATIVE_BENCHMARK_PATH")
    or MODULE_DIR / "data" / "native_classifier_benchmark.json"
)


class ListingResultStruct(ctypes.Structure):
    _fields_ = [
        ("matched", ctypes.c_int),
        ("market_flags", ctypes.c_uint32),
        ("ticker", ctypes.c_char * 16),
        ("asset_name", ctypes.c_char * 128),
        ("signal_type", ctypes.c_char * 16),
    ]


@dataclass
class NativeListingResult:
    signal_type: str
    ticker: str
    asset_name: str
    markets: list[str]

    def to_dict(self) -> dict[str, object]:
        return {
            "signal_type": self.signal_type,
            "ticker": self.ticker,
            "asset_name": self.asset_name,
            "markets": list(self.markets),
        }


def _decode_c_string(value: bytes) -> str:
    return value.split(b"\0", 1)[0].decode("utf-8", errors="ignore")


_MARKETS_FROM_FLAGS_TABLE = tuple(
    tuple(name for name, bit in MARKET_FLAGS if flags & bit)
    for flags in range(16)
)


def _markets_from_flags(flags: int) -> list[str]:
    if 0 <= flags < len(_MARKETS_FROM_FLAGS_TABLE):
        return list(_MARKETS_FROM_FLAGS_TABLE[flags])
    return [name for name, bit in MARKET_FLAGS if flags & bit]


def _listing_dict_from_struct(result: ListingResultStruct) -> dict[str, object]:
    return {
        "signal_type": _decode_c_string(result.signal_type),
        "ticker": _decode_c_string(result.ticker),
        "asset_name": _decode_c_string(result.asset_name),
        "markets": _markets_from_flags(int(result.market_flags)),
    }


def _minimal_listing_dict_from_struct(result: ListingResultStruct) -> dict[str, object]:
    return {
        "signal_type": _decode_c_string(result.signal_type),
        "ticker": _decode_c_string(result.ticker),
    }


class BoundNativeClassifierBackend:
    def __init__(self, backend: NativeClassifierBackend, exchange: str):
        self._backend = backend
        self._exchange = exchange
        self._exchange_bytes = exchange.encode("utf-8")

    def classify(self, title: str) -> NativeListingResult | None:
        return self._backend.classify(self._exchange, title)

    def classify_dict(self, title: str) -> dict[str, object] | None:
        result = self._backend._run_classify_encoded(self._exchange_bytes, title)
        if result is None:
            return None
        return _listing_dict_from_struct(result)

    def classify_minimal_dict(self, title: str) -> dict[str, object] | None:
        result = self._backend._run_classify_encoded(self._exchange_bytes, title)
        if result is None:
            return None
        return _minimal_listing_dict_from_struct(result)


class NativeClassifierBackend:
    def __init__(self, name: str, library_path: Path):
        self.name = name
        self.library_path = library_path
        self._lib = ctypes.CDLL(str(library_path))
        self._classify = self._lib.classify_listing_title
        self._classify.argtypes = [
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.POINTER(ListingResultStruct),
        ]
        self._classify.restype = ctypes.c_int

    def _run_classify_encoded(self, exchange: bytes, title: str) -> ListingResultStruct | None:
        result = ListingResultStruct()
        status = self._classify(
            exchange,
            title.encode("utf-8"),
            ctypes.byref(result),
        )
        if status < 0:
            raise RuntimeError(f"{self.name} classifier returned error {status}")
        if result.matched == 0:
            return None
        return result

    def _run_classify(self, exchange: str, title: str) -> ListingResultStruct | None:
        return self._run_classify_encoded(exchange.encode("utf-8"), title)

    def classify(self, exchange: str, title: str) -> NativeListingResult | None:
        result = self._run_classify(exchange, title)
        if result is None:
            return None
        return NativeListingResult(
            signal_type=_decode_c_string(result.signal_type),
            ticker=_decode_c_string(result.ticker),
            asset_name=_decode_c_string(result.asset_name),
            markets=_markets_from_flags(int(result.market_flags)),
        )

    def classify_dict(self, exchange: str, title: str) -> dict[str, object] | None:
        result = self._run_classify(exchange, title)
        if result is None:
            return None
        return _listing_dict_from_struct(result)

    def classify_minimal_dict(self, exchange: str, title: str) -> dict[str, object] | None:
        result = self._run_classify(exchange, title)
        if result is None:
            return None
        return _minimal_listing_dict_from_struct(result)

    def bind(self, exchange: str) -> BoundNativeClassifierBackend:
        return BoundNativeClassifierBackend(self, exchange)


def _native_library_paths() -> dict[str, Path]:
    suffix = _library_suffix()
    return {
        "cpp": MODULE_DIR / "bin" / f"liblisting_classifier_cpp{suffix}",
        "rust": MODULE_DIR / "bin" / f"liblisting_classifier_rust{suffix}",
    }


class NativeClassifierManager:
    """Load available native classifiers and select the measured winner."""

    def __init__(self):
        self._preferred = (
            SETTINGS.get("LISTING_CLASSIFIER_BACKEND", "auto").strip().lower()
            or "auto"
        )
        self._selected_backend: NativeClassifierBackend | None = None
        self._resolved = False
        self._load_error_logged = False

    def classify(self, exchange: str, title: str) -> dict[str, object] | None:
        backend = self._get_backend()
        if backend is None:
            raise RuntimeError("native_classifier_unavailable")
        result = backend.classify(exchange, title)
        return None if result is None else result.to_dict()

    def available_backend_names(self) -> list[str]:
        return list(self._load_available_backends().keys())

    def get_backend(self) -> NativeClassifierBackend | None:
        return self._get_backend()

    def _get_backend(self) -> NativeClassifierBackend | None:
        if self._resolved:
            return self._selected_backend

        available = self._load_available_backends()
        if not available:
            self._resolved = True
            return None

        selected_name: str | None = None
        if self._preferred in available:
            selected_name = self._preferred
        elif self._preferred == "python":
            self._resolved = True
            return None
        elif self._preferred == "auto":
            selected_name = self._read_cached_winner(available)
            if selected_name is None:
                selected_name = self._benchmark_and_cache(available)
        else:
            logger.warning(
                "Unknown LISTING_CLASSIFIER_BACKEND=%s, falling back to auto",
                self._preferred,
            )
            selected_name = self._read_cached_winner(available)
            if selected_name is None:
                selected_name = self._benchmark_and_cache(available)

        self._selected_backend = available.get(selected_name or "")
        self._resolved = True
        return self._selected_backend

    def _load_available_backends(self) -> dict[str, NativeClassifierBackend]:
        available: dict[str, NativeClassifierBackend] = {}
        for name, path in _native_library_paths().items():
            if not path.exists():
                continue
            try:
                backend = NativeClassifierBackend(name=name, library_path=path)
            except Exception as exc:
                if not self._load_error_logged:
                    logger.warning("Native classifier load failed for %s: %s", name, exc)
                self._load_error_logged = True
                continue
            if not self._backend_matches_semantic_canaries(backend):
                logger.warning(
                    "Native classifier %s failed semantic canary checks; ignoring %s",
                    name,
                    path,
                )
                continue
            available[name] = backend
        return available

    @staticmethod
    def _backend_matches_semantic_canaries(backend: NativeClassifierBackend) -> bool:
        for exchange, title, expected in SEMANTIC_CANARIES:
            result = backend.classify_dict(exchange, title)
            if expected is None:
                if result is not None:
                    return False
                continue
            if result is None:
                return False
            for key, value in expected.items():
                if result.get(key) != value:
                    return False
        return True

    def _read_cached_winner(
        self,
        available: dict[str, NativeClassifierBackend],
    ) -> str | None:
        try:
            payload = json.loads(BENCHMARK_CACHE_PATH.read_text())
        except Exception:
            return None
        winner = str(payload.get("winner", "")).strip().lower()
        return winner if winner in available else None

    def _benchmark_and_cache(
        self,
        available: dict[str, NativeClassifierBackend],
    ) -> str | None:
        if len(available) == 1:
            winner = next(iter(available))
            self._write_cache({"winner": winner, "single_backend": True})
            return winner

        metrics: dict[str, dict[str, float]] = {}
        for name, backend in available.items():
            p50_us = self._benchmark_backend_us(backend)
            metrics[name] = {"p50_us": p50_us}
        winner = min(metrics, key=lambda name: metrics[name]["p50_us"])
        self._write_cache(
            {
                "winner": winner,
                "benchmark_positive_title": BENCHMARK_TITLE_POSITIVE,
                "benchmark_negative_title": BENCHMARK_TITLE_NEGATIVE,
                "iterations": BENCHMARK_ITERS,
                "metrics": metrics,
            }
        )
        logger.info("Native classifier winner selected: %s", winner)
        return winner

    def _benchmark_backend_us(self, backend: NativeClassifierBackend) -> float:
        samples_ns: list[int] = []
        for i in range(BENCHMARK_ITERS):
            exchange = "upbit" if i % 2 == 0 else "bithumb"
            title = BENCHMARK_TITLE_POSITIVE if i % 3 else BENCHMARK_TITLE_NEGATIVE
            start_ns = time.perf_counter_ns()
            backend.classify(exchange, title)
            samples_ns.append(time.perf_counter_ns() - start_ns)
        ordered = sorted(samples_ns)
        return ordered[len(ordered) // 2] / 1_000.0

    def _write_cache(self, payload: dict[str, object]):
        BENCHMARK_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        BENCHMARK_CACHE_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2)
        )


_MANAGER: NativeClassifierManager | None = None


def get_native_classifier_manager() -> NativeClassifierManager:
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = NativeClassifierManager()
    return _MANAGER
