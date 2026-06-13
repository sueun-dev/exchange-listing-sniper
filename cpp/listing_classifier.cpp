#include <algorithm>
#include <cctype>
#include <cstdint>
#include <cstring>
#include <string>
#include <string_view>
#include <vector>

namespace {

constexpr uint32_t MARKET_FLAG_KRW = 1;
constexpr uint32_t MARKET_FLAG_BTC = 2;
constexpr uint32_t MARKET_FLAG_USDT = 4;
constexpr uint32_t MARKET_FLAG_ETH = 8;

constexpr std::string_view MARKET_CODES[] = {"KRW", "BTC", "USDT", "ETH"};
constexpr std::string_view UPBIT_LISTING_KEYWORDS[] = {
    "신규 거래지원",
    "KRW 마켓 디지털 자산 추가",
    "BTC 마켓 디지털 자산 추가",
    "USDT 마켓 디지털 자산 추가",
};
constexpr std::string_view UPBIT_EXCLUDE_KEYWORDS[] = {
    "입출금",
    "유통량",
    "거래유의",
    "유의종목",
    "스테이킹",
    "이벤트",
    "종료",
    "변경 안내",
};
constexpr std::string_view BITHUMB_LISTING_KEYWORDS[] = {
    "[마켓 추가]",
    "원화 마켓 추가",
};
constexpr std::string_view BITHUMB_LISTING_PREFIXES[] = {
    "[마켓 추가]",
    "[마켓 추가/수수료 이벤트]",
};
constexpr std::string_view BITHUMB_EXCLUDE_KEYWORDS[] = {
    "입출금",
    "유의촉구",
    "거래유의",
    "시세알림",
    "종료",
};

struct NativeListingResult {
    int matched;
    uint32_t market_flags;
    char ticker[16];
    char asset_name[128];
    char signal_type[16];
};

bool is_ascii_space(unsigned char ch) {
    return ch == ' ' || ch == '\t' || ch == '\n' ||
           ch == '\r' || ch == '\f' || ch == '\v';
}

std::string trim_ascii(std::string value) {
    value.erase(value.begin(), std::find_if(value.begin(), value.end(), [&](char ch) {
                    return !is_ascii_space(static_cast<unsigned char>(ch));
                }));
    value.erase(std::find_if(value.rbegin(), value.rend(), [&](char ch) {
                    return !is_ascii_space(static_cast<unsigned char>(ch));
                }).base(),
                value.end());
    return value;
}

bool contains_any(std::string_view title, const std::string_view* keywords, size_t count) {
    for (size_t i = 0; i < count; ++i) {
        if (title.find(keywords[i]) != std::string_view::npos) {
            return true;
        }
    }
    return false;
}

bool contains_none(std::string_view title, const std::string_view* keywords, size_t count) {
    for (size_t i = 0; i < count; ++i) {
        if (title.find(keywords[i]) != std::string_view::npos) {
            return false;
        }
    }
    return true;
}

bool is_ascii_word_char(char ch) {
    return std::isalnum(static_cast<unsigned char>(ch)) != 0 || ch == '_';
}

bool has_ascii_word(std::string_view title, std::string_view needle) {
    size_t pos = 0;
    while (true) {
        pos = title.find(needle, pos);
        if (pos == std::string_view::npos) {
            return false;
        }
        const bool left_ok =
            pos == 0 || !is_ascii_word_char(title[pos - 1]);
        const size_t right = pos + needle.size();
        const bool right_ok =
            right >= title.size() || !is_ascii_word_char(title[right]);
        if (left_ok && right_ok) {
            return true;
        }
        pos = right;
    }
}

std::vector<std::string> extract_ticker_candidates(std::string_view title) {
    std::vector<std::string> candidates;
    for (size_t i = 0; i < title.size(); ++i) {
        if (title[i] != '(') {
            continue;
        }
        const size_t end = title.find(')', i + 1);
        if (end == std::string_view::npos) {
            break;
        }
        const auto candidate = title.substr(i + 1, end - i - 1);
        if (candidate.empty() || candidate.size() > 10) {
            i = end;
            continue;
        }
        bool valid = true;
        for (char ch : candidate) {
            if (!(std::isupper(static_cast<unsigned char>(ch)) || std::isdigit(static_cast<unsigned char>(ch)))) {
                valid = false;
                break;
            }
        }
        if (valid) {
            candidates.emplace_back(candidate);
        }
        i = end;
    }
    return candidates;
}

bool is_market_code(std::string_view candidate) {
    for (auto code : MARKET_CODES) {
        if (candidate == code) {
            return true;
        }
    }
    return false;
}

bool parse_market_parenthetical(std::string_view candidate, uint32_t* flags_out = nullptr) {
    std::string normalized = trim_ascii(std::string(candidate));
    candidate = normalized;
    constexpr std::string_view suffix = "마켓";
    if (candidate.size() < suffix.size() ||
        candidate.substr(candidate.size() - suffix.size()) != suffix) {
        return false;
    }
    candidate.remove_suffix(suffix.size());
    normalized = trim_ascii(std::string(candidate));
    candidate = normalized;
    if (candidate.empty()) {
        return false;
    }
    uint32_t flags = 0;
    size_t start = 0;
    while (start < candidate.size()) {
        size_t comma = candidate.find(',', start);
        const size_t end = comma == std::string_view::npos ? candidate.size() : comma;
        const std::string part_str = trim_ascii(std::string(candidate.substr(start, end - start)));
        const auto part = std::string_view(part_str);
        if (part.empty() || !is_market_code(part)) {
            return false;
        }
        if (part == "KRW") {
            flags |= MARKET_FLAG_KRW;
        } else if (part == "BTC") {
            flags |= MARKET_FLAG_BTC;
        } else if (part == "USDT") {
            flags |= MARKET_FLAG_USDT;
        } else if (part == "ETH") {
            flags |= MARKET_FLAG_ETH;
        }
        if (comma == std::string_view::npos) {
            break;
        }
        start = comma + 1;
    }
    if (flags_out != nullptr) {
        *flags_out = flags;
    }
    return flags != 0;
}

size_t find_market_parenthetical_end(std::string_view title, size_t start = 0) {
    size_t search = start;
    while (true) {
        const size_t open = title.find('(', search);
        if (open == std::string_view::npos) {
            return std::string_view::npos;
        }
        const size_t close = title.find(')', open + 1);
        if (close == std::string_view::npos) {
            return std::string_view::npos;
        }
        if (parse_market_parenthetical(title.substr(open + 1, close - open - 1))) {
            return close + 1;
        }
        search = close + 1;
    }
}

std::string extract_primary_ticker(std::string_view title) {
    const auto candidates = extract_ticker_candidates(title);
    for (const auto& candidate : candidates) {
        if (!is_market_code(candidate)) {
            return candidate;
        }
    }
    return "";
}

uint32_t extract_market_flags(std::string_view title) {
    uint32_t flags = 0;
    if (title.find("원화 마켓") != std::string_view::npos) {
        flags |= MARKET_FLAG_KRW;
    }
    if (has_ascii_word(title, "KRW")) {
        flags |= MARKET_FLAG_KRW;
    }
    if (has_ascii_word(title, "BTC")) {
        flags |= MARKET_FLAG_BTC;
    }
    if (has_ascii_word(title, "USDT")) {
        flags |= MARKET_FLAG_USDT;
    }
    if (has_ascii_word(title, "ETH")) {
        flags |= MARKET_FLAG_ETH;
    }
    if (flags != 0) {
        return flags;
    }

    for (const auto& candidate : extract_ticker_candidates(title)) {
        if (candidate == "KRW") {
            flags |= MARKET_FLAG_KRW;
        } else if (candidate == "BTC") {
            flags |= MARKET_FLAG_BTC;
        } else if (candidate == "USDT") {
            flags |= MARKET_FLAG_USDT;
        } else if (candidate == "ETH") {
            flags |= MARKET_FLAG_ETH;
        }
    }
    return flags;
}

std::string extract_asset_name(std::string_view title) {
    const size_t bracket = title.find(']');
    if (bracket == std::string_view::npos) {
        return trim_ascii(std::string(title));
    }
    const size_t open = title.find('(', bracket + 1);
    if (open == std::string_view::npos || open <= bracket + 1) {
        return trim_ascii(std::string(title));
    }
    return trim_ascii(std::string(title.substr(bracket + 1, open - bracket - 1)));
}

bool is_allowed_bithumb_market_add_suffix(std::string_view suffix) {
    const std::string trimmed = trim_ascii(std::string(suffix));
    if (trimmed.empty() ||
        trimmed == "및 재단 에어드랍 안내" ||
        trimmed == "및 에어드랍 안내") {
        return true;
    }
    constexpr std::string_view blocked[] = {
        "시간 변경",
        "연기",
        "입출금",
        "재거래지원",
        "유의",
        "중단",
        "종료",
    };
    for (auto keyword : blocked) {
        if (trimmed.find(keyword) != std::string::npos) {
            return false;
        }
    }
    if (trimmed.rfind("(거래 수수료 무료)", 0) == 0 ||
        trimmed.rfind("(거래수수료 무료)", 0) == 0) {
        return true;
    }
    if (trimmed.find("거래 오픈") != std::string::npos ||
        trimmed.find("거래 개시") != std::string::npos) {
        return true;
    }
    const std::string suffix_end = " 안내";
    return trimmed.rfind("및 ", 0) == 0 &&
           trimmed.size() >= suffix_end.size() &&
           trimmed.compare(
               trimmed.size() - suffix_end.size(),
               suffix_end.size(),
               suffix_end
           ) == 0;
}

bool has_bithumb_listing_prefix(std::string_view title) {
    return contains_any(title, BITHUMB_LISTING_PREFIXES, std::size(BITHUMB_LISTING_PREFIXES)) &&
           (title.rfind("[마켓 추가]", 0) == 0 ||
            title.rfind("[마켓 추가/수수료 이벤트]", 0) == 0);
}

bool is_upbit_listing(std::string_view title) {
    if (title.rfind("[거래]", 0) != 0 ||
        !contains_none(title, UPBIT_EXCLUDE_KEYWORDS, std::size(UPBIT_EXCLUDE_KEYWORDS))) {
        return false;
    }
    constexpr std::string_view new_listing_anchor = "신규 거래지원 안내";
    if (title.find(new_listing_anchor) != std::string_view::npos) {
        const size_t market_end = find_market_parenthetical_end(
            title,
            title.find(new_listing_anchor)
        );
        return market_end != std::string_view::npos &&
               trim_ascii(std::string(title.substr(market_end))).empty();
    }
    constexpr std::string_view market_add_suffix = "마켓 디지털 자산 추가";
    const std::string trimmed = trim_ascii(std::string(title));
    const std::string suffix(market_add_suffix);
    return title.find(market_add_suffix) != std::string_view::npos &&
           trimmed.size() >= suffix.size() &&
           trimmed.compare(trimmed.size() - suffix.size(), suffix.size(), suffix) == 0;
}

bool is_bithumb_listing(std::string_view title) {
    if (!has_bithumb_listing_prefix(title) ||
        !contains_none(title, BITHUMB_EXCLUDE_KEYWORDS, std::size(BITHUMB_EXCLUDE_KEYWORDS)) ||
        title.find("원화 마켓 재거래지원 안내") != std::string_view::npos) {
        return false;
    }
    constexpr std::string_view marker = "원화 마켓 추가";
    const size_t marker_pos = title.find(marker);
    if (marker_pos == std::string_view::npos) {
        return false;
    }
    return is_allowed_bithumb_market_add_suffix(
        title.substr(marker_pos + marker.size())
    );
}

void copy_to_buffer(const std::string& value, char* output, size_t capacity) {
    if (capacity == 0) {
        return;
    }
    std::memset(output, 0, capacity);
    const size_t len = std::min(value.size(), capacity - 1);
    std::memcpy(output, value.data(), len);
}

int classify_listing_impl(std::string_view exchange, std::string_view title, NativeListingResult* out) {
    if (out == nullptr) {
        return -1;
    }
    std::memset(out, 0, sizeof(NativeListingResult));

    bool matched = false;
    std::string signal_type;
    if (exchange == "upbit") {
        matched = is_upbit_listing(title) && has_ascii_word(title, "KRW");
        signal_type = "new_listing";
    } else if (exchange == "bithumb") {
        matched = is_bithumb_listing(title) && title.find("원화 마켓") != std::string_view::npos;
        signal_type = "market_add";
    } else {
        return 0;
    }

    if (!matched) {
        return 0;
    }

    const std::string ticker = extract_primary_ticker(title);
    if (ticker.empty()) {
        return 0;
    }

    out->matched = 1;
    out->market_flags = extract_market_flags(title);
    copy_to_buffer(ticker, out->ticker, sizeof(out->ticker));
    copy_to_buffer(extract_asset_name(title), out->asset_name, sizeof(out->asset_name));
    copy_to_buffer(signal_type, out->signal_type, sizeof(out->signal_type));
    return 1;
}

}  // namespace

extern "C" int classify_listing_title(
    const char* exchange,
    const char* title,
    NativeListingResult* out
) {
    if (exchange == nullptr || title == nullptr) {
        return -1;
    }
    return classify_listing_impl(exchange, title, out);
}
