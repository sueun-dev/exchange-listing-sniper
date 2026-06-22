"""Listing announcement filters for exchange Telegram posts."""

from __future__ import annotations

from collections.abc import Callable

from .native_classifier import get_native_classifier_manager

MARKET_CODES = {"KRW", "BTC", "USDT", "ETH"}

UPBIT_LISTING_KEYWORDS = (
    "신규 거래지원",
    "KRW 마켓 디지털 자산 추가",
    "BTC 마켓 디지털 자산 추가",
    "USDT 마켓 디지털 자산 추가",
)
UPBIT_EXCLUDE_KEYWORDS = (
    "입출금",
    "유통량",
    "거래유의",
    "유의종목",
    "스테이킹",
    "이벤트",
    "종료",
    "변경 안내",
)
BITHUMB_LISTING_KEYWORDS = (
    "[마켓 추가]",
    "원화 마켓 추가",
)
BITHUMB_EXCLUDE_KEYWORDS = (
    "입출금",
    "유의촉구",
    "거래유의",
    "시세알림",
    "종료",
)
BITHUMB_ALLOWED_MARKET_ADD_SUFFIXES = {
    "",
    "및 재단 에어드랍 안내",
    "및 에어드랍 안내",
}
BITHUMB_LISTING_PREFIXES = (
    "[마켓 추가]",
    "[마켓 추가/수수료 이벤트]",
)
BITHUMB_ALLOWED_FEE_SUFFIX_PREFIXES = (
    "(거래 수수료 무료)",
    "(거래수수료 무료)",
)
BITHUMB_BLOCKED_MARKET_ADD_SUFFIX_KEYWORDS = (
    "시간 변경",
    "연기",
    "입출금",
    "재거래지원",
    "유의",
    "중단",
    "종료",
)


def _is_word_boundary_char(char: str) -> bool:
    return not (char.isalnum() or char == "_")


def _first_ticker_paren_open(title: str) -> int:
    """Index of the '(' of the first ticker parenthetical after the bracket."""
    bracket_idx = title.find("]")
    search = 0 if bracket_idx < 0 else bracket_idx + 1
    while True:
        open_idx = title.find("(", search)
        if open_idx < 0:
            return -1
        close_idx = title.find(")", open_idx + 1)
        if close_idx < 0:
            return -1
        candidate = title[open_idx + 1 : close_idx].strip()
        if _is_symbol_token(candidate) and candidate not in MARKET_CODES:
            return open_idx
        search = close_idx + 1


def _exclude_scan_text(title: str) -> str:
    """Title with the asset-name span blanked for exclude-keyword scanning.

    Exclude keywords (입출금/종료/이벤트/유의...) describe the NOTICE TYPE and appear
    in the structural prefix/tail — never as a reason to skip an asset whose own
    Korean name merely contains those letters (e.g. 이벤트체인(EVENT)). Blanking the
    name region between ']' and the first ticker '(' stops a genuine listing from
    being dropped, while the bracket prefix and the tail after the ticker are
    still scanned (so a real 종료/입출금 notice is still excluded).
    """
    bracket_idx = title.find("]")
    if bracket_idx < 0:
        return title
    name_start = bracket_idx + 1
    open_idx = _first_ticker_paren_open(title)
    if open_idx < 0 or open_idx <= name_start:
        return title
    return title[:name_start] + (" " * (open_idx - name_start)) + title[open_idx:]


def _remainder_is_only_parentheticals(text: str) -> bool:
    """True when `text` is blank or only `(...)` groups (trailing scheduling info)."""
    text = text.strip()
    while text:
        if not text.startswith("("):
            return False
        close = text.find(")")
        if close < 0:
            return False
        text = text[close + 1 :].strip()
    return True


def _contains_token(title: str, token: str) -> bool:
    start = title.find(token)
    token_len = len(token)
    while start != -1:
        end = start + token_len
        left_ok = start == 0 or _is_word_boundary_char(title[start - 1])
        right_ok = end == len(title) or _is_word_boundary_char(title[end])
        if left_ok and right_ok:
            return True
        start = title.find(token, start + 1)
    return False


def _is_symbol_token(value: str) -> bool:
    # A ticker is 1-10 ASCII chars of [A-Z0-9] with at least one letter. The
    # letter requirement rejects all-digit parentheticals like a year (2024) or
    # an amount, which would otherwise be mis-read as the ticker and buy the
    # wrong symbol. Real tickers with digits (e.g. 2Z, B3) keep a letter.
    return (
        1 <= len(value) <= 10
        and value.isascii()
        and any(char.isupper() for char in value)
        and all(char.isupper() or char.isdigit() for char in value)
    )


def _parse_market_parenthetical(value: str) -> list[str] | None:
    candidate = value.strip()
    if not candidate.endswith("마켓"):
        return None
    prefix = candidate[: -len("마켓")].strip()
    if not prefix:
        return None
    markets: list[str] = []
    for part in prefix.split(","):
        market = part.strip()
        if market not in MARKET_CODES:
            return None
        markets.append(market)
    return markets or None


def _find_market_parenthetical_end(title: str, start: int = 0) -> int | None:
    search = max(0, start)
    while True:
        open_idx = title.find("(", search)
        if open_idx < 0:
            return None
        close_idx = title.find(")", open_idx + 1)
        if close_idx < 0:
            return None
        candidate = title[open_idx + 1 : close_idx]
        if _parse_market_parenthetical(candidate) is not None:
            return close_idx + 1
        search = close_idx + 1


def _collect_parenthesized_tokens(title: str) -> list[str]:
    tokens: list[str] = []
    start = 0
    while True:
        open_idx = title.find("(", start)
        if open_idx < 0:
            return tokens
        close_idx = title.find(")", open_idx + 1)
        if close_idx < 0:
            return tokens
        candidate = title[open_idx + 1 : close_idx].strip()
        if _is_symbol_token(candidate):
            tokens.append(candidate)
        start = close_idx + 1


def _normalize_asset_segment(value: str) -> str:
    segment = value.strip().lstrip(",").strip()
    for prefix in ("및 ", "and ", "& ", "/ ", "· "):
        if segment.startswith(prefix):
            return segment[len(prefix) :].strip()
    return segment


def _collect_asset_ticker_pairs(title: str) -> list[dict[str, str]]:
    pairs: list[dict[str, str]] = []
    bracket_idx = title.find("]")
    name_start = 0 if bracket_idx < 0 else bracket_idx + 1
    search = name_start
    seen: set[str] = set()
    while True:
        open_idx = title.find("(", search)
        if open_idx < 0:
            return pairs
        close_idx = title.find(")", open_idx + 1)
        if close_idx < 0:
            return pairs
        candidate = title[open_idx + 1 : close_idx].strip()
        if _is_symbol_token(candidate) and candidate not in MARKET_CODES and candidate not in seen:
            asset_name = _normalize_asset_segment(title[name_start:open_idx])
            if asset_name:
                pairs.append({"ticker": candidate, "asset_name": asset_name})
                seen.add(candidate)
            name_start = close_idx + 1
        search = close_idx + 1


def _extract_asset_name_fast(title: str) -> str:
    bracket_idx = title.find("]")
    start = 0 if bracket_idx < 0 else bracket_idx + 1
    open_idx = title.find("(", start)
    if open_idx < 0:
        return title.strip()
    asset_name = title[start:open_idx].strip()
    return asset_name or title.strip()


def _parse_listing_title_fields(title: str) -> tuple[str | None, str, list[str]]:
    pairs = _collect_asset_ticker_pairs(title)
    candidates = _collect_parenthesized_tokens(title)
    ticker = pairs[0]["ticker"] if pairs else None

    markets: list[str] = []
    if "원화 마켓" in title:
        markets.append("KRW")
    for market in ("KRW", "BTC", "USDT", "ETH"):
        if _contains_token(title, market) and market not in markets:
            markets.append(market)
    if not markets:
        for candidate in candidates:
            if candidate in MARKET_CODES and candidate not in markets:
                markets.append(candidate)

    asset_name = pairs[0]["asset_name"] if pairs else _extract_asset_name_fast(title)
    return ticker, asset_name, markets


def extract_ticker_candidates(title: str) -> list[str]:
    return _collect_parenthesized_tokens(title)


def extract_listing_assets(title: str) -> list[dict[str, str]]:
    return _collect_asset_ticker_pairs(title)


def has_multiple_listing_assets_fast(title: str) -> bool:
    """Cheap hot-path check for multi-ticker notices without building asset dicts."""
    bracket_idx = title.find("]")
    search = 0 if bracket_idx < 0 else bracket_idx + 1
    first_ticker: str | None = None
    while True:
        open_idx = title.find("(", search)
        if open_idx < 0:
            return False
        close_idx = title.find(")", open_idx + 1)
        if close_idx < 0:
            return False
        candidate = title[open_idx + 1 : close_idx].strip()
        if _is_symbol_token(candidate) and candidate not in MARKET_CODES:
            if first_ticker is None:
                first_ticker = candidate
            elif candidate != first_ticker:
                return True
        search = close_idx + 1


def extract_primary_ticker(title: str) -> str | None:
    ticker, _, _ = _parse_listing_title_fields(title)
    return ticker


def extract_markets(title: str) -> list[str]:
    _, _, markets = _parse_listing_title_fields(title)
    return markets


def extract_asset_name(title: str) -> str:
    _, asset_name, _ = _parse_listing_title_fields(title)
    return asset_name


def has_krw_market(title: str) -> bool:
    markets = extract_markets(title)
    return "KRW" in markets


def _has_upbit_krw_market(title: str) -> bool:
    return _contains_token(title, "KRW")


def _has_bithumb_won_market(title: str) -> bool:
    return "원화 마켓" in title


def _has_bithumb_listing_prefix(title: str) -> bool:
    return any(title.startswith(prefix) for prefix in BITHUMB_LISTING_PREFIXES)


def _is_allowed_bithumb_market_add_suffix(suffix: str) -> bool:
    suffix = suffix.strip()
    if suffix in BITHUMB_ALLOWED_MARKET_ADD_SUFFIXES:
        return True
    if any(keyword in suffix for keyword in BITHUMB_BLOCKED_MARKET_ADD_SUFFIX_KEYWORDS):
        return False
    if suffix.startswith(BITHUMB_ALLOWED_FEE_SUFFIX_PREFIXES):
        return True
    if "거래 오픈" in suffix or "거래 개시" in suffix:
        return True
    # A symbol-rename re-announcement (e.g. 젠신(AIGENSYN)->AI) is a genuine
    # tradeable 원화 마켓 추가, so treat the rename suffix as actionable.
    if "심볼명 변경" in suffix or "심볼 변경" in suffix:
        return True
    return suffix.startswith("및 ") and suffix.endswith(" 안내")


def _is_upbit_listing(title: str) -> bool:
    if not title.startswith("[거래]"):
        return False
    scan = _exclude_scan_text(title)
    if any(keyword in scan for keyword in UPBIT_EXCLUDE_KEYWORDS):
        return False
    if "신규 거래지원 안내" in title:
        market_end = _find_market_parenthetical_end(
            title,
            start=title.find("신규 거래지원 안내"),
        )
        return market_end is not None and _remainder_is_only_parentheticals(
            title[market_end:]
        )
    marker = "마켓 디지털 자산 추가"
    marker_idx = title.rfind(marker)
    if marker_idx < 0:
        return False
    return _remainder_is_only_parentheticals(title[marker_idx + len(marker) :])


def _is_bithumb_listing(title: str) -> bool:
    if not _has_bithumb_listing_prefix(title):
        return False
    if any(keyword in _exclude_scan_text(title) for keyword in BITHUMB_EXCLUDE_KEYWORDS):
        return False
    if "원화 마켓 재거래지원 안내" in title:
        return False
    marker = "원화 마켓 추가"
    marker_idx = title.find(marker)
    if marker_idx < 0:
        return False
    return _is_allowed_bithumb_market_add_suffix(
        title[marker_idx + len(marker) :]
    )


def _attach_listing_context(
    listing: dict[str, object],
    *,
    exchange: str,
    display_name: str,
    title: str = "",
) -> dict[str, object]:
    listing["exchange"] = exchange
    listing["display_name"] = display_name
    if title:
        assets = extract_listing_assets(title)
        if assets:
            tickers = [asset["ticker"] for asset in assets]
            listing["tickers"] = tickers
            listing["assets"] = assets
            listing.setdefault("ticker", tickers[0])
            listing.setdefault("asset_name", assets[0]["asset_name"])
    return listing


def classify_listing_post(post: dict, channel_config: dict) -> dict | None:
    title = post.get("title", "")
    exchange = channel_config["exchange"]
    native_manager = get_native_classifier_manager()
    native_backend = native_manager.get_backend()
    if native_backend is not None:
        try:
            native_listing = native_backend.classify_dict(exchange, title)
        except Exception:
            native_listing = None
        else:
            if native_listing is not None:
                return _attach_listing_context(
                    native_listing,
                    exchange=exchange,
                    display_name=channel_config["display_name"],
                    title=title,
                )

    return classify_listing_title_python(
        exchange=exchange,
        title=title,
        display_name=channel_config["display_name"],
    )


def make_listing_title_classifier(
    *,
    exchange: str,
    display_name: str,
    minimal: bool = False,
) -> Callable[[str], dict | None]:
    def _classify_python(title: str) -> dict | None:
        return classify_listing_title_python(
            exchange=exchange,
            title=title,
            display_name=display_name,
            include_details=not minimal,
        )

    native_manager = get_native_classifier_manager()
    native_backend = native_manager.get_backend()
    if native_backend is not None:
        bound_backend = native_backend.bind(exchange)

        def _classify_native(title: str) -> dict | None:
            try:
                if minimal:
                    listing = bound_backend.classify_minimal_dict(title)
                else:
                    listing = bound_backend.classify_dict(title)
            except Exception:
                return _classify_python(title)
            if listing is None:
                return _classify_python(title)
            return _attach_listing_context(
                listing,
                exchange=exchange,
                display_name=display_name,
                title=title,
            )

        return _classify_native

    return _classify_python


def classify_listing_title_python(
    *,
    exchange: str,
    title: str,
    display_name: str,
    include_details: bool = True,
) -> dict | None:

    if exchange == "upbit":
        is_listing = _is_upbit_listing(title)
        signal_type = "new_listing"
    elif exchange == "bithumb":
        is_listing = _is_bithumb_listing(title)
        signal_type = "market_add"
    else:
        return None

    if not is_listing:
        return None

    if exchange == "upbit" and not _has_upbit_krw_market(title):
        return None
    if exchange == "bithumb" and not _has_bithumb_won_market(title):
        return None

    ticker, asset_name, markets = _parse_listing_title_fields(title)
    if not ticker:
        return None
    assets = extract_listing_assets(title)
    tickers = [asset["ticker"] for asset in assets] or [ticker]

    listing = {
        "exchange": exchange,
        "display_name": display_name,
        "signal_type": signal_type,
        "ticker": ticker,
        "tickers": tickers,
    }
    if assets:
        listing["assets"] = assets
    if include_details:
        listing["asset_name"] = asset_name
        listing["markets"] = markets
    return listing
