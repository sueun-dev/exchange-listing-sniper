"""Listing title classifier accuracy tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))
for module_name in list(sys.modules):
    if module_name == "src" or module_name.startswith("src."):
        sys.modules.pop(module_name, None)

from src.announcement_filter import (  # noqa: E402
    classify_listing_title_python,
    extract_listing_assets,
    make_listing_title_classifier,
)
from src.native_classifier import (  # noqa: E402
    NativeClassifierBackend,
    NativeClassifierManager,
    _native_library_paths,
)


CASES_PATH = Path(__file__).parent / "fixtures" / "listing_title_cases.json"
CASES = json.loads(CASES_PATH.read_text(encoding="utf-8"))
ACTIONABLE_CASES = [case for case in CASES if case["expected"] is not None]
FEE_EVENT_CASE = next(
    case for case in CASES if case["id"] == "bithumb_fee_event_single"
)


class _FakeNativeManager:
    def __init__(self, backend):
        self._backend = backend

    def get_backend(self):
        return self._backend


class _FakeNativeBackend:
    def __init__(self, bound_backend):
        self._bound_backend = bound_backend

    def bind(self, exchange: str):
        return self._bound_backend


class _MissingBoundBackend:
    def classify_dict(self, title: str):
        return None

    def classify_minimal_dict(self, title: str):
        return None


class _FailingBoundBackend:
    def classify_dict(self, title: str):
        raise RuntimeError("native miss")

    def classify_minimal_dict(self, title: str):
        raise RuntimeError("native miss")


def classify_python(exchange: str, title: str) -> dict | None:
    return classify_listing_title_python(
        exchange=exchange,
        title=title,
        display_name=exchange,
    )


def case_id(case: dict) -> str:
    return case["id"]


def assert_listing_matches(
    listing: dict | None,
    expected: dict | None,
    *,
    include_assets: bool,
):
    if expected is None:
        assert listing is None
        return

    assert listing is not None
    for key in ("signal_type", "ticker", "asset_name", "markets"):
        assert listing[key] == expected[key]
    if include_assets:
        assert listing["tickers"] == expected["tickers"]
        assert listing["assets"] == expected["assets"]


@pytest.mark.parametrize("case", CASES, ids=case_id)
def test_python_classifier_matches_golden_cases(case):
    listing = classify_python(case["exchange"], case["title"])

    assert_listing_matches(listing, case["expected"], include_assets=True)


@pytest.mark.parametrize("case", CASES, ids=case_id)
def test_default_classifier_matches_golden_cases(case):
    classifier = make_listing_title_classifier(
        exchange=case["exchange"],
        display_name=case["exchange"],
    )

    listing = classifier(case["title"])

    assert_listing_matches(listing, case["expected"], include_assets=True)


def test_default_classifier_falls_back_to_python_when_native_backend_misses(monkeypatch):
    monkeypatch.setattr(
        "src.announcement_filter.get_native_classifier_manager",
        lambda: _FakeNativeManager(_FakeNativeBackend(_MissingBoundBackend())),
    )
    classifier = make_listing_title_classifier(
        exchange=FEE_EVENT_CASE["exchange"],
        display_name=FEE_EVENT_CASE["exchange"],
    )

    listing = classifier(FEE_EVENT_CASE["title"])

    assert_listing_matches(
        listing,
        FEE_EVENT_CASE["expected"],
        include_assets=True,
    )


def test_minimal_classifier_falls_back_to_python_when_native_backend_fails(monkeypatch):
    monkeypatch.setattr(
        "src.announcement_filter.get_native_classifier_manager",
        lambda: _FakeNativeManager(_FakeNativeBackend(_FailingBoundBackend())),
    )
    classifier = make_listing_title_classifier(
        exchange=FEE_EVENT_CASE["exchange"],
        display_name=FEE_EVENT_CASE["exchange"],
        minimal=True,
    )

    listing = classifier(FEE_EVENT_CASE["title"])

    assert listing is not None
    assert listing["signal_type"] == FEE_EVENT_CASE["expected"]["signal_type"]
    assert listing["ticker"] == FEE_EVENT_CASE["expected"]["ticker"]
    assert listing["tickers"] == FEE_EVENT_CASE["expected"]["tickers"]
    assert listing["assets"] == FEE_EVENT_CASE["expected"]["assets"]
    assert "markets" not in listing


@pytest.mark.parametrize("case", CASES, ids=case_id)
def test_cpp_classifier_matches_golden_cases_primary_fields(case):
    library_path = _native_library_paths()["cpp"]
    if not library_path.exists():
        pytest.skip("C++ native classifier library is not built")

    backend = NativeClassifierBackend(name="cpp", library_path=library_path)

    listing = backend.classify_dict(case["exchange"], case["title"])

    assert_listing_matches(listing, case["expected"], include_assets=False)


@pytest.mark.parametrize("case", ACTIONABLE_CASES, ids=case_id)
def test_extract_listing_assets_matches_golden_cases(case):
    assert extract_listing_assets(case["title"]) == case["expected"]["assets"]


def test_native_manager_ignores_stale_rust_classifier_when_present():
    rust_path = _native_library_paths()["rust"]
    if not rust_path.exists():
        pytest.skip("Rust native classifier library is not present")

    manager = NativeClassifierManager()
    names = manager.available_backend_names()

    if "rust" in names:
        backend = NativeClassifierBackend(name="rust", library_path=rust_path)
        assert manager._backend_matches_semantic_canaries(backend)
    else:
        backend = NativeClassifierBackend(name="rust", library_path=rust_path)
        assert not manager._backend_matches_semantic_canaries(backend)
