use std::ffi::CStr;
use std::os::raw::{c_char, c_int};

const MARKET_FLAG_KRW: u32 = 1;
const MARKET_FLAG_BTC: u32 = 2;
const MARKET_FLAG_USDT: u32 = 4;
const MARKET_FLAG_ETH: u32 = 8;

const UPBIT_EXCLUDE_KEYWORDS: [&str; 8] = [
    "입출금",
    "유통량",
    "거래유의",
    "유의종목",
    "스테이킹",
    "이벤트",
    "종료",
    "변경 안내",
];
const BITHUMB_EXCLUDE_KEYWORDS: [&str; 5] = ["입출금", "유의촉구", "거래유의", "시세알림", "종료"];

fn has_bithumb_listing_prefix(title: &str) -> bool {
    title.starts_with("[마켓 추가]") || title.starts_with("[마켓 추가/수수료 이벤트]")
}

#[repr(C)]
pub struct NativeListingResult {
    matched: c_int,
    market_flags: u32,
    ticker: [c_char; 16],
    asset_name: [c_char; 128],
    signal_type: [c_char; 16],
}

fn contains_none(title: &str, keywords: &[&str]) -> bool {
    keywords.iter().all(|keyword| !title.contains(keyword))
}

fn is_allowed_bithumb_market_add_suffix(suffix: &str) -> bool {
    let trimmed = suffix.trim();
    if trimmed.is_empty() || trimmed == "및 재단 에어드랍 안내" || trimmed == "및 에어드랍 안내"
    {
        return true;
    }
    const BLOCKED: [&str; 7] = [
        "시간 변경",
        "연기",
        "입출금",
        "재거래지원",
        "유의",
        "중단",
        "종료",
    ];
    if BLOCKED.iter().any(|keyword| trimmed.contains(keyword)) {
        return false;
    }
    trimmed.starts_with("(거래 수수료 무료)")
        || trimmed.starts_with("(거래수수료 무료)")
        || trimmed.contains("거래 오픈")
        || trimmed.contains("거래 개시")
        // A symbol-rename re-announcement is a genuine tradeable 원화 마켓 추가.
        || trimmed.contains("심볼명 변경")
        || trimmed.contains("심볼 변경")
        || (trimmed.starts_with("및 ") && trimmed.ends_with(" 안내"))
}

fn is_ascii_word_char(ch: char) -> bool {
    ch.is_ascii_alphanumeric() || ch == '_'
}

fn has_ascii_word(title: &str, needle: &str) -> bool {
    let bytes = title.as_bytes();
    let needle_bytes = needle.as_bytes();
    let mut i = 0;
    while i + needle_bytes.len() <= bytes.len() {
        if &bytes[i..i + needle_bytes.len()] == needle_bytes {
            let left_ok = i == 0 || !is_ascii_word_char(bytes[i - 1] as char);
            let right_idx = i + needle_bytes.len();
            let right_ok =
                right_idx >= bytes.len() || !is_ascii_word_char(bytes[right_idx] as char);
            if left_ok && right_ok {
                return true;
            }
            i = right_idx;
        } else {
            i += 1;
        }
    }
    false
}

fn extract_ticker_candidates(title: &str) -> Vec<String> {
    let bytes = title.as_bytes();
    let mut results = Vec::new();
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] != b'(' {
            i += 1;
            continue;
        }
        let mut end = i + 1;
        while end < bytes.len() && bytes[end] != b')' {
            end += 1;
        }
        if end >= bytes.len() {
            break;
        }
        // Trim inner spaces and require a letter so "( BABY )" matches "(BABY)"
        // and an all-digit token (a year/amount) is never read as the ticker —
        // keeps this classifier identical to the Python/ultra paths.
        let candidate = title[i + 1..end].trim();
        if (1..=10).contains(&candidate.len())
            && candidate.bytes().any(|b| b.is_ascii_uppercase())
            && candidate
                .bytes()
                .all(|b| b.is_ascii_uppercase() || b.is_ascii_digit())
        {
            results.push(candidate.to_string());
        }
        i = end + 1;
    }
    results
}

fn parse_market_parenthetical(value: &str) -> bool {
    let candidate = value.trim();
    let Some(prefix) = candidate.strip_suffix("마켓") else {
        return false;
    };
    let prefix = prefix.trim();
    if prefix.is_empty() {
        return false;
    }
    let mut matched = false;
    for part in prefix.split(',') {
        let market = part.trim();
        if !matches!(market, "KRW" | "BTC" | "USDT" | "ETH") {
            return false;
        }
        matched = true;
    }
    matched
}

fn find_market_parenthetical_end(title: &str, start: usize) -> Option<usize> {
    let bytes = title.as_bytes();
    let mut i = start.min(bytes.len());
    while i < bytes.len() {
        if bytes[i] != b'(' {
            i += 1;
            continue;
        }
        let mut end = i + 1;
        while end < bytes.len() && bytes[end] != b')' {
            end += 1;
        }
        if end >= bytes.len() {
            return None;
        }
        if parse_market_parenthetical(&title[i + 1..end]) {
            return Some(end + 1);
        }
        i = end + 1;
    }
    None
}

fn extract_primary_ticker(title: &str) -> Option<String> {
    extract_ticker_candidates(title)
        .into_iter()
        .find(|candidate| !matches!(candidate.as_str(), "KRW" | "BTC" | "USDT" | "ETH"))
}

fn extract_market_flags(title: &str) -> u32 {
    let mut flags = 0;
    if title.contains("원화 마켓") || has_ascii_word(title, "KRW") {
        flags |= MARKET_FLAG_KRW;
    }
    if has_ascii_word(title, "BTC") {
        flags |= MARKET_FLAG_BTC;
    }
    if has_ascii_word(title, "USDT") {
        flags |= MARKET_FLAG_USDT;
    }
    if has_ascii_word(title, "ETH") {
        flags |= MARKET_FLAG_ETH;
    }
    if flags != 0 {
        return flags;
    }
    for candidate in extract_ticker_candidates(title) {
        match candidate.as_str() {
            "KRW" => flags |= MARKET_FLAG_KRW,
            "BTC" => flags |= MARKET_FLAG_BTC,
            "USDT" => flags |= MARKET_FLAG_USDT,
            "ETH" => flags |= MARKET_FLAG_ETH,
            _ => {}
        }
    }
    flags
}

fn trim_ascii(value: &str) -> String {
    value.trim().to_string()
}

fn extract_asset_name(title: &str) -> String {
    let Some(bracket) = title.find(']') else {
        return trim_ascii(title);
    };
    let Some(open) = title[bracket + 1..].find('(').map(|idx| idx + bracket + 1) else {
        return trim_ascii(title);
    };
    trim_ascii(&title[bracket + 1..open])
}

fn is_ticker_token(candidate: &str) -> bool {
    (1..=10).contains(&candidate.len())
        && candidate.bytes().any(|b| b.is_ascii_uppercase())
        && candidate
            .bytes()
            .all(|b| b.is_ascii_uppercase() || b.is_ascii_digit())
}

fn normalize_asset_segment(segment: &str) -> String {
    let value = segment.trim().trim_start_matches(',').trim();
    for prefix in ["및 ", "and ", "& ", "/ ", "· "] {
        if let Some(rest) = value.strip_prefix(prefix) {
            return rest.trim().to_string();
        }
    }
    value.to_string()
}

// Ticker-aware asset name (matches Python and the cpp/ultra paths): the name is
// the segment preceding the chosen ticker's parenthetical, so a skipped leading
// parenthetical (e.g. a year) stays with the name. Keeps asset_name identical.
fn extract_asset_name_for_ticker(title: &str, ticker: &str) -> String {
    let mut name_start = match title.find(']') {
        Some(bracket) => bracket + 1,
        None => 0,
    };
    let mut search = name_start;
    while search < title.len() {
        let Some(open) = title[search..].find('(').map(|idx| idx + search) else {
            break;
        };
        let Some(close) = title[open + 1..].find(')').map(|idx| idx + open + 1) else {
            break;
        };
        let candidate = title[open + 1..close].trim();
        if !matches!(candidate, "KRW" | "BTC" | "USDT" | "ETH") && is_ticker_token(candidate) {
            if candidate == ticker {
                let asset = normalize_asset_segment(&title[name_start..open]);
                if !asset.is_empty() {
                    return asset;
                }
                break;
            }
            name_start = close + 1;
        }
        search = close + 1;
    }
    extract_asset_name(title)
}

fn copy_to_buffer(value: &str, output: &mut [c_char]) {
    output.fill(0);
    let bytes = value.as_bytes();
    let copy_len = bytes.len().min(output.len().saturating_sub(1));
    for (idx, byte) in bytes.iter().take(copy_len).enumerate() {
        output[idx] = *byte as c_char;
    }
}

fn first_ticker_paren_open(title: &str) -> Option<usize> {
    let mut search = match title.find(']') {
        Some(bracket) => bracket + 1,
        None => 0,
    };
    while let Some(rel) = title[search..].find('(') {
        let open = search + rel;
        let close = match title[open + 1..].find(')') {
            Some(idx) => open + 1 + idx,
            None => return None,
        };
        let candidate = title[open + 1..close].trim();
        if !matches!(candidate, "KRW" | "BTC" | "USDT" | "ETH") && is_ticker_token(candidate) {
            return Some(open);
        }
        search = close + 1;
    }
    None
}

// Blank the asset-name span so exclude keywords inside the asset's own name do
// not drop a genuine listing; the prefix and tail are still scanned. See [11].
fn exclude_scan_text(title: &str) -> String {
    let Some(bracket) = title.find(']') else {
        return title.to_string();
    };
    let name_start = bracket + 1;
    let Some(open) = first_ticker_paren_open(title) else {
        return title.to_string();
    };
    if open <= name_start {
        return title.to_string();
    }
    format!("{} {}", &title[..name_start], &title[open..])
}

fn remainder_is_only_parentheticals(text: &str) -> bool {
    let mut text = text.trim_start();
    while !text.is_empty() {
        if !text.starts_with('(') {
            return false;
        }
        let Some(close) = text.find(')') else {
            return false;
        };
        text = text[close + 1..].trim_start();
    }
    true
}

/// Classify an exchange announcement title into a native listing result.
///
/// # Safety
/// `exchange` and `title` must be valid, NUL-terminated C strings (or null),
/// and `out` must be either null or a valid, writable pointer to a
/// `NativeListingResult`. Passing dangling or misaligned pointers is undefined
/// behavior. Null pointers are handled gracefully and return `-1`.
#[no_mangle]
pub unsafe extern "C" fn classify_listing_title(
    exchange: *const c_char,
    title: *const c_char,
    out: *mut NativeListingResult,
) -> c_int {
    if exchange.is_null() || title.is_null() || out.is_null() {
        return -1;
    }

    let exchange = match CStr::from_ptr(exchange).to_str() {
        Ok(value) => value,
        Err(_) => return -1,
    };
    let title = match CStr::from_ptr(title).to_str() {
        Ok(value) => value,
        Err(_) => return -1,
    };
    let out = &mut *out;
    out.matched = 0;
    out.market_flags = 0;
    out.ticker.fill(0);
    out.asset_name.fill(0);
    out.signal_type.fill(0);

    let (matched, signal_type) = match exchange {
        "upbit" => (
            title.starts_with("[거래]")
                && contains_none(&exclude_scan_text(title), &UPBIT_EXCLUDE_KEYWORDS)
                && has_ascii_word(title, "KRW")
                && if let Some(anchor) = title.find("신규 거래지원 안내") {
                    find_market_parenthetical_end(title, anchor)
                        .map(|end| remainder_is_only_parentheticals(&title[end..]))
                        .unwrap_or(false)
                } else if let Some(idx) = title.rfind("마켓 디지털 자산 추가") {
                    remainder_is_only_parentheticals(
                        &title[idx + "마켓 디지털 자산 추가".len()..],
                    )
                } else {
                    false
                },
            "new_listing",
        ),
        "bithumb" => (
            has_bithumb_listing_prefix(title)
                && contains_none(&exclude_scan_text(title), &BITHUMB_EXCLUDE_KEYWORDS)
                && !title.contains("원화 마켓 재거래지원 안내")
                && title
                    .find("원화 마켓 추가")
                    .map(|idx| {
                        is_allowed_bithumb_market_add_suffix(&title[idx + "원화 마켓 추가".len()..])
                    })
                    .unwrap_or(false),
            "market_add",
        ),
        _ => return 0,
    };

    if !matched {
        return 0;
    }

    let Some(ticker) = extract_primary_ticker(title) else {
        return 0;
    };

    out.matched = 1;
    out.market_flags = extract_market_flags(title);
    copy_to_buffer(&ticker, &mut out.ticker);
    copy_to_buffer(&extract_asset_name_for_ticker(title, &ticker), &mut out.asset_name);
    copy_to_buffer(signal_type, &mut out.signal_type);
    1
}
