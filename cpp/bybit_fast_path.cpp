#include <curl/curl.h>
#ifndef OPENSSL_API_COMPAT
#define OPENSSL_API_COMPAT 0x10100000L
#endif
#include <openssl/hmac.h>
#include <openssl/sha.h>

#include <array>
#include <atomic>
#include <algorithm>
#include <chrono>
#include <charconv>
#include <condition_variable>
#include <cctype>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <iomanip>
#include <iostream>
#include <memory>
#include <mutex>
#include <optional>
#include <sstream>
#include <string>
#include <string_view>
#include <thread>
#include <unordered_set>
#include <vector>

namespace {

constexpr std::size_t MAX_BULK_ORDERS = 16;
constexpr std::size_t MAX_COMMAND_PARTS = 2 + (MAX_BULK_ORDERS * 2);
constexpr std::size_t HMAC_SHA256_BLOCK_SIZE = 64;
constexpr std::size_t AUTH_HEADER_COUNT = 5;

struct HttpResult {
    long status_code{0};
    std::string body;
    std::string error;
    bool success{false};
};

using HmacPad = std::array<unsigned char, HMAC_SHA256_BLOCK_SIZE>;

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

struct BulkWorkerSlot {
    std::atomic<std::uint64_t> work_seq{0};
    std::atomic<std::uint64_t> done_seq{0};
    std::atomic<bool> stop_requested{false};
    std::string_view symbol;
    std::string_view quote_amount;
    std::string_view order_link_id;
    std::string response;
};

struct FrameParts {
    std::array<std::string_view, MAX_COMMAND_PARTS> values{};
    std::size_t count{0};
    bool overflow{false};

    bool empty() const {
        return count == 0;
    }

    std::size_t size() const {
        return count;
    }

    std::string_view operator[](std::size_t index) const {
        return values[index];
    }
};

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

std::string json_escape(std::string_view input) {
    std::string escaped;
    escaped.reserve(input.size());
    for (const char ch : input) {
        switch (ch) {
            case '\\':
                escaped += "\\\\";
                break;
            case '"':
                escaped += "\\\"";
                break;
            case '\n':
                escaped += "\\n";
                break;
            case '\r':
                escaped += "\\r";
                break;
            case '\t':
                escaped += "\\t";
                break;
            default:
                escaped.push_back(ch);
        }
    }
    return escaped;
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

FrameParts split_tab_views(std::string_view line) {
    FrameParts parts;
    size_t start = 0;
    while (start <= line.size()) {
        size_t end = line.find('\t', start);
        if (end == std::string::npos) {
            if (parts.count < parts.values.size()) {
                parts.values[parts.count++] = line.substr(start);
            } else {
                parts.overflow = true;
            }
            break;
        }
        if (parts.count < parts.values.size()) {
            parts.values[parts.count++] = line.substr(start, end - start);
        } else {
            parts.overflow = true;
        }
        start = end + 1;
    }
    return parts;
}

std::string to_string(std::string_view value) {
    return std::string(value.data(), value.size());
}

bool read_frame(std::istream& input, std::string& out) {
    unsigned char header[4];
    if (!input.read(reinterpret_cast<char*>(header), sizeof(header))) {
        return false;
    }
    const std::uint32_t size =
        (static_cast<std::uint32_t>(header[0]) << 24) |
        (static_cast<std::uint32_t>(header[1]) << 16) |
        (static_cast<std::uint32_t>(header[2]) << 8) |
        static_cast<std::uint32_t>(header[3]);
    out.resize(size);
    if (size == 0) {
        return true;
    }
    return static_cast<bool>(input.read(out.data(), static_cast<std::streamsize>(size)));
}

void write_frame(std::ostream& output, const std::string& payload) {
    const std::uint32_t size = static_cast<std::uint32_t>(payload.size());
    const unsigned char header[4] = {
        static_cast<unsigned char>((size >> 24) & 0xff),
        static_cast<unsigned char>((size >> 16) & 0xff),
        static_cast<unsigned char>((size >> 8) & 0xff),
        static_cast<unsigned char>(size & 0xff),
    };
    output.write(reinterpret_cast<const char*>(header), sizeof(header));
    if (!payload.empty()) {
        output.write(payload.data(), static_cast<std::streamsize>(payload.size()));
    }
    output.flush();
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

    HttpResult get(std::string_view path) {
        return perform(false, path, "", nullptr);
    }

    HttpResult post(
        std::string_view path,
        const std::string& body,
        const AuthHeaders& headers
    ) {
        return perform(true, path, body, &headers);
    }

    HttpResult post_order_create(
        const std::string& body,
        const AuthHeaders& headers
    ) {
        return perform_order_create(body, headers);
    }

    bool prepare_post_order_for_benchmark(
        const std::string& body,
        const AuthHeaders& headers
    ) {
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
        curl_easy_setopt(curl_, CURLOPT_USERAGENT, "ChainPulse-FastPath/1.0");
    }

    HttpResult perform(
        bool is_post,
        std::string_view path,
        std::string_view body,
        const AuthHeaders* headers
    ) {
        url_buffer_.clear();
        url_buffer_.reserve(base_url_.size() + path.size());
        url_buffer_ += base_url_;
        url_buffer_.append(path);
        order_create_url_ready_ = false;
        return perform_url(is_post, url_buffer_, body, headers);
    }

    HttpResult perform_url(
        bool is_post,
        const std::string& url,
        std::string_view body,
        const AuthHeaders* headers
    ) {
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

        if (is_post) {
            curl_easy_setopt(curl_, CURLOPT_POST, 1L);
            curl_easy_setopt(curl_, CURLOPT_POSTFIELDS, body.data());
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

    HttpResult perform_order_create(
        std::string_view body,
        const AuthHeaders& headers
    ) {
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

    bool prepare_post_for_benchmark(
        std::string_view body,
        const AuthHeaders& headers
    ) {
        if (curl_ == nullptr) {
            return false;
        }
        HttpResult result;
        curl_easy_setopt(curl_, CURLOPT_WRITEDATA, &result.body);
        struct curl_slist* header_list = stack_auth_header_list(headers, order_header_nodes_);
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

class BybitFastPath {
public:
    BybitFastPath()
        : api_key_(getenv_or("BYBIT_API_KEY")),
          api_secret_(getenv_or("BYBIT_API_SECRET")),
          base_url_(getenv_or("BYBIT_API_BASE_URL", "https://api.bybit.com")),
          recv_window_(getenv_or("BYBIT_RECV_WINDOW", "5000")),
          timestamp_bias_ms_(getenv_long_long_or("BYBIT_TIMESTAMP_BIAS_MS", -50)),
          order_on_cache_miss_(getenv_truthy("BYBIT_FAST_ORDER_ON_CACHE_MISS", false)),
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
        init_hmac_sha256_pads(api_secret_, hmac_ipad_, hmac_opad_);
        publish_spot_symbols(std::make_shared<SpotSymbolSet>());
        for (auto& client : parallel_clients_) {
            client = std::make_unique<CurlClient>(base_url_);
        }
    }

    ~BybitFastPath() {
        stop_bulk_workers();
    }

    bool self_test() {
        const std::string signature = sign("1700000000000testkey5000{}");
        if (signature.size() != 64) {
            std::cerr << "self-test failed: bad signature length\n";
            return false;
        }
        const auto response = make_success("VVVUSDT", "order-1", 0, "ok");
        const auto bulk = make_bulk_response({response});
        wait_bulk_workers_ready_once();
        return response.rfind("BUY\t1\t1\tVVVUSDT\torder-1\t0\tok\t", 0) == 0 &&
               bulk.rfind("BULK\t1\nBUY\t1\t1\tVVVUSDT\torder-1\t0\tok\t", 0) == 0 &&
               bulk_worker_pool_ready_.load(std::memory_order_acquire);
    }

    bool warmup() {
        const bool ok = refresh_symbols();
        warm_request_scratch();
        (void)warm_order_client_once();
        wait_bulk_workers_ready_once();
        warm_parallel_clients_once();
        return ok;
    }

    int run_server() {
        std::string frame;
        while (read_frame(std::cin, frame)) {
            write_frame(std::cout, handle_command(frame));
        }
        return 0;
    }

private:
    std::string handle_command(const std::string& line) {
        const auto parts = split_tab_views(line);
        if (parts.empty()) {
            return make_error("invalid_command");
        }
        if (parts.overflow) {
            return make_error("too_many_command_fields");
        }
        if (parts[0] == "PING") {
            return "PONG";
        }
        if (parts[0] == "REFRESH") {
            const bool ok = warmup();
            return std::string("REFRESH\t") + (ok ? "1" : "0") +
                   "\t" + std::to_string(spot_symbol_count());
        }
        if (parts[0] == "KEEPWARM") {
            (void)prime_order_client_for_hot_path(client_);
            wait_bulk_workers_ready_once();
            warm_parallel_clients_once();
            schedule_refresh();
            return std::string("KEEPWARM\t1\t") + std::to_string(spot_symbol_count());
        }
        if (parts[0] == "BUY") {
            if (parts.size() != 4) {
                return make_error("buy_command_requires_3_args");
            }
            return place_market_buy_quote(
                client_,
                parts[1],
                parts[2],
                parts[3]
            );
        }
        if (parts[0] == "BUYBULK") {
            if (parts.size() < 4 || ((parts.size() - 2) % 2) != 0) {
                return make_error("buybulk_requires_quote_and_symbol_link_pairs");
            }
            return place_market_buy_quote_bulk(parts);
        }
        if (parts[0] == "HAS") {
            if (parts.size() != 2) {
                return make_error("has_command_requires_symbol");
            }
            if (!has_spot_symbol(parts[1])) {
                schedule_refresh();
                return "HAS\t0";
            }
            return std::string("HAS\t") +
                   "1";
        }
        return make_error("unknown_command");
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
            const auto next_cursor = extract_json_string(response.body, "nextPageCursor");
            cursor = next_cursor.value_or("");
        } while (!cursor.empty());

        if (!next_symbols.empty()) {
            std::shared_ptr<const SpotSymbolSet> snapshot =
                std::make_shared<SpotSymbolSet>(std::move(next_symbols));
            publish_spot_symbols(std::move(snapshot));
        }
        return spot_symbol_count() > 0;
    }

    bool warm_order_client_once() {
        bool expected = false;
        if (!order_client_warmed_.compare_exchange_strong(expected, true)) {
            return true;
        }
        const auto response = client_.get("/v5/market/instruments-info?category=spot&limit=1");
        if (!response.success || !prime_order_client_for_hot_path(client_)) {
            order_client_warmed_.store(false);
            return false;
        }
        return true;
    }

    bool has_spot_symbol(std::string_view symbol) {
        const auto* symbols = spot_symbols_raw_.load(std::memory_order_acquire);
        return symbols != nullptr && symbols->find(symbol) != symbols->end();
    }

    std::size_t spot_symbol_count() {
        const auto* symbols = spot_symbols_raw_.load(std::memory_order_acquire);
        return symbols == nullptr ? 0 : symbols->size();
    }

    void publish_spot_symbols(std::shared_ptr<const SpotSymbolSet> snapshot) {
        const auto* raw = snapshot.get();
        {
            std::lock_guard<std::mutex> lock(spot_symbol_snapshots_mu_);
            spot_symbol_snapshots_.push_back(std::move(snapshot));
        }
        spot_symbols_raw_.store(raw, std::memory_order_release);
    }

    void schedule_refresh() {
        bool expected = false;
        if (!refresh_in_flight_.compare_exchange_strong(expected, true)) {
            return;
        }
        std::thread([this]() {
            refresh_symbols();
            refresh_in_flight_.store(false);
        }).detach();
    }

    bool warm_parallel_clients_once() {
        bool expected = false;
        if (!parallel_clients_warmed_.compare_exchange_strong(expected, true)) {
            return true;
        }
        std::array<std::thread, MAX_BULK_ORDERS> threads;
        std::array<bool, MAX_BULK_ORDERS> successes{};
        for (std::size_t i = 0; i < MAX_BULK_ORDERS; ++i) {
            threads[i] = std::thread([this, &successes, i]() {
                const auto response = parallel_clients_[i]->get(
                    "/v5/market/instruments-info?category=spot&limit=1"
                );
                successes[i] =
                    response.success && prime_order_client_for_hot_path(*parallel_clients_[i]);
            });
        }
        bool all_ready = true;
        for (auto& thread : threads) {
            if (thread.joinable()) {
                thread.join();
            }
        }
        for (const bool success : successes) {
            all_ready = all_ready && success;
        }
        if (!all_ready) {
            parallel_clients_warmed_.store(false);
            return false;
        }
        return true;
    }

    void start_bulk_workers_once() {
        bool expected = false;
        if (!bulk_workers_started_.compare_exchange_strong(expected, true)) {
            return;
        }
        bulk_worker_pool_ready_.store(false, std::memory_order_release);
        for (auto& slot : bulk_slots_) {
            slot.stop_requested.store(false, std::memory_order_release);
            slot.response.clear();
        }
        for (auto& ready : bulk_worker_ready_) {
            ready.store(false, std::memory_order_release);
        }
        for (std::size_t i = 0; i < MAX_BULK_ORDERS; ++i) {
            bulk_workers_[i] = std::thread([this, i]() {
                bulk_worker_loop(i);
            });
        }
    }

    void wait_bulk_workers_ready_once() {
        if (bulk_worker_pool_ready_.load(std::memory_order_acquire)) {
            return;
        }
        start_bulk_workers_once();
        for (auto& ready : bulk_worker_ready_) {
            bool observed = ready.load(std::memory_order_acquire);
            while (!observed) {
                ready.wait(observed, std::memory_order_acquire);
                observed = ready.load(std::memory_order_acquire);
            }
        }
        bulk_worker_pool_ready_.store(true, std::memory_order_release);
    }

    void stop_bulk_workers() {
        if (!bulk_workers_started_.exchange(false)) {
            return;
        }
        bulk_worker_pool_ready_.store(false, std::memory_order_release);
        for (auto& slot : bulk_slots_) {
            slot.stop_requested.store(true, std::memory_order_release);
            slot.work_seq.fetch_add(1, std::memory_order_acq_rel);
            slot.work_seq.notify_one();
        }
        for (auto& worker : bulk_workers_) {
            if (worker.joinable()) {
                worker.join();
            }
        }
    }

    void bulk_worker_loop(std::size_t index) {
        warm_request_scratch();
        (void)prime_order_client_for_hot_path(*parallel_clients_[index]);
        bulk_worker_ready_[index].store(true, std::memory_order_release);
        bulk_worker_ready_[index].notify_one();
        auto& slot = bulk_slots_[index];
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

            std::string response = place_market_buy_quote(
                *parallel_clients_[index],
                slot.symbol,
                slot.quote_amount,
                slot.order_link_id
            );

            slot.response = std::move(response);
            slot.done_seq.store(current_work_seq, std::memory_order_release);
            slot.done_seq.notify_one();
        }
    }

    std::string place_market_buy_quote(
        CurlClient& client,
        std::string_view symbol,
        std::string_view quote_amount,
        std::string_view order_link_id
    ) {
        if (api_key_.empty() || api_secret_.empty()) {
            return make_error("missing_api_config", symbol);
        }
        if (!order_on_cache_miss_ && !has_spot_symbol(symbol)) {
            schedule_refresh();
            return make_error("spot_symbol_unavailable", symbol, false);
        }

        thread_local std::string body;
        thread_local AuthHeaders headers;
        prepare_order_request_into(symbol, quote_amount, order_link_id, body, headers);
        const auto response = client.post_order_create(body, headers);
        const long long ret_code = extract_json_int(response.body, "retCode").value_or(-1);
        if (!response.success || ret_code != 0) {
            const std::string reason =
                extract_json_string(response.body, "retMsg").value_or(
                    response.error.empty() ? "order_create_failed" : response.error
                );
            return make_error(reason, symbol, true, static_cast<int>(ret_code));
        }

        const std::string order_id =
            extract_json_string(response.body, "orderId").value_or("");
        return make_success(symbol, order_id, static_cast<int>(ret_code), "cpp_fast_path");
    }

    std::string place_market_buy_quote_bulk(const FrameParts& parts) {
        const std::size_t count = std::min<std::size_t>((parts.size() - 2) / 2, MAX_BULK_ORDERS);
        std::array<std::string, MAX_BULK_ORDERS> responses;

        if (count == 1) {
            responses[0] = place_market_buy_quote(
                client_,
                parts[2],
                parts[1],
                parts[3]
            );
            return make_bulk_response(responses, 1);
        }

        wait_bulk_workers_ready_once();
        std::array<std::uint64_t, MAX_BULK_ORDERS> expected_done_seq{};
        const std::string_view quote_amount = parts[1];
        for (std::size_t i = 0; i < count; ++i) {
            const std::size_t symbol_index = 2 + (i * 2);
            auto& slot = bulk_slots_[i];
            slot.symbol = parts[symbol_index];
            slot.quote_amount = quote_amount;
            slot.order_link_id = parts[symbol_index + 1];
            slot.response.clear();
            const std::uint64_t next_work_seq =
                slot.work_seq.load(std::memory_order_relaxed) + 1;
            expected_done_seq[i] = next_work_seq;
            slot.work_seq.store(next_work_seq, std::memory_order_release);
            slot.work_seq.notify_one();
        }
        for (std::size_t i = 0; i < count; ++i) {
            auto& slot = bulk_slots_[i];
            std::uint64_t observed = slot.done_seq.load(std::memory_order_acquire);
            while (observed != expected_done_seq[i]) {
                slot.done_seq.wait(observed, std::memory_order_acquire);
                observed = slot.done_seq.load(std::memory_order_acquire);
            }
            responses[i] = slot.response;
            slot.response.clear();
            if (responses[i].empty()) {
                responses[i] = make_error(
                    "bulk_worker_empty_response",
                    to_string(parts[2 + (i * 2)])
                );
            }
        }
        return make_bulk_response(responses, count);
    }

    void prepare_order_request_into(
        std::string_view symbol,
        std::string_view quote_amount,
        std::string_view order_link_id,
        std::string& body,
        AuthHeaders& headers
    ) {
        body.clear();
        body.reserve(128 + symbol.size() + quote_amount.size() + order_link_id.size());
        body += "{\"category\":\"spot\",\"symbol\":\"";
        body.append(symbol);
        body += "\",\"side\":\"Buy\",\"orderType\":\"Market\",\"qty\":\"";
        body.append(quote_amount);
        body += "\",\"orderFilter\":\"Order\",\"marketUnit\":\"quoteCoin\",\"orderLinkId\":\"";
        body.append(order_link_id);
        body += "\"}";
        auth_headers_into(body, headers);
    }

    void warm_request_scratch() {
        thread_local std::string body;
        thread_local AuthHeaders headers;
        prepare_order_request_into(
            "XXXXXXXXXXUSDT",
            "999999999999",
            "ls-b-9223372036854775807-XXXXXXXXXX",
            body,
            headers
        );
    }

    bool prime_order_client_for_hot_path(CurlClient& client) {
        thread_local std::string body;
        thread_local AuthHeaders headers;
        prepare_order_request_into(
            "XXXXXXXXXXUSDT",
            "999999999999",
            "ls-b-9223372036854775807-XXXXXXXXXX",
            body,
            headers
        );
        return client.prepare_post_order_for_benchmark(body, headers);
    }

    void auth_headers_into(const std::string& body, AuthHeaders& headers) {
        std::array<char, 32> timestamp_buffer{};
        const std::string_view timestamp = current_timestamp_ms(timestamp_buffer, timestamp_bias_ms_);
        thread_local std::string plain;
        plain.clear();
        plain.reserve(timestamp.size() + auth_plain_static_.size() + body.size());
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

    std::string sign(std::string_view payload) const {
        std::string hex;
        append_signature_hex(payload, hex);
        return hex;
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

    static std::string make_error(
        std::string_view reason,
        std::string_view symbol = "",
        bool attempted = false,
        int ret_code = -1
    ) {
        std::string out = "BUY\t0\t";
        out += attempted ? "1\t" : "0\t";
        out.append(symbol);
        out += "\t\t";
        out += std::to_string(ret_code);
        out += "\tcpp_fast_path\t";
        out += json_escape(reason);
        return out;
    }

    static std::string make_success(
        std::string_view symbol,
        std::string_view order_id,
        int ret_code,
        std::string_view transport
    ) {
        std::string out = "BUY\t1\t1\t";
        out.append(symbol);
        out += "\t";
        out.append(order_id);
        out += "\t";
        out += std::to_string(ret_code);
        out += "\t";
        out.append(transport);
        out += "\t";
        return out;
    }

    static std::string make_bulk_response(
        const std::array<std::string, MAX_BULK_ORDERS>& responses,
        std::size_t count
    ) {
        std::string out = "BULK\t";
        out += std::to_string(count);
        for (std::size_t i = 0; i < count; ++i) {
            out += "\n";
            out += responses[i];
        }
        return out;
    }

    static std::string make_bulk_response(const std::vector<std::string>& responses) {
        std::string out = "BULK\t";
        out += std::to_string(responses.size());
        for (const auto& response : responses) {
            out += "\n";
            out += response;
        }
        return out;
    }

    std::string api_key_;
    std::string api_secret_;
    std::string base_url_;
    std::string recv_window_;
    long long timestamp_bias_ms_{-50};
    std::string content_type_header_;
    std::string api_key_header_;
    std::string recv_window_header_;
    std::string auth_plain_static_;
    HmacPad hmac_ipad_{};
    HmacPad hmac_opad_{};
    bool order_on_cache_miss_{false};
    CurlClient client_;
    CurlClient market_client_;
    std::array<std::unique_ptr<CurlClient>, MAX_BULK_ORDERS> parallel_clients_;
    std::array<BulkWorkerSlot, MAX_BULK_ORDERS> bulk_slots_;
    std::array<std::thread, MAX_BULK_ORDERS> bulk_workers_;
    std::array<std::atomic<bool>, MAX_BULK_ORDERS> bulk_worker_ready_{};
    std::atomic<bool> refresh_in_flight_{false};
    std::atomic<bool> order_client_warmed_{false};
    std::atomic<bool> parallel_clients_warmed_{false};
    std::atomic<bool> bulk_workers_started_{false};
    std::atomic<bool> bulk_worker_pool_ready_{false};
    std::mutex refresh_mu_;
    std::mutex spot_symbol_snapshots_mu_;
    std::atomic<const SpotSymbolSet*> spot_symbols_raw_{nullptr};
    std::vector<std::shared_ptr<const SpotSymbolSet>> spot_symbol_snapshots_;
};

}  // namespace

int main(int argc, char** argv) {
    BybitFastPath fast_path;

    if (argc > 1 && std::string(argv[1]) == "--self-test") {
        const bool ok = fast_path.self_test();
        std::cout << (ok ? "SELFTEST_OK" : "SELFTEST_FAIL") << '\n';
        return ok ? 0 : 1;
    }

    if (argc > 1 && std::string(argv[1]) == "--server") {
        return fast_path.run_server();
    }

    std::cerr << "usage: bybit_fast_path --server | --self-test\n";
    return 1;
}
