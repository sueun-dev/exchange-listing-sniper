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
const BITHUMB_EXCLUDE_KEYWORDS: [&str; 5] = [
    "입출금",
    "유의촉구",
    "거래유의",
    "시세알림",
    "종료",
];

fn has_bithumb_listing_prefix(title: &str) -> bool {
    title.starts_with("[마켓 추가]")
        || title.starts_with("[마켓 추가/수수료 이벤트]")
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
    if trimmed.is_empty()
        || trimmed == "및 재단 에어드랍 안내"
        || trimmed == "및 에어드랍 안내"
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
            let right_ok = right_idx >= bytes.len() || !is_ascii_word_char(bytes[right_idx] as char);
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
        let candidate = &title[i + 1..end];
        if (1..=10).contains(&candidate.len())
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

fn copy_to_buffer(value: &str, output: &mut [c_char]) {
    output.fill(0);
    let bytes = value.as_bytes();
    let copy_len = bytes.len().min(output.len().saturating_sub(1));
    for (idx, byte) in bytes.iter().take(copy_len).enumerate() {
        output[idx] = *byte as c_char;
    }
}

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
                && contains_none(title, &UPBIT_EXCLUDE_KEYWORDS)
                && has_ascii_word(title, "KRW")
                && if let Some(anchor) = title.find("신규 거래지원 안내") {
                    find_market_parenthetical_end(title, anchor)
                        .map(|end| title[end..].trim().is_empty())
                        .unwrap_or(false)
                } else {
                    title.contains("마켓 디지털 자산 추가")
                        && title.trim_end().ends_with("마켓 디지털 자산 추가")
                },
            "new_listing",
        ),
        "bithumb" => (
            has_bithumb_listing_prefix(title)
                && contains_none(title, &BITHUMB_EXCLUDE_KEYWORDS)
                && !title.contains("원화 마켓 재거래지원 안내")
                && title
                    .find("원화 마켓 추가")
                    .map(|idx| {
                        is_allowed_bithumb_market_add_suffix(
                            &title[idx + "원화 마켓 추가".len()..],
                        )
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
    copy_to_buffer(&extract_asset_name(title), &mut out.asset_name);
    copy_to_buffer(signal_type, &mut out.signal_type);
    1
}
