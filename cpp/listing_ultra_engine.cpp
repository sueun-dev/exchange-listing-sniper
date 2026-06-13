#include <curl/curl.h>
#ifndef OPENSSL_API_COMPAT
#define OPENSSL_API_COMPAT 0x10100000L
#endif
#include <openssl/hmac.h>
#include <openssl/sha.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <cctype>
#include <charconv>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <iomanip>
#include <memory>
#include <mutex>
#include <optional>
#include <sstream>
#include <string>
#include <string_view>
#include <thread>
#include <unordered_map>
#include <unordered_set>
#include <vector>

namespace {

constexpr uint32_t MARKET_FLAG_KRW = 1;
constexpr uint32_t MARKET_FLAG_BTC = 2;
constexpr uint32_t MARKET_FLAG_USDT = 4;
constexpr uint32_t MARKET_FLAG_ETH = 8;
constexpr std::size_t AUTH_HEADER_COUNT = 5;
constexpr std::size_t HMAC_SHA256_BLOCK_SIZE = 64;
constexpr std::size_t MAX_ULTRA_TICKERS = 16;
constexpr std::size_t FIRST_SECONDARY_WORKER = 1;
static_assert(MAX_ULTRA_TICKERS > FIRST_SECONDARY_WORKER);

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

struct NativeUltraResult {
    int matched;
    int duplicate;
    uint32_t market_flags;
    int attempted;
    int executed;
    int ret_code;
    int trade_count;
    int attempted_count;
    int executed_count;
    char ticker[16];
    char asset_name[128];
    char signal_type[16];
    char symbol[24];
    char order_id[64];
    char order_link_id[40];
    char transport[32];
    char reason[128];
};

struct NativeUltraTradeResult {
    int attempted;
    int executed;
    int ret_code;
    char ticker[16];
    char symbol[24];
    char order_id[64];
    char order_link_id[40];
    char transport[32];
    char reason[128];
};

struct HttpResult {
    long status_code{0};
    std::string body;
    std::string error;
    bool success{false};
};

struct ListingTickers {
    std::array<std::string_view, MAX_ULTRA_TICKERS> values{};
    std::size_t count{0};

    bool contains(std::string_view ticker) const {
        for (std::size_t i = 0; i < count; ++i) {
            if (values[i] == ticker) {
                return true;
            }
        }
        return false;
    }

    void clear() {
        count = 0;
    }

    void push_unique(std::string_view ticker) {
        if (count >= values.size() || contains(ticker)) {
            return;
        }
        values[count++] = ticker;
    }
};

struct UltraOrderResult {
    int attempted{0};
    int executed{0};
    int ret_code{-1};
    std::string symbol;
    std::string order_id;
    std::string order_link_id;
    std::string transport;
    std::string reason;
};

struct UltraOrderWorkerSlot {
    std::atomic<std::uint64_t> work_seq{0};
    std::atomic<std::uint64_t> done_seq{0};
    std::atomic<bool> stop_requested{false};
    std::string_view exchange;
    long long message_id{0};
    std::string_view ticker;
    UltraOrderResult result;
};

struct StoredUltraTrade {
    std::string ticker;
    UltraOrderResult trade;
};

struct StoredUltraTrades {
    std::size_t count{0};
    std::array<StoredUltraTrade, MAX_ULTRA_TICKERS> values{};
};

using HmacPad = std::array<unsigned char, HMAC_SHA256_BLOCK_SIZE>;

struct TransparentStringHash {
    using is_transparent = void;

    std::size_t operator()(std::string_view value) const noexcept {
        return std::hash<std::string_view>{}(value);
    }

    std::size_t operator()(const std::string& value) const noexcept {
        return (*this)(std::string_view(value));
    }
};

struct TransparentStringEqual {
    using is_transparent = void;

    bool operator()(std::string_view left, std::string_view right) const noexcept {
        return left == right;
    }
};

using SpotSymbolSet =
    std::unordered_set<std::string, TransparentStringHash, TransparentStringEqual>;

struct AuthHeaders {
    const std::string* content_type_header{nullptr};
    const std::string* api_key_header{nullptr};
    const std::string* recv_window_header{nullptr};
    std::string sign_header;
    std::string timestamp_header;

    const char* c_str(std::size_t index) const {
        switch (index) {
            case 0:
                return content_type_header == nullptr ? "" : content_type_header->c_str();
            case 1:
                return api_key_header == nullptr ? "" : api_key_header->c_str();
            case 2:
                return sign_header.c_str();
            case 3:
                return timestamp_header.c_str();
            case 4:
                return recv_window_header == nullptr ? "" : recv_window_header->c_str();
            default:
                return "";
        }
    }
};

struct PreparedOrderRequest {
    std::string body;
    AuthHeaders headers;
};

void init_hmac_sha256_pads(std::string_view secret, HmacPad& ipad, HmacPad& opad) {
    std::array<unsigned char, SHA256_DIGEST_LENGTH> hashed_key{};
    const unsigned char* key = reinterpret_cast<const unsigned char*>(secret.data());
    std::size_t key_len = secret.size();
    if (key_len > HMAC_SHA256_BLOCK_SIZE) {
        SHA256(key, key_len, hashed_key.data());
        key = hashed_key.data();
        key_len = hashed_key.size();
    }
    ipad.fill(0x36);
    opad.fill(0x5c);
    for (std::size_t i = 0; i < key_len; ++i) {
        ipad[i] ^= key[i];
        opad[i] ^= key[i];
    }
}

size_t write_callback(void* contents, size_t size, size_t nmemb, void* userp) {
    const size_t total = size * nmemb;
    auto* output = static_cast<std::string*>(userp);
    output->append(static_cast<char*>(contents), total);
    return total;
}

struct curl_slist* stack_auth_header_list(
    const AuthHeaders& headers,
    std::array<curl_slist, AUTH_HEADER_COUNT>& nodes
) {
    for (std::size_t i = 0; i < AUTH_HEADER_COUNT; ++i) {
        nodes[i].data = const_cast<char*>(headers.c_str(i));
        nodes[i].next = (i + 1 < AUTH_HEADER_COUNT) ? &nodes[i + 1] : nullptr;
    }
    return nodes.data();
}

std::string getenv_or(const char* key, const char* fallback = "") {
    const char* value = std::getenv(key);
    return value ? std::string(value) : std::string(fallback);
}

long long getenv_long_long_or(const char* key, long long fallback) {
    const char* value = std::getenv(key);
    if (value == nullptr || *value == '\0') {
        return fallback;
    }
    char* end = nullptr;
    const long long parsed = std::strtoll(value, &end, 10);
    if (end == value) {
        return fallback;
    }
    return parsed;
}

void append_int64(std::string& out, long long value) {
    std::array<char, 32> buffer{};
    auto [ptr, ec] = std::to_chars(buffer.data(), buffer.data() + buffer.size(), value);
    if (ec == std::errc()) {
        out.append(buffer.data(), static_cast<size_t>(ptr - buffer.data()));
    }
}

std::string_view current_timestamp_ms(std::array<char, 32>& buffer, long long bias_ms) {
    const auto now = std::chrono::time_point_cast<std::chrono::milliseconds>(
        std::chrono::system_clock::now()
    );
    const auto value = now.time_since_epoch().count() + bias_ms;
    auto [ptr, ec] = std::to_chars(buffer.data(), buffer.data() + buffer.size(), value);
    if (ec != std::errc()) {
        return {};
    }
    return std::string_view(buffer.data(), static_cast<std::size_t>(ptr - buffer.data()));
}

bool is_positive_decimal_text(std::string_view value) {
    while (!value.empty() && std::isspace(static_cast<unsigned char>(value.front()))) {
        value.remove_prefix(1);
    }
    while (!value.empty() && std::isspace(static_cast<unsigned char>(value.back()))) {
        value.remove_suffix(1);
    }
    if (value.empty() || value.front() == '-') {
        return false;
    }
    if (value.front() == '+') {
        value.remove_prefix(1);
    }
    bool saw_digit = false;
    bool saw_nonzero = false;
    bool saw_dot = false;
    for (char ch : value) {
        if (ch == '.') {
            if (saw_dot) {
                return false;
            }
            saw_dot = true;
            continue;
        }
        if (ch < '0' || ch > '9') {
            return false;
        }
        saw_digit = true;
        saw_nonzero = saw_nonzero || ch != '0';
    }
    return saw_digit && saw_nonzero;
}

std::string_view spot_symbol_view(std::string_view ticker, std::array<char, 32>& buffer) {
    constexpr std::string_view suffix = "USDT";
    const std::size_t length = ticker.size() + suffix.size();
    if (length > buffer.size()) {
        return {};
    }
    std::memcpy(buffer.data(), ticker.data(), ticker.size());
    std::memcpy(buffer.data() + ticker.size(), suffix.data(), suffix.size());
    return std::string_view(buffer.data(), length);
}

std::string_view order_link_exchange_code(std::string_view exchange) {
    return exchange;
}

std::string_view build_order_link_id_view(
    std::string_view exchange,
    long long message_id,
    std::string_view ticker,
    std::array<char, 64>& buffer
) {
    const std::string_view exchange_code = order_link_exchange_code(exchange);
    std::size_t pos = 0;
    auto append = [&](std::string_view value) {
        const std::size_t available = buffer.size() - pos;
        const std::size_t length = std::min(value.size(), available);
        if (length != 0) {
            std::memcpy(buffer.data() + pos, value.data(), length);
            pos += length;
        }
    };
    append("ls-");
    append(exchange_code);
    append("-");
    if (pos < buffer.size()) {
        auto [ptr, ec] = std::to_chars(
            buffer.data() + pos,
            buffer.data() + buffer.size(),
            message_id
        );
        if (ec == std::errc()) {
            pos = static_cast<std::size_t>(ptr - buffer.data());
        }
    }
    append("-");
    append(ticker);
    if (pos > 36) {
        pos = 36;
    }
    return std::string_view(buffer.data(), pos);
}

bool getenv_truthy(const char* key, bool fallback = false) {
    const char* value = std::getenv(key);
    if (value == nullptr) {
        return fallback;
    }
    std::string text(value);
    std::transform(text.begin(), text.end(), text.begin(), [](unsigned char ch) {
        return static_cast<char>(std::tolower(ch));
    });
    return text == "1" || text == "true" || text == "yes" || text == "on";
}

std::string json_escape(const std::string& input) {
    std::ostringstream escaped;
    for (const char ch : input) {
        switch (ch) {
            case '\\':
                escaped << "\\\\";
                break;
            case '"':
                escaped << "\\\"";
                break;
            case '\n':
                escaped << "\\n";
                break;
            case '\r':
                escaped << "\\r";
                break;
            case '\t':
                escaped << "\\t";
                break;
            default:
                escaped << ch;
        }
    }
    return escaped.str();
}

std::optional<std::string> extract_json_string(const std::string& body, const std::string& key) {
    const std::string pattern = "\"" + key + "\":";
    size_t pos = body.find(pattern);
    if (pos == std::string::npos) {
        return std::nullopt;
    }
    pos = body.find('"', pos + pattern.size());
    if (pos == std::string::npos) {
        return std::nullopt;
    }
    ++pos;
    std::string result;
    bool escaped = false;
    for (; pos < body.size(); ++pos) {
        const char ch = body[pos];
        if (escaped) {
            result.push_back(ch);
            escaped = false;
            continue;
        }
        if (ch == '\\') {
            escaped = true;
            continue;
        }
        if (ch == '"') {
            return result;
        }
        result.push_back(ch);
    }
    return std::nullopt;
}

std::optional<long long> extract_json_int(const std::string& body, const std::string& key) {
    const std::string pattern = "\"" + key + "\":";
    size_t pos = body.find(pattern);
    if (pos == std::string::npos) {
        return std::nullopt;
    }
    pos += pattern.size();
    while (pos < body.size() && std::isspace(static_cast<unsigned char>(body[pos]))) {
        ++pos;
    }
    size_t end = pos;
    if (end < body.size() && (body[end] == '-' || body[end] == '+')) {
        ++end;
    }
    while (end < body.size() && std::isdigit(static_cast<unsigned char>(body[end]))) {
        ++end;
    }
    if (end == pos) {
        return std::nullopt;
    }
    return std::stoll(body.substr(pos, end - pos));
}

std::vector<std::string> extract_symbols(const std::string& body) {
    std::vector<std::string> symbols;
    const std::string needle = "\"symbol\":\"";
    size_t pos = 0;
    while (true) {
        pos = body.find(needle, pos);
        if (pos == std::string::npos) {
            break;
        }
        pos += needle.size();
        size_t end = body.find('"', pos);
        if (end == std::string::npos) {
            break;
        }
        symbols.push_back(body.substr(pos, end - pos));
        pos = end + 1;
    }
    return symbols;
}

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

std::string_view trim_ascii_view(std::string_view value) {
    while (!value.empty() && is_ascii_space(static_cast<unsigned char>(value.front()))) {
        value.remove_prefix(1);
    }
    while (!value.empty() && is_ascii_space(static_cast<unsigned char>(value.back()))) {
        value.remove_suffix(1);
    }
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
        const bool left_ok = pos == 0 || !is_ascii_word_char(title[pos - 1]);
        const size_t right = pos + needle.size();
        const bool right_ok = right >= title.size() || !is_ascii_word_char(title[right]);
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
        const auto candidate = trim_ascii_view(title.substr(i + 1, end - i - 1));
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

bool parse_market_parenthetical(std::string_view candidate) {
    candidate = trim_ascii_view(candidate);
    constexpr std::string_view suffix = "마켓";
    if (candidate.size() < suffix.size() ||
        candidate.substr(candidate.size() - suffix.size()) != suffix) {
        return false;
    }
    candidate = candidate.substr(0, candidate.size() - suffix.size());
    candidate = trim_ascii_view(candidate);
    if (candidate.empty()) {
        return false;
    }
    size_t start = 0;
    bool matched = false;
    while (start < candidate.size()) {
        size_t comma = candidate.find(',', start);
        const size_t end = comma == std::string_view::npos ? candidate.size() : comma;
        const auto part = trim_ascii_view(candidate.substr(start, end - start));
        if (part.empty() || !is_market_code(part)) {
            return false;
        }
        matched = true;
        if (comma == std::string_view::npos) {
            break;
        }
        start = comma + 1;
    }
    return matched;
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

struct TickerScanResult {
    std::string_view primary;
    bool multiple{false};
};

bool same_view_value(std::string_view left, std::string_view right) {
    if (left.size() != right.size()) {
        return false;
    }
    for (size_t i = 0; i < left.size(); ++i) {
        if (left[i] != right[i]) {
            return false;
        }
    }
    return true;
}

TickerScanResult scan_listing_tickers(std::string_view title) {
    TickerScanResult result;
    for (size_t i = 0; i < title.size(); ++i) {
        if (title[i] != '(') {
            continue;
        }
        const size_t end = title.find(')', i + 1);
        if (end == std::string_view::npos) {
            break;
        }
        const auto candidate = trim_ascii_view(title.substr(i + 1, end - i - 1));
        if (candidate.empty() || candidate.size() > 10 || is_market_code(candidate)) {
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
            if (result.primary.empty()) {
                result.primary = candidate;
            } else if (!same_view_value(result.primary, candidate)) {
                result.multiple = true;
                return result;
            }
        }
        i = end;
    }
    return result;
}

void extract_listing_tickers_into(std::string_view title, ListingTickers& tickers) {
    tickers.clear();
    size_t search = 0;
    while (search < title.size()) {
        const size_t open = title.find('(', search);
        if (open == std::string_view::npos) {
            break;
        }
        const size_t end = title.find(')', open + 1);
        if (end == std::string_view::npos) {
            break;
        }
        const auto candidate = trim_ascii_view(title.substr(open + 1, end - open - 1));
        if (candidate.empty() || candidate.size() > 10 || is_market_code(candidate)) {
            search = end + 1;
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
            tickers.push_unique(candidate);
        }
        search = end + 1;
    }
}

ListingTickers extract_listing_tickers(std::string_view title) {
    ListingTickers tickers;
    extract_listing_tickers_into(title, tickers);
    return tickers;
}

std::string normalize_asset_segment(std::string_view segment) {
    std::string value = trim_ascii(std::string(segment));
    while (!value.empty() && value.front() == ',') {
        value.erase(value.begin());
        value = trim_ascii(std::move(value));
    }
    constexpr std::string_view prefixes[] = {"및 ", "and ", "& ", "/ ", "· "};
    for (const auto prefix : prefixes) {
        if (value.rfind(prefix, 0) == 0) {
            return trim_ascii(value.substr(prefix.size()));
        }
    }
    return value;
}

uint32_t extract_market_flags(std::string_view title) {
    uint32_t flags = 0;
    if (title.find("원화 마켓") != std::string_view::npos || has_ascii_word(title, "KRW")) {
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

std::string extract_asset_name_for_ticker(std::string_view title, std::string_view ticker) {
    const size_t bracket = title.find(']');
    size_t name_start = bracket == std::string_view::npos ? 0 : bracket + 1;
    size_t search = name_start;
    while (search < title.size()) {
        const size_t open = title.find('(', search);
        if (open == std::string_view::npos) {
            break;
        }
        const size_t close = title.find(')', open + 1);
        if (close == std::string_view::npos) {
            break;
        }
        const auto candidate = trim_ascii_view(title.substr(open + 1, close - open - 1));
        if (!candidate.empty() && candidate.size() <= 10 && !is_market_code(candidate)) {
            bool valid = true;
            for (char ch : candidate) {
                if (!(std::isupper(static_cast<unsigned char>(ch)) ||
                      std::isdigit(static_cast<unsigned char>(ch)))) {
                    valid = false;
                    break;
                }
            }
            if (valid) {
                if (candidate == ticker) {
                    const std::string asset_name =
                        normalize_asset_segment(title.substr(name_start, open - name_start));
                    if (!asset_name.empty()) {
                        return asset_name;
                    }
                    break;
                }
                name_start = close + 1;
            }
        }
        search = close + 1;
    }
    return extract_asset_name(title);
}

bool is_allowed_bithumb_market_add_suffix(std::string_view suffix) {
    const std::string_view trimmed = trim_ascii_view(suffix);
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
    return title.rfind("[마켓 추가]", 0) == 0 ||
           title.rfind("[마켓 추가/수수료 이벤트]", 0) == 0;
}

void copy_to_buffer(std::string_view value, char* output, size_t capacity) {
    if (capacity == 0) {
        return;
    }
    std::memset(output, 0, capacity);
    const size_t len = std::min(value.size(), capacity - 1);
    std::memcpy(output, value.data(), len);
}

void fill_listing_result_fields(
    NativeUltraResult* out,
    std::string_view title,
    std::string_view ticker,
    std::string_view asset_name,
    std::string_view signal_type,
    std::string_view symbol
) {
    out->matched = 1;
    out->market_flags = extract_market_flags(title);
    copy_to_buffer(ticker, out->ticker, sizeof(out->ticker));
    copy_to_buffer(asset_name, out->asset_name, sizeof(out->asset_name));
    copy_to_buffer(signal_type, out->signal_type, sizeof(out->signal_type));
    copy_to_buffer(symbol, out->symbol, sizeof(out->symbol));
}

void copy_trade_result(
    NativeUltraTradeResult* out,
    std::string_view ticker,
    const UltraOrderResult& trade
) {
    out->attempted = trade.attempted;
    out->executed = trade.executed;
    out->ret_code = trade.ret_code;
    copy_to_buffer(ticker, out->ticker, sizeof(out->ticker));
    copy_to_buffer(trade.symbol, out->symbol, sizeof(out->symbol));
    copy_to_buffer(trade.order_id, out->order_id, sizeof(out->order_id));
    copy_to_buffer(trade.order_link_id, out->order_link_id, sizeof(out->order_link_id));
    copy_to_buffer(trade.transport, out->transport, sizeof(out->transport));
    copy_to_buffer(trade.reason, out->reason, sizeof(out->reason));
}

void reset_result_for_handle(NativeUltraResult* out) {
    out->matched = 0;
    out->duplicate = 0;
    out->market_flags = 0;
    out->attempted = 0;
    out->executed = 0;
    out->ret_code = 0;
    out->trade_count = 0;
    out->attempted_count = 0;
    out->executed_count = 0;
    out->ticker[0] = '\0';
    out->asset_name[0] = '\0';
    out->signal_type[0] = '\0';
    out->symbol[0] = '\0';
    out->order_id[0] = '\0';
    out->order_link_id[0] = '\0';
    out->transport[0] = '\0';
    out->reason[0] = '\0';
}

void record_trade_result(
    NativeUltraResult* out,
    const UltraOrderResult& trade
) {
    out->attempted_count += trade.attempted ? 1 : 0;
    out->executed_count += trade.executed ? 1 : 0;
}

void fill_primary_trade_result(NativeUltraResult* out, const UltraOrderResult& trade) {
    out->attempted = trade.attempted;
    out->executed = trade.executed;
    out->ret_code = trade.ret_code;
    if (!trade.symbol.empty()) {
        copy_to_buffer(trade.symbol, out->symbol, sizeof(out->symbol));
    }
    copy_to_buffer(trade.order_id, out->order_id, sizeof(out->order_id));
    copy_to_buffer(trade.order_link_id, out->order_link_id, sizeof(out->order_link_id));
    copy_to_buffer(trade.transport, out->transport, sizeof(out->transport));
    copy_to_buffer(trade.reason, out->reason, sizeof(out->reason));
}

class CurlClient {
public:
    explicit CurlClient(std::string base_url)
        : base_url_(std::move(base_url)),
          order_create_url_(base_url_ + "/v5/order/create") {
        static const bool curl_global_ready = []() {
            curl_global_init(CURL_GLOBAL_DEFAULT);
            return true;
        }();
        (void)curl_global_ready;
        curl_ = curl_easy_init();
        configure_common_options();
    }

    ~CurlClient() {
        if (curl_ != nullptr) {
            curl_easy_cleanup(curl_);
        }
    }

    HttpResult get(const std::string& path) {
        return perform("GET", path, "", nullptr);
    }

    HttpResult post(const std::string& path, const std::string& body, const AuthHeaders& headers) {
        return perform("POST", path, body, &headers);
    }

    HttpResult post_order_create(const std::string& body, const AuthHeaders& headers) {
        return perform_order_create(body, headers);
    }

    bool prepare_post_order_for_benchmark(const std::string& body, const AuthHeaders& headers) {
        if (!ensure_order_create_url()) {
            return false;
        }
        return prepare_post_for_benchmark(body, headers);
    }

    bool prime_order_create_url() {
        return ensure_order_create_url();
    }

    std::string escape(const std::string& value) {
        if (curl_ == nullptr) {
            return value;
        }
        char* encoded = curl_easy_escape(curl_, value.c_str(), static_cast<int>(value.size()));
        if (encoded == nullptr) {
            return value;
        }
        std::string result(encoded);
        curl_free(encoded);
        return result;
    }

private:
    void configure_common_options() {
        if (curl_ == nullptr) {
            return;
        }
        curl_easy_setopt(curl_, CURLOPT_WRITEFUNCTION, write_callback);
        curl_easy_setopt(curl_, CURLOPT_TIMEOUT, 10L);
        curl_easy_setopt(curl_, CURLOPT_CONNECTTIMEOUT_MS, 1000L);
        curl_easy_setopt(curl_, CURLOPT_DNS_CACHE_TIMEOUT, 3600L);
        curl_easy_setopt(curl_, CURLOPT_TCP_KEEPALIVE, 1L);
        curl_easy_setopt(curl_, CURLOPT_TCP_NODELAY, 1L);
#ifdef CURLOPT_TCP_FASTOPEN
        curl_easy_setopt(curl_, CURLOPT_TCP_FASTOPEN, 1L);
#endif
        curl_easy_setopt(curl_, CURLOPT_NOSIGNAL, 1L);
        curl_easy_setopt(curl_, CURLOPT_USERAGENT, "ChainPulse-UltraEngine/1.0");
    }

    HttpResult perform(const std::string& method, const std::string& path, const std::string& body, const AuthHeaders* headers) {
        url_buffer_.clear();
        url_buffer_.reserve(base_url_.size() + path.size());
        url_buffer_ += base_url_;
        url_buffer_ += path;
        order_create_url_ready_ = false;
        return perform_url(method, url_buffer_, body, headers);
    }

    HttpResult perform_url(const std::string& method, const std::string& url, const std::string& body, const AuthHeaders* headers) {
        HttpResult result;
        if (curl_ == nullptr) {
            result.error = "curl_init_failed";
            return result;
        }
        curl_easy_setopt(curl_, CURLOPT_URL, url.c_str());
        order_create_url_ready_ = false;
        curl_easy_setopt(curl_, CURLOPT_WRITEDATA, &result.body);

        std::array<curl_slist, AUTH_HEADER_COUNT> header_nodes{};
        struct curl_slist* header_list = nullptr;
        if (headers != nullptr) {
            header_list = stack_auth_header_list(*headers, header_nodes);
        }
        if (header_list != nullptr) {
            curl_easy_setopt(curl_, CURLOPT_HTTPHEADER, header_list);
        } else {
            curl_easy_setopt(curl_, CURLOPT_HTTPHEADER, nullptr);
        }
        order_header_list_applied_ = false;
        if (method == "POST") {
            curl_easy_setopt(curl_, CURLOPT_POST, 1L);
            curl_easy_setopt(curl_, CURLOPT_POSTFIELDS, body.c_str());
            curl_easy_setopt(curl_, CURLOPT_POSTFIELDSIZE, body.size());
        } else {
            curl_easy_setopt(curl_, CURLOPT_HTTPGET, 1L);
        }

        const CURLcode code = curl_easy_perform(curl_);
        if (code != CURLE_OK) {
            result.error = curl_easy_strerror(code);
        } else {
            curl_easy_getinfo(curl_, CURLINFO_RESPONSE_CODE, &result.status_code);
            result.success = result.status_code >= 200 && result.status_code < 300;
        }

        curl_easy_setopt(curl_, CURLOPT_HTTPHEADER, nullptr);
        order_header_list_applied_ = false;
        curl_easy_setopt(curl_, CURLOPT_POSTFIELDS, nullptr);
        return result;
    }

    HttpResult perform_order_create(const std::string& body, const AuthHeaders& headers) {
        HttpResult result;
        if (curl_ == nullptr) {
            result.error = "curl_init_failed";
            return result;
        }
        if (!ensure_order_create_url()) {
            result.error = "curl_url_failed";
            return result;
        }

        curl_easy_setopt(curl_, CURLOPT_WRITEDATA, &result.body);

        struct curl_slist* header_list = stack_auth_header_list(headers, order_header_nodes_);
        if (!order_header_list_applied_) {
            curl_easy_setopt(curl_, CURLOPT_HTTPHEADER, header_list);
            order_header_list_applied_ = true;
        }
        curl_easy_setopt(curl_, CURLOPT_POSTFIELDS, body.data());
        curl_easy_setopt(curl_, CURLOPT_POSTFIELDSIZE, body.size());

        const CURLcode code = curl_easy_perform(curl_);
        if (code != CURLE_OK) {
            result.error = curl_easy_strerror(code);
        } else {
            curl_easy_getinfo(curl_, CURLINFO_RESPONSE_CODE, &result.status_code);
            result.success = result.status_code >= 200 && result.status_code < 300;
        }

        curl_easy_setopt(curl_, CURLOPT_POSTFIELDS, nullptr);
        return result;
    }

    bool prepare_post_for_benchmark(const std::string& body, const AuthHeaders& headers) {
        HttpResult result;
        struct curl_slist* header_list = stack_auth_header_list(headers, order_header_nodes_);
        curl_easy_setopt(curl_, CURLOPT_WRITEDATA, &result.body);
        if (!order_header_list_applied_) {
            curl_easy_setopt(curl_, CURLOPT_HTTPHEADER, header_list);
            order_header_list_applied_ = true;
        }
        curl_easy_setopt(curl_, CURLOPT_POSTFIELDS, body.data());
        curl_easy_setopt(curl_, CURLOPT_POSTFIELDSIZE, body.size());
        curl_easy_setopt(curl_, CURLOPT_POSTFIELDS, nullptr);
        return true;
    }

    bool ensure_order_create_url() {
        if (curl_ == nullptr) {
            return false;
        }
        if (order_create_url_ready_) {
            return true;
        }
        const CURLcode url_code = curl_easy_setopt(curl_, CURLOPT_URL, order_create_url_.c_str());
        if (url_code != CURLE_OK) {
            return false;
        }
        const CURLcode post_code = curl_easy_setopt(curl_, CURLOPT_POST, 1L);
        if (post_code != CURLE_OK) {
            return false;
        }
        order_create_url_ready_ = true;
        return true;
    }

    std::string base_url_;
    std::string order_create_url_;
    std::string url_buffer_;
    std::array<curl_slist, AUTH_HEADER_COUNT> order_header_nodes_{};
    CURL* curl_{nullptr};
    bool order_create_url_ready_{false};
    bool order_header_list_applied_{false};
};

class ListingUltraEngine {
public:
    ListingUltraEngine()
        : api_key_(getenv_or("BYBIT_API_KEY")),
          api_secret_(getenv_or("BYBIT_API_SECRET")),
          base_url_(getenv_or("BYBIT_API_BASE_URL", "https://api.bybit.com")),
          recv_window_(getenv_or("BYBIT_RECV_WINDOW", "5000")),
          timestamp_bias_ms_(getenv_long_long_or("BYBIT_TIMESTAMP_BIAS_MS", -50)),
          buy_enabled_(getenv_truthy("BYBIT_SPOT_BUY_ENABLED", false)),
          buy_quote_amount_(getenv_or("BYBIT_SPOT_BUY_USDT_AMOUNT", "0")),
          buy_quote_amount_valid_(is_positive_decimal_text(buy_quote_amount_)),
          cache_only_symbol_check_(getenv_truthy("BYBIT_PREFER_CACHED_SYMBOL_CHECK", true)),
          order_on_cache_miss_(getenv_truthy("LISTING_CPP_ULTRA_ORDER_ON_CACHE_MISS", false)),
          order_preflight_only_(getenv_truthy("LISTING_CPP_ULTRA_ORDER_PREFLIGHT_ONLY", false)),
          client_(base_url_),
          market_client_(base_url_) {
        content_type_header_ = "Content-Type: application/json";
        api_key_header_.reserve(16 + api_key_.size());
        api_key_header_ = "X-BAPI-API-KEY: ";
        api_key_header_ += api_key_;
        recv_window_header_.reserve(20 + recv_window_.size());
        recv_window_header_ = "X-BAPI-RECV-WINDOW: ";
        recv_window_header_ += recv_window_;
        auth_plain_static_.reserve(api_key_.size() + recv_window_.size());
        auth_plain_static_ = api_key_;
        auth_plain_static_ += recv_window_;
        order_body_mid_.reserve(96 + buy_quote_amount_.size());
        order_body_mid_ = "\",\"side\":\"Buy\",\"orderType\":\"Market\",\"qty\":\"";
        order_body_mid_ += buy_quote_amount_;
        order_body_mid_ += "\",\"orderFilter\":\"Order\",\"marketUnit\":\"quoteCoin\",\"orderLinkId\":\"";
        init_hmac_sha256_pads(api_secret_, hmac_ipad_, hmac_opad_);
        seen_keys_.reserve(65536);
        seen_listing_keys_.reserve(65536);
        for (std::size_t i = FIRST_SECONDARY_WORKER; i < MAX_ULTRA_TICKERS; ++i) {
            parallel_clients_[i] = std::make_unique<CurlClient>(base_url_);
        }
    }

    ~ListingUltraEngine() {
        stop_workers();
    }

    int warmup() {
        if (buy_enabled_ && (api_key_.empty() || api_secret_.empty() || !buy_quote_amount_valid_)) {
            return -2;
        }
        wait_workers_ready_once();
        const bool symbols_ready = refresh_symbols();
        (void)warm_order_clients_once();
        return symbols_ready ? 0 : -1;
    }

    int handle(std::string_view exchange, long long message_id, std::string_view title, NativeUltraResult* out) {
        if (out == nullptr) {
            return -1;
        }
        reset_result_for_handle(out);

        std::uint64_t exchange_key = 0;
        if (exchange == "upbit") {
            exchange_key = 1;
        } else if (exchange == "bithumb") {
            exchange_key = 2;
        } else {
            return 0;
        }
        const std::uint64_t dedup_key =
            (exchange_key << 60) ^ static_cast<std::uint64_t>(message_id);
        {
            std::lock_guard<std::mutex> lock(seen_mu_);
            auto [_, inserted] = seen_keys_.insert(dedup_key);
            if (!inserted) {
                out->duplicate = 1;
                copy_to_buffer("duplicate", out->reason, sizeof(out->reason));
                return 1;
            }
        }

        bool matched = false;
        std::string_view signal_type;
        if (exchange_key == 1) {
            matched = false;
            if (title.rfind("[거래]", 0) == 0 &&
                contains_none(title, UPBIT_EXCLUDE_KEYWORDS, std::size(UPBIT_EXCLUDE_KEYWORDS)) &&
                has_ascii_word(title, "KRW")) {
                constexpr std::string_view new_listing_anchor = "신규 거래지원 안내";
                if (title.find(new_listing_anchor) != std::string_view::npos) {
                    const size_t market_end = find_market_parenthetical_end(
                        title,
                        title.find(new_listing_anchor)
                    );
                    matched = market_end != std::string_view::npos &&
                              trim_ascii_view(title.substr(market_end)).empty();
                } else {
                    constexpr std::string_view market_add_suffix = "마켓 디지털 자산 추가";
                    const std::string_view trimmed = trim_ascii_view(title);
                    matched = title.find(market_add_suffix) != std::string_view::npos &&
                              trimmed.size() >= market_add_suffix.size() &&
                              trimmed.compare(
                                  trimmed.size() - market_add_suffix.size(),
                                  market_add_suffix.size(),
                                  market_add_suffix
                              ) == 0;
                }
            }
            signal_type = "new_listing";
        } else {
            matched = false;
            if (has_bithumb_listing_prefix(title) &&
                contains_none(title, BITHUMB_EXCLUDE_KEYWORDS, std::size(BITHUMB_EXCLUDE_KEYWORDS)) &&
                title.find("원화 마켓 재거래지원 안내") == std::string_view::npos) {
                constexpr std::string_view marker = "원화 마켓 추가";
                const size_t marker_pos = title.find(marker);
                matched = marker_pos != std::string_view::npos &&
                          is_allowed_bithumb_market_add_suffix(
                              title.substr(marker_pos + marker.size())
                          );
            }
            signal_type = "market_add";
        }

        if (!matched) {
            return 0;
        }

        const TickerScanResult ticker_scan = scan_listing_tickers(title);
        const std::string_view ticker = ticker_scan.primary;
        if (ticker.empty()) {
            return 0;
        }
        ListingTickers tickers;
        if (ticker_scan.multiple) {
            tickers = extract_listing_tickers(title);
        } else {
            tickers.push_unique(ticker);
        }
        if (tickers.count == 0) {
            return 0;
        }
        ListingTickers fresh_tickers;
        std::array<std::string, MAX_ULTRA_TICKERS> listing_keys{};
        {
            std::lock_guard<std::mutex> lock(seen_mu_);
            for (std::size_t i = 0; i < tickers.count; ++i) {
                std::string listing_key;
                listing_key.reserve(exchange.size() + 1 + tickers.values[i].size());
                listing_key.append(exchange);
                listing_key.push_back(':');
                listing_key.append(tickers.values[i]);
                if (seen_listing_keys_.find(listing_key) == seen_listing_keys_.end()) {
                    listing_keys[fresh_tickers.count] = std::move(listing_key);
                    fresh_tickers.push_unique(tickers.values[i]);
                }
            }
            if (fresh_tickers.count == 0) {
                out->duplicate = 1;
                copy_to_buffer("duplicate_listing_ticker", out->reason, sizeof(out->reason));
                return 1;
            }
            for (std::size_t i = 0; i < fresh_tickers.count; ++i) {
                seen_listing_keys_.insert(std::move(listing_keys[i]));
            }
        }

        std::array<char, 32> symbol_buffer{};
        const std::string_view primary_ticker = fresh_tickers.values[0];
        const std::string_view symbol = spot_symbol_view(primary_ticker, symbol_buffer);
        if (symbol.empty()) {
            return 0;
        }

        const std::string asset_name = extract_asset_name_for_ticker(title, primary_ticker);
        fill_listing_result_fields(out, title, primary_ticker, asset_name, signal_type, symbol);
        if (fresh_tickers.count == 1) {
            const UltraOrderResult trade = place_order_with_client(
                client_,
                exchange,
                message_id,
                fresh_tickers.values[0]
            );
            out->trade_count = 1;
            record_trade_result(out, trade);
            fill_primary_trade_result(out, trade);
            return 1;
        }

        std::array<UltraOrderResult, MAX_ULTRA_TICKERS> trades{};
        const std::size_t trade_count = buy_listing_orders(
            exchange,
            message_id,
            fresh_tickers,
            trades
        );
        out->trade_count = static_cast<int>(trade_count);
        for (std::size_t i = 0; i < trade_count; ++i) {
            record_trade_result(out, trades[i]);
        }
        if (trade_count == 0) {
            copy_to_buffer("order_not_attempted", out->reason, sizeof(out->reason));
            return 1;
        }

        store_multi_trade_results(dedup_key, fresh_tickers, trades, trade_count);
        fill_primary_trade_result(out, trades[0]);
        return 1;
    }

private:
    static std::uint64_t make_trade_key(std::string_view exchange, long long message_id) {
        std::uint64_t exchange_key = 0;
        if (exchange == "upbit" || exchange == "u") {
            exchange_key = 1;
        } else if (exchange == "bithumb" || exchange == "b") {
            exchange_key = 2;
        }
        if (exchange_key == 0) {
            return 0;
        }
        return (exchange_key << 60) ^ static_cast<std::uint64_t>(message_id);
    }

    void store_multi_trade_results(
        std::uint64_t key,
        const ListingTickers& tickers,
        const std::array<UltraOrderResult, MAX_ULTRA_TICKERS>& trades,
        std::size_t trade_count
    ) {
        if (key == 0 || trade_count <= 1) {
            return;
        }
        StoredUltraTrades stored;
        stored.count = std::min(trade_count, MAX_ULTRA_TICKERS);
        for (std::size_t i = 0; i < stored.count; ++i) {
            stored.values[i].ticker.assign(tickers.values[i]);
            stored.values[i].trade = trades[i];
        }
        std::lock_guard<std::mutex> lock(trade_results_mu_);
        if (multi_trade_results_.size() > 1024) {
            multi_trade_results_.clear();
        }
        multi_trade_results_[key] = std::move(stored);
    }

public:
    int get_trade_results(
        std::string_view exchange,
        long long message_id,
        NativeUltraTradeResult* out,
        int capacity
    ) {
        if (out == nullptr || capacity <= 0) {
            return 0;
        }
        const std::uint64_t key = make_trade_key(exchange, message_id);
        if (key == 0) {
            return 0;
        }
        StoredUltraTrades stored;
        {
            std::lock_guard<std::mutex> lock(trade_results_mu_);
            auto found = multi_trade_results_.find(key);
            if (found == multi_trade_results_.end()) {
                return 0;
            }
            stored = std::move(found->second);
            multi_trade_results_.erase(found);
        }
        const std::size_t count = std::min<std::size_t>(
            stored.count,
            static_cast<std::size_t>(capacity)
        );
        for (std::size_t i = 0; i < count; ++i) {
            copy_trade_result(
                &out[i],
                stored.values[i].ticker,
                stored.values[i].trade
            );
        }
        return static_cast<int>(count);
    }

private:
    std::size_t buy_listing_orders(
        std::string_view exchange,
        long long message_id,
        const ListingTickers& tickers,
        std::array<UltraOrderResult, MAX_ULTRA_TICKERS>& out
    ) {
        if (tickers.count == 0) {
            return 0;
        }
        if (tickers.count == 1) {
            out[0] = place_order_with_client(
                client_,
                exchange,
                message_id,
                tickers.values[0]
            );
            return 1;
        }

        wait_workers_ready_once();
        std::array<std::uint64_t, MAX_ULTRA_TICKERS> expected_done_seq{};
        for (std::size_t i = 1; i < tickers.count; ++i) {
            auto& slot = worker_slots_[i];
            slot.exchange = exchange;
            slot.message_id = message_id;
            slot.ticker = tickers.values[i];
            slot.result = UltraOrderResult();
            const std::uint64_t next_work_seq =
                slot.work_seq.load(std::memory_order_relaxed) + 1;
            expected_done_seq[i] = next_work_seq;
            slot.work_seq.store(next_work_seq, std::memory_order_release);
            slot.work_seq.notify_one();
        }
        out[0] = place_order_with_client(
            client_,
            exchange,
            message_id,
            tickers.values[0]
        );
        for (std::size_t i = 1; i < tickers.count; ++i) {
            auto& slot = worker_slots_[i];
            std::uint64_t observed = slot.done_seq.load(std::memory_order_acquire);
            while (observed != expected_done_seq[i]) {
                slot.done_seq.wait(observed, std::memory_order_acquire);
                observed = slot.done_seq.load(std::memory_order_acquire);
            }
            out[i] = std::move(slot.result);
            slot.result = UltraOrderResult();
        }
        return tickers.count;
    }

    UltraOrderResult place_order_with_client(
        CurlClient& client,
        std::string_view exchange,
        long long message_id,
        std::string_view ticker
    ) {
        UltraOrderResult result;
        std::array<char, 32> symbol_buffer{};
        const std::string_view symbol = spot_symbol_view(ticker, symbol_buffer);
        result.symbol.assign(symbol);

        auto fail = [&](std::string_view reason) {
            result.reason.assign(reason);
            return result;
        };

        if (symbol.empty()) {
            return fail("invalid_symbol");
        }
        if (!buy_enabled_) {
            return fail("buy_disabled");
        }
        if (api_key_.empty() || api_secret_.empty()) {
            return fail("missing_api_config");
        }
        if (!buy_quote_amount_valid_) {
            return fail("quote_amount_invalid");
        }
        if (!order_on_cache_miss_ && !has_spot_symbol(symbol)) {
            return fail("spot_symbol_unavailable");
        }

        std::array<char, 64> order_link_id_buffer{};
        const std::string_view order_link_id = build_order_link_id_view(
            exchange,
            message_id,
            ticker,
            order_link_id_buffer
        );
        result.order_link_id.assign(order_link_id);
        result.transport = "cpp_ultra_rest";
        result.attempted = 1;

        const PreparedOrderRequest& request = prepare_order_request(symbol, order_link_id);
        if (order_preflight_only_) {
            const bool prepared = client.prepare_post_order_for_benchmark(
                request.body,
                request.headers
            );
            result.ret_code = prepared ? 0 : -1;
            result.reason = prepared ? "cpp_ultra_rest_preflight" : "curl_preflight_failed";
            return result;
        }

        const auto response = client.post_order_create(request.body, request.headers);
        const long long ret_code = extract_json_int(response.body, "retCode").value_or(-1);
        result.ret_code = static_cast<int>(ret_code);
        if (!response.success || ret_code != 0) {
            result.reason = extract_json_string(response.body, "retMsg").value_or(
                response.error.empty() ? "order_create_failed" : response.error
            );
            return result;
        }

        result.executed = 1;
        result.order_id = extract_json_string(response.body, "orderId").value_or("");
        result.reason = "cpp_ultra_rest";
        return result;
    }

    void start_workers_once() {
        bool expected = false;
        if (!workers_started_.compare_exchange_strong(expected, true)) {
            return;
        }
        worker_pool_ready_.store(false, std::memory_order_release);
        for (std::size_t i = FIRST_SECONDARY_WORKER; i < MAX_ULTRA_TICKERS; ++i) {
            auto& slot = worker_slots_[i];
            slot.stop_requested.store(false, std::memory_order_release);
            slot.result = UltraOrderResult();
        }
        for (std::size_t i = FIRST_SECONDARY_WORKER; i < MAX_ULTRA_TICKERS; ++i) {
            worker_ready_[i].store(false, std::memory_order_release);
        }
        for (std::size_t i = FIRST_SECONDARY_WORKER; i < MAX_ULTRA_TICKERS; ++i) {
            worker_threads_[i] = std::thread([this, i]() {
                worker_loop(i);
            });
        }
    }

    void wait_workers_ready_once() {
        if (worker_pool_ready_.load(std::memory_order_acquire)) {
            return;
        }
        start_workers_once();
        for (std::size_t i = FIRST_SECONDARY_WORKER; i < MAX_ULTRA_TICKERS; ++i) {
            bool observed = worker_ready_[i].load(std::memory_order_acquire);
            while (!observed) {
                worker_ready_[i].wait(observed, std::memory_order_acquire);
                observed = worker_ready_[i].load(std::memory_order_acquire);
            }
        }
        worker_pool_ready_.store(true, std::memory_order_release);
    }

    void stop_workers() {
        if (!workers_started_.exchange(false)) {
            return;
        }
        worker_pool_ready_.store(false, std::memory_order_release);
        for (std::size_t i = FIRST_SECONDARY_WORKER; i < MAX_ULTRA_TICKERS; ++i) {
            auto& slot = worker_slots_[i];
            slot.stop_requested.store(true, std::memory_order_release);
            slot.work_seq.fetch_add(1, std::memory_order_acq_rel);
            slot.work_seq.notify_one();
        }
        for (std::size_t i = FIRST_SECONDARY_WORKER; i < MAX_ULTRA_TICKERS; ++i) {
            if (worker_threads_[i].joinable()) {
                worker_threads_[i].join();
            }
        }
    }

    void worker_loop(std::size_t index) {
        (void)prime_order_client_for_hot_path(*parallel_clients_[index]);
        worker_ready_[index].store(true, std::memory_order_release);
        worker_ready_[index].notify_one();
        auto& slot = worker_slots_[index];
        std::uint64_t seen_work_seq = slot.done_seq.load(std::memory_order_relaxed);
        while (true) {
            if (slot.stop_requested.load(std::memory_order_acquire)) {
                return;
            }
            const std::uint64_t current_work_seq =
                slot.work_seq.load(std::memory_order_acquire);
            if (current_work_seq == seen_work_seq) {
                slot.work_seq.wait(seen_work_seq, std::memory_order_acquire);
                continue;
            }
            if (slot.stop_requested.load(std::memory_order_acquire)) {
                return;
            }
            seen_work_seq = current_work_seq;

            UltraOrderResult result = place_order_with_client(
                *parallel_clients_[index],
                slot.exchange,
                slot.message_id,
                slot.ticker
            );

            slot.result = std::move(result);
            slot.done_seq.store(current_work_seq, std::memory_order_release);
            slot.done_seq.notify_one();
        }
    }

    bool refresh_symbols() {
        std::lock_guard<std::mutex> lock(refresh_mu_);
        SpotSymbolSet next_symbols;
        std::string cursor;
        do {
            std::string path = "/v5/market/instruments-info?category=spot&limit=1000";
            if (!cursor.empty()) {
                path += "&cursor=" + market_client_.escape(cursor);
            }
            const auto response = market_client_.get(path);
            if (!response.success) {
                return false;
            }
            const auto symbols = extract_symbols(response.body);
            for (const auto& symbol : symbols) {
                next_symbols.insert(symbol);
            }
            cursor = extract_json_string(response.body, "nextPageCursor").value_or("");
        } while (!cursor.empty());
        if (!next_symbols.empty()) {
            std::shared_ptr<const SpotSymbolSet> snapshot =
                std::make_shared<SpotSymbolSet>(std::move(next_symbols));
            std::atomic_store_explicit(
                &spot_symbols_,
                snapshot,
                std::memory_order_release
            );
        }
        return spot_symbol_count() > 0;
    }

    bool warm_order_clients_once() {
        bool expected = false;
        if (!order_clients_warmed_.compare_exchange_strong(expected, true)) {
            return true;
        }

        const auto primary_response =
            client_.get("/v5/market/instruments-info?category=spot&limit=1");
        bool all_ready = primary_response.success && prime_order_client_for_hot_path(client_);

        std::array<std::thread, MAX_ULTRA_TICKERS> threads;
        std::array<bool, MAX_ULTRA_TICKERS> successes{};
        for (std::size_t i = FIRST_SECONDARY_WORKER; i < MAX_ULTRA_TICKERS; ++i) {
            threads[i] = std::thread([this, &successes, i]() {
                const auto response = parallel_clients_[i]->get(
                    "/v5/market/instruments-info?category=spot&limit=1"
                );
                successes[i] =
                    response.success && prime_order_client_for_hot_path(*parallel_clients_[i]);
            });
        }
        for (std::size_t i = FIRST_SECONDARY_WORKER; i < MAX_ULTRA_TICKERS; ++i) {
            if (threads[i].joinable()) {
                threads[i].join();
            }
        }
        for (std::size_t i = FIRST_SECONDARY_WORKER; i < MAX_ULTRA_TICKERS; ++i) {
            all_ready = all_ready && successes[i];
        }
        if (!all_ready) {
            order_clients_warmed_.store(false);
            return false;
        }
        return true;
    }

    bool has_spot_symbol(std::string_view symbol) {
        auto symbols = std::atomic_load_explicit(
            &spot_symbols_,
            std::memory_order_acquire
        );
        if (symbols != nullptr && symbols->find(symbol) != symbols->end()) {
            return true;
        }
        if (cache_only_symbol_check_) {
            return false;
        }
        refresh_symbols();
        symbols = std::atomic_load_explicit(
            &spot_symbols_,
            std::memory_order_acquire
        );
        return symbols != nullptr && symbols->find(symbol) != symbols->end();
    }

    std::size_t spot_symbol_count() const {
        const auto symbols = std::atomic_load_explicit(
            &spot_symbols_,
            std::memory_order_acquire
        );
        return symbols == nullptr ? 0 : symbols->size();
    }

    bool prime_order_client_for_hot_path(CurlClient& client) const {
        const PreparedOrderRequest& request = prepare_order_request(
            "XXXXXXXXXXUSDT",
            "ls-bithumb-9223372036854775807-XXXXX"
        );
        return client.prepare_post_order_for_benchmark(request.body, request.headers);
    }

    const PreparedOrderRequest& prepare_order_request(
        std::string_view symbol,
        std::string_view order_link_id
    ) const {
        constexpr std::string_view body_prefix = "{\"category\":\"spot\",\"symbol\":\"";
        thread_local PreparedOrderRequest request;
        request.body.clear();
        request.body += body_prefix;
        request.body.append(symbol);
        request.body += order_body_mid_;
        request.body.append(order_link_id);
        request.body += "\"}";
        auth_headers_into(request.body, request.headers);
        return request;
    }

    void auth_headers_into(std::string_view body, AuthHeaders& headers) const {
        std::array<char, 32> timestamp_buffer{};
        const std::string_view timestamp = current_timestamp_ms(timestamp_buffer, timestamp_bias_ms_);
        thread_local std::string plain;
        plain.clear();
        plain.reserve(timestamp.size() + api_key_.size() + recv_window_.size() + body.size());
        plain.append(timestamp);
        plain += auth_plain_static_;
        plain.append(body);
        headers.content_type_header = &content_type_header_;
        headers.api_key_header = &api_key_header_;
        headers.recv_window_header = &recv_window_header_;
        headers.sign_header.clear();
        headers.sign_header.reserve(14 + SHA256_DIGEST_LENGTH * 2);
        headers.sign_header = "X-BAPI-SIGN: ";
        append_signature_hex(plain, headers.sign_header);
        headers.timestamp_header.clear();
        headers.timestamp_header.reserve(18 + timestamp.size());
        headers.timestamp_header = "X-BAPI-TIMESTAMP: ";
        headers.timestamp_header.append(timestamp);
    }

    void append_signature_hex(std::string_view payload, std::string& out) const {
        unsigned char inner_digest[SHA256_DIGEST_LENGTH];
        unsigned char digest[SHA256_DIGEST_LENGTH];
        SHA256_CTX ctx;
        SHA256_Init(&ctx);
        SHA256_Update(&ctx, hmac_ipad_.data(), hmac_ipad_.size());
        SHA256_Update(&ctx, payload.data(), payload.size());
        SHA256_Final(inner_digest, &ctx);
        SHA256_Init(&ctx);
        SHA256_Update(&ctx, hmac_opad_.data(), hmac_opad_.size());
        SHA256_Update(&ctx, inner_digest, sizeof(inner_digest));
        SHA256_Final(digest, &ctx);
        append_hex_digest(digest, out);
    }

    static void append_hex_digest(const unsigned char* digest, std::string& out) {
        static constexpr char kHex[] = "0123456789abcdef";
        const std::size_t start = out.size();
        out.resize(start + SHA256_DIGEST_LENGTH * 2);
        for (std::size_t i = 0; i < SHA256_DIGEST_LENGTH; ++i) {
            out[start + static_cast<std::size_t>(i) * 2] = kHex[(digest[i] >> 4) & 0x0F];
            out[start + static_cast<std::size_t>(i) * 2 + 1] = kHex[digest[i] & 0x0F];
        }
    }

    std::string api_key_;
    std::string api_secret_;
    std::string base_url_;
    std::string recv_window_;
    long long timestamp_bias_ms_{-50};
    bool buy_enabled_{false};
    std::string buy_quote_amount_;
    bool buy_quote_amount_valid_{false};
    std::string content_type_header_;
    std::string api_key_header_;
    std::string recv_window_header_;
    std::string auth_plain_static_;
    std::string order_body_mid_;
    HmacPad hmac_ipad_{};
    HmacPad hmac_opad_{};
    bool cache_only_symbol_check_{true};
    bool order_on_cache_miss_{false};
    bool order_preflight_only_{false};
    CurlClient client_;
    CurlClient market_client_;
    std::array<std::unique_ptr<CurlClient>, MAX_ULTRA_TICKERS> parallel_clients_;
    std::array<UltraOrderWorkerSlot, MAX_ULTRA_TICKERS> worker_slots_;
    std::array<std::thread, MAX_ULTRA_TICKERS> worker_threads_;
    std::array<std::atomic<bool>, MAX_ULTRA_TICKERS> worker_ready_{};
    std::atomic<bool> order_clients_warmed_{false};
    std::atomic<bool> workers_started_{false};
    std::atomic<bool> worker_pool_ready_{false};
    std::unordered_map<std::uint64_t, StoredUltraTrades> multi_trade_results_;
    std::mutex trade_results_mu_;
    std::shared_ptr<const SpotSymbolSet> spot_symbols_{std::make_shared<SpotSymbolSet>()};
    std::unordered_set<std::uint64_t> seen_keys_;
    std::unordered_set<std::string> seen_listing_keys_;
    std::mutex seen_mu_;
    std::mutex refresh_mu_;
};

ListingUltraEngine& global_engine() {
    static ListingUltraEngine engine;
    return engine;
}

}  // namespace

extern "C" int listing_ultra_warmup() {
    return global_engine().warmup();
}

extern "C" int handle_listing_post(
    const char* exchange,
    long long message_id,
    const char* title,
    NativeUltraResult* out
) {
    if (exchange == nullptr || title == nullptr || out == nullptr) {
        return -1;
    }
    return global_engine().handle(exchange, message_id, title, out);
}

extern "C" int get_listing_trades(
    const char* exchange,
    long long message_id,
    NativeUltraTradeResult* out,
    int capacity
) {
    if (exchange == nullptr || out == nullptr || capacity <= 0) {
        return 0;
    }
    return global_engine().get_trade_results(exchange, message_id, out, capacity);
}
