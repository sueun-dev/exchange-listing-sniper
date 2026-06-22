#include <atomic>
#include <algorithm>
#include <array>
#include <cctype>
#include <charconv>
#include <chrono>
#include <condition_variable>
#include <curl/curl.h>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <memory>
#include <mutex>
#include <optional>
#ifndef OPENSSL_API_COMPAT
#define OPENSSL_API_COMPAT 0x10100000L
#endif
#include <openssl/hmac.h>
#include <openssl/sha.h>
#if defined(__APPLE__)
#include <pthread/qos.h>
#endif
#include <sstream>
#include <string>
#include <string_view>
#include <thread>
#include <unordered_set>
#include <utility>
#include <vector>

#include "td/telegram/td_json_client.h"

namespace {
long long monotonic_now_ns() {
  const auto now = std::chrono::steady_clock::now().time_since_epoch();
  return std::chrono::duration_cast<std::chrono::nanoseconds>(now).count();
}

long long wall_now_sec() {
  const auto now = std::chrono::system_clock::now().time_since_epoch();
  return std::chrono::duration_cast<std::chrono::seconds>(now).count();
}

inline void cpu_relax() {
#if defined(__x86_64__) || defined(__i386__)
  __builtin_ia32_pause();
#elif defined(__aarch64__) || defined(__arm__)
  __asm__ __volatile__("yield" ::: "memory");
#else
  std::atomic_signal_fence(std::memory_order_acq_rel);
#endif
}

constexpr uint32_t MARKET_FLAG_KRW = 1;
constexpr uint32_t MARKET_FLAG_BTC = 2;
constexpr uint32_t MARKET_FLAG_USDT = 4;
constexpr uint32_t MARKET_FLAG_ETH = 8;
constexpr size_t MAX_LISTING_TICKERS = 16;
constexpr size_t MAX_WATCH_CHATS = 8;
constexpr size_t MAX_HOT_ORDER_CLIENT_SNAPSHOTS = 32;
constexpr long CURL_REQUEST_TIMEOUT_MS = 10000L;
constexpr long CURL_CONNECT_TIMEOUT_MS = 1000L;
constexpr size_t HMAC_SHA256_BLOCK_SIZE = 64;
constexpr size_t AUTH_HEADER_COUNT = 5;
constexpr size_t SIGN_HEADER_CAPACITY =
    (sizeof("X-BAPI-SIGN: ") - 1) + SHA256_DIGEST_LENGTH * 2 + 1;
constexpr size_t TIMESTAMP_HEADER_CAPACITY =
    (sizeof("X-BAPI-TIMESTAMP: ") - 1) + 32 + 1;
constexpr size_t ORDER_REQUEST_BODY_CAPACITY = 512;
constexpr size_t ORDER_RESPONSE_BODY_CAPACITY = 4096;
static_assert(
    MAX_HOT_ORDER_CLIENT_SNAPSHOTS > static_cast<size_t>(CURL_REQUEST_TIMEOUT_MS / 1000L),
    "hot order client snapshots must outlive the worst single curl request");

constexpr std::string_view MARKET_CODES[] = {"KRW", "BTC", "USDT", "ETH"};
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
constexpr std::string_view BITHUMB_EXCLUDE_KEYWORDS[] = {
    "입출금",
    "유의촉구",
    "거래유의",
    "시세알림",
    "종료",
};
constexpr std::string_view BITHUMB_LISTING_PREFIXES[] = {
    "[마켓 추가]",
    "[마켓 추가/수수료 이벤트]",
};

struct ListingTickers {
  std::array<std::string_view, MAX_LISTING_TICKERS> values{};
  size_t count{0};

  bool empty() const {
    return count == 0;
  }

  void clear() {
    count = 0;
  }

  std::string_view front() const {
    return values[0];
  }

  bool contains(std::string_view ticker) const {
    for (size_t i = 0; i < count; ++i) {
      if (values[i] == ticker) {
        return true;
      }
    }
    return false;
  }

  void push_unique(std::string_view ticker) {
    if (count >= values.size() || contains(ticker)) {
      return;
    }
    values[count++] = ticker;
  }
};

struct ListingMatch {
  std::string_view exchange;
  std::string_view order_link_exchange;
  std::string_view signal_type;
  std::string_view ticker;
  ListingTickers tickers;
};

struct HttpResult {
  long status_code{0};
  std::string body;
  std::string error;
  bool success{false};
};

struct OrderHttpResult {
  long status_code{0};
  char body[ORDER_RESPONSE_BODY_CAPACITY];
  size_t body_size{0};
  bool body_truncated{false};
  long long perform_started_monotonic_ns{0};
  std::string error;
  bool success{false};

  std::string_view body_view() const {
    return std::string_view(body, body_size);
  }
};

using HmacPad = std::array<unsigned char, HMAC_SHA256_BLOCK_SIZE>;

struct AuthHeaders {
  const std::string* content_type_header{nullptr};
  const std::string* api_key_header{nullptr};
  const std::string* recv_window_header{nullptr};
  std::array<char, SIGN_HEADER_CAPACITY> sign_header{};
  std::array<char, TIMESTAMP_HEADER_CAPACITY> timestamp_header{};
  size_t sign_header_size{0};
  size_t timestamp_header_size{0};

  const char* c_str(size_t index) const {
    switch (index) {
      case 0:
        return content_type_header == nullptr ? "" : content_type_header->c_str();
      case 1:
        return api_key_header == nullptr ? "" : api_key_header->c_str();
      case 2:
        return sign_header.data();
      case 3:
        return timestamp_header.data();
      case 4:
        return recv_window_header == nullptr ? "" : recv_window_header->c_str();
      default:
        return "";
    }
  }

  void reset_sign_header() {
    sign_header_size = 0;
    sign_header[0] = '\0';
  }

  void reset_timestamp_header() {
    timestamp_header_size = 0;
    timestamp_header[0] = '\0';
  }

  bool append_sign_header(std::string_view value) {
    return append_header_buffer(sign_header, sign_header_size, value);
  }

  bool append_timestamp_header(std::string_view value) {
    return append_header_buffer(timestamp_header, timestamp_header_size, value);
  }

  bool append_sign_hex_digest(const unsigned char* digest, size_t digest_size) {
    static constexpr char kHex[] = "0123456789abcdef";
    const size_t hex_size = digest_size * 2;
    if (hex_size > sign_header.size() - sign_header_size - 1) {
      reset_sign_header();
      return false;
    }
    for (size_t i = 0; i < digest_size; ++i) {
      sign_header[sign_header_size + i * 2] = kHex[(digest[i] >> 4) & 0x0F];
      sign_header[sign_header_size + i * 2 + 1] = kHex[digest[i] & 0x0F];
    }
    sign_header_size += hex_size;
    sign_header[sign_header_size] = '\0';
    return true;
  }

  bool set_sign_header(std::string_view value) {
    reset_sign_header();
    return append_sign_header(value);
  }

  bool set_timestamp_header(std::string_view value) {
    reset_timestamp_header();
    return append_timestamp_header(value);
  }

private:
  template <size_t Capacity>
  static bool append_header_buffer(
      std::array<char, Capacity>& buffer,
      size_t& size,
      std::string_view value) {
    if (value.size() > buffer.size() - size - 1) {
      size = 0;
      buffer[0] = '\0';
      return false;
    }
    if (!value.empty()) {
      std::memcpy(buffer.data() + size, value.data(), value.size());
      size += value.size();
    }
    buffer[size] = '\0';
    return true;
  }
};

void init_hmac_sha256_pads(std::string_view secret, HmacPad& ipad, HmacPad& opad) {
  std::array<unsigned char, SHA256_DIGEST_LENGTH> hashed_key{};
  const unsigned char* key = reinterpret_cast<const unsigned char*>(secret.data());
  size_t key_len = secret.size();
  if (key_len > HMAC_SHA256_BLOCK_SIZE) {
    SHA256(key, key_len, hashed_key.data());
    key = hashed_key.data();
    key_len = hashed_key.size();
  }
  ipad.fill(0x36);
  opad.fill(0x5c);
  for (size_t i = 0; i < key_len; ++i) {
    ipad[i] ^= key[i];
    opad[i] ^= key[i];
  }
}

struct NativeTradeResult {
  bool enabled{false};
  bool attempted{false};
  bool executed{false};
  int ret_code{-1};
  long long trade_started_monotonic_ns{0};
  long long order_send_started_monotonic_ns{0};
  long long trade_finished_monotonic_ns{0};
  std::string symbol;
  std::string order_id;
  std::string order_link_id;
  std::string_view transport{"tdlib_native_rest"};
  std::string reason;
};

struct NativeOrderStartSignal {
  std::atomic<uint64_t>* started_seq{nullptr};
  std::atomic<long long>* started_monotonic_ns{nullptr};
  uint64_t expected_seq{0};

  void mark(long long monotonic_ns) const {
    if (started_monotonic_ns != nullptr) {
      started_monotonic_ns->store(monotonic_ns, std::memory_order_release);
    }
    if (started_seq != nullptr) {
      started_seq->store(expected_seq, std::memory_order_release);
      started_seq->notify_one();
    }
  }
};

struct NativeDispatchResult {
  bool dispatched{false};
  bool no_worker{false};
  size_t worker_index{0};
  uint64_t work_seq{0};
  std::string_view reason;
};

void write_json_string(std::ostream& out, std::string_view input);

void write_native_dispatch_json(
    std::ostream& out,
    const NativeDispatchResult& dispatch,
    std::string_view ticker) {
  out << "{\"ticker\":";
  write_json_string(out, ticker);
  out << ",\"dispatched\":" << (dispatch.dispatched ? "true" : "false")
      << ",\"no_worker\":" << (dispatch.no_worker ? "true" : "false")
      << ",\"worker_index\":" << dispatch.worker_index
      << ",\"work_seq\":" << dispatch.work_seq
      << ",\"reason\":";
  write_json_string(out, dispatch.reason);
  out << "}";
}

struct PreparedOrderRequest {
  std::array<char, ORDER_REQUEST_BODY_CAPACITY> body{};
  size_t body_size{0};
  AuthHeaders headers;

  void clear_body() {
    body_size = 0;
  }

  bool append_body(std::string_view value) {
    if (value.size() > body.size() - body_size) {
      body_size = 0;
      return false;
    }
    if (!value.empty()) {
      std::memcpy(body.data() + body_size, value.data(), value.size());
      body_size += value.size();
    }
    return true;
  }

  std::string_view body_view() const {
    return std::string_view(body.data(), body_size);
  }
};

struct TransparentStringHash {
  using is_transparent = void;

  size_t operator()(std::string_view value) const noexcept {
    return std::hash<std::string_view>{}(value);
  }

  size_t operator()(const std::string& value) const noexcept {
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

enum class ExchangeId : std::uint8_t {
  Unknown = 0,
  Upbit,
  Bithumb,
};

ExchangeId exchange_id_from_handle(std::string_view handle);
std::string_view exchange_name(ExchangeId exchange);

struct WatchChat {
  long long chat_id{0};
  std::string handle;
  ExchangeId exchange_id{ExchangeId::Unknown};
  std::string_view exchange;
};

struct WatchChatSet {
  std::array<WatchChat, MAX_WATCH_CHATS> entries{};
  size_t count{0};

  void upsert(long long chat_id, std::string handle) {
    const ExchangeId exchange_id = exchange_id_from_handle(handle);
    const std::string_view exchange = exchange_name(exchange_id);
    for (size_t i = 0; i < count; ++i) {
      if (entries[i].chat_id == chat_id) {
        entries[i].handle = std::move(handle);
        entries[i].exchange_id = exchange_id;
        entries[i].exchange = exchange;
        return;
      }
    }
    if (count >= entries.size()) {
      return;
    }
    entries[count].chat_id = chat_id;
    entries[count].handle = std::move(handle);
    entries[count].exchange_id = exchange_id;
    entries[count].exchange = exchange;
    ++count;
  }

  const WatchChat* find(long long chat_id) const {
    for (size_t i = 0; i < count; ++i) {
      if (entries[i].chat_id == chat_id) {
        return &entries[i];
      }
    }
    return nullptr;
  }
};

struct ParsedJsonInt {
  long long value{0};
  size_t end_pos{0};
};

struct TdlibMessageHeader {
  long long message_id{0};
  long long chat_id{0};
  size_t after_chat_pos{std::string_view::npos};
  size_t content_pos{std::string_view::npos};
  long long date_unix{0};
};

class WatchChatRegistry {
public:
  WatchChatRegistry() {
    publish(std::make_shared<WatchChatSet>());
  }

  void publish(std::shared_ptr<const WatchChatSet> snapshot) {
    const auto* raw = snapshot.get();
    {
      std::lock_guard<std::mutex> lock(mu_);
      snapshots_.push_back(std::move(snapshot));
    }
    raw_.store(raw, std::memory_order_release);
  }

  const WatchChatSet* load() const {
    return raw_.load(std::memory_order_acquire);
  }

private:
  std::atomic<const WatchChatSet*> raw_{nullptr};
  std::mutex mu_;
  std::vector<std::shared_ptr<const WatchChatSet>> snapshots_;
};

class NativeMessageDeduper {
public:
  NativeMessageDeduper() = default;

  bool claim(ExchangeId exchange_id, long long message_id) {
    const uint64_t key = make_key(exchange_id, message_id);
    if (last_key_.load(std::memory_order_relaxed) == key) {
      return false;
    }
    const size_t index = slot_index(key);
    if (slots_[index].load(std::memory_order_relaxed) == key) {
      last_key_.store(key, std::memory_order_relaxed);
      return false;
    }
    slots_[index].store(key, std::memory_order_relaxed);
    last_key_.store(key, std::memory_order_relaxed);
    return true;
  }

private:
  // Direct-mapped message-id dedup cache. Enlarged from 256 so a re-delivered
  // (edited/duplicate) Telegram update is far less likely to be evicted and
  // re-bought; the deterministic orderLinkId remains the cross-process backstop.
  // 8192 * 8 bytes = 64KB, negligible. See [24].
  static constexpr size_t SLOT_COUNT = 8192;
  static_assert((SLOT_COUNT & (SLOT_COUNT - 1)) == 0);

  static uint64_t make_key(ExchangeId exchange_id, long long message_id) {
    const auto exchange_key = static_cast<uint64_t>(
        static_cast<std::uint8_t>(exchange_id));
    return (exchange_key << 56) ^ static_cast<uint64_t>(message_id);
  }

  static size_t slot_index(uint64_t key) {
    constexpr uint64_t kMul = 11400714819323198485ull;
    return static_cast<size_t>((key * kMul) & (SLOT_COUNT - 1));
  }

  std::atomic<uint64_t> last_key_{0};
  std::array<std::atomic<uint64_t>, SLOT_COUNT> slots_{};
};

class NullBuffer : public std::streambuf {
public:
  int overflow(int ch) override {
    return ch;
  }
};

struct NativeBuyWorkerSlot {
  std::atomic<uint64_t> work_seq{0};
  std::atomic<uint64_t> done_seq{0};
  std::atomic<uint64_t> order_send_started_seq{0};
  std::atomic<long long> order_send_started_ns{0};
  std::atomic<bool> claimed{false};
  std::atomic<bool> fire_and_forget{false};
  std::atomic<bool> stop_requested{false};
  std::string_view exchange;
  long long message_id{0};
  std::string_view ticker;
  std::array<char, 16> ticker_storage{};
  size_t ticker_size{0};
  bool spot_symbol_prechecked{false};
  std::optional<NativeTradeResult> trade;

  bool set_ticker_copy(std::string_view value) {
    if (value.size() > ticker_storage.size()) {
      ticker = {};
      ticker_size = 0;
      return false;
    }
    if (!value.empty()) {
      std::memcpy(ticker_storage.data(), value.data(), value.size());
    }
    ticker_size = value.size();
    ticker = std::string_view(ticker_storage.data(), ticker_size);
    return true;
  }
};

size_t write_callback(void* contents, size_t size, size_t nmemb, void* userp) {
  const size_t total = size * nmemb;
  auto* output = static_cast<std::string*>(userp);
  output->append(static_cast<char*>(contents), total);
  return total;
}

size_t order_write_callback(void* contents, size_t size, size_t nmemb, void* userp) {
  const size_t total = size * nmemb;
  auto* output = static_cast<OrderHttpResult*>(userp);
  const size_t remaining = output->body_size >= ORDER_RESPONSE_BODY_CAPACITY
      ? 0
      : ORDER_RESPONSE_BODY_CAPACITY - output->body_size;
  const size_t to_copy = std::min(total, remaining);
  if (to_copy != 0) {
    std::memcpy(output->body + output->body_size, contents, to_copy);
    output->body_size += to_copy;
  }
  if (to_copy != total) {
    output->body_truncated = true;
  }
  return total;
}

struct curl_slist* stack_auth_header_list(
    const AuthHeaders& headers,
    std::array<curl_slist, AUTH_HEADER_COUNT>& nodes) {
  for (size_t i = 0; i < AUTH_HEADER_COUNT; ++i) {
    nodes[i].data = const_cast<char*>(headers.c_str(i));
    nodes[i].next = (i + 1 < AUTH_HEADER_COUNT) ? &nodes[i + 1] : nullptr;
  }
  return nodes.data();
}

std::string getenv_or(const char* key, const char* fallback = "") {
  const char* value = std::getenv(key);
  return value ? std::string(value) : std::string(fallback);
}

int getenv_int_or(const char* key, int fallback) {
  const char* value = std::getenv(key);
  if (value == nullptr || *value == '\0') {
    return fallback;
  }
  char* end = nullptr;
  const long parsed = std::strtol(value, &end, 10);
  if (end == value || parsed <= 0) {
    return fallback;
  }
  return static_cast<int>(parsed);
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

double getenv_nonnegative_double_or(const char* key, double fallback) {
  const char* value = std::getenv(key);
  if (value == nullptr || *value == '\0') {
    return fallback;
  }
  char* end = nullptr;
  const double parsed = std::strtod(value, &end);
  if (end == value || parsed < 0.0) {
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
      std::chrono::system_clock::now());
  const auto value = now.time_since_epoch().count() + bias_ms;
  auto [ptr, ec] = std::to_chars(buffer.data(), buffer.data() + buffer.size(), value);
  if (ec != std::errc()) {
    return {};
  }
  return std::string_view(buffer.data(), static_cast<size_t>(ptr - buffer.data()));
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

// Money-safety gate for the native order amount: a positive decimal that is also
// at or below BYBIT_SPOT_BUY_MAX_USDT_AMOUNT (default 1000). is_positive_decimal_text
// already rejects negatives / non-finite / non-numeric, so this adds the ceiling
// so a fat-finger amount refuses to fire a native order. Mirrors the Python buyer.
bool is_valid_quote_amount(std::string_view value) {
  if (!is_positive_decimal_text(value)) {
    return false;
  }
  double amount = 0.0;
  try {
    amount = std::stod(std::string(value));
  } catch (...) {
    return false;
  }
  double ceiling = 1000.0;
  const std::string ceiling_text = getenv_or("BYBIT_SPOT_BUY_MAX_USDT_AMOUNT", "1000");
  if (is_positive_decimal_text(ceiling_text)) {
    try {
      ceiling = std::stod(ceiling_text);
    } catch (...) {
      ceiling = 1000.0;
    }
  }
  return amount > 0.0 && amount <= ceiling;
}

std::string_view spot_symbol_view(std::string_view ticker, std::array<char, 32>& buffer) {
  constexpr std::string_view suffix = "USDT";
  const size_t length = ticker.size() + suffix.size();
  if (length > buffer.size()) {
    return {};
  }
  std::memcpy(buffer.data(), ticker.data(), ticker.size());
  std::memcpy(buffer.data() + ticker.size(), suffix.data(), suffix.size());
  return std::string_view(buffer.data(), length);
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

void boost_current_thread_for_hot_path() {
#if defined(__APPLE__)
  if (!getenv_truthy("LISTING_TDLIB_DISABLE_QOS_BOOST", false)) {
    (void)pthread_set_qos_class_self_np(QOS_CLASS_USER_INTERACTIVE, 0);
  }
#endif
}

void lower_current_thread_for_background() {
#if defined(__APPLE__)
  if (!getenv_truthy("LISTING_TDLIB_DISABLE_BACKGROUND_QOS_LOWER", false)) {
    (void)pthread_set_qos_class_self_np(QOS_CLASS_UTILITY, 0);
  }
#endif
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
        break;
    }
  }
  return escaped;
}

void write_json_escaped(std::ostream& out, std::string_view input) {
  for (const char ch : input) {
    switch (ch) {
      case '\\':
        out << "\\\\";
        break;
      case '"':
        out << "\\\"";
        break;
      case '\n':
        out << "\\n";
        break;
      case '\r':
        out << "\\r";
        break;
      case '\t':
        out << "\\t";
        break;
      default:
        out.put(ch);
        break;
    }
  }
}

void write_json_string(std::ostream& out, std::string_view input) {
  out.put('"');
  write_json_escaped(out, input);
  out.put('"');
}

bool parse_json_int_value_into(
    std::string_view body,
    size_t pos,
    ParsedJsonInt& out) {
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
    return false;
  }
  long long value = 0;
  const auto* begin = body.data() + pos;
  const auto* finish = body.data() + end;
  const auto parsed = std::from_chars(begin, finish, value);
  if (parsed.ec != std::errc()) {
    return false;
  }
  out = ParsedJsonInt{value, end};
  return true;
}

std::optional<ParsedJsonInt> parse_json_int_value(std::string_view body, size_t pos) {
  ParsedJsonInt parsed;
  if (!parse_json_int_value_into(body, pos, parsed)) {
    return std::nullopt;
  }
  return parsed;
}

std::optional<long long> extract_json_int_pattern(std::string_view body, std::string_view pattern, size_t start_pos = 0) {
  size_t pos = body.find(pattern, start_pos);
  if (pos == std::string::npos) {
    return std::nullopt;
  }
  pos += pattern.size();
  const auto parsed = parse_json_int_value(body, pos);
  if (!parsed.has_value()) {
    return std::nullopt;
  }
  return parsed->value;
}

bool extract_tdlib_message_header_into(
    std::string_view body,
    TdlibMessageHeader& out) {
  constexpr std::string_view compact_prefix =
      "{\"@type\":\"updateNewMessage\",\"message\":{";
  constexpr std::string_view compact_message_id_prefix =
      "{\"@type\":\"updateNewMessage\",\"message\":{\"@type\":\"message\",\"id\":";
  constexpr std::string_view message_pattern = "\"message\":{";
  constexpr std::string_view id_pattern = "\"id\":";
  constexpr std::string_view chat_pattern = "\"chat_id\":";
  constexpr std::string_view compact_chat_prefix = ",\"chat_id\":";
  constexpr std::string_view date_prefix = ",\"date\":";
  constexpr std::string_view content_prefix = ",\"content\":";
  size_t message_body_pos = compact_prefix.size();
  size_t id_pos = std::string_view::npos;
  size_t id_value_pos = std::string_view::npos;
  if (body.rfind(compact_message_id_prefix, 0) == 0) {
    id_value_pos = compact_message_id_prefix.size();
  } else if (body.rfind(compact_prefix, 0) == 0) {
    id_pos = body.find(id_pattern, message_body_pos);
    id_value_pos = id_pos == std::string_view::npos
        ? std::string_view::npos
        : id_pos + id_pattern.size();
  } else {
    const size_t message_pos = body.find(message_pattern);
    if (message_pos == std::string_view::npos) {
      return false;
    }
    message_body_pos = message_pos + message_pattern.size();
    id_pos = body.find(id_pattern, message_body_pos);
    id_value_pos = id_pos == std::string_view::npos
        ? std::string_view::npos
        : id_pos + id_pattern.size();
  }
  if (id_value_pos == std::string_view::npos) {
    return false;
  }
  ParsedJsonInt message_id;
  if (!parse_json_int_value_into(body, id_value_pos, message_id)) {
    return false;
  }
  size_t chat_value_pos = std::string_view::npos;
  if (message_id.end_pos + compact_chat_prefix.size() <= body.size() &&
      body.substr(message_id.end_pos, compact_chat_prefix.size()) == compact_chat_prefix) {
    chat_value_pos = message_id.end_pos + compact_chat_prefix.size();
  } else {
    const size_t chat_pos = body.find(chat_pattern, message_id.end_pos);
    if (chat_pos == std::string_view::npos) {
      return false;
    }
    chat_value_pos = chat_pos + chat_pattern.size();
  }
  ParsedJsonInt chat_id;
  if (!parse_json_int_value_into(body, chat_value_pos, chat_id)) {
    return false;
  }
  size_t content_pos = std::string_view::npos;
  size_t compact_pos = chat_id.end_pos;
  long long date_unix = 0;
  if (compact_pos + date_prefix.size() < body.size() &&
      body.substr(compact_pos, date_prefix.size()) == date_prefix) {
    ParsedJsonInt date_value;
    if (parse_json_int_value_into(body, compact_pos + date_prefix.size(), date_value)) {
      date_unix = date_value.value;
      compact_pos = date_value.end_pos;
      if (compact_pos + content_prefix.size() <= body.size() &&
          body.substr(compact_pos, content_prefix.size()) == content_prefix) {
        content_pos = compact_pos + 1;
      }
    }
  }
  out = TdlibMessageHeader{message_id.value, chat_id.value, chat_id.end_pos, content_pos, date_unix};
  return true;
}

std::optional<std::string> extract_json_string_value(std::string_view body, size_t value_pos) {
  while (value_pos < body.size() &&
         std::isspace(static_cast<unsigned char>(body[value_pos]))) {
    ++value_pos;
  }
  if (value_pos >= body.size() || body[value_pos] != '"') {
    return std::nullopt;
  }
  size_t pos = value_pos + 1;
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

std::optional<std::string_view> extract_json_string_view_value(std::string_view body, size_t value_pos) {
  while (value_pos < body.size() &&
         std::isspace(static_cast<unsigned char>(body[value_pos]))) {
    ++value_pos;
  }
  if (value_pos >= body.size() || body[value_pos] != '"') {
    return std::nullopt;
  }
  const size_t start = value_pos + 1;
  for (size_t pos = start; pos < body.size(); ++pos) {
    const char ch = body[pos];
    if (ch == '\\') {
      return std::nullopt;
    }
    if (ch == '"') {
      return body.substr(start, pos - start);
    }
  }
  return std::nullopt;
}

std::optional<std::string_view> extract_json_string_first_line_view_value(std::string_view body, size_t value_pos) {
  while (value_pos < body.size() &&
         std::isspace(static_cast<unsigned char>(body[value_pos]))) {
    ++value_pos;
  }
  if (value_pos >= body.size() || body[value_pos] != '"') {
    return std::nullopt;
  }
  const size_t start = value_pos + 1;
  for (size_t pos = start; pos < body.size(); ++pos) {
    const char ch = body[pos];
    if (ch == '"' || ch == '\n' || ch == '\r') {
      return body.substr(start, pos - start);
    }
    if (ch != '\\') {
      continue;
    }
    if (pos + 1 >= body.size()) {
      return std::nullopt;
    }
    const char escaped = body[pos + 1];
    if (escaped == 'n' || escaped == 'r') {
      return body.substr(start, pos - start);
    }
    return std::nullopt;
  }
  return std::nullopt;
}

std::string_view first_line_view(std::string_view value) {
  const size_t end = value.find_first_of("\r\n");
  if (end == std::string_view::npos) {
    return value;
  }
  return value.substr(0, end);
}

std::optional<std::string> extract_json_string(std::string_view body, std::string_view key, size_t start_pos = 0) {
  std::string pattern;
  pattern.reserve(key.size() + 3);
  pattern += "\"";
  pattern += key;
  pattern += "\":";
  size_t pos = body.find(pattern, start_pos);
  if (pos == std::string::npos) {
    return std::nullopt;
  }
  return extract_json_string_value(body, pos + pattern.size());
}

bool has_json_type(std::string_view body, std::string_view type_name) {
  if (type_name == "updateNewMessage") {
    constexpr std::string_view prefix = "{\"@type\":\"updateNewMessage\"";
    if (body.rfind(prefix, 0) == 0) {
      return true;
    }
    constexpr std::string_view top_level_type_prefix = "{\"@type\":\"";
    if (body.rfind(top_level_type_prefix, 0) == 0) {
      return false;
    }
    return body.find("\"@type\":\"updateNewMessage\"") != std::string_view::npos;
  }
  return false;
}

bool cstr_has_update_new_message_type(const char* body) {
  if (body == nullptr) {
    return false;
  }
  constexpr std::string_view prefix = "{\"@type\":\"updateNewMessage\"";
  if (std::strncmp(body, prefix.data(), prefix.size()) == 0) {
    return true;
  }
  constexpr std::string_view top_level_type_prefix = "{\"@type\":\"";
  if (std::strncmp(body, top_level_type_prefix.data(), top_level_type_prefix.size()) == 0) {
    return false;
  }
  return std::strstr(body, "\"@type\":\"updateNewMessage\"") != nullptr;
}

bool tdlib_content_is_message_text(std::string_view body, size_t content_pos) {
  constexpr std::string_view prefix = "\"content\":{\"@type\":\"messageText\"";
  if (content_pos <= body.size() &&
      body.substr(content_pos, prefix.size()) == prefix) {
    return true;
  }
  return body.find("\"@type\":\"messageText\"", content_pos) != std::string_view::npos;
}

std::optional<size_t> find_tdlib_message_text_value_pos(std::string_view body, size_t content_pos) {
  constexpr std::string_view pattern = "\"text\":";
  size_t pos = body.find(pattern, content_pos);
  if (pos == std::string::npos) {
    return std::nullopt;
  }
  pos += pattern.size();
  while (pos < body.size() && std::isspace(static_cast<unsigned char>(body[pos]))) {
    ++pos;
  }
  if (pos < body.size() && body[pos] == '"') {
    return pos;
  }
  if (pos >= body.size() || body[pos] != '{') {
    return std::nullopt;
  }
  const size_t nested = body.find(pattern, pos + 1);
  if (nested == std::string::npos) {
    return std::nullopt;
  }
  return nested + pattern.size();
}

std::optional<std::string_view> extract_tdlib_message_text_view(std::string_view body, size_t content_pos) {
  constexpr std::string_view formatted_text_prefix =
      "\"content\":{\"@type\":\"messageText\",\"text\":{\"@type\":\"formattedText\",\"text\":";
  if (content_pos <= body.size() &&
      body.substr(content_pos, formatted_text_prefix.size()) == formatted_text_prefix) {
    return extract_json_string_view_value(body, content_pos + formatted_text_prefix.size());
  }
  if (!tdlib_content_is_message_text(body, content_pos)) {
    return std::nullopt;
  }
  const auto value_pos = find_tdlib_message_text_value_pos(body, content_pos);
  if (!value_pos.has_value()) {
    return std::nullopt;
  }
  return extract_json_string_view_value(body, *value_pos);
}

std::optional<std::string_view> extract_tdlib_message_title_view(std::string_view body, size_t content_pos) {
  constexpr std::string_view formatted_text_prefix =
      "\"content\":{\"@type\":\"messageText\",\"text\":{\"@type\":\"formattedText\",\"text\":";
  if (content_pos <= body.size() &&
      body.substr(content_pos, formatted_text_prefix.size()) == formatted_text_prefix) {
    return extract_json_string_first_line_view_value(body, content_pos + formatted_text_prefix.size());
  }
  if (!tdlib_content_is_message_text(body, content_pos)) {
    return std::nullopt;
  }
  const auto value_pos = find_tdlib_message_text_value_pos(body, content_pos);
  if (!value_pos.has_value()) {
    return std::nullopt;
  }
  return extract_json_string_first_line_view_value(body, *value_pos);
}

std::optional<std::string> extract_tdlib_message_text(std::string_view body, size_t content_pos) {
  if (!tdlib_content_is_message_text(body, content_pos)) {
    return std::nullopt;
  }
  const auto value_pos = find_tdlib_message_text_value_pos(body, content_pos);
  if (!value_pos.has_value()) {
    return std::nullopt;
  }
  return extract_json_string_value(body, *value_pos);
}

std::vector<std::string> extract_symbols(std::string_view body) {
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
    symbols.emplace_back(body.substr(pos, end - pos));
    pos = end + 1;
  }
  return symbols;
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
  return (ch >= 'A' && ch <= 'Z') ||
         (ch >= 'a' && ch <= 'z') ||
         (ch >= '0' && ch <= '9') ||
         ch == '_';
}

bool is_ascii_upper_or_digit(char ch) {
  return (ch >= 'A' && ch <= 'Z') || (ch >= '0' && ch <= '9');
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

std::string_view trim_ascii_view(std::string_view value);
std::string normalize_asset_segment(std::string_view segment);

// A ticker token is 1-10 [A-Z0-9] chars with at least one letter (already
// trimmed by the caller). The letter requirement rejects all-digit tokens like
// a year (2024) so they are never read as a ticker — identical to the Python
// and standalone-classifier rule, keeping every path's verdict in lockstep.
bool is_ticker_token(std::string_view candidate) {
  if (candidate.empty() || candidate.size() > 10) {
    return false;
  }
  bool has_alpha = false;
  for (char ch : candidate) {
    if (std::isupper(static_cast<unsigned char>(ch))) {
      has_alpha = true;
    } else if (!std::isdigit(static_cast<unsigned char>(ch))) {
      return false;
    }
  }
  return has_alpha;
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
    if (is_ticker_token(candidate)) {
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

void extract_listing_tickers_into(std::string_view title, ListingTickers& tickers) {
  // Asset-name-aware extraction matching Python _collect_asset_ticker_pairs and
  // the ultra engine: only a parenthetical preceded by a non-empty asset-name
  // segment is a ticker, so a chained/standalone parenthetical with no name
  // (e.g. "월드(WLFI)(M)" -> [WLFI]) is not bought. front() stays the primary.
  tickers.clear();
  const size_t bracket = title.find(']');
  size_t name_start = bracket == std::string_view::npos ? 0 : bracket + 1;
  size_t search = name_start;
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
    if (!is_market_code(candidate) && is_ticker_token(candidate) &&
        !tickers.contains(candidate)) {
      const std::string asset_name =
          normalize_asset_segment(title.substr(name_start, open - name_start));
      if (!asset_name.empty()) {
        tickers.push_unique(candidate);
        name_start = end + 1;
      }
    }
    search = end + 1;
  }
}

ListingTickers extract_listing_tickers(std::string_view title) {
  ListingTickers tickers;
  extract_listing_tickers_into(title, tickers);
  return tickers;
}

std::string tickers_json(const ListingTickers& tickers) {
  std::string result = "[";
  for (size_t i = 0; i < tickers.count; ++i) {
    if (i != 0) {
      result += ",";
    }
    result += "\"";
    result += json_escape(tickers.values[i]);
    result += "\"";
  }
  result += "]";
  return result;
}

void write_tickers_json(std::ostream& out, const ListingTickers& tickers) {
  out.put('[');
  for (size_t i = 0; i < tickers.count; ++i) {
    if (i != 0) {
      out.put(',');
    }
    write_json_string(out, tickers.values[i]);
  }
  out.put(']');
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

std::string_view extract_asset_name_view(std::string_view title) {
  const size_t bracket = title.find(']');
  if (bracket == std::string_view::npos) {
    return trim_ascii_view(title);
  }
  const size_t open = title.find('(', bracket + 1);
  if (open == std::string_view::npos || open <= bracket + 1) {
    return trim_ascii_view(title);
  }
  return trim_ascii_view(title.substr(bracket + 1, open - bracket - 1));
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

// Ticker-aware asset name (matches Python and the cpp/ultra paths): the name is
// the segment preceding the chosen ticker's parenthetical, so a skipped leading
// parenthetical (e.g. a year) stays with the name. Keeps asset_name identical.
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
    if (!is_market_code(candidate) && is_ticker_token(candidate)) {
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
    search = close + 1;
  }
  return std::string(extract_asset_name_view(title));
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
  // A symbol-rename re-announcement is a genuine tradeable 원화 마켓 추가.
  if (trimmed.find("심볼명 변경") != std::string::npos ||
      trimmed.find("심볼 변경") != std::string::npos) {
    return true;
  }
  constexpr std::string_view suffix_end = " 안내";
  return trimmed.rfind("및 ", 0) == 0 &&
         trimmed.size() >= suffix_end.size() &&
         trimmed.compare(
             trimmed.size() - suffix_end.size(),
             suffix_end.size(),
             suffix_end) == 0;
}

bool has_bithumb_listing_prefix(std::string_view title) {
  for (auto prefix : BITHUMB_LISTING_PREFIXES) {
    if (title.rfind(prefix, 0) == 0) {
      return true;
    }
  }
  return false;
}

size_t first_ticker_paren_open(std::string_view title) {
  const size_t bracket = title.find(']');
  size_t search = bracket == std::string_view::npos ? 0 : bracket + 1;
  while (true) {
    const size_t open = title.find('(', search);
    if (open == std::string_view::npos) {
      return std::string_view::npos;
    }
    const size_t close = title.find(')', open + 1);
    if (close == std::string_view::npos) {
      return std::string_view::npos;
    }
    const auto candidate = trim_ascii_view(title.substr(open + 1, close - open - 1));
    if (!is_market_code(candidate) && is_ticker_token(candidate)) {
      return open;
    }
    search = close + 1;
  }
}

// Blank the asset-name span so exclude keywords inside the asset's own name do
// not drop a genuine listing; the prefix and tail are still scanned. See [11].
std::string exclude_scan_text(std::string_view title) {
  const size_t bracket = title.find(']');
  if (bracket == std::string_view::npos) {
    return std::string(title);
  }
  const size_t name_start = bracket + 1;
  const size_t open = first_ticker_paren_open(title);
  if (open == std::string_view::npos || open <= name_start) {
    return std::string(title);
  }
  std::string result(title.substr(0, name_start));
  result.append(open - name_start, ' ');
  result.append(title.substr(open));
  return result;
}

bool remainder_is_only_parentheticals(std::string_view text) {
  text = trim_ascii_view(text);
  while (!text.empty()) {
    if (text.front() != '(') {
      return false;
    }
    const size_t close = text.find(')');
    if (close == std::string_view::npos) {
      return false;
    }
    text = trim_ascii_view(text.substr(close + 1));
  }
  return true;
}

bool is_upbit_krw_listing(std::string_view title) {
  if (title.rfind("[거래]", 0) != 0) {
    return false;
  }
  constexpr std::string_view new_listing_anchor = "신규 거래지원 안내";
  const size_t new_listing_pos = title.find(new_listing_anchor);
  if (new_listing_pos != std::string_view::npos) {
    const size_t market_end = find_market_parenthetical_end(
        title,
        new_listing_pos);
    if (market_end == std::string_view::npos ||
        !has_ascii_word(title.substr(new_listing_pos, market_end - new_listing_pos), "KRW") ||
        !remainder_is_only_parentheticals(title.substr(market_end))) {
      return false;
    }
    return contains_none(exclude_scan_text(title), UPBIT_EXCLUDE_KEYWORDS,
                         std::size(UPBIT_EXCLUDE_KEYWORDS));
  }
  constexpr std::string_view krw_market_add_suffix = "KRW 마켓 디지털 자산 추가";
  const size_t marker_idx = title.rfind(krw_market_add_suffix);
  if (marker_idx == std::string_view::npos ||
      !remainder_is_only_parentheticals(
          title.substr(marker_idx + krw_market_add_suffix.size()))) {
    return false;
  }
  return contains_none(exclude_scan_text(title), UPBIT_EXCLUDE_KEYWORDS,
                       std::size(UPBIT_EXCLUDE_KEYWORDS));
}

bool is_bithumb_listing(std::string_view title) {
  if (!has_bithumb_listing_prefix(title)) {
    return false;
  }
  constexpr std::string_view marker = "원화 마켓 추가";
  const size_t marker_pos = title.find(marker);
  if (marker_pos == std::string_view::npos) {
    return false;
  }
  if (!is_allowed_bithumb_market_add_suffix(title.substr(marker_pos + marker.size()))) {
    return false;
  }
  return contains_none(exclude_scan_text(title), BITHUMB_EXCLUDE_KEYWORDS,
                       std::size(BITHUMB_EXCLUDE_KEYWORDS));
}

bool classify_listing_title_into(
    ExchangeId exchange,
    std::string_view title,
    ListingMatch& out) {
  bool matched = false;
  std::string_view signal_type;
  std::string_view exchange_text;
  std::string_view order_link_exchange;
  switch (exchange) {
    case ExchangeId::Upbit:
      matched = is_upbit_krw_listing(title);
      signal_type = "new_listing";
      exchange_text = "upbit";
      // Full name so the orderLinkId prefix (ls-upbit-) matches the Python
      // poller and the C++ ultra engine; single-letter codes desynced the
      // duplicate-orderLinkId dedupe across paths.
      order_link_exchange = "upbit";
      break;
    case ExchangeId::Bithumb:
      matched = is_bithumb_listing(title);
      signal_type = "market_add";
      exchange_text = "bithumb";
      order_link_exchange = "bithumb";
      break;
    case ExchangeId::Unknown:
      return false;
  }

  if (!matched) {
    return false;
  }
  out.exchange = exchange_text;
  out.order_link_exchange = order_link_exchange;
  out.signal_type = signal_type;
  extract_listing_tickers_into(title, out.tickers);
  if (out.tickers.empty()) {
    return false;
  }
  out.ticker = out.tickers.front();
  return true;
}

std::optional<ListingMatch> classify_listing_title(ExchangeId exchange, std::string_view title) {
  ListingMatch match;
  if (!classify_listing_title_into(exchange, title, match)) {
    return std::nullopt;
  }
  return match;
}

ExchangeId exchange_id_from_handle(std::string_view handle) {
  if (handle == "upbit_news") {
    return ExchangeId::Upbit;
  }
  if (handle == "BithumbExchange" || handle == "bithumbexchange") {
    return ExchangeId::Bithumb;
  }
  return ExchangeId::Unknown;
}

std::string_view exchange_name(ExchangeId exchange) {
  switch (exchange) {
    case ExchangeId::Upbit:
      return "upbit";
    case ExchangeId::Bithumb:
      return "bithumb";
    case ExchangeId::Unknown:
      return {};
  }
  return {};
}

std::string market_flags_json(uint32_t flags) {
  std::string result = "[";
  bool first = true;
  auto append = [&](const char* market) {
    if (!first) {
      result += ",";
    }
    first = false;
    result += "\"";
    result += market;
    result += "\"";
  };
  if (flags & MARKET_FLAG_KRW) {
    append("KRW");
  }
  if (flags & MARKET_FLAG_BTC) {
    append("BTC");
  }
  if (flags & MARKET_FLAG_USDT) {
    append("USDT");
  }
  if (flags & MARKET_FLAG_ETH) {
    append("ETH");
  }
  result += "]";
  return result;
}

void write_market_flags_json(std::ostream& out, uint32_t flags) {
  out.put('[');
  bool first = true;
  auto append = [&](std::string_view market) {
    if (!first) {
      out.put(',');
    }
    first = false;
    write_json_string(out, market);
  };
  if (flags & MARKET_FLAG_KRW) {
    append("KRW");
  }
  if (flags & MARKET_FLAG_BTC) {
    append("BTC");
  }
  if (flags & MARKET_FLAG_USDT) {
    append("USDT");
  }
  if (flags & MARKET_FLAG_ETH) {
    append("ETH");
  }
  out.put(']');
}

class CurlClient {
public:
  explicit CurlClient(std::string base_url)
      : base_url_(std::move(base_url)),
        order_create_url_(base_url_ + "/v5/order/create"),
        http_timeout_ms_(
            getenv_int_or("LISTING_BYBIT_HTTP_TIMEOUT_MS", CURL_REQUEST_TIMEOUT_MS)),
        order_response_timeout_ms_(getenv_int_or(
            "LISTING_BYBIT_ORDER_RESPONSE_TIMEOUT_MS",
            getenv_int_or("LISTING_BYBIT_HTTP_TIMEOUT_MS", CURL_REQUEST_TIMEOUT_MS))),
        connect_timeout_ms_(
            getenv_int_or("LISTING_BYBIT_CONNECT_TIMEOUT_MS", CURL_CONNECT_TIMEOUT_MS)),
        base_is_file_url_(base_url_.rfind("file://", 0) == 0) {
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
      const AuthHeaders& headers) {
    return perform(true, path, body, &headers);
  }

  OrderHttpResult post_order_create(
      std::string_view body,
      const AuthHeaders& headers,
      bool capture_timing = false,
      const NativeOrderStartSignal* start_signal = nullptr) {
    return perform_order_create(body, headers, capture_timing, start_signal);
  }

  bool prepare_post_order_for_benchmark(
      std::string_view body,
      const AuthHeaders& headers) {
    if (!prime_order_create_url()) {
      return false;
    }
    return prepare_post_for_benchmark(body, headers);
  }

  bool prime_order_create_url() {
    if (!ensure_order_create_url()) {
      return false;
    }
    set_order_write_callback();
    return true;
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
  enum class WriteCallbackMode : std::uint8_t {
    Unknown = 0,
    String,
    Order,
  };

  void configure_common_options() {
    if (curl_ == nullptr) {
      return;
    }
    set_string_write_callback();
    curl_easy_setopt(curl_, CURLOPT_TIMEOUT_MS, http_timeout_ms_);
    curl_easy_setopt(curl_, CURLOPT_CONNECTTIMEOUT_MS, connect_timeout_ms_);
    curl_easy_setopt(curl_, CURLOPT_DNS_CACHE_TIMEOUT, 3600L);
    curl_easy_setopt(curl_, CURLOPT_TCP_KEEPALIVE, 1L);
    curl_easy_setopt(curl_, CURLOPT_TCP_NODELAY, 1L);
#ifdef CURLOPT_TCP_FASTOPEN
    curl_easy_setopt(curl_, CURLOPT_TCP_FASTOPEN, 1L);
#endif
    curl_easy_setopt(curl_, CURLOPT_NOSIGNAL, 1L);
    curl_easy_setopt(curl_, CURLOPT_USERAGENT, "ChainPulse-TDLibNative/1.0");
  }

  HttpResult perform(
      bool is_post,
      std::string_view path,
      std::string_view body,
      const AuthHeaders* headers) {
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
      const AuthHeaders* headers) {
    HttpResult result;
    if (curl_ == nullptr) {
      result.error = "curl_init_failed";
      return result;
    }
    curl_easy_setopt(curl_, CURLOPT_URL, url.c_str());
    curl_easy_setopt(curl_, CURLOPT_TIMEOUT_MS, http_timeout_ms_);
    order_create_url_ready_ = false;
    set_string_write_callback();
    curl_easy_setopt(curl_, CURLOPT_WRITEDATA, &result.body);

    struct curl_slist* header_list = nullptr;
    if (headers != nullptr) {
      header_list = stack_auth_header_list(*headers, generic_header_nodes_);
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
      result.success = (result.status_code >= 200 && result.status_code < 300) ||
          (base_is_file_url_ && result.status_code == 0);
    }
    return result;
  }

  OrderHttpResult perform_order_create(
      std::string_view body,
      const AuthHeaders& headers,
      bool capture_timing,
      const NativeOrderStartSignal* start_signal) {
    OrderHttpResult result;
    if (curl_ == nullptr) {
      result.error = "curl_init_failed";
      return result;
    }
    if (!ensure_order_create_url()) {
      result.error = "curl_url_failed";
      return result;
    }
    curl_easy_setopt(curl_, CURLOPT_TIMEOUT_MS, order_response_timeout_ms_);
    set_order_write_callback();
    curl_easy_setopt(curl_, CURLOPT_WRITEDATA, &result);

    struct curl_slist* header_list = stack_auth_header_list(headers, order_header_nodes_);
    if (!order_header_list_applied_) {
      curl_easy_setopt(curl_, CURLOPT_HTTPHEADER, header_list);
      order_header_list_applied_ = true;
    }
    curl_easy_setopt(curl_, CURLOPT_POSTFIELDS, body.data());
    curl_easy_setopt(curl_, CURLOPT_POSTFIELDSIZE, body.size());

    if (capture_timing || start_signal != nullptr) {
      const long long started_ns = monotonic_now_ns();
      if (capture_timing) {
        result.perform_started_monotonic_ns = started_ns;
      }
      if (start_signal != nullptr) {
        start_signal->mark(started_ns);
      }
    }
    const CURLcode code = curl_easy_perform(curl_);
    if (code != CURLE_OK) {
      result.error = curl_easy_strerror(code);
    } else {
      curl_easy_getinfo(curl_, CURLINFO_RESPONSE_CODE, &result.status_code);
      result.success = (result.status_code >= 200 && result.status_code < 300) ||
          (base_is_file_url_ && result.status_code == 0);
    }
    return result;
  }

  bool prepare_post_for_benchmark(
      std::string_view body,
      const AuthHeaders& headers) {
    if (curl_ == nullptr) {
      return false;
    }
    HttpResult result;
    struct curl_slist* header_list = stack_auth_header_list(headers, order_header_nodes_);
    if (!order_header_list_applied_) {
      curl_easy_setopt(curl_, CURLOPT_HTTPHEADER, header_list);
      order_header_list_applied_ = true;
    }
    curl_easy_setopt(curl_, CURLOPT_WRITEDATA, &result.body);
    curl_easy_setopt(curl_, CURLOPT_POSTFIELDS, body.data());
    curl_easy_setopt(curl_, CURLOPT_POSTFIELDSIZE, body.size());
    return true;
  }

  bool ensure_order_create_url() {
    if (curl_ == nullptr) {
      return false;
    }
    if (order_create_url_ready_) {
      return true;
    }
    const CURLcode code = curl_easy_setopt(curl_, CURLOPT_URL, order_create_url_.c_str());
    if (code != CURLE_OK) {
      return false;
    }
    const CURLcode post_code = curl_easy_setopt(curl_, CURLOPT_POST, 1L);
    if (post_code != CURLE_OK) {
      return false;
    }
    order_create_url_ready_ = true;
    return true;
  }

  void set_string_write_callback() {
    if (write_callback_mode_ == WriteCallbackMode::String || curl_ == nullptr) {
      return;
    }
    curl_easy_setopt(curl_, CURLOPT_WRITEFUNCTION, write_callback);
    write_callback_mode_ = WriteCallbackMode::String;
  }

  void set_order_write_callback() {
    if (write_callback_mode_ == WriteCallbackMode::Order || curl_ == nullptr) {
      return;
    }
    curl_easy_setopt(curl_, CURLOPT_WRITEFUNCTION, order_write_callback);
    write_callback_mode_ = WriteCallbackMode::Order;
  }

  std::string base_url_;
  std::string order_create_url_;
  std::string url_buffer_;
  long http_timeout_ms_{CURL_REQUEST_TIMEOUT_MS};
  long order_response_timeout_ms_{CURL_REQUEST_TIMEOUT_MS};
  long connect_timeout_ms_{CURL_CONNECT_TIMEOUT_MS};
  std::array<curl_slist, AUTH_HEADER_COUNT> generic_header_nodes_{};
  std::array<curl_slist, AUTH_HEADER_COUNT> order_header_nodes_{};
  CURL* curl_{nullptr};
  bool order_create_url_ready_{false};
  bool order_header_list_applied_{false};
  bool base_is_file_url_{false};
  WriteCallbackMode write_callback_mode_{WriteCallbackMode::Unknown};
};

class BybitNativeBuyer {
public:
  BybitNativeBuyer()
      : api_key_(getenv_or("BYBIT_API_KEY")),
        api_secret_(getenv_or("BYBIT_API_SECRET")),
        base_url_(getenv_or("BYBIT_API_BASE_URL", "https://api.bybit.com")),
        recv_window_(getenv_or("BYBIT_RECV_WINDOW", "5000")),
        timestamp_bias_ms_(getenv_long_long_or("BYBIT_TIMESTAMP_BIAS_MS", -50)),
        buy_enabled_(getenv_truthy("BYBIT_SPOT_BUY_ENABLED", false)),
        timing_enabled_(getenv_truthy("LISTING_TDLIB_NATIVE_TIMING_ENABLED", false)),
        buy_quote_amount_(getenv_or("BYBIT_SPOT_BUY_USDT_AMOUNT", "0")),
        buy_quote_amount_valid_(is_valid_quote_amount(buy_quote_amount_)),
        keepwarm_interval_sec_(
            getenv_int_or("LISTING_TDLIB_NATIVE_KEEPWARM_INTERVAL", 15)),
        symbol_refresh_interval_sec_(
            getenv_int_or("LISTING_TDLIB_NATIVE_SYMBOL_REFRESH_INTERVAL", 15)),
        parallel_keepwarm_client_count_(
            std::min(
                getenv_int_or("LISTING_TDLIB_NATIVE_PARALLEL_KEEPWARM_CLIENTS", 4),
                static_cast<int>(MAX_LISTING_TICKERS))),
        order_client_keepwarm_enabled_(
            getenv_truthy("LISTING_TDLIB_NATIVE_ORDER_CLIENT_KEEPWARM_ENABLED", true)),
        immediate_keepwarm_refresh_enabled_(
            getenv_truthy("LISTING_TDLIB_NATIVE_IMMEDIATE_KEEPWARM_REFRESH", true)),
        blocking_hot_order_warmup_enabled_(
            getenv_truthy("LISTING_TDLIB_NATIVE_BLOCKING_HOT_ORDER_WARMUP", false)),
        order_on_cache_miss_(
            getenv_truthy("LISTING_TDLIB_NATIVE_ORDER_ON_CACHE_MISS", false)),
        async_order_dispatch_enabled_(
            getenv_truthy("LISTING_TDLIB_NATIVE_ASYNC_ORDER_DISPATCH", false)),
        worker_spin_wait_enabled_(
            getenv_truthy("LISTING_TDLIB_NATIVE_WORKER_SPIN_WAIT", false)),
        worker_spin_count_(
            std::clamp(
                getenv_int_or("LISTING_TDLIB_NATIVE_WORKER_SPIN_COUNT", 2),
                0,
                static_cast<int>(MAX_LISTING_TICKERS))),
        order_start_spin_count_(
            std::clamp(
                getenv_int_or("LISTING_TDLIB_NATIVE_ORDER_START_SPIN_COUNT", 64),
                0,
                1000000)),
        skip_order_start_signal_for_self_test_(
            getenv_truthy("LISTING_TDLIB_NATIVE_SKIP_ORDER_START_SIGNAL_FOR_SELF_TEST", false)),
        worker_read_delay_for_self_test_(
            getenv_truthy("LISTING_TDLIB_NATIVE_WORKER_READ_DELAY_FOR_SELF_TEST", false)),
        symbol_cache_path_(
            getenv_or("LISTING_TDLIB_NATIVE_SYMBOL_CACHE_PATH", "data/tdlib_bybit_spot_symbols.txt")),
        symbol_cache_max_age_sec_(
            getenv_int_or("LISTING_TDLIB_NATIVE_SYMBOL_CACHE_MAX_AGE_SEC", 300)),
        symbol_cache_min_count_(
            getenv_int_or("LISTING_TDLIB_NATIVE_SYMBOL_CACHE_MIN_COUNT", 100)),
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
    config_ready_ = buy_enabled_ &&
                    !api_key_.empty() &&
                    !api_secret_.empty() &&
                    buy_quote_amount_valid_;
    init_hmac_sha256_pads(api_secret_, hmac_ipad_, hmac_opad_);
    init_hmac_sha256_contexts();
    publish_spot_symbols(std::make_shared<SpotSymbolSet>());
    for (auto& client : parallel_clients_) {
      client = std::make_unique<CurlClient>(base_url_);
    }
  }

  ~BybitNativeBuyer() {
    stop_keepwarm();
    stop_workers();
  }

  bool set_active(bool active) {
    if (active) {
      begin_activation();
      return finish_activation_warmup();
    }
    ready_.store(false, std::memory_order_release);
    active_.store(false, std::memory_order_release);
    {
      std::lock_guard<std::mutex> lock(warmup_state_mu_);
      warmup_in_progress_ = false;
    }
    warmup_state_cv_.notify_all();
    stop_keepwarm();
    return true;
  }

  void begin_activation() {
    active_.store(true, std::memory_order_release);
    ready_.store(false, std::memory_order_release);
    {
      std::lock_guard<std::mutex> lock(warmup_state_mu_);
      warmup_in_progress_ = true;
    }
  }

  bool finish_activation_warmup() {
    const bool ready = warmup();
    ready_.store(ready, std::memory_order_release);
    {
      std::lock_guard<std::mutex> lock(warmup_state_mu_);
      warmup_in_progress_ = false;
    }
    warmup_state_cv_.notify_all();
    return ready;
  }

  bool is_active() const {
    return active_.load();
  }

  bool warmup() {
    if (!config_ready_) {
      return false;
    }
    bool symbols_ready = load_spot_symbols_from_cache();
    if (!symbols_ready && !order_on_cache_miss_) {
      symbols_ready = refresh_symbols();
    }
    const bool ready = order_on_cache_miss_ || symbols_ready;
    if (ready) {
      warm_request_scratch();
      warm_order_client_once();
      wait_workers_ready_once();
      (void)warm_parallel_clients_once();
      if (blocking_hot_order_warmup_enabled_) {
        const bool hot_primary_ready = refresh_hot_order_client();
        const bool hot_parallel_ready = refresh_hot_parallel_order_clients();
        blocking_hot_order_warmup_done_.store(
            hot_primary_ready && hot_parallel_ready,
            std::memory_order_release);
      }
      start_keepwarm_once();
    }
    return ready;
  }

  std::string readiness_reason() const {
    if (!active_.load()) {
      return "tdlib_native_buy_inactive";
    }
    if (!buy_enabled_) {
      return "buy_disabled";
    }
    if (api_key_.empty() || api_secret_.empty()) {
      return "missing_api_config";
    }
    if (!buy_quote_amount_valid_) {
      return "quote_amount_invalid";
    }
    const auto* symbols = spot_symbols_raw_.load(std::memory_order_acquire);
    if (!order_on_cache_miss_ && (symbols == nullptr || symbols->empty())) {
      return "spot_symbol_cache_empty";
    }
    if (!order_on_cache_miss_ && symbols != nullptr && symbols->size() < symbol_cache_min_count_) {
      return "spot_symbol_cache_too_small";
    }
    return "ready";
  }

  NativeTradeResult buy_listing(
      std::string_view exchange,
      long long message_id,
      std::string_view ticker,
      size_t preferred_worker_index = 0) {
    if (async_order_dispatch_enabled_ && !order_preflight_only_) {
      if (auto trade = buy_listing_async_until_order_send_started(
              exchange,
              message_id,
              ticker,
              preferred_worker_index)) {
        return *trade;
      }
    }
    if (order_client_keepwarm_enabled_) {
      CurlClient* hot_client = hot_order_client_raw_.load(std::memory_order_acquire);
      if (hot_client != nullptr) {
        return buy_listing_with_client(*hot_client, exchange, message_id, ticker);
      }
    }
    return buy_listing_with_client(client_, exchange, message_id, ticker);
  }

  NativeDispatchResult dispatch_listing_async(
      std::string_view exchange,
      long long message_id,
      std::string_view ticker,
      size_t preferred_worker_index = 0) {
    NativeDispatchResult result;
    if (!async_order_dispatch_enabled_ || order_preflight_only_) {
      result.no_worker = true;
      result.reason = "async_dispatch_unavailable";
      return result;
    }

    bool ready_now = ready_.load(std::memory_order_relaxed);
    std::array<char, 32> symbol_buffer{};
    const std::string_view symbol = spot_symbol_view(ticker, symbol_buffer);
    if (symbol.empty()) {
      result.reason = "invalid_symbol";
      return result;
    }
    if (!ready_now) {
      const bool active = active_.load(std::memory_order_relaxed);
      if (!active) {
        result.reason = "tdlib_native_buy_inactive";
        return result;
      }
      if (!config_ready_) {
        result.reason = config_not_ready_reason();
        return result;
      }
      if (!wait_until_ready_for_order()) {
        result.reason = "native_buy_not_ready";
        return result;
      }
      ready_now = true;
    }
    if (!order_on_cache_miss_ && !has_spot_symbol(symbol)) {
      result.reason = "spot_symbol_unavailable";
      return result;
    }

    auto claim = claim_available_worker_slot(
        preferred_worker_index,
        exchange,
        message_id,
        ticker,
        true,
        true);
    if (!claim.has_value()) {
      result.no_worker = true;
      result.reason = "native_worker_unavailable";
      return result;
    }
    result.dispatched = true;
    result.worker_index = claim->first;
    result.work_seq = claim->second;
    result.reason = "tdlib_native_rest_fire_and_forget";
    return result;
  }

  void activate_workers_for_self_test() {
    active_.store(true, std::memory_order_release);
    ready_.store(true, std::memory_order_release);
    start_workers_once();
  }

  void begin_preflight_activation_for_self_test() {
    order_preflight_only_ = true;
    begin_activation();
    warm_request_scratch();
    (void)prime_order_client_for_hot_path(client_);
    for (auto& client : parallel_clients_) {
      (void)prime_order_client_for_hot_path(*client);
    }
    wait_workers_ready_once();
    (void)warm_parallel_clients_once();
  }

  void finish_preflight_activation_for_self_test(bool ready) {
    ready_.store(ready, std::memory_order_release);
    {
      std::lock_guard<std::mutex> lock(warmup_state_mu_);
      warmup_in_progress_ = false;
    }
    warmup_state_cv_.notify_all();
  }

  void inject_spot_symbol_for_self_test(std::string symbol) {
    auto next_symbols = std::make_shared<SpotSymbolSet>();
    const auto* current = spot_symbols_raw_.load(std::memory_order_acquire);
    if (current != nullptr) {
      next_symbols->insert(current->begin(), current->end());
    }
    next_symbols->insert(std::move(symbol));
    publish_spot_symbols(std::shared_ptr<const SpotSymbolSet>(std::move(next_symbols)));
  }

  void inject_hot_order_client_for_self_test() {
    auto client = std::make_shared<CurlClient>(base_url_);
    (void)prime_order_client_for_hot_path(*client);
    publish_hot_order_client(std::move(client));
  }

  void inject_hot_parallel_order_client_for_self_test(size_t index) {
    auto client = std::make_shared<CurlClient>(base_url_);
    (void)prime_order_client_for_hot_path(*client);
    publish_hot_parallel_order_client(index, std::move(client));
  }

  bool hot_order_client_ready_for_self_test() const {
    return hot_order_client_raw_.load(std::memory_order_acquire) != nullptr;
  }

  bool hot_parallel_order_client_ready_for_self_test(size_t index) const {
    return index < MAX_LISTING_TICKERS &&
           hot_parallel_order_client_raw_[index].load(std::memory_order_acquire) != nullptr;
  }

  bool workers_ready_for_self_test() const {
    for (const auto& ready : worker_ready_) {
      if (!ready.load(std::memory_order_acquire)) {
        return false;
      }
    }
    return true;
  }

  NativeTradeResult wait_worker_done_copy_for_self_test(
      size_t index,
      uint64_t expected_seq) {
    return wait_worker_done_copy(index, expected_seq);
  }

  uint64_t worker_work_seq_for_self_test(size_t index) const {
    if (index >= MAX_LISTING_TICKERS) {
      return 0;
    }
    return worker_slots_[index].work_seq.load(std::memory_order_acquire);
  }

  bool worker_claimed_for_self_test(size_t index) const {
    return index < MAX_LISTING_TICKERS &&
           worker_slots_[index].claimed.load(std::memory_order_acquire);
  }

  size_t hot_order_client_snapshot_count_for_self_test() {
    std::lock_guard<std::mutex> lock(hot_order_client_snapshots_mu_);
    return hot_order_client_snapshots_.size();
  }

  size_t hot_parallel_order_client_snapshot_count_for_self_test(size_t index) {
    if (index >= MAX_LISTING_TICKERS) {
      return 0;
    }
    std::lock_guard<std::mutex> lock(hot_parallel_order_client_snapshots_mu_);
    return hot_parallel_order_client_snapshots_[index].size();
  }

  int parallel_keepwarm_client_count_for_self_test() const {
    return parallel_keepwarm_client_count_;
  }

  bool load_spot_symbols_from_cache_for_self_test() {
    return load_spot_symbols_from_cache();
  }

  bool prepare_order_for_benchmark(
      std::string_view exchange,
      long long message_id,
      std::string_view ticker) {
    std::array<char, 32> symbol_buffer{};
    const std::string_view symbol = spot_symbol_view(ticker, symbol_buffer);
    if (symbol.empty()) {
      return false;
    }
    if (!has_spot_symbol(symbol)) {
      return false;
    }
    std::array<char, 64> order_link_id_buffer{};
    const std::string_view order_link_id =
        build_order_link_id_view(exchange, message_id, ticker, order_link_id_buffer);
    const PreparedOrderRequest& request = prepare_order_request(symbol, order_link_id);
    const std::string_view body = request.body_view();
    return !body.empty() &&
           body.find(symbol) != std::string_view::npos &&
           std::string_view(request.headers.c_str(2)).rfind("X-BAPI-SIGN: ", 0) == 0;
  }

  bool prepare_order_curl_for_benchmark(
      std::string_view exchange,
      long long message_id,
      std::string_view ticker) {
    std::array<char, 32> symbol_buffer{};
    const std::string_view symbol = spot_symbol_view(ticker, symbol_buffer);
    if (symbol.empty()) {
      return false;
    }
    if (!has_spot_symbol(symbol)) {
      return false;
    }
    std::array<char, 64> order_link_id_buffer{};
    const std::string_view order_link_id =
        build_order_link_id_view(exchange, message_id, ticker, order_link_id_buffer);
    const PreparedOrderRequest& request = prepare_order_request(symbol, order_link_id);
    return client_.prepare_post_order_for_benchmark(request.body_view(), request.headers);
  }

  void enable_order_preflight_for_benchmark() {
    order_preflight_only_ = true;
    active_.store(true, std::memory_order_relaxed);
    ready_.store(true, std::memory_order_release);
    warm_request_scratch();
    (void)client_.prime_order_create_url();
    if (order_client_keepwarm_enabled_) {
      inject_hot_order_client_for_self_test();
    }
    wait_workers_ready_once();
    (void)warm_parallel_clients_once();
  }

  bool buy_listing_preflight_for_benchmark(
      std::string_view exchange,
      long long message_id,
      std::string_view ticker) {
    const auto trade = buy_listing(exchange, message_id, ticker);
    return trade.attempted &&
           !trade.executed &&
           trade.ret_code == 0 &&
           trade.reason == "tdlib_native_rest_preflight";
  }

  size_t buy_listings(
      std::string_view exchange,
      long long message_id,
      const ListingTickers& tickers,
      std::array<std::optional<NativeTradeResult>, MAX_LISTING_TICKERS>& out) {
    if (tickers.count == 0) {
      return 0;
    }
    if (async_order_dispatch_enabled_ && !order_preflight_only_) {
      size_t count = 0;
      for (size_t i = 0; i < tickers.count; ++i) {
        out[count].emplace(buy_listing(exchange, message_id, tickers.values[i], i));
        ++count;
      }
      return count;
    }
    if (tickers.count == 1 || !ready_for_order()) {
      size_t count = 0;
      for (size_t i = 0; i < tickers.count; ++i) {
        out[count].emplace(buy_listing(exchange, message_id, tickers.values[i]));
        ++count;
      }
      return count;
    }
    // Hot-path readiness is only a gate; client/symbol snapshots keep acquire loads.
    if (!ready_.load(std::memory_order_relaxed) && !wait_until_ready_for_order()) {
      size_t count = 0;
      for (size_t i = 0; i < tickers.count; ++i) {
        out[count].emplace(buy_listing(exchange, message_id, tickers.values[i]));
        ++count;
      }
      return count;
    }

    wait_workers_ready_once();
    std::array<uint64_t, MAX_LISTING_TICKERS> expected_done_seq{};
    for (size_t i = 1; i < tickers.count; ++i) {
      auto& slot = worker_slots_[i];
      slot.exchange = exchange;
      slot.message_id = message_id;
      slot.ticker = tickers.values[i];
      slot.spot_symbol_prechecked = false;
      const uint64_t next_work_seq =
          slot.work_seq.load(std::memory_order_relaxed) + 1;
      expected_done_seq[i] = next_work_seq;
      slot.work_seq.store(next_work_seq, std::memory_order_release);
      slot.work_seq.notify_one();
    }
    out[0].emplace(buy_listing(exchange, message_id, tickers.values[0]));
    for (size_t i = 1; i < tickers.count; ++i) {
      auto& slot = worker_slots_[i];
      uint64_t observed = slot.done_seq.load(std::memory_order_acquire);
      while (observed != expected_done_seq[i]) {
        slot.done_seq.wait(observed, std::memory_order_acquire);
        observed = slot.done_seq.load(std::memory_order_acquire);
      }
      if (slot.trade.has_value()) {
        out[i].emplace(std::move(*slot.trade));
      }
      slot.trade.reset();
    }
    return tickers.count;
  }

private:
  bool ready_for_order() const {
    return active_.load(std::memory_order_relaxed) && config_ready_;
  }

  const char* config_not_ready_reason() const {
    if (!buy_enabled_) {
      return "buy_disabled";
    }
    if (api_key_.empty() || api_secret_.empty()) {
      return "missing_api_config";
    }
    if (!buy_quote_amount_valid_) {
      return "quote_amount_invalid";
    }
    return "config_not_ready";
  }

  std::optional<std::pair<size_t, uint64_t>> claim_available_worker_slot(
      size_t preferred_index,
      std::string_view exchange,
      long long message_id,
      std::string_view ticker,
      bool fire_and_forget = false,
      bool spot_symbol_prechecked = false) {
    wait_workers_ready_once();
    for (size_t offset = 0; offset < MAX_LISTING_TICKERS; ++offset) {
      const size_t index = (preferred_index + offset) % MAX_LISTING_TICKERS;
      auto& slot = worker_slots_[index];
      bool expected_claimed = false;
      if (!slot.claimed.compare_exchange_strong(
              expected_claimed,
              true,
              std::memory_order_acq_rel,
              std::memory_order_acquire)) {
        continue;
      }
      const uint64_t work_seq = slot.work_seq.load(std::memory_order_acquire);
      const uint64_t done_seq = slot.done_seq.load(std::memory_order_acquire);
      if (work_seq != done_seq) {
        slot.claimed.store(false, std::memory_order_release);
        continue;
      }
      slot.trade.reset();
      slot.order_send_started_ns.store(0, std::memory_order_release);
      slot.order_send_started_seq.store(done_seq, std::memory_order_release);
      slot.fire_and_forget.store(fire_and_forget, std::memory_order_release);
      slot.exchange = exchange;
      slot.message_id = message_id;
      if (!slot.set_ticker_copy(ticker)) {
        slot.fire_and_forget.store(false, std::memory_order_release);
        slot.claimed.store(false, std::memory_order_release);
        continue;
      }
      slot.spot_symbol_prechecked = spot_symbol_prechecked;
      const uint64_t next_work_seq = work_seq + 1;
      slot.work_seq.store(next_work_seq, std::memory_order_release);
      slot.work_seq.notify_one();
      return std::make_pair(index, next_work_seq);
    }
    return std::nullopt;
  }

  long long wait_worker_order_send_started(size_t index, uint64_t expected_seq) {
    auto& slot = worker_slots_[index];
    uint64_t observed = slot.order_send_started_seq.load(std::memory_order_acquire);
    if (observed != expected_seq &&
        worker_spin_wait_enabled_ &&
        index < static_cast<size_t>(worker_spin_count_)) {
      for (int i = 0; i < order_start_spin_count_ && observed != expected_seq; ++i) {
        cpu_relax();
        observed = slot.order_send_started_seq.load(std::memory_order_acquire);
      }
    }
    while (observed != expected_seq) {
      slot.order_send_started_seq.wait(observed, std::memory_order_acquire);
      observed = slot.order_send_started_seq.load(std::memory_order_acquire);
    }
    return slot.order_send_started_ns.load(std::memory_order_acquire);
  }

  NativeTradeResult wait_worker_done_copy(size_t index, uint64_t expected_seq) {
    auto& slot = worker_slots_[index];
    uint64_t observed = slot.done_seq.load(std::memory_order_acquire);
    while (observed != expected_seq) {
      slot.done_seq.wait(observed, std::memory_order_acquire);
      observed = slot.done_seq.load(std::memory_order_acquire);
    }
      NativeTradeResult result;
      if (slot.trade.has_value()) {
        result = *slot.trade;
      } else {
        result.reason = "native_worker_missing_result";
      }
      slot.trade.reset();
      slot.fire_and_forget.store(false, std::memory_order_release);
      slot.claimed.store(false, std::memory_order_release);
      return result;
  }

  std::optional<NativeTradeResult> buy_listing_async_until_order_send_started(
      std::string_view exchange,
      long long message_id,
      std::string_view ticker,
      size_t preferred_worker_index) {
    const long long started_monotonic_ns = timing_enabled_ ? monotonic_now_ns() : 0;
    bool ready_now = ready_.load(std::memory_order_relaxed);
    std::array<char, 32> symbol_buffer{};
    const std::string_view symbol = spot_symbol_view(ticker, symbol_buffer);

    auto failure_result = [&](const char* reason) {
      NativeTradeResult result;
      result.enabled = ready_now && buy_enabled_;
      result.symbol.assign(symbol);
      result.reason = reason;
      if (timing_enabled_) {
        result.trade_started_monotonic_ns = started_monotonic_ns;
        result.trade_finished_monotonic_ns = monotonic_now_ns();
      }
      return result;
    };

    if (symbol.empty()) {
      return failure_result("invalid_symbol");
    }
    if (!ready_now) {
      const bool active = active_.load(std::memory_order_relaxed);
      if (!active) {
        return failure_result("tdlib_native_buy_inactive");
      }
      if (!config_ready_) {
        return failure_result(config_not_ready_reason());
      }
      if (!wait_until_ready_for_order()) {
        return failure_result("native_buy_not_ready");
      }
      ready_now = true;
    }
    if (!order_on_cache_miss_ && !has_spot_symbol(symbol)) {
      return failure_result("spot_symbol_unavailable");
    }

    auto claim = claim_available_worker_slot(
        preferred_worker_index,
        exchange,
        message_id,
        ticker,
        false,
        true);
    if (!claim.has_value()) {
      return std::nullopt;
    }

    const size_t worker_index = claim->first;
    const uint64_t expected_seq = claim->second;
    const long long order_send_started_ns =
        wait_worker_order_send_started(worker_index, expected_seq);
    if (order_send_started_ns <= 0) {
      return wait_worker_done_copy(worker_index, expected_seq);
    }

    std::array<char, 64> order_link_id_buffer{};
    const std::string_view order_link_id =
        build_order_link_id_view(exchange, message_id, ticker, order_link_id_buffer);
    NativeTradeResult result;
    result.enabled = ready_now && buy_enabled_;
    result.attempted = true;
    result.ret_code = -1;
    result.symbol.assign(symbol);
    result.order_link_id.assign(order_link_id);
    result.reason = "tdlib_native_rest_dispatched";
    if (timing_enabled_) {
      result.trade_started_monotonic_ns = started_monotonic_ns;
      result.order_send_started_monotonic_ns = order_send_started_ns;
    }
    return result;
  }

  void start_workers_once() {
    bool expected = false;
    if (!workers_started_.compare_exchange_strong(expected, true)) {
      return;
    }
    worker_pool_ready_.store(false, std::memory_order_release);
    for (auto& slot : worker_slots_) {
      slot.stop_requested.store(false, std::memory_order_release);
      slot.claimed.store(false, std::memory_order_release);
      slot.fire_and_forget.store(false, std::memory_order_release);
      slot.spot_symbol_prechecked = false;
      slot.trade.reset();
    }
    for (auto& ready : worker_ready_) {
      ready.store(false, std::memory_order_release);
    }
    for (size_t i = 0; i < MAX_LISTING_TICKERS; ++i) {
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
    for (auto& ready : worker_ready_) {
      bool observed = ready.load(std::memory_order_acquire);
      while (!observed) {
        ready.wait(observed, std::memory_order_acquire);
        observed = ready.load(std::memory_order_acquire);
      }
    }
    worker_pool_ready_.store(true, std::memory_order_release);
  }

  void stop_workers() {
    if (!workers_started_.exchange(false)) {
      return;
    }
    worker_pool_ready_.store(false, std::memory_order_release);
    for (auto& slot : worker_slots_) {
      slot.stop_requested.store(true, std::memory_order_release);
      slot.work_seq.fetch_add(1, std::memory_order_acq_rel);
      slot.work_seq.notify_one();
    }
    for (auto& worker : worker_threads_) {
      if (worker.joinable()) {
        worker.join();
      }
    }
  }

  bool warm_order_client_once() {
    bool expected = false;
    if (!order_client_warmed_.compare_exchange_strong(expected, true)) {
      return true;
    }
    if (!prime_order_client_for_hot_path(client_)) {
      order_client_warmed_.store(false);
      return false;
    }
    publish_primary_order_client();
    return true;
  }

  bool refresh_hot_order_client() {
    if (!order_client_keepwarm_enabled_ || !config_ready_) {
      return false;
    }
    auto client = std::make_shared<CurlClient>(base_url_);
    const auto response = client->get("/v5/market/instruments-info?category=spot&limit=1");
    if (!response.success || !prime_order_client_for_hot_path(*client)) {
      return false;
    }
    publish_hot_order_client(std::move(client));
    return true;
  }

  bool refresh_hot_parallel_order_clients() {
    if (!order_client_keepwarm_enabled_ || !config_ready_) {
      return false;
    }
    const size_t refresh_count = std::min(
        static_cast<size_t>(parallel_keepwarm_client_count_),
        static_cast<size_t>(MAX_LISTING_TICKERS));
    std::array<std::thread, MAX_LISTING_TICKERS> threads;
    std::array<std::shared_ptr<CurlClient>, MAX_LISTING_TICKERS> clients;
    std::array<bool, MAX_LISTING_TICKERS> successes{};
    for (size_t i = 0; i < refresh_count; ++i) {
      threads[i] = std::thread([this, &clients, &successes, i]() {
        lower_current_thread_for_background();
        auto client = std::make_shared<CurlClient>(base_url_);
        const auto response = client->get(
            "/v5/market/instruments-info?category=spot&limit=1");
        if (response.success && prime_order_client_for_hot_path(*client)) {
          clients[i] = std::move(client);
          successes[i] = true;
        }
      });
    }
    bool any_ready = false;
    for (size_t i = 0; i < refresh_count; ++i) {
      if (threads[i].joinable()) {
        threads[i].join();
      }
    }
    for (size_t i = 0; i < refresh_count; ++i) {
      if (successes[i] && clients[i] != nullptr) {
        publish_hot_parallel_order_client(i, std::move(clients[i]));
        any_ready = true;
      }
    }
    return any_ready;
  }

  void publish_hot_order_client(std::shared_ptr<CurlClient> client) {
    CurlClient* raw = client.get();
    {
      std::lock_guard<std::mutex> lock(hot_order_client_snapshots_mu_);
      hot_order_client_snapshots_.push_back(std::move(client));
      if (hot_order_client_snapshots_.size() > MAX_HOT_ORDER_CLIENT_SNAPSHOTS) {
        hot_order_client_snapshots_.erase(hot_order_client_snapshots_.begin());
      }
    }
    hot_order_client_raw_.store(raw, std::memory_order_release);
  }

  void publish_primary_order_client() {
    if (order_client_keepwarm_enabled_) {
      hot_order_client_raw_.store(&client_, std::memory_order_release);
    }
  }

  void publish_primary_parallel_order_clients() {
    if (!order_client_keepwarm_enabled_) {
      return;
    }
    for (size_t i = 0; i < MAX_LISTING_TICKERS; ++i) {
      hot_parallel_order_client_raw_[i].store(
          parallel_clients_[i].get(),
          std::memory_order_release);
    }
  }

  void publish_hot_parallel_order_client(
      size_t index,
      std::shared_ptr<CurlClient> client) {
    if (index >= MAX_LISTING_TICKERS || client == nullptr) {
      return;
    }
    CurlClient* raw = client.get();
    {
      std::lock_guard<std::mutex> lock(hot_parallel_order_client_snapshots_mu_);
      auto& snapshots = hot_parallel_order_client_snapshots_[index];
      snapshots.push_back(std::move(client));
      if (snapshots.size() > MAX_HOT_ORDER_CLIENT_SNAPSHOTS) {
        snapshots.erase(snapshots.begin());
      }
    }
    hot_parallel_order_client_raw_[index].store(raw, std::memory_order_release);
  }

  bool warm_parallel_clients_once() {
    bool expected = false;
    if (!parallel_clients_warmed_.compare_exchange_strong(expected, true)) {
      return true;
    }
    bool all_ready = true;
    for (size_t i = 0; i < MAX_LISTING_TICKERS; ++i) {
      all_ready = prime_order_client_for_hot_path(*parallel_clients_[i]) && all_ready;
    }
    if (!all_ready) {
      parallel_clients_warmed_.store(false);
      return false;
    }
    publish_primary_parallel_order_clients();
    return true;
  }

  void start_keepwarm_once() {
    bool expected = false;
    if (!keepwarm_started_.compare_exchange_strong(expected, true)) {
      return;
    }
    stop_keepwarm_.store(false);
    keepwarm_thread_ = std::thread([this]() {
      keepwarm_loop();
    });
  }

  void stop_keepwarm() {
    if (!keepwarm_started_.exchange(false)) {
      return;
    }
    stop_keepwarm_.store(true);
    keepwarm_cv_.notify_one();
    if (keepwarm_thread_.joinable()) {
      keepwarm_thread_.join();
    }
  }

  void keepwarm_loop() {
    lower_current_thread_for_background();
    wait_workers_ready_once();
    (void)warm_parallel_clients_once();
    auto last_symbol_refresh = std::chrono::steady_clock::now() -
        std::chrono::seconds(symbol_refresh_interval_sec_);
    if (immediate_keepwarm_refresh_enabled_) {
      (void)refresh_symbols();
      last_symbol_refresh = std::chrono::steady_clock::now();
      if (!blocking_hot_order_warmup_done_.load(std::memory_order_acquire)) {
        (void)refresh_hot_order_client();
        (void)refresh_hot_parallel_order_clients();
      }
    }
    while (true) {
      std::unique_lock<std::mutex> lock(keepwarm_mu_);
      if (keepwarm_cv_.wait_for(
              lock,
              std::chrono::seconds(keepwarm_interval_sec_),
              [this]() { return stop_keepwarm_.load(); })) {
        return;
      }
      lock.unlock();
      const auto now = std::chrono::steady_clock::now();
      if (now - last_symbol_refresh >= std::chrono::seconds(symbol_refresh_interval_sec_)) {
        (void)refresh_symbols();
        last_symbol_refresh = now;
      }
      (void)refresh_hot_order_client();
      (void)refresh_hot_parallel_order_clients();
    }
  }

  void worker_loop(size_t index) {
    boost_current_thread_for_hot_path();
    warm_request_scratch();
    worker_ready_[index].store(true, std::memory_order_release);
    worker_ready_[index].notify_one();
    auto& slot = worker_slots_[index];
    uint64_t seen_work_seq = slot.done_seq.load(std::memory_order_relaxed);
    while (true) {
      if (slot.stop_requested.load(std::memory_order_acquire)) {
        return;
      }
      uint64_t current_work_seq = slot.work_seq.load(std::memory_order_acquire);
      if (current_work_seq == seen_work_seq) {
        if (worker_spin_wait_enabled_ &&
            index < static_cast<size_t>(worker_spin_count_)) {
          do {
            if (slot.stop_requested.load(std::memory_order_acquire)) {
              return;
            }
            cpu_relax();
            current_work_seq = slot.work_seq.load(std::memory_order_acquire);
          } while (current_work_seq == seen_work_seq);
        } else {
          slot.work_seq.wait(seen_work_seq, std::memory_order_acquire);
          continue;
        }
      }
      if (slot.stop_requested.load(std::memory_order_acquire)) {
        return;
      }
      seen_work_seq = current_work_seq;
      if (worker_read_delay_for_self_test_) {
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
      }
      const std::string_view exchange = slot.exchange;
      const long long message_id = slot.message_id;
      const std::string_view ticker = slot.ticker;
      const bool spot_symbol_prechecked = slot.spot_symbol_prechecked;

      CurlClient* client = hot_parallel_order_client_raw_[index].load(
          std::memory_order_acquire);
      if (client == nullptr) {
        client = parallel_clients_[index].get();
      }
      NativeOrderStartSignal start_signal{
          &slot.order_send_started_seq,
          &slot.order_send_started_ns,
          current_work_seq,
      };
      const NativeOrderStartSignal* order_start_signal =
          skip_order_start_signal_for_self_test_ ? nullptr : &start_signal;
      auto trade = buy_listing_with_client(
          *client,
          exchange,
          message_id,
          ticker,
          order_start_signal,
          spot_symbol_prechecked);

      slot.trade.emplace(std::move(trade));
      if (slot.order_send_started_seq.load(std::memory_order_acquire) != current_work_seq) {
        const bool fire_and_forget =
            slot.fire_and_forget.load(std::memory_order_acquire);
        slot.done_seq.store(current_work_seq, std::memory_order_release);
        slot.done_seq.notify_one();
        slot.order_send_started_ns.store(
            slot.trade->order_send_started_monotonic_ns,
            std::memory_order_release);
        slot.order_send_started_seq.store(current_work_seq, std::memory_order_release);
        slot.order_send_started_seq.notify_one();
        if (fire_and_forget) {
          slot.trade.reset();
          slot.fire_and_forget.store(false, std::memory_order_release);
          slot.claimed.store(false, std::memory_order_release);
        }
        continue;
      }
      slot.done_seq.store(current_work_seq, std::memory_order_release);
      slot.fire_and_forget.store(false, std::memory_order_release);
      slot.claimed.store(false, std::memory_order_release);
      slot.done_seq.notify_one();
    }
  }

  NativeTradeResult buy_listing_with_client(
      CurlClient& client,
      std::string_view exchange,
      long long message_id,
      std::string_view ticker,
      const NativeOrderStartSignal* start_signal = nullptr,
      bool spot_symbol_prechecked = false) {
    const long long started_monotonic_ns = timing_enabled_ ? monotonic_now_ns() : 0;
    // Hot-path readiness is only a gate; client/symbol snapshots keep acquire loads.
    bool ready_now = ready_.load(std::memory_order_relaxed);
    std::array<char, 32> symbol_buffer{};
    const std::string_view symbol = spot_symbol_view(ticker, symbol_buffer);

    auto finish_result = [&](NativeTradeResult& result) {
      if (timing_enabled_) {
        result.trade_started_monotonic_ns = started_monotonic_ns;
        result.trade_finished_monotonic_ns = monotonic_now_ns();
      }
    };
    auto failure_result = [&](const char* reason) {
      NativeTradeResult result;
      result.enabled = ready_now && buy_enabled_;
      result.symbol.assign(symbol);
      result.reason = reason;
      finish_result(result);
      return result;
    };

    if (symbol.empty()) {
      return failure_result("invalid_symbol");
    }
    if (!ready_now) {
      const bool active = active_.load(std::memory_order_relaxed);
      if (!active) {
        return failure_result("tdlib_native_buy_inactive");
      }
      if (!config_ready_) {
        return failure_result(config_not_ready_reason());
      }
      if (!wait_until_ready_for_order()) {
        return failure_result("native_buy_not_ready");
      }
      ready_now = true;
    }
    if (!spot_symbol_prechecked && !order_on_cache_miss_ && !has_spot_symbol(symbol)) {
      return failure_result("spot_symbol_unavailable");
    }

    std::array<char, 64> order_link_id_buffer{};
    const std::string_view order_link_id =
        build_order_link_id_view(exchange, message_id, ticker, order_link_id_buffer);
    const PreparedOrderRequest& request = prepare_order_request(symbol, order_link_id);
    const std::string_view request_body = request.body_view();
    if (request_body.empty()) {
      return failure_result("order_request_too_large");
    }
    if (order_preflight_only_) {
      const bool prepared = client.prepare_post_order_for_benchmark(request_body, request.headers);
      NativeTradeResult result;
      result.enabled = ready_now && buy_enabled_;
      result.symbol.assign(symbol);
      result.order_link_id.assign(order_link_id);
      result.attempted = true;
      result.ret_code = prepared ? 0 : -1;
      result.reason = prepared ? "tdlib_native_rest_preflight" : "curl_preflight_failed";
      finish_result(result);
      return result;
    }
    const auto response = client.post_order_create(
        request_body,
        request.headers,
        timing_enabled_,
        start_signal);
    NativeTradeResult result;
    if (timing_enabled_) {
      result.trade_started_monotonic_ns = started_monotonic_ns;
      result.order_send_started_monotonic_ns = response.perform_started_monotonic_ns;
    }
    result.enabled = ready_now && buy_enabled_;
    result.symbol.assign(symbol);
    result.order_link_id.assign(order_link_id);
    result.attempted = true;
    const std::string_view response_body = response.body_view();
    const long long ret_code = extract_json_int_pattern(response_body, "\"retCode\":").value_or(-1);
    result.ret_code = static_cast<int>(ret_code);
    if (!response.success || ret_code != 0) {
      result.reason = extract_json_string(response_body, "retMsg").value_or(
          response.body_truncated
              ? "order_response_truncated"
              : (response.error.empty() ? "order_create_failed" : response.error));
      mark_trade_finished(result);
      return result;
    }

    result.executed = true;
    result.order_id = extract_json_string(response_body, "orderId").value_or("");
    result.reason = "tdlib_native_rest";
    mark_trade_finished(result);
    return result;
  }

  void mark_trade_finished(NativeTradeResult& result) const {
    if (timing_enabled_) {
      result.trade_finished_monotonic_ns = monotonic_now_ns();
    }
  }

  bool wait_until_ready_for_order() {
    if (ready_.load(std::memory_order_acquire)) {
      return true;
    }
    std::unique_lock<std::mutex> lock(warmup_state_mu_);
    if (!warmup_in_progress_) {
      return ready_.load(std::memory_order_acquire);
    }
    warmup_state_cv_.wait(lock, [this]() {
      return ready_.load(std::memory_order_acquire) ||
             !warmup_in_progress_ ||
             !active_.load(std::memory_order_acquire);
    });
    return ready_.load(std::memory_order_acquire);
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
      for (const auto& symbol : extract_symbols(response.body)) {
        next_symbols.insert(symbol);
      }
      cursor = extract_json_string(response.body, "nextPageCursor").value_or("");
    } while (!cursor.empty());
    if (next_symbols.empty()) {
      return false;
    }
    std::shared_ptr<const SpotSymbolSet> snapshot =
        std::make_shared<SpotSymbolSet>(std::move(next_symbols));
    save_spot_symbols_to_cache(*snapshot);
    publish_spot_symbols(std::move(snapshot));
    return true;
  }

  bool load_spot_symbols_from_cache() {
    if (symbol_cache_path_.empty()) {
      return false;
    }
    std::ifstream input(symbol_cache_path_);
    if (!input.good()) {
      return false;
    }
    std::string line;
    if (!std::getline(input, line)) {
      return false;
    }
    constexpr std::string_view saved_prefix = "# saved_unix_sec=";
    if (line.rfind(saved_prefix, 0) != 0) {
      return false;
    }
    long long saved_sec = 0;
    const auto timestamp_text = std::string_view(line).substr(saved_prefix.size());
    const auto parsed = std::from_chars(
        timestamp_text.data(),
        timestamp_text.data() + timestamp_text.size(),
        saved_sec);
    if (parsed.ec != std::errc()) {
      return false;
    }
    const long long age_sec = wall_now_sec() - saved_sec;
    if (age_sec < 0 || age_sec > symbol_cache_max_age_sec_) {
      return false;
    }

    SpotSymbolSet cached_symbols;
    while (std::getline(input, line)) {
      if (!line.empty() && line.back() == '\r') {
        line.pop_back();
      }
      if (!line.empty() && line.front() != '#') {
        cached_symbols.insert(std::move(line));
      }
    }
    if (cached_symbols.empty() ||
        cached_symbols.size() < static_cast<size_t>(symbol_cache_min_count_)) {
      return false;
    }
    std::shared_ptr<const SpotSymbolSet> snapshot =
        std::make_shared<SpotSymbolSet>(std::move(cached_symbols));
    publish_spot_symbols(std::move(snapshot));
    return true;
  }

  void save_spot_symbols_to_cache(const SpotSymbolSet& symbols) const {
    if (symbol_cache_path_.empty() || symbols.empty()) {
      return;
    }
    std::error_code ec;
    const std::filesystem::path path(symbol_cache_path_);
    if (path.has_parent_path()) {
      std::filesystem::create_directories(path.parent_path(), ec);
      if (ec) {
        return;
      }
    }
    const std::filesystem::path tmp_path =
        path.string() + ".tmp";
    {
      std::ofstream output(tmp_path);
      if (!output.good()) {
        return;
      }
      output << "# saved_unix_sec=" << wall_now_sec() << '\n';
      for (const auto& symbol : symbols) {
        output << symbol << '\n';
      }
    }
    std::filesystem::rename(tmp_path, path, ec);
    if (ec) {
      std::filesystem::remove(tmp_path, ec);
    }
  }

  bool has_spot_symbol(std::string_view symbol) {
    const auto* symbols = spot_symbols_raw_.load(std::memory_order_acquire);
    return symbols != nullptr && symbols->find(symbol) != symbols->end();
  }

  void publish_spot_symbols(std::shared_ptr<const SpotSymbolSet> snapshot) {
    const auto* raw = snapshot.get();
    {
      std::lock_guard<std::mutex> lock(spot_symbol_snapshots_mu_);
      spot_symbol_snapshots_.push_back(std::move(snapshot));
    }
    spot_symbols_raw_.store(raw, std::memory_order_release);
  }

  std::string build_order_link_id(
      std::string_view exchange,
      long long message_id,
      std::string_view ticker) const {
    std::array<char, 64> buffer{};
    const auto value = build_order_link_id_view(exchange, message_id, ticker, buffer);
    return std::string(value);
  }

  std::string_view build_order_link_id_view(
      std::string_view exchange,
      long long message_id,
      std::string_view ticker,
      std::array<char, 64>& buffer) const {
    const std::string_view exchange_code = order_link_exchange_code(exchange);
    size_t pos = 0;
    auto append = [&](std::string_view value) {
      const size_t available = buffer.size() - pos;
      const size_t length = std::min(value.size(), available);
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
          message_id);
      if (ec == std::errc()) {
        pos = static_cast<size_t>(ptr - buffer.data());
      }
    }
    append("-");
    append(ticker);
    if (pos > 36) {
      pos = 36;
    }
    return std::string_view(buffer.data(), pos);
  }

  static std::string_view order_link_exchange_code(std::string_view exchange) {
    // Must match the Python poller (ExchangeListingPoller._exchange_code) and the
    // C++ ultra engine (order_link_exchange_code in listing_ultra_engine.cpp),
    // both of which pass the exchange name through verbatim. Emitting the full
    // name ("upbit"->"upbit", "bithumb"->"bithumb") keeps the orderLinkId
    // ("ls-upbit-"/"ls-bithumb-") identical across all three paths so Bybit's
    // duplicate-orderLinkId guard dedupes the same notice across paths.
    return exchange;
  }

  const PreparedOrderRequest& prepare_order_request(
      std::string_view symbol,
      std::string_view order_link_id) const {
    constexpr std::string_view body_prefix = "{\"category\":\"spot\",\"symbol\":\"";
    thread_local PreparedOrderRequest request;
    request.clear_body();
    if (!request.append_body(body_prefix) ||
        !request.append_body(symbol) ||
        !request.append_body(order_body_mid_) ||
        !request.append_body(order_link_id) ||
        !request.append_body("\"}")) {
      return request;
    }
    auth_headers_into(request.body_view(), request.headers);
    return request;
  }

  void warm_request_scratch() const {
    (void)prepare_order_request(
        "XXXXXXXXXXUSDT",
        "ls-b-9223372036854775807-XXXXXXXXXX");
  }

  bool prime_order_client_for_hot_path(CurlClient& client) const {
    const PreparedOrderRequest& request = prepare_order_request(
        "XXXXXXXXXXUSDT",
        "ls-b-9223372036854775807-XXXXXXXXXX");
    return client.prepare_post_order_for_benchmark(request.body_view(), request.headers);
  }

  void auth_headers_into(std::string_view body, AuthHeaders& headers) const {
    constexpr std::string_view sign_prefix = "X-BAPI-SIGN: ";
    constexpr std::string_view timestamp_prefix = "X-BAPI-TIMESTAMP: ";
    std::array<char, 32> timestamp_buffer{};
    const std::string_view timestamp = current_timestamp_ms(timestamp_buffer, timestamp_bias_ms_);
    headers.content_type_header = &content_type_header_;
    headers.api_key_header = &api_key_header_;
    headers.recv_window_header = &recv_window_header_;
    headers.reset_sign_header();
    headers.append_sign_header(sign_prefix);
    append_signature_hex_parts(timestamp, auth_plain_static_, body, headers);
    headers.reset_timestamp_header();
    headers.append_timestamp_header(timestamp_prefix);
    headers.append_timestamp_header(timestamp);
  }

  void append_signature_hex_parts(
      std::string_view first,
      std::string_view second,
      std::string_view third,
      AuthHeaders& headers) const {
    unsigned char inner_digest[SHA256_DIGEST_LENGTH];
    unsigned char digest[SHA256_DIGEST_LENGTH];
    SHA256_CTX ctx = hmac_inner_base_;
    SHA256_Update(&ctx, first.data(), first.size());
    SHA256_Update(&ctx, second.data(), second.size());
    SHA256_Update(&ctx, third.data(), third.size());
    SHA256_Final(inner_digest, &ctx);
    SHA256_CTX outer = hmac_outer_base_;
    SHA256_Update(&outer, inner_digest, sizeof(inner_digest));
    SHA256_Final(digest, &outer);
    headers.append_sign_hex_digest(digest, sizeof(digest));
  }

  void init_hmac_sha256_contexts() {
    SHA256_Init(&hmac_inner_base_);
    SHA256_Update(&hmac_inner_base_, hmac_ipad_.data(), hmac_ipad_.size());
    SHA256_Init(&hmac_outer_base_);
    SHA256_Update(&hmac_outer_base_, hmac_opad_.data(), hmac_opad_.size());
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
  std::string order_body_mid_;
  HmacPad hmac_ipad_{};
  HmacPad hmac_opad_{};
  SHA256_CTX hmac_inner_base_{};
  SHA256_CTX hmac_outer_base_{};
  bool buy_enabled_{false};
  bool timing_enabled_{false};
  bool config_ready_{false};
  bool order_preflight_only_{false};
  bool order_client_keepwarm_enabled_{true};
  bool immediate_keepwarm_refresh_enabled_{true};
  bool blocking_hot_order_warmup_enabled_{false};
  bool order_on_cache_miss_{false};
  bool async_order_dispatch_enabled_{false};
  bool worker_spin_wait_enabled_{false};
  int worker_spin_count_{0};
  int order_start_spin_count_{64};
  bool skip_order_start_signal_for_self_test_{false};
  bool worker_read_delay_for_self_test_{false};
  std::string buy_quote_amount_;
  bool buy_quote_amount_valid_{false};
  int keepwarm_interval_sec_{15};
  int symbol_refresh_interval_sec_{15};
  int parallel_keepwarm_client_count_{4};
  std::string symbol_cache_path_;
  int symbol_cache_max_age_sec_{300};
  int symbol_cache_min_count_{100};
  CurlClient client_;
  CurlClient market_client_;
  std::array<std::unique_ptr<CurlClient>, MAX_LISTING_TICKERS> parallel_clients_;
  std::array<NativeBuyWorkerSlot, MAX_LISTING_TICKERS> worker_slots_;
  std::array<std::thread, MAX_LISTING_TICKERS> worker_threads_;
  std::array<std::atomic<bool>, MAX_LISTING_TICKERS> worker_ready_{};
  std::atomic<bool> worker_pool_ready_{false};
  std::thread keepwarm_thread_;
  std::mutex refresh_mu_;
  std::mutex spot_symbol_snapshots_mu_;
  std::mutex hot_order_client_snapshots_mu_;
  std::mutex hot_parallel_order_client_snapshots_mu_;
  std::mutex keepwarm_mu_;
  std::mutex warmup_state_mu_;
  std::condition_variable keepwarm_cv_;
  std::condition_variable warmup_state_cv_;
  std::atomic<bool> active_{false};
  std::atomic<bool> ready_{false};
  std::atomic<bool> workers_started_{false};
  std::atomic<bool> keepwarm_started_{false};
  std::atomic<bool> stop_keepwarm_{false};
  bool warmup_in_progress_{false};
  std::atomic<bool> order_client_warmed_{false};
  std::atomic<bool> parallel_clients_warmed_{false};
  std::atomic<bool> blocking_hot_order_warmup_done_{false};
  std::atomic<CurlClient*> hot_order_client_raw_{nullptr};
  std::array<std::atomic<CurlClient*>, MAX_LISTING_TICKERS> hot_parallel_order_client_raw_{};
  std::atomic<const SpotSymbolSet*> spot_symbols_raw_{nullptr};
  std::vector<std::shared_ptr<CurlClient>> hot_order_client_snapshots_;
  std::array<std::vector<std::shared_ptr<CurlClient>>, MAX_LISTING_TICKERS>
      hot_parallel_order_client_snapshots_;
  std::vector<std::shared_ptr<const SpotSymbolSet>> spot_symbol_snapshots_;
};

// Derive the bare ticker from the order symbol (always "{TICKER}USDT" here) so
// native trade events carry an explicit ticker. Python aligns multi-ticker
// trade proofs by ticker; without it, alignment is positional and a divergent
// extraction order could attach a proof to the wrong ticker. See [13].
std::string ticker_from_symbol(const std::string& symbol) {
  constexpr std::string_view quote = "USDT";
  if (symbol.size() > quote.size() &&
      symbol.compare(symbol.size() - quote.size(), quote.size(), quote) == 0) {
    return symbol.substr(0, symbol.size() - quote.size());
  }
  return symbol;
}

std::string native_trade_json(const NativeTradeResult& trade) {
  const long long elapsed_ns = std::max(
      0LL,
      trade.trade_finished_monotonic_ns - trade.trade_started_monotonic_ns);
  const long long order_prepare_elapsed_ns =
      trade.order_send_started_monotonic_ns > 0 && trade.trade_started_monotonic_ns > 0
          ? std::max(0LL, trade.order_send_started_monotonic_ns - trade.trade_started_monotonic_ns)
          : 0LL;
  std::string out = "{";
  out += "\"enabled\":";
  out += trade.enabled ? "true" : "false";
  out += ",\"attempted\":";
  out += trade.attempted ? "true" : "false";
  out += ",\"executed\":";
  out += trade.executed ? "true" : "false";
  out += ",\"ret_code\":" + std::to_string(trade.ret_code);
  out += ",\"symbol\":\"" + json_escape(trade.symbol) + "\"";
  out += ",\"ticker\":\"" + json_escape(ticker_from_symbol(trade.symbol)) + "\"";
  out += ",\"order_id\":\"" + json_escape(trade.order_id) + "\"";
  out += ",\"order_link_id\":\"" + json_escape(trade.order_link_id) + "\"";
  out += ",\"transport\":\"" + json_escape(trade.transport) + "\"";
  out += ",\"reason\":\"" + json_escape(trade.reason) + "\"";
  out += ",\"trade_started_monotonic_ns\":" + std::to_string(trade.trade_started_monotonic_ns);
  out += ",\"order_send_started_monotonic_ns\":" + std::to_string(trade.order_send_started_monotonic_ns);
  out += ",\"order_prepare_elapsed_ns\":" + std::to_string(order_prepare_elapsed_ns);
  out += ",\"order_prepare_elapsed_us\":" + std::to_string(order_prepare_elapsed_ns / 1000.0);
  out += ",\"order_prepare_elapsed_ms\":" + std::to_string(order_prepare_elapsed_ns / 1000000.0);
  out += ",\"trade_finished_monotonic_ns\":" + std::to_string(trade.trade_finished_monotonic_ns);
  out += ",\"trade_elapsed_ns\":" + std::to_string(elapsed_ns);
  out += ",\"trade_elapsed_us\":" + std::to_string(elapsed_ns / 1000.0);
  out += ",\"trade_elapsed_ms\":" + std::to_string(elapsed_ns / 1000000.0);
  out += "}";
  return out;
}

std::string native_trades_json(
    const std::array<std::optional<NativeTradeResult>, MAX_LISTING_TICKERS>& trades,
    size_t count) {
  std::string out = "[";
  for (size_t i = 0; i < count; ++i) {
    if (i != 0) {
      out += ",";
    }
    out += native_trade_json(*trades[i]);
  }
  out += "]";
  return out;
}

void write_native_trade_json(std::ostream& out, const NativeTradeResult& trade) {
  const long long elapsed_ns = std::max(
      0LL,
      trade.trade_finished_monotonic_ns - trade.trade_started_monotonic_ns);
  const long long order_prepare_elapsed_ns =
      trade.order_send_started_monotonic_ns > 0 && trade.trade_started_monotonic_ns > 0
          ? std::max(0LL, trade.order_send_started_monotonic_ns - trade.trade_started_monotonic_ns)
          : 0LL;
  out << "{\"enabled\":" << (trade.enabled ? "true" : "false")
      << ",\"attempted\":" << (trade.attempted ? "true" : "false")
      << ",\"executed\":" << (trade.executed ? "true" : "false")
      << ",\"ret_code\":" << trade.ret_code
      << ",\"symbol\":";
  write_json_string(out, trade.symbol);
  out << ",\"ticker\":";
  write_json_string(out, ticker_from_symbol(trade.symbol));
  out << ",\"order_id\":";
  write_json_string(out, trade.order_id);
  out << ",\"order_link_id\":";
  write_json_string(out, trade.order_link_id);
  out << ",\"transport\":";
  write_json_string(out, trade.transport);
  out << ",\"reason\":";
  write_json_string(out, trade.reason);
  out << ",\"trade_started_monotonic_ns\":" << trade.trade_started_monotonic_ns
      << ",\"order_send_started_monotonic_ns\":" << trade.order_send_started_monotonic_ns
      << ",\"order_prepare_elapsed_ns\":" << order_prepare_elapsed_ns
      << ",\"order_prepare_elapsed_us\":" << (order_prepare_elapsed_ns / 1000.0)
      << ",\"order_prepare_elapsed_ms\":" << (order_prepare_elapsed_ns / 1000000.0)
      << ",\"trade_finished_monotonic_ns\":" << trade.trade_finished_monotonic_ns
      << ",\"trade_elapsed_ns\":" << elapsed_ns
      << ",\"trade_elapsed_us\":" << (elapsed_ns / 1000.0)
      << ",\"trade_elapsed_ms\":" << (elapsed_ns / 1000000.0)
      << "}";
}

void write_native_trade_event_json(std::ostream& out, const NativeTradeResult& trade) {
  out << "{\"enabled\":" << (trade.enabled ? "true" : "false")
      << ",\"attempted\":" << (trade.attempted ? "true" : "false")
      << ",\"executed\":" << (trade.executed ? "true" : "false")
      << ",\"ret_code\":" << trade.ret_code
      << ",\"symbol\":";
  write_json_string(out, trade.symbol);
  out << ",\"ticker\":";
  write_json_string(out, ticker_from_symbol(trade.symbol));
  out << ",\"order_id\":";
  write_json_string(out, trade.order_id);
  out << ",\"order_link_id\":";
  write_json_string(out, trade.order_link_id);
  out << ",\"transport\":";
  write_json_string(out, trade.transport);
  out << ",\"reason\":";
  write_json_string(out, trade.reason);
  if (trade.trade_started_monotonic_ns > 0) {
    out << ",\"trade_started_monotonic_ns\":" << trade.trade_started_monotonic_ns;
  }
  if (trade.order_send_started_monotonic_ns > 0) {
    out << ",\"order_send_started_monotonic_ns\":" << trade.order_send_started_monotonic_ns;
  }
  if (trade.trade_finished_monotonic_ns > 0) {
    out << ",\"trade_finished_monotonic_ns\":" << trade.trade_finished_monotonic_ns;
  }
  out << "}";
}

void write_native_trades_json(
    std::ostream& out,
    const std::array<std::optional<NativeTradeResult>, MAX_LISTING_TICKERS>& trades,
    size_t count) {
  out.put('[');
  for (size_t i = 0; i < count; ++i) {
    if (i != 0) {
      out.put(',');
    }
    write_native_trade_json(out, *trades[i]);
  }
  out.put(']');
}

void write_native_trade_events_json(
    std::ostream& out,
    const std::array<std::optional<NativeTradeResult>, MAX_LISTING_TICKERS>& trades,
    size_t count) {
  out.put('[');
  for (size_t i = 0; i < count; ++i) {
    if (i != 0) {
      out.put(',');
    }
    write_native_trade_event_json(out, *trades[i]);
  }
  out.put(']');
}

int run_native_buy_disabled_self_test() {
  const auto bithumb = classify_listing_title(
      ExchangeId::Bithumb,
      "[마켓 추가] 스타크넷(STRK) 원화 마켓 추가 및 재단 에어드랍 안내");
  if (!bithumb.has_value() || bithumb->ticker != "STRK" ||
      bithumb->tickers.count != 1 ||
      (extract_market_flags("[마켓 추가] 스타크넷(STRK) 원화 마켓 추가 및 재단 에어드랍 안내") & MARKET_FLAG_KRW) == 0) {
    std::cerr << "bithumb_classify_failed" << std::endl;
    return 1;
  }
  const auto bithumb_retrade = classify_listing_title(
      ExchangeId::Bithumb,
      "[마켓 추가] 스타크넷(STRK) 원화 마켓 재거래지원 안내");
  if (bithumb_retrade.has_value()) {
    std::cerr << "bithumb_retrade_should_not_match" << std::endl;
    return 1;
  }

  const auto upbit = classify_listing_title(
      ExchangeId::Upbit,
      "[거래] 베니스토큰(VVV) 신규 거래지원 안내 (KRW 마켓)");
  if (!upbit.has_value() || upbit->ticker != "VVV" ||
      upbit->tickers.count != 1 ||
      (extract_market_flags("[거래] 베니스토큰(VVV) 신규 거래지원 안내 (KRW 마켓)") & MARKET_FLAG_KRW) == 0) {
    std::cerr << "upbit_classify_failed" << std::endl;
    return 1;
  }
  const auto upbit_btc = classify_listing_title(
      ExchangeId::Upbit,
      "[거래] 테스트토큰(TTT) 신규 거래지원 안내 (BTC 마켓)");
  if (upbit_btc.has_value()) {
    std::cerr << "upbit_btc_market_should_not_match" << std::endl;
    return 1;
  }

  BybitNativeBuyer buyer;
  buyer.set_active(true);
  const auto trade = buyer.buy_listing("bithumb", 123456789, bithumb->ticker);
  if (trade.attempted || trade.executed || trade.symbol != "STRKUSDT" ||
      trade.reason != "buy_disabled") {
    std::cerr << "native_disabled_gate_failed " << native_trade_json(trade) << std::endl;
    return 1;
  }

  std::cout << "SELFTEST_NATIVE_BUY_DISABLED_OK "
            << native_trade_json(trade) << std::endl;
  return 0;
}

int run_order_response_buffer_self_test() {
  OrderHttpResult response;
  const std::string chunk1 = R"({"retCode":0,"retMsg":"OK","result":{)";
  const std::string chunk2 = R"("orderId":"abc-123"}})";
  if (order_write_callback(
          const_cast<char*>(chunk1.data()),
          1,
          chunk1.size(),
          &response) != chunk1.size() ||
      order_write_callback(
          const_cast<char*>(chunk2.data()),
          1,
          chunk2.size(),
          &response) != chunk2.size()) {
    std::cerr << "order_response_buffer_callback_failed" << std::endl;
    return 1;
  }
  const std::string_view body = response.body_view();
  const long long ret_code = extract_json_int_pattern(body, "\"retCode\":").value_or(-1);
  const std::string order_id = extract_json_string(body, "orderId").value_or("");
  if (response.body_truncated || ret_code != 0 || order_id != "abc-123") {
    std::cerr << "order_response_buffer_self_test_failed"
              << " truncated=" << response.body_truncated
              << " ret_code=" << ret_code
              << " order_id=" << order_id
              << " body=" << std::string(body)
              << std::endl;
    return 1;
  }
  std::cout << "SELFTEST_ORDER_RESPONSE_BUFFER_OK" << std::endl;
  return 0;
}

int run_native_invalid_quote_amount_self_test() {
  setenv("BYBIT_SPOT_BUY_ENABLED", "1", 1);
  setenv("BYBIT_API_KEY", "test-key", 1);
  setenv("BYBIT_API_SECRET", "test-secret", 1);
  setenv("BYBIT_SPOT_BUY_USDT_AMOUNT", "-5", 1);
  setenv("LISTING_TDLIB_NATIVE_ORDER_ON_CACHE_MISS", "1", 1);
  setenv("LISTING_TDLIB_NATIVE_ORDER_CLIENT_KEEPWARM_ENABLED", "0", 1);

  BybitNativeBuyer buyer;
  const bool ready = buyer.set_active(true);
  const NativeTradeResult trade = buyer.buy_listing("bithumb", 321987, "STRK");
  if (ready ||
      buyer.readiness_reason() != "quote_amount_invalid" ||
      trade.attempted ||
      trade.executed ||
      trade.reason != "quote_amount_invalid" ||
      trade.symbol != "STRKUSDT") {
    std::cerr << "native_invalid_quote_amount_self_test_failed"
              << " ready=" << ready
              << " reason=" << buyer.readiness_reason()
              << " trade=" << native_trade_json(trade)
              << std::endl;
    return 1;
  }

  std::cout << "SELFTEST_NATIVE_INVALID_QUOTE_AMOUNT_OK "
            << native_trade_json(trade) << std::endl;
  return 0;
}

int run_native_order_file_scheme_self_test() {
  setenv("BYBIT_SPOT_BUY_ENABLED", "1", 1);
  setenv("BYBIT_API_KEY", "test-key", 1);
  setenv("BYBIT_API_SECRET", "test-secret", 1);
  setenv("BYBIT_SPOT_BUY_USDT_AMOUNT", "5", 1);
  setenv("LISTING_TDLIB_NATIVE_ORDER_ON_CACHE_MISS", "0", 1);
  setenv("LISTING_TDLIB_NATIVE_ORDER_CLIENT_KEEPWARM_ENABLED", "0", 1);
  setenv("LISTING_TDLIB_NATIVE_ASYNC_ORDER_DISPATCH", "0", 1);
  setenv("LISTING_TDLIB_NATIVE_TIMING_ENABLED", "1", 1);

  const std::filesystem::path root =
      std::filesystem::temp_directory_path() /
      ("tdlib_native_order_file_" + std::to_string(monotonic_now_ns()));
  const std::filesystem::path order_dir = root / "v5" / "order";
  std::error_code ec;
  std::filesystem::create_directories(order_dir, ec);
  if (ec) {
    std::cerr << "native_order_file_scheme_create_dir_failed " << ec.message() << std::endl;
    return 1;
  }
  {
    std::ofstream output(order_dir / "create");
    output << R"({"retCode":0,"retMsg":"OK","result":{"orderId":"file-order-1"}})";
  }
  const std::string base_url = "file://" + root.string();
  setenv("BYBIT_API_BASE_URL", base_url.c_str(), 1);

  BybitNativeBuyer buyer;
  buyer.inject_spot_symbol_for_self_test("STRKUSDT");
  buyer.activate_workers_for_self_test();
  const NativeTradeResult trade = buyer.buy_listing("bithumb", 321987, "STRK");
  std::filesystem::remove_all(root, ec);

  if (!trade.enabled ||
      !trade.attempted ||
      !trade.executed ||
      trade.ret_code != 0 ||
      trade.reason != "tdlib_native_rest" ||
      trade.symbol != "STRKUSDT" ||
      trade.order_id != "file-order-1" ||
      trade.order_link_id != "ls-bithumb-321987-STRK" ||
      trade.trade_started_monotonic_ns <= 0 ||
      trade.order_send_started_monotonic_ns <= 0 ||
      trade.trade_finished_monotonic_ns <= 0 ||
      trade.trade_started_monotonic_ns > trade.order_send_started_monotonic_ns ||
      trade.order_send_started_monotonic_ns > trade.trade_finished_monotonic_ns) {
    std::cerr << "native_order_file_scheme_self_test_failed "
              << native_trade_json(trade) << std::endl;
    return 1;
  }

  std::cout << "SELFTEST_NATIVE_ORDER_FILE_SCHEME_OK "
            << native_trade_json(trade) << std::endl;
  return 0;
}

WatchChatSet parse_watch_map(const std::string& spec) {
  WatchChatSet out;
  size_t start = 0;
  while (start < spec.size()) {
    size_t end = spec.find(',', start);
    const std::string item = spec.substr(start, end == std::string::npos ? std::string::npos : end - start);
    const size_t sep = item.find(':');
    if (sep != std::string::npos) {
      try {
        const long long chat_id = std::stoll(item.substr(0, sep));
        out.upsert(chat_id, item.substr(sep + 1));
      } catch (...) {
      }
    }
    if (end == std::string::npos) {
      break;
    }
    start = end + 1;
  }
  return out;
}

bool maybe_emit_listing_matched(
    std::string_view result,
    const WatchChatSet& watched_chats,
    bool native_listing_mode,
    bool native_buy_mode,
    BybitNativeBuyer& native_buyer,
    bool known_update_new_message = false,
    long long relay_received_monotonic_ns = 0,
    const std::atomic<bool>* native_buy_mode_live = nullptr,
    NativeMessageDeduper* native_deduper = nullptr,
    bool flush_listing_event = true,
    bool emit_listing_event = true,
    bool emit_selftest_status = false) {
  if (!native_listing_mode) {
    return false;
  }
  if (relay_received_monotonic_ns <= 0) {
    relay_received_monotonic_ns = monotonic_now_ns();
  }
  if (!known_update_new_message && !has_json_type(result, "updateNewMessage")) {
    return false;
  }
  TdlibMessageHeader header;
  const bool has_header = extract_tdlib_message_header_into(result, header);
  long long chat_id = 0;
  long long header_message_id = 0;
  size_t content_search_pos = 0;
  if (has_header) {
    chat_id = header.chat_id;
    header_message_id = header.message_id;
    content_search_pos = header.after_chat_pos;
  } else {
    const auto parsed_chat_id = extract_json_int_pattern(result, "\"chat_id\":");
    if (!parsed_chat_id.has_value()) {
      return false;
    }
    chat_id = *parsed_chat_id;
  }
  const WatchChat* watch = watched_chats.find(chat_id);
  if (watch == nullptr) {
    return false;
  }
  const std::string& handle = watch->handle;
  if (watch->exchange_id == ExchangeId::Unknown) {
    return false;
  }

  size_t content_pos = has_header ? header.content_pos : std::string_view::npos;
  if (content_pos == std::string_view::npos) {
    content_pos = result.find("\"content\":", content_search_pos);
  }
  if (content_pos == std::string_view::npos) {
    return false;
  }
  std::optional<std::string> text_storage;
  auto title_view = extract_tdlib_message_title_view(result, content_pos);
  if (!title_view.has_value()) {
    text_storage = extract_tdlib_message_text(result, content_pos);
    if (!text_storage.has_value()) {
      return false;
    }
    title_view = first_line_view(*text_storage);
  }
  const std::string_view title = trim_ascii_view(*title_view);
  if (title.empty()) {
    return false;
  }
  ListingMatch listing;
  if (!classify_listing_title_into(watch->exchange_id, title, listing)) {
    return true;
  }

  long long message_id = header_message_id;
  if (!has_header) {
    const auto parsed_message_id = extract_json_int_pattern(result, "\"id\":");
    if (!parsed_message_id.has_value()) {
      return true;
    }
    message_id = *parsed_message_id;
  }
  const auto& tickers = listing.tickers;
  std::optional<NativeTradeResult> single_native_trade;
  std::optional<std::array<std::optional<NativeTradeResult>, MAX_LISTING_TICKERS>> native_trades;
  size_t native_trade_count = 0;
  std::optional<std::array<NativeDispatchResult, MAX_LISTING_TICKERS>> native_dispatches;
  size_t native_dispatch_count = 0;
  size_t native_dispatch_attempt_count = 0;
  size_t native_dispatch_fallback_count = 0;
  const bool should_native_buy = native_buy_mode_live == nullptr
      ? native_buy_mode
      : native_buy_mode_live->load(std::memory_order_relaxed);
  if (should_native_buy) {
    if (native_deduper != nullptr &&
        !native_deduper->claim(watch->exchange_id, message_id)) {
      return true;
    }
    if (!emit_listing_event) {
      if (emit_selftest_status) {
        native_dispatches.emplace();
      }
      if (tickers.count == 1) {
        const NativeDispatchResult dispatch = native_buyer.dispatch_listing_async(
            listing.order_link_exchange,
            message_id,
            tickers.front());
        if (emit_selftest_status) {
          (*native_dispatches)[native_dispatch_count++] = dispatch;
          if (dispatch.dispatched) {
            ++native_dispatch_attempt_count;
          }
        }
        if (dispatch.no_worker) {
          (void)native_buyer.buy_listing(
              listing.order_link_exchange,
              message_id,
              tickers.front());
          if (emit_selftest_status) {
            ++native_dispatch_fallback_count;
            ++native_dispatch_attempt_count;
          }
        }
      } else {
        for (size_t i = 0; i < tickers.count; ++i) {
          const NativeDispatchResult dispatch = native_buyer.dispatch_listing_async(
              listing.order_link_exchange,
              message_id,
              tickers.values[i],
              i);
          if (emit_selftest_status) {
            (*native_dispatches)[native_dispatch_count++] = dispatch;
            if (dispatch.dispatched) {
              ++native_dispatch_attempt_count;
            }
          }
          if (dispatch.no_worker) {
            (void)native_buyer.buy_listing(
                listing.order_link_exchange,
                message_id,
                tickers.values[i],
                i);
            if (emit_selftest_status) {
              ++native_dispatch_fallback_count;
              ++native_dispatch_attempt_count;
            }
          }
        }
      }
    }
    if (emit_listing_event && tickers.count == 1) {
      single_native_trade.emplace(native_buyer.buy_listing(
          listing.order_link_exchange,
          message_id,
          tickers.front()));
    } else if (emit_listing_event) {
      native_trades.emplace();
      native_trade_count = native_buyer.buy_listings(
          listing.order_link_exchange,
          message_id,
          tickers,
          *native_trades);
    }
  }
  if (!emit_listing_event) {
    if (emit_selftest_status) {
      std::ostream& out = std::cout;
      out << relay_received_monotonic_ns << '\t'
          << "{\"@type\":\"selftestUpdateStatus\","
          << "\"consumed\":true,"
          << "\"relay_received_monotonic_ns\":" << relay_received_monotonic_ns
          << ",\"channel_handle\":";
      write_json_string(out, handle);
      out << ",\"message_id\":" << message_id
          << ",\"title\":";
      write_json_string(out, title);
      out << ",\"ticker\":";
      write_json_string(out, listing.ticker);
      out << ",\"tickers\":";
      write_tickers_json(out, tickers);
      out << ",\"native_dispatch_attempt_count\":" << native_dispatch_attempt_count
          << ",\"native_dispatch_fallback_count\":" << native_dispatch_fallback_count
          << ",\"native_dispatches\":[";
      for (size_t i = 0; i < native_dispatch_count; ++i) {
        if (i != 0) {
          out.put(',');
        }
        write_native_dispatch_json(out, (*native_dispatches)[i], tickers.values[i]);
      }
      out << "]}\n";
      out.flush();
    }
    return true;
  }
  long long published_at_unix = has_header ? header.date_unix : 0;
  if (published_at_unix <= 0) {
    published_at_unix = extract_json_int_pattern(result, "\"date\":").value_or(0);
  }
  std::ostream& out = std::cout;
  out << relay_received_monotonic_ns << '\t'
      << "{\"@type\":\"listingMatched\","
      << "\"relay_received_monotonic_ns\":" << relay_received_monotonic_ns
      << ",\"channel_handle\":";
  write_json_string(out, handle);
  out << ",\"message_id\":" << message_id
      << ",\"published_at_unix\":" << published_at_unix
      << ",\"title\":";
  write_json_string(out, title);
  out << ",\"ticker\":";
  write_json_string(out, listing.ticker);
  out << ",\"tickers\":";
  write_tickers_json(out, tickers);
  if (single_native_trade.has_value()) {
    out << ",\"native_trades\":[";
    write_native_trade_event_json(out, *single_native_trade);
    out << "]";
  } else if (native_trade_count != 0 && native_trades.has_value()) {
    out << ",\"native_trades\":";
    write_native_trade_events_json(out, *native_trades, native_trade_count);
  }
  out << "}\n";
  if (flush_listing_event) {
    out.flush();
  }
  return true;
}

bool maybe_native_buy_preflight_from_tdlib_for_benchmark(
    std::string_view result,
    const WatchChatSet& watched_chats,
    BybitNativeBuyer& native_buyer,
    bool known_update_new_message = false) {
  if (!known_update_new_message && !has_json_type(result, "updateNewMessage")) {
    return false;
  }
  TdlibMessageHeader header;
  const bool has_header = extract_tdlib_message_header_into(result, header);
  long long chat_id = 0;
  long long header_message_id = 0;
  size_t content_search_pos = 0;
  if (has_header) {
    chat_id = header.chat_id;
    header_message_id = header.message_id;
    content_search_pos = header.after_chat_pos;
  } else {
    const auto parsed_chat_id = extract_json_int_pattern(result, "\"chat_id\":");
    if (!parsed_chat_id.has_value()) {
      return false;
    }
    chat_id = *parsed_chat_id;
  }
  const WatchChat* watch = watched_chats.find(chat_id);
  if (watch == nullptr || watch->exchange_id == ExchangeId::Unknown) {
    return false;
  }

  size_t content_pos = has_header ? header.content_pos : std::string_view::npos;
  if (content_pos == std::string_view::npos) {
    content_pos = result.find("\"content\":", content_search_pos);
  }
  if (content_pos == std::string_view::npos) {
    return false;
  }
  std::optional<std::string> text_storage;
  auto title_view = extract_tdlib_message_title_view(result, content_pos);
  if (!title_view.has_value()) {
    text_storage = extract_tdlib_message_text(result, content_pos);
    if (!text_storage.has_value()) {
      return false;
    }
    title_view = first_line_view(*text_storage);
  }
  ListingMatch listing;
  const std::string_view title = trim_ascii_view(*title_view);
  if (!classify_listing_title_into(watch->exchange_id, title, listing)) {
    return false;
  }

  long long message_id = header_message_id;
  if (!has_header) {
    const auto parsed_message_id = extract_json_int_pattern(result, "\"id\":");
    if (!parsed_message_id.has_value()) {
      return false;
    }
    message_id = *parsed_message_id;
  }

  if (listing.tickers.count == 1) {
    const auto trade = native_buyer.buy_listing(
        listing.order_link_exchange,
        message_id,
        listing.tickers.front());
    return trade.attempted &&
           trade.ret_code == 0 &&
           trade.reason == "tdlib_native_rest_preflight";
  }

  std::array<std::optional<NativeTradeResult>, MAX_LISTING_TICKERS> native_trades;
  const size_t native_trade_count = native_buyer.buy_listings(
      listing.order_link_exchange,
      message_id,
      listing.tickers,
      native_trades);
  return native_trade_count != 0 &&
         native_trades[0].has_value() &&
         native_trades[0]->attempted &&
         native_trades[0]->ret_code == 0 &&
         native_trades[0]->reason == "tdlib_native_rest_preflight";
}

int run_tdlib_message_disabled_self_test() {
  WatchChatSet watched_chats;
  watched_chats.upsert(777001, "BithumbExchange");
  BybitNativeBuyer buyer;
  buyer.set_active(true);
  const std::string payload =
      R"({"@type":"updateNewMessage","message":{"@type":"message","id":321987,"chat_id":777001,"date":1778680000,"content":{"@type":"messageText","text":{"@type":"formattedText","text":"[마켓 추가] 스타크넷(STRK) 원화 마켓 추가 및 재단 에어드랍 안내","entities":[]}}}})";

  std::ostringstream captured;
  auto* previous = std::cout.rdbuf(captured.rdbuf());
  const bool consumed = maybe_emit_listing_matched(
      payload,
      watched_chats,
      true,
      true,
      buyer);
  std::cout.rdbuf(previous);

  const std::string output = captured.str();
  if (!consumed ||
      output.find("\"@type\":\"listingMatched\"") == std::string::npos ||
      output.find("\"ticker\":\"STRK\"") == std::string::npos ||
      output.find("\"native_trades\"") == std::string::npos ||
      output.find("\"attempted\":false") == std::string::npos ||
      output.find("\"reason\":\"buy_disabled\"") == std::string::npos) {
    std::cerr << "tdlib_message_disabled_self_test_failed " << output << std::endl;
    return 1;
  }

  const std::string multi_payload =
      R"({"@type":"updateNewMessage","message":{"@type":"message","id":321988,"chat_id":777001,"date":1778680001,"content":{"@type":"messageText","text":{"@type":"formattedText","text":"[마켓 추가] 센티언트(SENT), 헤이엘사(ELSA) 원화 마켓 추가","entities":[]}}}})";

  std::ostringstream multi_captured;
  previous = std::cout.rdbuf(multi_captured.rdbuf());
  const bool multi_consumed = maybe_emit_listing_matched(
      multi_payload,
      watched_chats,
      true,
      true,
      buyer);
  std::cout.rdbuf(previous);

  const std::string multi_output = multi_captured.str();
  if (!multi_consumed ||
      multi_output.find("\"tickers\":[\"SENT\",\"ELSA\"]") == std::string::npos ||
      multi_output.find("\"native_trades\"") == std::string::npos ||
      multi_output.find("\"symbol\":\"SENTUSDT\"") == std::string::npos ||
      multi_output.find("\"symbol\":\"ELSAUSDT\"") == std::string::npos ||
      multi_output.find("\"attempted\":true") != std::string::npos) {
    std::cerr << "tdlib_multi_ticker_self_test_failed " << multi_output << std::endl;
    return 1;
  }

  std::cout << "SELFTEST_TDLIB_MESSAGE_DISABLED_OK " << output
            << "SELFTEST_TDLIB_MULTI_TICKER_OK " << multi_output;
  return 0;
}

int run_tdlib_message_emit_disabled_self_test() {
  WatchChatSet watched_chats;
  watched_chats.upsert(777001, "BithumbExchange");
  BybitNativeBuyer buyer;
  buyer.set_active(true);
  const std::string payload =
      R"({"@type":"updateNewMessage","message":{"@type":"message","id":321987,"chat_id":777001,"date":1778680000,"content":{"@type":"messageText","text":{"@type":"formattedText","text":"[마켓 추가] 스타크넷(STRK) 원화 마켓 추가 및 재단 에어드랍 안내","entities":[]}}}})";

  std::ostringstream captured;
  auto* previous = std::cout.rdbuf(captured.rdbuf());
  const bool consumed = maybe_emit_listing_matched(
      payload,
      watched_chats,
      true,
      true,
      buyer,
      false,
      0,
      nullptr,
      nullptr,
      false,
      false);
  std::cout.rdbuf(previous);

  const std::string output = captured.str();
  if (!consumed || !output.empty()) {
    std::cerr << "tdlib_message_emit_disabled_self_test_failed "
              << "consumed=" << consumed
              << " output=" << output << std::endl;
    return 1;
  }

  std::cout << "SELFTEST_TDLIB_MESSAGE_EMIT_DISABLED_OK" << std::endl;
  return 0;
}

int run_native_message_dedup_self_test() {
  setenv("BYBIT_SPOT_BUY_ENABLED", "1", 1);
  setenv("BYBIT_API_KEY", "test-key", 1);
  setenv("BYBIT_API_SECRET", "test-secret", 1);
  setenv("BYBIT_SPOT_BUY_USDT_AMOUNT", "5", 1);
  setenv("LISTING_TDLIB_NATIVE_ORDER_CLIENT_KEEPWARM_ENABLED", "0", 1);

  WatchChatSet watched_chats;
  watched_chats.upsert(777001, "BithumbExchange");
  BybitNativeBuyer buyer;
  buyer.inject_spot_symbol_for_self_test("STRKUSDT");
  buyer.enable_order_preflight_for_benchmark();
  NativeMessageDeduper deduper;
  const std::string payload =
      R"({"@type":"updateNewMessage","message":{"@type":"message","id":321987,"chat_id":777001,"date":1778680000,"content":{"@type":"messageText","text":{"@type":"formattedText","text":"[마켓 추가] 스타크넷(STRK) 원화 마켓 추가 및 재단 에어드랍 안내","entities":[]}}}})";

  std::ostringstream first_captured;
  auto* previous = std::cout.rdbuf(first_captured.rdbuf());
  const bool first_consumed = maybe_emit_listing_matched(
      payload,
      watched_chats,
      true,
      true,
      buyer,
      true,
      monotonic_now_ns(),
      nullptr,
      &deduper);
  std::cout.rdbuf(previous);

  std::ostringstream second_captured;
  previous = std::cout.rdbuf(second_captured.rdbuf());
  const bool second_consumed = maybe_emit_listing_matched(
      payload,
      watched_chats,
      true,
      true,
      buyer,
      true,
      monotonic_now_ns(),
      nullptr,
      &deduper);
  std::cout.rdbuf(previous);

  const std::string first_output = first_captured.str();
  const std::string second_output = second_captured.str();
  if (!first_consumed ||
      first_output.find("\"@type\":\"listingMatched\"") == std::string::npos ||
      first_output.find("\"attempted\":true") == std::string::npos ||
      first_output.find("\"ret_code\":0") == std::string::npos ||
      !second_consumed ||
      !second_output.empty()) {
    std::cerr << "native_message_dedup_self_test_failed"
              << " first_consumed=" << first_consumed
              << " second_consumed=" << second_consumed
              << " first=" << first_output
              << " second=" << second_output
              << std::endl;
    return 1;
  }

  std::cout << "SELFTEST_NATIVE_MESSAGE_DEDUP_OK" << std::endl;
  return 0;
}

int run_native_worker_pool_self_test() {
  setenv("BYBIT_SPOT_BUY_ENABLED", "1", 1);
  setenv("BYBIT_API_KEY", "test-key", 1);
  setenv("BYBIT_API_SECRET", "test-secret", 1);
  setenv("BYBIT_SPOT_BUY_USDT_AMOUNT", "5", 1);
  setenv("LISTING_TDLIB_NATIVE_ORDER_ON_CACHE_MISS", "0", 1);

  BybitNativeBuyer buyer;
  buyer.activate_workers_for_self_test();

  ListingTickers tickers;
  tickers.push_unique("SENT");
  tickers.push_unique("ELSA");
  std::array<std::optional<NativeTradeResult>, MAX_LISTING_TICKERS> trades;
  auto check_buy_listings = [&](long long message_id, const char* label) -> bool {
    for (auto& trade : trades) {
      trade.reset();
    }
    const size_t count = buyer.buy_listings("bithumb", message_id, tickers, trades);
    if (count != 2 ||
        !trades[0].has_value() ||
        !trades[1].has_value() ||
        trades[0]->symbol != "SENTUSDT" ||
        trades[1]->symbol != "ELSAUSDT" ||
        trades[0]->attempted ||
        trades[1]->attempted ||
        trades[0]->reason != "spot_symbol_unavailable" ||
        trades[1]->reason != "spot_symbol_unavailable") {
      std::cerr << "native_worker_pool_self_test_failed"
                << " label=" << label
                << " count=" << count << std::endl;
      return false;
    }
    return true;
  };

  if (!check_buy_listings(321988, "initial")) {
    return 1;
  }
  buyer.set_active(false);
  buyer.activate_workers_for_self_test();
  if (!check_buy_listings(321989, "restart")) {
    return 1;
  }

  std::cout << "SELFTEST_NATIVE_WORKER_POOL_OK "
            << native_trades_json(trades, 2) << std::endl;
  return 0;
}

int run_native_async_order_dispatch_self_test() {
  setenv("BYBIT_SPOT_BUY_ENABLED", "1", 1);
  setenv("BYBIT_API_KEY", "test-key", 1);
  setenv("BYBIT_API_SECRET", "test-secret", 1);
  setenv("BYBIT_SPOT_BUY_USDT_AMOUNT", "5", 1);
  setenv("LISTING_TDLIB_NATIVE_ASYNC_ORDER_DISPATCH", "1", 1);
  setenv("LISTING_TDLIB_NATIVE_TIMING_ENABLED", "1", 1);
  setenv("LISTING_TDLIB_NATIVE_ORDER_CLIENT_KEEPWARM_ENABLED", "0", 1);

  const std::filesystem::path root =
      std::filesystem::temp_directory_path() /
      ("tdlib_async_order_" + std::to_string(monotonic_now_ns()));
  const std::filesystem::path order_path = root / "v5" / "order" / "create";
  std::error_code ec;
  std::filesystem::create_directories(order_path.parent_path(), ec);
  {
    std::ofstream output(order_path);
    output << R"({"retCode":0,"retMsg":"OK","result":{"orderId":"async-file-order-1"}})";
  }
  const std::string base_url = "file://" + root.string();
  setenv("BYBIT_API_BASE_URL", base_url.c_str(), 1);

  BybitNativeBuyer buyer;
  buyer.inject_spot_symbol_for_self_test("STRKUSDT");
  buyer.activate_workers_for_self_test();
  const NativeTradeResult trade = buyer.buy_listing("bithumb", 321987, "STRK");
  std::filesystem::remove_all(root, ec);

  if (!trade.enabled ||
      !trade.attempted ||
      trade.executed ||
      trade.ret_code != -1 ||
      trade.symbol != "STRKUSDT" ||
      trade.order_link_id != "ls-bithumb-321987-STRK" ||
      trade.reason != "tdlib_native_rest_dispatched" ||
      trade.trade_started_monotonic_ns <= 0 ||
      trade.order_send_started_monotonic_ns <= 0 ||
      trade.trade_finished_monotonic_ns != 0) {
    std::cerr << "native_async_order_dispatch_self_test_failed "
              << native_trade_json(trade) << std::endl;
    return 1;
  }

  std::cout << "SELFTEST_NATIVE_ASYNC_ORDER_DISPATCH_OK "
            << native_trade_json(trade) << std::endl;
  return 0;
}

int run_native_async_fire_and_forget_self_test() {
  setenv("BYBIT_SPOT_BUY_ENABLED", "1", 1);
  setenv("BYBIT_API_KEY", "test-key", 1);
  setenv("BYBIT_API_SECRET", "test-secret", 1);
  setenv("BYBIT_SPOT_BUY_USDT_AMOUNT", "5", 1);
  setenv("LISTING_TDLIB_NATIVE_ASYNC_ORDER_DISPATCH", "1", 1);
  setenv("LISTING_TDLIB_NATIVE_TIMING_ENABLED", "1", 1);
  setenv("LISTING_TDLIB_NATIVE_ORDER_CLIENT_KEEPWARM_ENABLED", "0", 1);

  const std::filesystem::path root =
      std::filesystem::temp_directory_path() /
      ("tdlib_fire_and_forget_order_" + std::to_string(monotonic_now_ns()));
  const std::filesystem::path order_path = root / "v5" / "order" / "create";
  std::error_code ec;
  std::filesystem::create_directories(order_path.parent_path(), ec);
  {
    std::ofstream output(order_path);
    output << R"({"retCode":0,"retMsg":"OK","result":{"orderId":"fire-file-order-1"}})";
  }
  const std::string base_url = "file://" + root.string();
  setenv("BYBIT_API_BASE_URL", base_url.c_str(), 1);

  BybitNativeBuyer buyer;
  buyer.inject_spot_symbol_for_self_test("STRKUSDT");
  buyer.activate_workers_for_self_test();
  const NativeDispatchResult dispatch =
      buyer.dispatch_listing_async("b", 321987, "STRK");
  NativeTradeResult trade;
  if (dispatch.dispatched) {
    trade = buyer.wait_worker_done_copy_for_self_test(
        dispatch.worker_index,
        dispatch.work_seq);
  }
  std::filesystem::remove_all(root, ec);

  if (!dispatch.dispatched ||
      dispatch.no_worker ||
      dispatch.reason != "tdlib_native_rest_fire_and_forget" ||
      !trade.enabled ||
      !trade.attempted ||
      !trade.executed ||
      trade.ret_code != 0 ||
      trade.symbol != "STRKUSDT" ||
      trade.order_link_id != "ls-b-321987-STRK" ||
      trade.order_id != "fire-file-order-1" ||
      trade.reason != "tdlib_native_rest") {
    std::cerr << "native_async_fire_and_forget_self_test_failed"
              << " dispatched=" << dispatch.dispatched
              << " no_worker=" << dispatch.no_worker
              << " reason=" << dispatch.reason
              << " trade=" << native_trade_json(trade)
              << std::endl;
    return 1;
  }

  std::cout << "SELFTEST_NATIVE_ASYNC_FIRE_AND_FORGET_OK "
            << native_trade_json(trade) << std::endl;
  return 0;
}

int run_native_async_fire_and_forget_worker_reclaim_self_test() {
  setenv("BYBIT_SPOT_BUY_ENABLED", "1", 1);
  setenv("BYBIT_API_KEY", "test-key", 1);
  setenv("BYBIT_API_SECRET", "test-secret", 1);
  setenv("BYBIT_SPOT_BUY_USDT_AMOUNT", "5", 1);
  setenv("LISTING_TDLIB_NATIVE_ASYNC_ORDER_DISPATCH", "1", 1);
  setenv("LISTING_TDLIB_NATIVE_TIMING_ENABLED", "1", 1);
  setenv("LISTING_TDLIB_NATIVE_ORDER_CLIENT_KEEPWARM_ENABLED", "0", 1);
  setenv("LISTING_TDLIB_NATIVE_SKIP_ORDER_START_SIGNAL_FOR_SELF_TEST", "1", 1);

  const std::filesystem::path root =
      std::filesystem::temp_directory_path() /
      ("tdlib_fire_reclaim_order_" + std::to_string(monotonic_now_ns()));
  const std::filesystem::path order_path = root / "v5" / "order" / "create";
  std::error_code ec;
  std::filesystem::create_directories(order_path.parent_path(), ec);
  {
    std::ofstream output(order_path);
    output << R"({"retCode":0,"retMsg":"OK","result":{"orderId":"fire-reclaim-order"}})";
  }
  const std::string base_url = "file://" + root.string();
  setenv("BYBIT_API_BASE_URL", base_url.c_str(), 1);

  BybitNativeBuyer buyer;
  buyer.inject_spot_symbol_for_self_test("STRKUSDT");
  buyer.activate_workers_for_self_test();

  const NativeDispatchResult first =
      buyer.dispatch_listing_async("b", 321987, "STRK");
  bool first_reclaimed = false;
  for (int i = 0; i < 100000; ++i) {
    if (!buyer.worker_claimed_for_self_test(0)) {
      first_reclaimed = true;
      break;
    }
    cpu_relax();
  }
  if (!first_reclaimed) {
    for (int i = 0; i < 20; ++i) {
      std::this_thread::sleep_for(std::chrono::milliseconds(1));
      if (!buyer.worker_claimed_for_self_test(0)) {
        first_reclaimed = true;
        break;
      }
    }
  }

  const NativeDispatchResult second =
      buyer.dispatch_listing_async("b", 321988, "STRK", 0);
  bool second_reclaimed = false;
  for (int i = 0; i < 100000; ++i) {
    if (!buyer.worker_claimed_for_self_test(0)) {
      second_reclaimed = true;
      break;
    }
    cpu_relax();
  }
  if (!second_reclaimed) {
    for (int i = 0; i < 20; ++i) {
      std::this_thread::sleep_for(std::chrono::milliseconds(1));
      if (!buyer.worker_claimed_for_self_test(0)) {
        second_reclaimed = true;
        break;
      }
    }
  }
  std::filesystem::remove_all(root, ec);

  if (!first.dispatched ||
      first.worker_index != 0 ||
      !first_reclaimed ||
      !second.dispatched ||
      second.worker_index != 0 ||
      !second_reclaimed) {
    std::cerr << "native_async_fire_and_forget_worker_reclaim_self_test_failed"
              << " first_dispatched=" << first.dispatched
              << " first_worker=" << first.worker_index
              << " first_reclaimed=" << first_reclaimed
              << " second_dispatched=" << second.dispatched
              << " second_worker=" << second.worker_index
              << " second_reclaimed=" << second_reclaimed
              << " second_reason=" << second.reason
              << std::endl;
    return 1;
  }

  std::cout << "SELFTEST_NATIVE_ASYNC_FIRE_AND_FORGET_WORKER_RECLAIM_OK"
            << " first_worker=" << first.worker_index
            << " second_worker=" << second.worker_index
            << std::endl;
  return 0;
}

int run_native_async_ticker_copy_self_test() {
  setenv("BYBIT_SPOT_BUY_ENABLED", "1", 1);
  setenv("BYBIT_API_KEY", "test-key", 1);
  setenv("BYBIT_API_SECRET", "test-secret", 1);
  setenv("BYBIT_SPOT_BUY_USDT_AMOUNT", "5", 1);
  setenv("LISTING_TDLIB_NATIVE_ASYNC_ORDER_DISPATCH", "1", 1);
  setenv("LISTING_TDLIB_NATIVE_TIMING_ENABLED", "1", 1);
  setenv("LISTING_TDLIB_NATIVE_ORDER_CLIENT_KEEPWARM_ENABLED", "0", 1);
  setenv("LISTING_TDLIB_NATIVE_WORKER_READ_DELAY_FOR_SELF_TEST", "1", 1);

  const std::filesystem::path root =
      std::filesystem::temp_directory_path() /
      ("tdlib_ticker_copy_order_" + std::to_string(monotonic_now_ns()));
  const std::filesystem::path order_path = root / "v5" / "order" / "create";
  std::error_code ec;
  std::filesystem::create_directories(order_path.parent_path(), ec);
  {
    std::ofstream output(order_path);
    output << R"({"retCode":0,"retMsg":"OK","result":{"orderId":"ticker-copy-order"}})";
  }
  const std::string base_url = "file://" + root.string();
  setenv("BYBIT_API_BASE_URL", base_url.c_str(), 1);

  BybitNativeBuyer buyer;
  buyer.inject_spot_symbol_for_self_test("STRKUSDT");
  buyer.activate_workers_for_self_test();

  std::string mutable_ticker = "STRK";
  const NativeDispatchResult dispatch =
      buyer.dispatch_listing_async("b", 321987, mutable_ticker);
  mutable_ticker.assign("FAKE");
  NativeTradeResult trade;
  if (dispatch.dispatched) {
    trade = buyer.wait_worker_done_copy_for_self_test(
        dispatch.worker_index,
        dispatch.work_seq);
  }
  std::filesystem::remove_all(root, ec);

  if (!dispatch.dispatched ||
      trade.symbol != "STRKUSDT" ||
      trade.order_link_id != "ls-b-321987-STRK" ||
      trade.order_id != "ticker-copy-order") {
    std::cerr << "native_async_ticker_copy_self_test_failed"
              << " dispatched=" << dispatch.dispatched
              << " reason=" << dispatch.reason
              << " trade=" << native_trade_json(trade)
              << std::endl;
    return 1;
  }

  std::cout << "SELFTEST_NATIVE_ASYNC_TICKER_COPY_OK "
            << native_trade_json(trade) << std::endl;
  return 0;
}

int run_native_buy_wait_for_ready_self_test() {
  setenv("BYBIT_SPOT_BUY_ENABLED", "1", 1);
  setenv("BYBIT_API_KEY", "test-key", 1);
  setenv("BYBIT_API_SECRET", "test-secret", 1);
  setenv("BYBIT_SPOT_BUY_USDT_AMOUNT", "5", 1);

  BybitNativeBuyer buyer;
  buyer.inject_spot_symbol_for_self_test("STRKUSDT");
  buyer.begin_preflight_activation_for_self_test();

  std::optional<NativeTradeResult> trade;
  std::atomic<bool> done{false};
  std::thread order_thread([&]() {
    trade.emplace(buyer.buy_listing("bithumb", 321987, "STRK"));
    done.store(true, std::memory_order_release);
  });

  std::this_thread::sleep_for(std::chrono::milliseconds(20));
  if (done.load(std::memory_order_acquire)) {
    order_thread.join();
    std::cerr << "native_buy_did_not_wait_for_ready" << std::endl;
    return 1;
  }

  buyer.finish_preflight_activation_for_self_test(true);
  order_thread.join();
  if (!trade.has_value() ||
      !trade->enabled ||
      !trade->attempted ||
      trade->ret_code != 0 ||
      trade->reason != "tdlib_native_rest_preflight" ||
      trade->symbol != "STRKUSDT") {
    std::cerr << "native_buy_wait_for_ready_failed "
              << (trade.has_value() ? native_trade_json(*trade) : "{}")
              << std::endl;
    return 1;
  }

  std::cout << "SELFTEST_NATIVE_BUY_WAIT_FOR_READY_OK "
            << native_trade_json(*trade) << std::endl;
  return 0;
}

int run_native_multi_buy_wait_for_ready_self_test() {
  setenv("BYBIT_SPOT_BUY_ENABLED", "1", 1);
  setenv("BYBIT_API_KEY", "test-key", 1);
  setenv("BYBIT_API_SECRET", "test-secret", 1);
  setenv("BYBIT_SPOT_BUY_USDT_AMOUNT", "5", 1);

  BybitNativeBuyer buyer;
  buyer.inject_spot_symbol_for_self_test("SENTUSDT");
  buyer.inject_spot_symbol_for_self_test("ELSAUSDT");
  buyer.begin_preflight_activation_for_self_test();

  ListingTickers tickers;
  tickers.push_unique("SENT");
  tickers.push_unique("ELSA");
  std::array<std::optional<NativeTradeResult>, MAX_LISTING_TICKERS> trades;
  size_t count = 0;
  std::atomic<bool> done{false};
  std::thread order_thread([&]() {
    count = buyer.buy_listings("bithumb", 321988, tickers, trades);
    done.store(true, std::memory_order_release);
  });

  std::this_thread::sleep_for(std::chrono::milliseconds(20));
  if (done.load(std::memory_order_acquire)) {
    order_thread.join();
    std::cerr << "native_multi_buy_did_not_wait_for_ready" << std::endl;
    return 1;
  }

  buyer.finish_preflight_activation_for_self_test(true);
  order_thread.join();
  if (count != 2 ||
      !trades[0].has_value() ||
      !trades[1].has_value() ||
      !trades[0]->attempted ||
      !trades[1]->attempted ||
      trades[0]->ret_code != 0 ||
      trades[1]->ret_code != 0 ||
      trades[0]->reason != "tdlib_native_rest_preflight" ||
      trades[1]->reason != "tdlib_native_rest_preflight" ||
      trades[0]->symbol != "SENTUSDT" ||
      trades[1]->symbol != "ELSAUSDT") {
    std::cerr << "native_multi_buy_wait_for_ready_failed "
              << native_trades_json(trades, count) << std::endl;
    return 1;
  }

  std::cout << "SELFTEST_NATIVE_MULTI_BUY_WAIT_FOR_READY_OK "
            << native_trades_json(trades, count) << std::endl;
  return 0;
}

int run_native_symbol_cache_self_test() {
  setenv("BYBIT_SPOT_BUY_ENABLED", "1", 1);
  setenv("BYBIT_API_KEY", "test-key", 1);
  setenv("BYBIT_API_SECRET", "test-secret", 1);
  setenv("BYBIT_SPOT_BUY_USDT_AMOUNT", "5", 1);
  setenv("LISTING_TDLIB_NATIVE_SYMBOL_CACHE_MAX_AGE_SEC", "3600", 1);
  setenv("LISTING_TDLIB_NATIVE_SYMBOL_CACHE_MIN_COUNT", "1", 1);

  const std::filesystem::path cache_path =
      std::filesystem::temp_directory_path() /
      ("tdlib_spot_symbols_" + std::to_string(monotonic_now_ns()) + ".txt");
  setenv("LISTING_TDLIB_NATIVE_SYMBOL_CACHE_PATH", cache_path.string().c_str(), 1);
  {
    std::ofstream output(cache_path);
    output << "# saved_unix_sec=" << wall_now_sec() << '\n';
    output << "STRKUSDT\n";
  }

  BybitNativeBuyer buyer;
  const bool loaded = buyer.load_spot_symbols_from_cache_for_self_test();
  const bool prepared = buyer.prepare_order_for_benchmark("bithumb", 321987, "STRK");
  buyer.enable_order_preflight_for_benchmark();
  const NativeTradeResult trade = buyer.buy_listing("bithumb", 321987, "STRK");
  std::error_code ec;
  std::filesystem::remove(cache_path, ec);
  if (!loaded ||
      !prepared ||
      !trade.attempted ||
      trade.order_link_id != "ls-bithumb-321987-STRK") {
    std::cerr << "native_symbol_cache_self_test_failed"
              << " loaded=" << loaded
              << " prepared=" << prepared
              << " attempted=" << trade.attempted
              << " order_link_id=" << trade.order_link_id
              << std::endl;
    return 1;
  }

  std::cout << "SELFTEST_NATIVE_SYMBOL_CACHE_OK" << std::endl;
  return 0;
}

int run_native_cache_miss_order_self_test() {
  setenv("BYBIT_SPOT_BUY_ENABLED", "1", 1);
  setenv("BYBIT_API_KEY", "test-key", 1);
  setenv("BYBIT_API_SECRET", "test-secret", 1);
  setenv("BYBIT_SPOT_BUY_USDT_AMOUNT", "5", 1);
  unsetenv("LISTING_TDLIB_NATIVE_ORDER_ON_CACHE_MISS");
  {
    BybitNativeBuyer buyer;
    buyer.inject_spot_symbol_for_self_test("BTCUSDT");
    buyer.enable_order_preflight_for_benchmark();
    const NativeTradeResult trade = buyer.buy_listing("bithumb", 321987, "STRK");
    if (trade.attempted ||
        trade.reason != "spot_symbol_unavailable" ||
        trade.symbol != "STRKUSDT") {
      std::cerr << "native_cache_miss_default_strict_failed "
                << native_trade_json(trade) << std::endl;
      return 1;
    }
  }

  setenv("LISTING_TDLIB_NATIVE_ORDER_ON_CACHE_MISS", "1", 1);
  {
    BybitNativeBuyer buyer;
    buyer.inject_spot_symbol_for_self_test("BTCUSDT");
    buyer.enable_order_preflight_for_benchmark();
    const NativeTradeResult trade = buyer.buy_listing("bithumb", 321987, "STRK");
    if (!trade.attempted ||
        trade.ret_code != 0 ||
        trade.reason != "tdlib_native_rest_preflight" ||
        trade.symbol != "STRKUSDT") {
      std::cerr << "native_cache_miss_order_enabled_failed "
                << native_trade_json(trade) << std::endl;
      return 1;
    }
  }

  setenv("LISTING_TDLIB_NATIVE_ORDER_ON_CACHE_MISS", "0", 1);
  {
    BybitNativeBuyer buyer;
    buyer.inject_spot_symbol_for_self_test("BTCUSDT");
    buyer.enable_order_preflight_for_benchmark();
    const NativeTradeResult trade = buyer.buy_listing("bithumb", 321987, "STRK");
    if (trade.attempted ||
        trade.reason != "spot_symbol_unavailable" ||
        trade.symbol != "STRKUSDT") {
      std::cerr << "native_cache_miss_order_disabled_failed "
                << native_trade_json(trade) << std::endl;
      return 1;
    }
  }

  std::cout << "SELFTEST_NATIVE_CACHE_MISS_ORDER_OK" << std::endl;
  return 0;
}

int run_native_startup_no_network_warmup_self_test() {
  setenv("BYBIT_SPOT_BUY_ENABLED", "1", 1);
  setenv("BYBIT_API_KEY", "test-key", 1);
  setenv("BYBIT_API_SECRET", "test-secret", 1);
  setenv("BYBIT_SPOT_BUY_USDT_AMOUNT", "5", 1);
  setenv("BYBIT_API_BASE_URL", "https://127.0.0.1:1", 1);
  setenv("LISTING_TDLIB_NATIVE_ORDER_ON_CACHE_MISS", "1", 1);
  setenv("LISTING_TDLIB_NATIVE_IMMEDIATE_KEEPWARM_REFRESH", "0", 1);

  const std::filesystem::path cache_path =
      std::filesystem::temp_directory_path() /
      ("tdlib_missing_spot_symbols_" + std::to_string(monotonic_now_ns()) + ".txt");
  setenv("LISTING_TDLIB_NATIVE_SYMBOL_CACHE_PATH", cache_path.string().c_str(), 1);

  BybitNativeBuyer buyer;
  buyer.begin_activation();
  const bool ready = buyer.finish_activation_warmup();
  if (!ready ||
      buyer.readiness_reason() != "ready" ||
      !buyer.workers_ready_for_self_test() ||
      !buyer.hot_parallel_order_client_ready_for_self_test(1)) {
    std::cerr << "native_startup_no_network_warmup_failed"
              << " ready=" << ready
              << " reason=" << buyer.readiness_reason()
              << " workers_ready=" << buyer.workers_ready_for_self_test()
              << " parallel_ready=" << buyer.hot_parallel_order_client_ready_for_self_test(1)
              << std::endl;
    return 1;
  }

  std::cout << "SELFTEST_NATIVE_STARTUP_NO_NETWORK_WARMUP_OK" << std::endl;
  return 0;
}

int run_native_parallel_keepwarm_limit_self_test() {
  setenv("BYBIT_SPOT_BUY_ENABLED", "1", 1);
  setenv("BYBIT_API_KEY", "test-key", 1);
  setenv("BYBIT_API_SECRET", "test-secret", 1);
  setenv("BYBIT_SPOT_BUY_USDT_AMOUNT", "5", 1);

  setenv("LISTING_TDLIB_NATIVE_PARALLEL_KEEPWARM_CLIENTS", "2", 1);
  BybitNativeBuyer limited_buyer;
  if (limited_buyer.parallel_keepwarm_client_count_for_self_test() != 2) {
    std::cerr << "native_parallel_keepwarm_limit_failed limited="
              << limited_buyer.parallel_keepwarm_client_count_for_self_test()
              << std::endl;
    return 1;
  }

  setenv("LISTING_TDLIB_NATIVE_PARALLEL_KEEPWARM_CLIENTS", "999", 1);
  BybitNativeBuyer capped_buyer;
  if (capped_buyer.parallel_keepwarm_client_count_for_self_test() !=
      static_cast<int>(MAX_LISTING_TICKERS)) {
    std::cerr << "native_parallel_keepwarm_cap_failed capped="
              << capped_buyer.parallel_keepwarm_client_count_for_self_test()
              << std::endl;
    return 1;
  }

  unsetenv("LISTING_TDLIB_NATIVE_PARALLEL_KEEPWARM_CLIENTS");
  BybitNativeBuyer default_buyer;
  if (default_buyer.parallel_keepwarm_client_count_for_self_test() != 4) {
    std::cerr << "native_parallel_keepwarm_default_failed default="
              << default_buyer.parallel_keepwarm_client_count_for_self_test()
              << std::endl;
    return 1;
  }

  std::cout << "SELFTEST_NATIVE_PARALLEL_KEEPWARM_LIMIT_OK" << std::endl;
  return 0;
}

int run_hot_order_client_snapshot_self_test() {
  setenv("BYBIT_SPOT_BUY_ENABLED", "1", 1);
  setenv("BYBIT_API_KEY", "test-key", 1);
  setenv("BYBIT_API_SECRET", "test-secret", 1);
  setenv("BYBIT_SPOT_BUY_USDT_AMOUNT", "5", 1);

  BybitNativeBuyer buyer;
  buyer.inject_spot_symbol_for_self_test("STRKUSDT");
  buyer.enable_order_preflight_for_benchmark();
  for (size_t i = 0; i < MAX_HOT_ORDER_CLIENT_SNAPSHOTS + 5; ++i) {
    buyer.inject_hot_order_client_for_self_test();
  }
  const NativeTradeResult trade = buyer.buy_listing("bithumb", 321987, "STRK");
  const size_t snapshot_count = buyer.hot_order_client_snapshot_count_for_self_test();
  if (!buyer.hot_order_client_ready_for_self_test() ||
      snapshot_count != MAX_HOT_ORDER_CLIENT_SNAPSHOTS ||
      !trade.attempted ||
      trade.ret_code != 0 ||
      trade.reason != "tdlib_native_rest_preflight" ||
      trade.symbol != "STRKUSDT") {
    std::cerr << "hot_order_client_snapshot_self_test_failed"
              << " ready=" << buyer.hot_order_client_ready_for_self_test()
              << " snapshots=" << snapshot_count
              << " trade=" << native_trade_json(trade)
              << std::endl;
    return 1;
  }

  std::cout << "SELFTEST_HOT_ORDER_CLIENT_SNAPSHOT_OK "
            << native_trade_json(trade)
            << " snapshots=" << snapshot_count
            << std::endl;
  return 0;
}

int run_hot_parallel_order_client_snapshot_self_test() {
  setenv("BYBIT_SPOT_BUY_ENABLED", "1", 1);
  setenv("BYBIT_API_KEY", "test-key", 1);
  setenv("BYBIT_API_SECRET", "test-secret", 1);
  setenv("BYBIT_SPOT_BUY_USDT_AMOUNT", "5", 1);

  BybitNativeBuyer buyer;
  buyer.inject_spot_symbol_for_self_test("SENTUSDT");
  buyer.inject_spot_symbol_for_self_test("ELSAUSDT");
  buyer.enable_order_preflight_for_benchmark();
  for (size_t i = 0; i < MAX_HOT_ORDER_CLIENT_SNAPSHOTS + 5; ++i) {
    buyer.inject_hot_parallel_order_client_for_self_test(0);
    buyer.inject_hot_parallel_order_client_for_self_test(1);
  }

  ListingTickers tickers;
  tickers.push_unique("SENT");
  tickers.push_unique("ELSA");
  std::array<std::optional<NativeTradeResult>, MAX_LISTING_TICKERS> trades;
  const size_t count = buyer.buy_listings("b", 321988, tickers, trades);
  const size_t first_snapshots =
      buyer.hot_parallel_order_client_snapshot_count_for_self_test(0);
  const size_t second_snapshots =
      buyer.hot_parallel_order_client_snapshot_count_for_self_test(1);
  if (!buyer.hot_parallel_order_client_ready_for_self_test(0) ||
      !buyer.hot_parallel_order_client_ready_for_self_test(1) ||
      first_snapshots != MAX_HOT_ORDER_CLIENT_SNAPSHOTS ||
      second_snapshots != MAX_HOT_ORDER_CLIENT_SNAPSHOTS ||
      count != 2 ||
      !trades[0].has_value() ||
      !trades[1].has_value() ||
      !trades[0]->attempted ||
      !trades[1]->attempted ||
      trades[0]->ret_code != 0 ||
      trades[1]->ret_code != 0 ||
      trades[0]->reason != "tdlib_native_rest_preflight" ||
      trades[1]->reason != "tdlib_native_rest_preflight") {
    std::cerr << "hot_parallel_order_client_snapshot_self_test_failed"
              << " first_ready=" << buyer.hot_parallel_order_client_ready_for_self_test(0)
              << " second_ready=" << buyer.hot_parallel_order_client_ready_for_self_test(1)
              << " first_snapshots=" << first_snapshots
              << " second_snapshots=" << second_snapshots
              << " count=" << count
              << " trades=" << native_trades_json(trades, count)
              << std::endl;
    return 1;
  }

  std::cout << "SELFTEST_HOT_PARALLEL_ORDER_CLIENT_SNAPSHOT_OK "
            << native_trades_json(trades, count)
            << " first_snapshots=" << first_snapshots
            << " second_snapshots=" << second_snapshots
            << std::endl;
  return 0;
}

int run_tdlib_message_disabled_benchmark(int iterations) {
  if (iterations <= 0) {
    iterations = 10000;
  }

  WatchChatSet watched_chats;
  watched_chats.upsert(777001, "BithumbExchange");
  BybitNativeBuyer buyer;
  buyer.set_active(true);
  const std::string payload =
      R"({"@type":"updateNewMessage","message":{"@type":"message","id":321987,"chat_id":777001,"date":1778680000,"content":{"@type":"messageText","text":{"@type":"formattedText","text":"[마켓 추가] 스타크넷(STRK) 원화 마켓 추가 및 재단 에어드랍 안내","entities":[]}}}})";

  NullBuffer null_buffer;
  std::ostream null_stream(&null_buffer);
  auto* previous = std::cout.rdbuf(null_stream.rdbuf());
  std::vector<long long> samples;
  samples.reserve(static_cast<size_t>(iterations));

  for (int i = 0; i < iterations; ++i) {
    const long long started = monotonic_now_ns();
    const bool consumed = maybe_emit_listing_matched(
        payload,
        watched_chats,
        true,
        true,
        buyer);
    const long long elapsed = monotonic_now_ns() - started;
    if (!consumed) {
      std::cout.rdbuf(previous);
      std::cerr << "benchmark_tdlib_message_disabled_not_consumed" << std::endl;
      return 1;
    }
    samples.push_back(elapsed);
  }
  std::cout.rdbuf(previous);

  std::sort(samples.begin(), samples.end());
  long double total = 0.0;
  for (const long long sample : samples) {
    total += static_cast<long double>(sample);
  }
  const auto pick = [&](double percentile) -> long long {
    const size_t index = std::min<size_t>(
        samples.size() - 1,
        static_cast<size_t>(samples.size() * percentile));
    return samples[index];
  };

  std::cout << "BENCHMARK_TDLIB_MESSAGE_DISABLED"
            << " iterations=" << iterations
            << " p50_us=" << (pick(0.50) / 1000.0)
            << " p95_us=" << (pick(0.95) / 1000.0)
            << " avg_us=" << (static_cast<double>(total / samples.size()) / 1000.0)
            << std::endl;
  return 0;
}

int run_tdlib_unwatched_update_benchmark(int iterations) {
  if (iterations <= 0) {
    iterations = 10000;
  }

  WatchChatSet watched_chats;
  watched_chats.upsert(777001, "BithumbExchange");
  BybitNativeBuyer buyer;
  buyer.set_active(true);
  const std::string payload =
      R"({"@type":"updateNewMessage","message":{"@type":"message","id":321987,"chat_id":777009,"date":1778680000,"content":{"@type":"messageText","text":{"@type":"formattedText","text":"[마켓 추가] 스타크넷(STRK) 원화 마켓 추가 및 재단 에어드랍 안내","entities":[]}}}})";

  std::vector<long long> samples;
  samples.reserve(static_cast<size_t>(iterations));

  for (int i = 0; i < iterations; ++i) {
    const long long started = monotonic_now_ns();
    const bool consumed = maybe_emit_listing_matched(
        payload,
        watched_chats,
        true,
        true,
        buyer,
        true,
        1);
    const long long elapsed = monotonic_now_ns() - started;
    if (consumed) {
      std::cerr << "benchmark_tdlib_unwatched_update_consumed" << std::endl;
      return 1;
    }
    samples.push_back(elapsed);
  }

  std::sort(samples.begin(), samples.end());
  long double total = 0.0;
  for (const long long sample : samples) {
    total += static_cast<long double>(sample);
  }
  const auto pick = [&](double percentile) -> long long {
    const size_t index = std::min<size_t>(
        samples.size() - 1,
        static_cast<size_t>(samples.size() * percentile));
    return samples[index];
  };

  std::cout << "BENCHMARK_TDLIB_UNWATCHED_UPDATE"
            << " iterations=" << iterations
            << " p50_ns=" << pick(0.50)
            << " p95_ns=" << pick(0.95)
            << " avg_ns=" << static_cast<double>(total / samples.size())
            << std::endl;
  return 0;
}

int run_tdlib_message_buy_preflight_benchmark(int iterations) {
  if (iterations <= 0) {
    iterations = 10000;
  }
  setenv("BYBIT_SPOT_BUY_ENABLED", "1", 1);
  setenv("BYBIT_API_KEY", "benchmark-key", 1);
  setenv("BYBIT_API_SECRET", "benchmark-secret", 1);
  setenv("BYBIT_SPOT_BUY_USDT_AMOUNT", "5", 1);

  WatchChatSet watched_chats;
  watched_chats.upsert(777001, "BithumbExchange");
  BybitNativeBuyer buyer;
  buyer.inject_spot_symbol_for_self_test("STRKUSDT");
  buyer.enable_order_preflight_for_benchmark();
  const std::string payload =
      R"({"@type":"updateNewMessage","message":{"@type":"message","id":321987,"chat_id":777001,"date":1778680000,"content":{"@type":"messageText","text":{"@type":"formattedText","text":"[마켓 추가] 스타크넷(STRK) 원화 마켓 추가 및 재단 에어드랍 안내","entities":[]}}}})";

  std::vector<long long> samples;
  samples.reserve(static_cast<size_t>(iterations));
  for (int i = 0; i < iterations; ++i) {
    const long long started = monotonic_now_ns();
    const bool prepared = maybe_native_buy_preflight_from_tdlib_for_benchmark(
        payload,
        watched_chats,
        buyer);
    const long long elapsed = monotonic_now_ns() - started;
    if (!prepared) {
      std::cerr << "benchmark_tdlib_message_buy_preflight_failed" << std::endl;
      return 1;
    }
    samples.push_back(elapsed);
  }

  std::sort(samples.begin(), samples.end());
  long double total = 0.0;
  for (const long long sample : samples) {
    total += static_cast<long double>(sample);
  }
  const auto pick = [&](double percentile) -> long long {
    const size_t index = std::min<size_t>(
        samples.size() - 1,
        static_cast<size_t>(samples.size() * percentile));
    return samples[index];
  };

  std::cout << "BENCHMARK_TDLIB_MESSAGE_BUY_PREFLIGHT"
            << " iterations=" << iterations
            << " p50_us=" << (pick(0.50) / 1000.0)
            << " p95_us=" << (pick(0.95) / 1000.0)
            << " avg_us=" << (static_cast<double>(total / samples.size()) / 1000.0)
            << std::endl;
  return 0;
}

int run_tdlib_message_buy_preflight_long_body_benchmark(int iterations) {
  if (iterations <= 0) {
    iterations = 10000;
  }
  setenv("BYBIT_SPOT_BUY_ENABLED", "1", 1);
  setenv("BYBIT_API_KEY", "benchmark-key", 1);
  setenv("BYBIT_API_SECRET", "benchmark-secret", 1);
  setenv("BYBIT_SPOT_BUY_USDT_AMOUNT", "5", 1);

  WatchChatSet watched_chats;
  watched_chats.upsert(777001, "BithumbExchange");
  BybitNativeBuyer buyer;
  buyer.inject_spot_symbol_for_self_test("STRKUSDT");
  buyer.enable_order_preflight_for_benchmark();
  const std::string payload =
      R"JSON({"@type":"updateNewMessage","message":{"@type":"message","id":321987,"chat_id":777001,"date":1778680000,"content":{"@type":"messageText","text":{"@type":"formattedText","text":"[마켓 추가] 스타크넷(STRK) 원화 마켓 추가 및 재단 에어드랍 안내\n)JSON" +
      std::string(4096, 'x') +
      R"JSON(","entities":[]}}}})JSON";

  std::vector<long long> samples;
  samples.reserve(static_cast<size_t>(iterations));
  for (int i = 0; i < iterations; ++i) {
    const long long started = monotonic_now_ns();
    const bool prepared = maybe_native_buy_preflight_from_tdlib_for_benchmark(
        payload,
        watched_chats,
        buyer);
    const long long elapsed = monotonic_now_ns() - started;
    if (!prepared) {
      std::cerr << "benchmark_tdlib_message_buy_preflight_long_body_failed" << std::endl;
      return 1;
    }
    samples.push_back(elapsed);
  }

  std::sort(samples.begin(), samples.end());
  long double total = 0.0;
  for (const long long sample : samples) {
    total += static_cast<long double>(sample);
  }
  const auto pick = [&](double percentile) -> long long {
    const size_t index = std::min<size_t>(
        samples.size() - 1,
        static_cast<size_t>(samples.size() * percentile));
    return samples[index];
  };

  std::cout << "BENCHMARK_TDLIB_MESSAGE_BUY_PREFLIGHT_LONG_BODY"
            << " iterations=" << iterations
            << " p50_us=" << (pick(0.50) / 1000.0)
            << " p95_us=" << (pick(0.95) / 1000.0)
            << " avg_us=" << (static_cast<double>(total / samples.size()) / 1000.0)
            << std::endl;
  return 0;
}

int run_tdlib_message_buy_preflight_upbit_benchmark(int iterations) {
  if (iterations <= 0) {
    iterations = 10000;
  }
  setenv("BYBIT_SPOT_BUY_ENABLED", "1", 1);
  setenv("BYBIT_API_KEY", "benchmark-key", 1);
  setenv("BYBIT_API_SECRET", "benchmark-secret", 1);
  setenv("BYBIT_SPOT_BUY_USDT_AMOUNT", "5", 1);

  WatchChatSet watched_chats;
  watched_chats.upsert(777002, "upbit_news");
  BybitNativeBuyer buyer;
  buyer.inject_spot_symbol_for_self_test("VVVUSDT");
  buyer.enable_order_preflight_for_benchmark();
  const std::string payload =
      R"JSON({"@type":"updateNewMessage","message":{"@type":"message","id":421987,"chat_id":777002,"date":1778680000,"content":{"@type":"messageText","text":{"@type":"formattedText","text":"[거래] 베니스토큰(VVV) 신규 거래지원 안내 (KRW 마켓)","entities":[]}}}})JSON";

  std::vector<long long> samples;
  samples.reserve(static_cast<size_t>(iterations));
  for (int i = 0; i < iterations; ++i) {
    const long long started = monotonic_now_ns();
    const bool prepared = maybe_native_buy_preflight_from_tdlib_for_benchmark(
        payload,
        watched_chats,
        buyer);
    const long long elapsed = monotonic_now_ns() - started;
    if (!prepared) {
      std::cerr << "benchmark_tdlib_message_buy_preflight_upbit_failed" << std::endl;
      return 1;
    }
    samples.push_back(elapsed);
  }

  std::sort(samples.begin(), samples.end());
  long double total = 0.0;
  for (const long long sample : samples) {
    total += static_cast<long double>(sample);
  }
  const auto pick = [&](double percentile) -> long long {
    const size_t index = std::min<size_t>(
        samples.size() - 1,
        static_cast<size_t>(samples.size() * percentile));
    return samples[index];
  };

  std::cout << "BENCHMARK_TDLIB_MESSAGE_BUY_PREFLIGHT_UPBIT"
            << " iterations=" << iterations
            << " p50_us=" << (pick(0.50) / 1000.0)
            << " p95_us=" << (pick(0.95) / 1000.0)
            << " avg_us=" << (static_cast<double>(total / samples.size()) / 1000.0)
            << std::endl;
  return 0;
}

int run_tdlib_message_buy_preflight_multi_benchmark(int iterations) {
  if (iterations <= 0) {
    iterations = 10000;
  }
  setenv("BYBIT_SPOT_BUY_ENABLED", "1", 1);
  setenv("BYBIT_API_KEY", "benchmark-key", 1);
  setenv("BYBIT_API_SECRET", "benchmark-secret", 1);
  setenv("BYBIT_SPOT_BUY_USDT_AMOUNT", "5", 1);

  WatchChatSet watched_chats;
  watched_chats.upsert(777001, "BithumbExchange");
  BybitNativeBuyer buyer;
  buyer.inject_spot_symbol_for_self_test("SENTUSDT");
  buyer.inject_spot_symbol_for_self_test("ELSAUSDT");
  buyer.enable_order_preflight_for_benchmark();
  buyer.activate_workers_for_self_test();
  const std::string payload =
      R"({"@type":"updateNewMessage","message":{"@type":"message","id":321988,"chat_id":777001,"date":1778680001,"content":{"@type":"messageText","text":{"@type":"formattedText","text":"[마켓 추가] 센티언트(SENT), 헤이엘사(ELSA) 원화 마켓 추가","entities":[]}}}})";

  std::vector<long long> samples;
  samples.reserve(static_cast<size_t>(iterations));
  for (int i = 0; i < iterations; ++i) {
    const long long started = monotonic_now_ns();
    const bool prepared = maybe_native_buy_preflight_from_tdlib_for_benchmark(
        payload,
        watched_chats,
        buyer);
    const long long elapsed = monotonic_now_ns() - started;
    if (!prepared) {
      std::cerr << "benchmark_tdlib_message_buy_preflight_multi_failed" << std::endl;
      return 1;
    }
    samples.push_back(elapsed);
  }

  std::sort(samples.begin(), samples.end());
  long double total = 0.0;
  for (const long long sample : samples) {
    total += static_cast<long double>(sample);
  }
  const auto pick = [&](double percentile) -> long long {
    const size_t index = std::min<size_t>(
        samples.size() - 1,
        static_cast<size_t>(samples.size() * percentile));
    return samples[index];
  };

  std::cout << "BENCHMARK_TDLIB_MESSAGE_BUY_PREFLIGHT_MULTI"
            << " iterations=" << iterations
            << " p50_us=" << (pick(0.50) / 1000.0)
            << " p95_us=" << (pick(0.95) / 1000.0)
            << " avg_us=" << (static_cast<double>(total / samples.size()) / 1000.0)
            << std::endl;
  return 0;
}

int run_tdlib_message_emit_preflight_benchmark(int iterations) {
  if (iterations <= 0) {
    iterations = 10000;
  }
  setenv("BYBIT_SPOT_BUY_ENABLED", "1", 1);
  setenv("BYBIT_API_KEY", "benchmark-key", 1);
  setenv("BYBIT_API_SECRET", "benchmark-secret", 1);
  setenv("BYBIT_SPOT_BUY_USDT_AMOUNT", "5", 1);

  WatchChatSet watched_chats;
  watched_chats.upsert(777001, "BithumbExchange");
  BybitNativeBuyer buyer;
  buyer.inject_spot_symbol_for_self_test("STRKUSDT");
  buyer.enable_order_preflight_for_benchmark();
  const std::string payload =
      R"({"@type":"updateNewMessage","message":{"@type":"message","id":321987,"chat_id":777001,"date":1778680000,"content":{"@type":"messageText","text":{"@type":"formattedText","text":"[마켓 추가] 스타크넷(STRK) 원화 마켓 추가 및 재단 에어드랍 안내","entities":[]}}}})";

  NullBuffer null_buffer;
  std::ostream null_stream(&null_buffer);
  auto* previous = std::cout.rdbuf(null_stream.rdbuf());
  std::vector<long long> samples;
  samples.reserve(static_cast<size_t>(iterations));

  for (int i = 0; i < iterations; ++i) {
    const long long started = monotonic_now_ns();
    const bool consumed = maybe_emit_listing_matched(
        payload,
        watched_chats,
        true,
        true,
        buyer,
        true);
    const long long elapsed = monotonic_now_ns() - started;
    if (!consumed) {
      std::cout.rdbuf(previous);
      std::cerr << "benchmark_tdlib_message_emit_preflight_not_consumed" << std::endl;
      return 1;
    }
    samples.push_back(elapsed);
  }
  std::cout.rdbuf(previous);

  std::sort(samples.begin(), samples.end());
  long double total = 0.0;
  for (const long long sample : samples) {
    total += static_cast<long double>(sample);
  }
  const auto pick = [&](double percentile) -> long long {
    const size_t index = std::min<size_t>(
        samples.size() - 1,
        static_cast<size_t>(samples.size() * percentile));
    return samples[index];
  };

  std::cout << "BENCHMARK_TDLIB_MESSAGE_EMIT_PREFLIGHT"
            << " iterations=" << iterations
            << " p50_us=" << (pick(0.50) / 1000.0)
            << " p95_us=" << (pick(0.95) / 1000.0)
            << " avg_us=" << (static_cast<double>(total / samples.size()) / 1000.0)
            << std::endl;
  return 0;
}

int run_tdlib_message_fire_and_forget_benchmark(int iterations) {
  if (iterations <= 0) {
    iterations = 10000;
  }
  setenv("BYBIT_SPOT_BUY_ENABLED", "1", 1);
  setenv("BYBIT_API_KEY", "benchmark-key", 1);
  setenv("BYBIT_API_SECRET", "benchmark-secret", 1);
  setenv("BYBIT_SPOT_BUY_USDT_AMOUNT", "5", 1);
  setenv("LISTING_TDLIB_NATIVE_ASYNC_ORDER_DISPATCH", "1", 1);
  setenv("LISTING_TDLIB_NATIVE_TIMING_ENABLED", "1", 1);
  setenv("LISTING_TDLIB_NATIVE_ORDER_CLIENT_KEEPWARM_ENABLED", "0", 1);

  const std::filesystem::path root =
      std::filesystem::temp_directory_path() /
      ("tdlib_message_fire_and_forget_" + std::to_string(monotonic_now_ns()));
  const std::filesystem::path order_path = root / "v5" / "order" / "create";
  std::error_code ec;
  std::filesystem::create_directories(order_path.parent_path(), ec);
  {
    std::ofstream output(order_path);
    output << R"({"retCode":0,"retMsg":"OK","result":{"orderId":"tdlib-fire-order"}})";
  }
  const std::string base_url = "file://" + root.string();
  setenv("BYBIT_API_BASE_URL", base_url.c_str(), 1);

  WatchChatSet watched_chats;
  watched_chats.upsert(777001, "BithumbExchange");
  BybitNativeBuyer buyer;
  buyer.inject_spot_symbol_for_self_test("STRKUSDT");
  buyer.activate_workers_for_self_test();
  const std::string payload =
      R"({"@type":"updateNewMessage","message":{"@type":"message","id":321987,"chat_id":777001,"date":1778680000,"content":{"@type":"messageText","text":{"@type":"formattedText","text":"[마켓 추가] 스타크넷(STRK) 원화 마켓 추가 및 재단 에어드랍 안내","entities":[]}}}})";

  std::vector<long long> receive_return_samples;
  std::vector<long long> order_send_samples;
  receive_return_samples.reserve(static_cast<size_t>(iterations));
  order_send_samples.reserve(static_cast<size_t>(iterations));
  for (int i = 0; i < iterations; ++i) {
    const uint64_t expected_seq = buyer.worker_work_seq_for_self_test(0) + 1;
    const long long started = monotonic_now_ns();
    const bool consumed = maybe_emit_listing_matched(
        payload,
        watched_chats,
        true,
        true,
        buyer,
        true,
        started,
        nullptr,
        nullptr,
        false,
        false);
    const long long returned = monotonic_now_ns();
    const NativeTradeResult trade =
        buyer.wait_worker_done_copy_for_self_test(0, expected_seq);
    if (!consumed ||
        !trade.attempted ||
        !trade.executed ||
        trade.ret_code != 0 ||
        trade.order_send_started_monotonic_ns <= 0) {
      std::filesystem::remove_all(root, ec);
      std::cerr << "benchmark_tdlib_message_fire_and_forget_failed"
                << " consumed=" << consumed
                << " trade=" << native_trade_json(trade)
                << std::endl;
      return 1;
    }
    receive_return_samples.push_back(returned - started);
    order_send_samples.push_back(trade.order_send_started_monotonic_ns - started);
  }
  std::filesystem::remove_all(root, ec);

  auto summarize = [](std::vector<long long>& samples) {
    std::sort(samples.begin(), samples.end());
    long double total = 0.0;
    for (const long long sample : samples) {
      total += static_cast<long double>(sample);
    }
    const auto pick = [&](double percentile) -> long long {
      const size_t index = std::min<size_t>(
          samples.size() - 1,
          static_cast<size_t>(samples.size() * percentile));
      return samples[index];
    };
    return std::array<double, 3>{
        pick(0.50) / 1000.0,
        pick(0.95) / 1000.0,
        static_cast<double>(total / samples.size()) / 1000.0};
  };

  const auto receive_summary = summarize(receive_return_samples);
  const auto order_summary = summarize(order_send_samples);
  std::cout << "BENCHMARK_TDLIB_MESSAGE_FIRE_AND_FORGET"
            << " iterations=" << iterations
            << " receive_return_p50_us=" << receive_summary[0]
            << " receive_return_p95_us=" << receive_summary[1]
            << " receive_return_avg_us=" << receive_summary[2]
            << " order_send_started_p50_us=" << order_summary[0]
            << " order_send_started_p95_us=" << order_summary[1]
            << " order_send_started_avg_us=" << order_summary[2]
            << std::endl;
  return 0;
}

int run_tdlib_message_fire_and_forget_multi_benchmark(int iterations) {
  if (iterations <= 0) {
    iterations = 10000;
  }
  setenv("BYBIT_SPOT_BUY_ENABLED", "1", 1);
  setenv("BYBIT_API_KEY", "benchmark-key", 1);
  setenv("BYBIT_API_SECRET", "benchmark-secret", 1);
  setenv("BYBIT_SPOT_BUY_USDT_AMOUNT", "5", 1);
  setenv("LISTING_TDLIB_NATIVE_ASYNC_ORDER_DISPATCH", "1", 1);
  setenv("LISTING_TDLIB_NATIVE_TIMING_ENABLED", "1", 1);
  setenv("LISTING_TDLIB_NATIVE_ORDER_CLIENT_KEEPWARM_ENABLED", "0", 1);

  const std::filesystem::path root =
      std::filesystem::temp_directory_path() /
      ("tdlib_message_fire_and_forget_multi_" + std::to_string(monotonic_now_ns()));
  const std::filesystem::path order_path = root / "v5" / "order" / "create";
  std::error_code ec;
  std::filesystem::create_directories(order_path.parent_path(), ec);
  {
    std::ofstream output(order_path);
    output << R"({"retCode":0,"retMsg":"OK","result":{"orderId":"tdlib-fire-multi-order"}})";
  }
  const std::string base_url = "file://" + root.string();
  setenv("BYBIT_API_BASE_URL", base_url.c_str(), 1);

  WatchChatSet watched_chats;
  watched_chats.upsert(777001, "BithumbExchange");
  BybitNativeBuyer buyer;
  buyer.inject_spot_symbol_for_self_test("SENTUSDT");
  buyer.inject_spot_symbol_for_self_test("ELSAUSDT");
  buyer.activate_workers_for_self_test();
  const std::string payload =
      R"({"@type":"updateNewMessage","message":{"@type":"message","id":321988,"chat_id":777001,"date":1778680000,"content":{"@type":"messageText","text":{"@type":"formattedText","text":"[마켓 추가] 센티언트(SENT), 헤이엘사(ELSA) 원화 마켓 추가","entities":[]}}}})";

  std::vector<long long> receive_return_samples;
  std::vector<long long> order_send_samples;
  receive_return_samples.reserve(static_cast<size_t>(iterations));
  order_send_samples.reserve(static_cast<size_t>(iterations));
  for (int i = 0; i < iterations; ++i) {
    const uint64_t expected_seq0 = buyer.worker_work_seq_for_self_test(0) + 1;
    const uint64_t expected_seq1 = buyer.worker_work_seq_for_self_test(1) + 1;
    const long long started = monotonic_now_ns();
    const bool consumed = maybe_emit_listing_matched(
        payload,
        watched_chats,
        true,
        true,
        buyer,
        true,
        started,
        nullptr,
        nullptr,
        false,
        false);
    const long long returned = monotonic_now_ns();
    const NativeTradeResult trade0 =
        buyer.wait_worker_done_copy_for_self_test(0, expected_seq0);
    const NativeTradeResult trade1 =
        buyer.wait_worker_done_copy_for_self_test(1, expected_seq1);
    if (!consumed ||
        !trade0.attempted ||
        !trade0.executed ||
        trade0.ret_code != 0 ||
        trade0.order_send_started_monotonic_ns <= 0 ||
        !trade1.attempted ||
        !trade1.executed ||
        trade1.ret_code != 0 ||
        trade1.order_send_started_monotonic_ns <= 0) {
      std::filesystem::remove_all(root, ec);
      std::cerr << "benchmark_tdlib_message_fire_and_forget_multi_failed"
                << " consumed=" << consumed
                << " trade0=" << native_trade_json(trade0)
                << " trade1=" << native_trade_json(trade1)
                << std::endl;
      return 1;
    }
    receive_return_samples.push_back(returned - started);
    order_send_samples.push_back(
        std::max(
            trade0.order_send_started_monotonic_ns,
            trade1.order_send_started_monotonic_ns) -
        started);
  }
  std::filesystem::remove_all(root, ec);

  auto summarize = [](std::vector<long long>& samples) {
    std::sort(samples.begin(), samples.end());
    long double total = 0.0;
    for (const long long sample : samples) {
      total += static_cast<long double>(sample);
    }
    const auto pick = [&](double percentile) -> long long {
      const size_t index = std::min<size_t>(
          samples.size() - 1,
          static_cast<size_t>(samples.size() * percentile));
      return samples[index];
    };
    return std::array<double, 3>{
        pick(0.50) / 1000.0,
        pick(0.95) / 1000.0,
        static_cast<double>(total / samples.size()) / 1000.0};
  };

  const auto receive_summary = summarize(receive_return_samples);
  const auto order_summary = summarize(order_send_samples);
  std::cout << "BENCHMARK_TDLIB_MESSAGE_FIRE_AND_FORGET_MULTI"
            << " iterations=" << iterations
            << " receive_return_p50_us=" << receive_summary[0]
            << " receive_return_p95_us=" << receive_summary[1]
            << " receive_return_avg_us=" << receive_summary[2]
            << " order_send_started_p50_us=" << order_summary[0]
            << " order_send_started_p95_us=" << order_summary[1]
            << " order_send_started_avg_us=" << order_summary[2]
            << std::endl;
  return 0;
}

int run_native_activation_warmup_benchmark(int iterations) {
  if (iterations <= 0) {
    iterations = 1000;
  }
  setenv("BYBIT_SPOT_BUY_ENABLED", "1", 1);
  setenv("BYBIT_API_KEY", "benchmark-key", 1);
  setenv("BYBIT_API_SECRET", "benchmark-secret", 1);
  setenv("BYBIT_SPOT_BUY_USDT_AMOUNT", "5", 1);
  setenv("BYBIT_API_BASE_URL", "https://127.0.0.1:1", 1);
  setenv("LISTING_TDLIB_NATIVE_ORDER_ON_CACHE_MISS", "1", 1);
  setenv("LISTING_TDLIB_NATIVE_KEEPWARM_INTERVAL", "3600", 1);

  const std::filesystem::path cache_path =
      std::filesystem::temp_directory_path() /
      ("tdlib_missing_spot_symbols_" + std::to_string(monotonic_now_ns()) + ".txt");
  std::filesystem::remove(cache_path);
  setenv("LISTING_TDLIB_NATIVE_SYMBOL_CACHE_PATH", cache_path.string().c_str(), 1);

  std::vector<long long> samples;
  samples.reserve(static_cast<size_t>(iterations));
  for (int i = 0; i < iterations; ++i) {
    std::optional<BybitNativeBuyer> buyer;
    buyer.emplace();
    const long long started = monotonic_now_ns();
    buyer->begin_activation();
    const bool ready = buyer->finish_activation_warmup();
    const long long elapsed = monotonic_now_ns() - started;
    if (!ready || buyer->readiness_reason() != "ready") {
      std::cerr << "benchmark_native_activation_warmup_failed"
                << " ready=" << ready
                << " reason=" << buyer->readiness_reason()
                << std::endl;
      return 1;
    }
    samples.push_back(elapsed);
    buyer.reset();
  }

  std::sort(samples.begin(), samples.end());
  long double total = 0.0;
  for (const long long sample : samples) {
    total += static_cast<long double>(sample);
  }
  const auto pick = [&](double percentile) -> long long {
    const size_t index = std::min<size_t>(
        samples.size() - 1,
        static_cast<size_t>(samples.size() * percentile));
    return samples[index];
  };

  std::cout << "BENCHMARK_NATIVE_ACTIVATION_WARMUP"
            << " iterations=" << iterations
            << " p50_us=" << (pick(0.50) / 1000.0)
            << " p95_us=" << (pick(0.95) / 1000.0)
            << " avg_us=" << (static_cast<double>(total / samples.size()) / 1000.0)
            << std::endl;
  return 0;
}

int run_native_order_prepare_benchmark(int iterations) {
  if (iterations <= 0) {
    iterations = 10000;
  }
  setenv("BYBIT_SPOT_BUY_ENABLED", "1", 1);
  setenv("BYBIT_API_KEY", "benchmark-key", 1);
  setenv("BYBIT_API_SECRET", "benchmark-secret", 1);
  setenv("BYBIT_SPOT_BUY_USDT_AMOUNT", "5", 1);

  BybitNativeBuyer buyer;
  buyer.inject_spot_symbol_for_self_test("STRKUSDT");

  std::vector<long long> samples;
  samples.reserve(static_cast<size_t>(iterations));
  for (int i = 0; i < iterations; ++i) {
    const long long started = monotonic_now_ns();
    const bool prepared = buyer.prepare_order_for_benchmark(
        "bithumb",
        321987,
        "STRK");
    const long long elapsed = monotonic_now_ns() - started;
    if (!prepared) {
      std::cerr << "benchmark_native_order_prepare_failed" << std::endl;
      return 1;
    }
    samples.push_back(elapsed);
  }

  std::sort(samples.begin(), samples.end());
  long double total = 0.0;
  for (const long long sample : samples) {
    total += static_cast<long double>(sample);
  }
  const auto pick = [&](double percentile) -> long long {
    const size_t index = std::min<size_t>(
        samples.size() - 1,
        static_cast<size_t>(samples.size() * percentile));
    return samples[index];
  };

  std::cout << "BENCHMARK_NATIVE_ORDER_PREPARE"
            << " iterations=" << iterations
            << " p50_us=" << (pick(0.50) / 1000.0)
            << " p95_us=" << (pick(0.95) / 1000.0)
            << " avg_us=" << (static_cast<double>(total / samples.size()) / 1000.0)
            << std::endl;
  return 0;
}

int run_native_order_curl_prepare_benchmark(int iterations) {
  if (iterations <= 0) {
    iterations = 10000;
  }
  setenv("BYBIT_SPOT_BUY_ENABLED", "1", 1);
  setenv("BYBIT_API_KEY", "benchmark-key", 1);
  setenv("BYBIT_API_SECRET", "benchmark-secret", 1);
  setenv("BYBIT_SPOT_BUY_USDT_AMOUNT", "5", 1);

  BybitNativeBuyer buyer;
  buyer.inject_spot_symbol_for_self_test("STRKUSDT");

  std::vector<long long> samples;
  samples.reserve(static_cast<size_t>(iterations));
  for (int i = 0; i < iterations; ++i) {
    const long long started = monotonic_now_ns();
    const bool prepared = buyer.prepare_order_curl_for_benchmark(
        "bithumb",
        321987,
        "STRK");
    const long long elapsed = monotonic_now_ns() - started;
    if (!prepared) {
      std::cerr << "benchmark_native_order_curl_prepare_failed" << std::endl;
      return 1;
    }
    samples.push_back(elapsed);
  }

  std::sort(samples.begin(), samples.end());
  long double total = 0.0;
  for (const long long sample : samples) {
    total += static_cast<long double>(sample);
  }
  const auto pick = [&](double percentile) -> long long {
    const size_t index = std::min<size_t>(
        samples.size() - 1,
        static_cast<size_t>(samples.size() * percentile));
    return samples[index];
  };

  std::cout << "BENCHMARK_NATIVE_ORDER_CURL_PREPARE"
            << " iterations=" << iterations
            << " p50_us=" << (pick(0.50) / 1000.0)
            << " p95_us=" << (pick(0.95) / 1000.0)
            << " avg_us=" << (static_cast<double>(total / samples.size()) / 1000.0)
            << std::endl;
  return 0;
}

int run_native_buy_preflight_benchmark(int iterations) {
  if (iterations <= 0) {
    iterations = 10000;
  }
  setenv("BYBIT_SPOT_BUY_ENABLED", "1", 1);
  setenv("BYBIT_API_KEY", "benchmark-key", 1);
  setenv("BYBIT_API_SECRET", "benchmark-secret", 1);
  setenv("BYBIT_SPOT_BUY_USDT_AMOUNT", "5", 1);

  BybitNativeBuyer buyer;
  buyer.inject_spot_symbol_for_self_test("STRKUSDT");
  buyer.enable_order_preflight_for_benchmark();

  std::vector<long long> samples;
  samples.reserve(static_cast<size_t>(iterations));
  for (int i = 0; i < iterations; ++i) {
    const long long started = monotonic_now_ns();
    const bool prepared = buyer.buy_listing_preflight_for_benchmark(
        "bithumb",
        321987,
        "STRK");
    const long long elapsed = monotonic_now_ns() - started;
    if (!prepared) {
      std::cerr << "benchmark_native_buy_preflight_failed" << std::endl;
      return 1;
    }
    samples.push_back(elapsed);
  }

  std::sort(samples.begin(), samples.end());
  long double total = 0.0;
  for (const long long sample : samples) {
    total += static_cast<long double>(sample);
  }
  const auto pick = [&](double percentile) -> long long {
    const size_t index = std::min<size_t>(
        samples.size() - 1,
        static_cast<size_t>(samples.size() * percentile));
    return samples[index];
  };

  std::cout << "BENCHMARK_NATIVE_BUY_PREFLIGHT"
            << " iterations=" << iterations
            << " p50_us=" << (pick(0.50) / 1000.0)
            << " p95_us=" << (pick(0.95) / 1000.0)
            << " avg_us=" << (static_cast<double>(total / samples.size()) / 1000.0)
            << std::endl;
  return 0;
}

int run_native_buy_preflight_multi_benchmark(int iterations) {
  if (iterations <= 0) {
    iterations = 10000;
  }
  setenv("BYBIT_SPOT_BUY_ENABLED", "1", 1);
  setenv("BYBIT_API_KEY", "benchmark-key", 1);
  setenv("BYBIT_API_SECRET", "benchmark-secret", 1);
  setenv("BYBIT_SPOT_BUY_USDT_AMOUNT", "5", 1);

  BybitNativeBuyer buyer;
  buyer.inject_spot_symbol_for_self_test("SENTUSDT");
  buyer.inject_spot_symbol_for_self_test("ELSAUSDT");
  buyer.enable_order_preflight_for_benchmark();
  buyer.activate_workers_for_self_test();
  ListingTickers tickers;
  tickers.push_unique("SENT");
  tickers.push_unique("ELSA");

  std::vector<long long> samples;
  samples.reserve(static_cast<size_t>(iterations));
  std::array<std::optional<NativeTradeResult>, MAX_LISTING_TICKERS> trades;
  for (int i = 0; i < iterations; ++i) {
    const long long started = monotonic_now_ns();
    const size_t count = buyer.buy_listings("b", 321988 + i, tickers, trades);
    const long long elapsed = monotonic_now_ns() - started;
    if (count != 2 ||
        !trades[0].has_value() ||
        !trades[1].has_value() ||
        !trades[0]->attempted ||
        !trades[1]->attempted ||
        trades[0]->ret_code != 0 ||
        trades[1]->ret_code != 0) {
      std::cerr << "benchmark_native_buy_preflight_multi_failed" << std::endl;
      return 1;
    }
    samples.push_back(elapsed);
  }

  std::sort(samples.begin(), samples.end());
  long double total = 0.0;
  for (const long long sample : samples) {
    total += static_cast<long double>(sample);
  }
  const auto pick = [&](double percentile) -> long long {
    const size_t index = std::min<size_t>(
        samples.size() - 1,
        static_cast<size_t>(samples.size() * percentile));
    return samples[index];
  };

  std::cout << "BENCHMARK_NATIVE_BUY_PREFLIGHT_MULTI"
            << " iterations=" << iterations
            << " p50_us=" << (pick(0.50) / 1000.0)
            << " p95_us=" << (pick(0.95) / 1000.0)
            << " avg_us=" << (static_cast<double>(total / samples.size()) / 1000.0)
            << std::endl;
  return 0;
}

int run_native_async_fire_and_forget_benchmark(int iterations) {
  if (iterations <= 0) {
    iterations = 10000;
  }
  setenv("BYBIT_SPOT_BUY_ENABLED", "1", 1);
  setenv("BYBIT_API_KEY", "benchmark-key", 1);
  setenv("BYBIT_API_SECRET", "benchmark-secret", 1);
  setenv("BYBIT_SPOT_BUY_USDT_AMOUNT", "5", 1);
  setenv("LISTING_TDLIB_NATIVE_ASYNC_ORDER_DISPATCH", "1", 1);
  setenv("LISTING_TDLIB_NATIVE_TIMING_ENABLED", "1", 1);
  setenv("LISTING_TDLIB_NATIVE_ORDER_CLIENT_KEEPWARM_ENABLED", "0", 1);

  const std::filesystem::path root =
      std::filesystem::temp_directory_path() /
      ("tdlib_fire_and_forget_benchmark_" + std::to_string(monotonic_now_ns()));
  const std::filesystem::path order_path = root / "v5" / "order" / "create";
  std::error_code ec;
  std::filesystem::create_directories(order_path.parent_path(), ec);
  {
    std::ofstream output(order_path);
    output << R"({"retCode":0,"retMsg":"OK","result":{"orderId":"fire-bench-order"}})";
  }
  const std::string base_url = "file://" + root.string();
  setenv("BYBIT_API_BASE_URL", base_url.c_str(), 1);

  BybitNativeBuyer buyer;
  buyer.inject_spot_symbol_for_self_test("STRKUSDT");
  buyer.activate_workers_for_self_test();

  std::vector<long long> dispatch_return_samples;
  std::vector<long long> order_send_samples;
  dispatch_return_samples.reserve(static_cast<size_t>(iterations));
  order_send_samples.reserve(static_cast<size_t>(iterations));
  for (int i = 0; i < iterations; ++i) {
    const long long started = monotonic_now_ns();
    const NativeDispatchResult dispatch =
        buyer.dispatch_listing_async("b", 321987 + i, "STRK");
    const long long returned = monotonic_now_ns();
    NativeTradeResult trade;
    if (dispatch.dispatched) {
      trade = buyer.wait_worker_done_copy_for_self_test(
          dispatch.worker_index,
          dispatch.work_seq);
    }
    if (!dispatch.dispatched ||
        !trade.attempted ||
        !trade.executed ||
        trade.ret_code != 0 ||
        trade.order_send_started_monotonic_ns <= 0) {
      std::filesystem::remove_all(root, ec);
      std::cerr << "benchmark_native_async_fire_and_forget_failed"
                << " dispatched=" << dispatch.dispatched
                << " no_worker=" << dispatch.no_worker
                << " reason=" << dispatch.reason
                << " trade=" << native_trade_json(trade)
                << std::endl;
      return 1;
    }
    dispatch_return_samples.push_back(returned - started);
    order_send_samples.push_back(trade.order_send_started_monotonic_ns - started);
  }
  std::filesystem::remove_all(root, ec);

  auto summarize = [](std::vector<long long>& samples) {
    std::sort(samples.begin(), samples.end());
    long double total = 0.0;
    for (const long long sample : samples) {
      total += static_cast<long double>(sample);
    }
    const auto pick = [&](double percentile) -> long long {
      const size_t index = std::min<size_t>(
          samples.size() - 1,
          static_cast<size_t>(samples.size() * percentile));
      return samples[index];
    };
    return std::array<double, 3>{
        pick(0.50) / 1000.0,
        pick(0.95) / 1000.0,
        static_cast<double>(total / samples.size()) / 1000.0};
  };

  const auto dispatch_summary = summarize(dispatch_return_samples);
  const auto order_summary = summarize(order_send_samples);
  std::cout << "BENCHMARK_NATIVE_ASYNC_FIRE_AND_FORGET"
            << " iterations=" << iterations
            << " dispatch_return_p50_us=" << dispatch_summary[0]
            << " dispatch_return_p95_us=" << dispatch_summary[1]
            << " dispatch_return_avg_us=" << dispatch_summary[2]
            << " order_send_started_p50_us=" << order_summary[0]
            << " order_send_started_p95_us=" << order_summary[1]
            << " order_send_started_avg_us=" << order_summary[2]
            << std::endl;
  return 0;
}

bool legacy_has_update_new_message_for_benchmark(std::string_view body) {
  constexpr std::string_view prefix = "{\"@type\":\"updateNewMessage\"";
  if (body.rfind(prefix, 0) == 0) {
    return true;
  }
  return body.find("\"@type\":\"updateNewMessage\"") != std::string_view::npos;
}

int run_tdlib_type_filter_benchmark(int iterations) {
  if (iterations <= 0) {
    iterations = 10000;
  }

  const std::string payload =
      R"({"@type":"updateUserStatus","user_id":777001,"status":{"@type":"userStatusOnline","expires":1778689999},"extra":{"source":"tdlib","padding":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}})";
  constexpr int ops_per_sample = 128;
  volatile int sink = 0;
  std::vector<long long> legacy_samples;
  std::vector<long long> fast_samples;
  std::vector<long long> live_cstr_samples;
  legacy_samples.reserve(static_cast<size_t>(iterations));
  fast_samples.reserve(static_cast<size_t>(iterations));
  live_cstr_samples.reserve(static_cast<size_t>(iterations));

  for (int i = 0; i < iterations; ++i) {
    const long long started = monotonic_now_ns();
    for (int op = 0; op < ops_per_sample; ++op) {
      sink += legacy_has_update_new_message_for_benchmark(payload) ? 1 : 0;
    }
    legacy_samples.push_back((monotonic_now_ns() - started) / ops_per_sample);
  }
  for (int i = 0; i < iterations; ++i) {
    const long long started = monotonic_now_ns();
    for (int op = 0; op < ops_per_sample; ++op) {
      sink += has_json_type(payload, "updateNewMessage") ? 1 : 0;
    }
    fast_samples.push_back((monotonic_now_ns() - started) / ops_per_sample);
  }
  for (int i = 0; i < iterations; ++i) {
    const long long started = monotonic_now_ns();
    for (int op = 0; op < ops_per_sample; ++op) {
      sink += cstr_has_update_new_message_type(payload.c_str()) ? 1 : 0;
    }
    live_cstr_samples.push_back((monotonic_now_ns() - started) / ops_per_sample);
  }
  if (sink != 0) {
    std::cerr << "benchmark_tdlib_type_filter_unexpected_match" << std::endl;
    return 1;
  }

  std::sort(legacy_samples.begin(), legacy_samples.end());
  std::sort(fast_samples.begin(), fast_samples.end());
  std::sort(live_cstr_samples.begin(), live_cstr_samples.end());
  long double legacy_total = 0.0;
  long double fast_total = 0.0;
  long double live_cstr_total = 0.0;
  for (const long long sample : legacy_samples) {
    legacy_total += static_cast<long double>(sample);
  }
  for (const long long sample : fast_samples) {
    fast_total += static_cast<long double>(sample);
  }
  for (const long long sample : live_cstr_samples) {
    live_cstr_total += static_cast<long double>(sample);
  }
  const auto pick = [](const std::vector<long long>& samples, double percentile) -> long long {
    const size_t index = std::min<size_t>(
        samples.size() - 1,
        static_cast<size_t>(samples.size() * percentile));
    return samples[index];
  };

  std::cout << "BENCHMARK_TDLIB_TYPE_FILTER"
            << " iterations=" << iterations
            << " ops_per_sample=" << ops_per_sample
            << " legacy_p50_ns=" << pick(legacy_samples, 0.50)
            << " legacy_p95_ns=" << pick(legacy_samples, 0.95)
            << " legacy_avg_ns=" << static_cast<double>(legacy_total / legacy_samples.size())
            << " fast_p50_ns=" << pick(fast_samples, 0.50)
            << " fast_p95_ns=" << pick(fast_samples, 0.95)
            << " fast_avg_ns=" << static_cast<double>(fast_total / fast_samples.size())
            << " live_cstr_p50_ns=" << pick(live_cstr_samples, 0.50)
            << " live_cstr_p95_ns=" << pick(live_cstr_samples, 0.95)
            << " live_cstr_avg_ns="
            << static_cast<double>(live_cstr_total / live_cstr_samples.size())
            << std::endl;
  return 0;
}

int run_curl_header_list_benchmark(int iterations) {
  if (iterations <= 0) {
    iterations = 10000;
  }
  const std::string content_type = "Content-Type: application/json";
  const std::string api_key = "X-BAPI-API-KEY: benchmark-key";
  const std::string recv_window = "X-BAPI-RECV-WINDOW: 5000";
  AuthHeaders headers;
  headers.content_type_header = &content_type;
  headers.api_key_header = &api_key;
  headers.recv_window_header = &recv_window;
  headers.set_sign_header(
      "X-BAPI-SIGN: 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef");
  headers.set_timestamp_header("X-BAPI-TIMESTAMP: 1778680000123");
  constexpr int ops_per_sample = 64;
  volatile std::uintptr_t sink = 0;
  std::vector<long long> heap_samples;
  std::vector<long long> stack_samples;
  heap_samples.reserve(static_cast<size_t>(iterations));
  stack_samples.reserve(static_cast<size_t>(iterations));

  for (int i = 0; i < iterations; ++i) {
    const long long started = monotonic_now_ns();
    for (int op = 0; op < ops_per_sample; ++op) {
      struct curl_slist* header_list = nullptr;
      for (size_t header_index = 0; header_index < AUTH_HEADER_COUNT; ++header_index) {
        header_list = curl_slist_append(header_list, headers.c_str(header_index));
      }
      for (auto* node = header_list; node != nullptr; node = node->next) {
        sink = sink ^ reinterpret_cast<std::uintptr_t>(node->data);
      }
      curl_slist_free_all(header_list);
    }
    heap_samples.push_back((monotonic_now_ns() - started) / ops_per_sample);
  }

  for (int i = 0; i < iterations; ++i) {
    const long long started = monotonic_now_ns();
    for (int op = 0; op < ops_per_sample; ++op) {
      std::array<curl_slist, AUTH_HEADER_COUNT> nodes{};
      struct curl_slist* header_list = stack_auth_header_list(headers, nodes);
      for (auto* node = header_list; node != nullptr; node = node->next) {
        sink = sink ^ reinterpret_cast<std::uintptr_t>(node->data);
      }
    }
    stack_samples.push_back((monotonic_now_ns() - started) / ops_per_sample);
  }

  auto summarize = [](std::vector<long long>& samples) {
    std::sort(samples.begin(), samples.end());
    long double total = 0.0;
    for (const long long sample : samples) {
      total += static_cast<long double>(sample);
    }
    struct Summary {
      long long p50;
      long long p95;
      double avg;
    };
    const auto pick = [&samples](double percentile) -> long long {
      const size_t index = std::min<size_t>(
          samples.size() - 1,
          static_cast<size_t>(samples.size() * percentile));
      return samples[index];
    };
    return Summary{
        pick(0.50),
        pick(0.95),
        static_cast<double>(total / samples.size()),
    };
  };

  const auto heap = summarize(heap_samples);
  const auto stack = summarize(stack_samples);
  std::cout << "BENCHMARK_CURL_HEADER_LIST"
            << " iterations=" << iterations
            << " ops_per_sample=" << ops_per_sample
            << " heap_p50_us=" << (heap.p50 / 1000.0)
            << " heap_p95_us=" << (heap.p95 / 1000.0)
            << " heap_avg_us=" << (heap.avg / 1000.0)
            << " stack_p50_us=" << (stack.p50 / 1000.0)
            << " stack_p95_us=" << (stack.p95 / 1000.0)
            << " stack_avg_us=" << (stack.avg / 1000.0)
            << " sink=" << sink
            << std::endl;
  return 0;
}

ExchangeId exchange_id_from_cli_name(std::string_view value) {
  if (value == "upbit") {
    return ExchangeId::Upbit;
  }
  if (value == "bithumb") {
    return ExchangeId::Bithumb;
  }
  return ExchangeId::Unknown;
}

int run_classify_title_cli(std::string_view exchange_name, std::string_view title) {
  const ExchangeId exchange = exchange_id_from_cli_name(exchange_name);
  ListingMatch listing;
  if (!classify_listing_title_into(exchange, title, listing)) {
    std::cout << "{\"matched\":false}" << std::endl;
    return 0;
  }

  std::ostream& out = std::cout;
  out << "{\"matched\":true,"
      << "\"exchange\":";
  write_json_string(out, listing.exchange);
  out << ",\"signal_type\":";
  write_json_string(out, listing.signal_type);
  out << ",\"ticker\":";
  write_json_string(out, listing.ticker);
  out << ",\"tickers\":";
  write_tickers_json(out, listing.tickers);
  out << ",\"asset_name\":";
  write_json_string(out, extract_asset_name_for_ticker(title, listing.ticker));
  out << ",\"markets\":" << market_flags_json(extract_market_flags(title))
      << "}" << std::endl;
  return 0;
}
}  // namespace

int main(int argc, char** argv) {
  if (argc > 3 && std::strcmp(argv[1], "--classify-title") == 0) {
    return run_classify_title_cli(argv[2], argv[3]);
  }
  if (argc > 1 && std::strcmp(argv[1], "--self-test-native-buy-disabled") == 0) {
    return run_native_buy_disabled_self_test();
  }
  if (argc > 1 && std::strcmp(argv[1], "--self-test-order-response-buffer") == 0) {
    return run_order_response_buffer_self_test();
  }
  if (argc > 1 && std::strcmp(argv[1], "--self-test-native-invalid-quote-amount") == 0) {
    return run_native_invalid_quote_amount_self_test();
  }
  if (argc > 1 && std::strcmp(argv[1], "--self-test-native-order-file-scheme") == 0) {
    return run_native_order_file_scheme_self_test();
  }
  if (argc > 1 && std::strcmp(argv[1], "--self-test-tdlib-message-disabled") == 0) {
    return run_tdlib_message_disabled_self_test();
  }
  if (argc > 1 && std::strcmp(argv[1], "--self-test-tdlib-message-emit-disabled") == 0) {
    return run_tdlib_message_emit_disabled_self_test();
  }
  if (argc > 1 && std::strcmp(argv[1], "--self-test-native-message-dedup") == 0) {
    return run_native_message_dedup_self_test();
  }
  if (argc > 1 && std::strcmp(argv[1], "--self-test-native-worker-pool") == 0) {
    return run_native_worker_pool_self_test();
  }
  if (argc > 1 && std::strcmp(argv[1], "--self-test-native-async-order-dispatch") == 0) {
    return run_native_async_order_dispatch_self_test();
  }
  if (argc > 1 && std::strcmp(argv[1], "--self-test-native-async-fire-and-forget") == 0) {
    return run_native_async_fire_and_forget_self_test();
  }
  if (argc > 1 && std::strcmp(argv[1], "--self-test-native-async-fire-and-forget-reclaim") == 0) {
    return run_native_async_fire_and_forget_worker_reclaim_self_test();
  }
  if (argc > 1 && std::strcmp(argv[1], "--self-test-native-async-ticker-copy") == 0) {
    return run_native_async_ticker_copy_self_test();
  }
  if (argc > 1 && std::strcmp(argv[1], "--self-test-native-buy-wait-ready") == 0) {
    return run_native_buy_wait_for_ready_self_test();
  }
  if (argc > 1 && std::strcmp(argv[1], "--self-test-native-multi-buy-wait-ready") == 0) {
    return run_native_multi_buy_wait_for_ready_self_test();
  }
  if (argc > 1 && std::strcmp(argv[1], "--self-test-native-symbol-cache") == 0) {
    return run_native_symbol_cache_self_test();
  }
  if (argc > 1 && std::strcmp(argv[1], "--self-test-native-cache-miss-order") == 0) {
    return run_native_cache_miss_order_self_test();
  }
  if (argc > 1 && std::strcmp(argv[1], "--self-test-native-startup-no-network-warmup") == 0) {
    return run_native_startup_no_network_warmup_self_test();
  }
  if (argc > 1 && std::strcmp(argv[1], "--self-test-native-parallel-keepwarm-limit") == 0) {
    return run_native_parallel_keepwarm_limit_self_test();
  }
  if (argc > 1 && std::strcmp(argv[1], "--self-test-hot-order-client-snapshot") == 0) {
    return run_hot_order_client_snapshot_self_test();
  }
  if (argc > 1 && std::strcmp(argv[1], "--self-test-hot-parallel-order-client-snapshot") == 0) {
    return run_hot_parallel_order_client_snapshot_self_test();
  }
  if (argc > 1 && std::strcmp(argv[1], "--benchmark-tdlib-message-disabled") == 0) {
    int iterations = 10000;
    if (argc > 2) {
      char* end = nullptr;
      const long parsed = std::strtol(argv[2], &end, 10);
      if (end != argv[2] && parsed > 0) {
        iterations = static_cast<int>(parsed);
      }
    }
    return run_tdlib_message_disabled_benchmark(iterations);
  }
  if (argc > 1 && std::strcmp(argv[1], "--benchmark-tdlib-unwatched-update") == 0) {
    int iterations = 10000;
    if (argc > 2) {
      char* end = nullptr;
      const long parsed = std::strtol(argv[2], &end, 10);
      if (end != argv[2] && parsed > 0) {
        iterations = static_cast<int>(parsed);
      }
    }
    return run_tdlib_unwatched_update_benchmark(iterations);
  }
  if (argc > 1 && std::strcmp(argv[1], "--benchmark-tdlib-message-buy-preflight") == 0) {
    int iterations = 10000;
    if (argc > 2) {
      char* end = nullptr;
      const long parsed = std::strtol(argv[2], &end, 10);
      if (end != argv[2] && parsed > 0) {
        iterations = static_cast<int>(parsed);
      }
    }
    return run_tdlib_message_buy_preflight_benchmark(iterations);
  }
  if (argc > 1 && std::strcmp(argv[1], "--benchmark-tdlib-message-buy-preflight-long-body") == 0) {
    int iterations = 10000;
    if (argc > 2) {
      char* end = nullptr;
      const long parsed = std::strtol(argv[2], &end, 10);
      if (end != argv[2] && parsed > 0) {
        iterations = static_cast<int>(parsed);
      }
    }
    return run_tdlib_message_buy_preflight_long_body_benchmark(iterations);
  }
  if (argc > 1 && std::strcmp(argv[1], "--benchmark-tdlib-message-buy-preflight-upbit") == 0) {
    int iterations = 10000;
    if (argc > 2) {
      char* end = nullptr;
      const long parsed = std::strtol(argv[2], &end, 10);
      if (end != argv[2] && parsed > 0) {
        iterations = static_cast<int>(parsed);
      }
    }
    return run_tdlib_message_buy_preflight_upbit_benchmark(iterations);
  }
  if (argc > 1 && std::strcmp(argv[1], "--benchmark-tdlib-message-buy-preflight-multi") == 0) {
    int iterations = 10000;
    if (argc > 2) {
      char* end = nullptr;
      const long parsed = std::strtol(argv[2], &end, 10);
      if (end != argv[2] && parsed > 0) {
        iterations = static_cast<int>(parsed);
      }
    }
    return run_tdlib_message_buy_preflight_multi_benchmark(iterations);
  }
  if (argc > 1 && std::strcmp(argv[1], "--benchmark-tdlib-message-emit-preflight") == 0) {
    int iterations = 10000;
    if (argc > 2) {
      char* end = nullptr;
      const long parsed = std::strtol(argv[2], &end, 10);
      if (end != argv[2] && parsed > 0) {
        iterations = static_cast<int>(parsed);
      }
    }
    return run_tdlib_message_emit_preflight_benchmark(iterations);
  }
  if (argc > 1 && std::strcmp(argv[1], "--benchmark-tdlib-message-fire-and-forget") == 0) {
    int iterations = 10000;
    if (argc > 2) {
      char* end = nullptr;
      const long parsed = std::strtol(argv[2], &end, 10);
      if (end != argv[2] && parsed > 0) {
        iterations = static_cast<int>(parsed);
      }
    }
    return run_tdlib_message_fire_and_forget_benchmark(iterations);
  }
  if (argc > 1 && std::strcmp(argv[1], "--benchmark-tdlib-message-fire-and-forget-multi") == 0) {
    int iterations = 10000;
    if (argc > 2) {
      char* end = nullptr;
      const long parsed = std::strtol(argv[2], &end, 10);
      if (end != argv[2] && parsed > 0) {
        iterations = static_cast<int>(parsed);
      }
    }
    return run_tdlib_message_fire_and_forget_multi_benchmark(iterations);
  }
  if (argc > 1 && std::strcmp(argv[1], "--benchmark-native-activation-warmup") == 0) {
    int iterations = 1000;
    if (argc > 2) {
      char* end = nullptr;
      const long parsed = std::strtol(argv[2], &end, 10);
      if (end != argv[2] && parsed > 0) {
        iterations = static_cast<int>(parsed);
      }
    }
    return run_native_activation_warmup_benchmark(iterations);
  }
  if (argc > 1 && std::strcmp(argv[1], "--benchmark-native-order-prepare") == 0) {
    int iterations = 10000;
    if (argc > 2) {
      char* end = nullptr;
      const long parsed = std::strtol(argv[2], &end, 10);
      if (end != argv[2] && parsed > 0) {
        iterations = static_cast<int>(parsed);
      }
    }
    return run_native_order_prepare_benchmark(iterations);
  }
  if (argc > 1 && std::strcmp(argv[1], "--benchmark-native-order-curl-prepare") == 0) {
    int iterations = 10000;
    if (argc > 2) {
      char* end = nullptr;
      const long parsed = std::strtol(argv[2], &end, 10);
      if (end != argv[2] && parsed > 0) {
        iterations = static_cast<int>(parsed);
      }
    }
    return run_native_order_curl_prepare_benchmark(iterations);
  }
  if (argc > 1 && std::strcmp(argv[1], "--benchmark-native-buy-preflight") == 0) {
    int iterations = 10000;
    if (argc > 2) {
      char* end = nullptr;
      const long parsed = std::strtol(argv[2], &end, 10);
      if (end != argv[2] && parsed > 0) {
        iterations = static_cast<int>(parsed);
      }
    }
    return run_native_buy_preflight_benchmark(iterations);
  }
  if (argc > 1 && std::strcmp(argv[1], "--benchmark-native-buy-preflight-multi") == 0) {
    int iterations = 10000;
    if (argc > 2) {
      char* end = nullptr;
      const long parsed = std::strtol(argv[2], &end, 10);
      if (end != argv[2] && parsed > 0) {
        iterations = static_cast<int>(parsed);
      }
    }
    return run_native_buy_preflight_multi_benchmark(iterations);
  }
  if (argc > 1 && std::strcmp(argv[1], "--benchmark-native-async-fire-and-forget") == 0) {
    int iterations = 10000;
    if (argc > 2) {
      char* end = nullptr;
      const long parsed = std::strtol(argv[2], &end, 10);
      if (end != argv[2] && parsed > 0) {
        iterations = static_cast<int>(parsed);
      }
    }
    return run_native_async_fire_and_forget_benchmark(iterations);
  }
  if (argc > 1 && std::strcmp(argv[1], "--benchmark-tdlib-type-filter") == 0) {
    int iterations = 10000;
    if (argc > 2) {
      char* end = nullptr;
      const long parsed = std::strtol(argv[2], &end, 10);
      if (end != argv[2] && parsed > 0) {
        iterations = static_cast<int>(parsed);
      }
    }
    return run_tdlib_type_filter_benchmark(iterations);
  }
  if (argc > 1 && std::strcmp(argv[1], "--benchmark-curl-header-list") == 0) {
    int iterations = 10000;
    if (argc > 2) {
      char* end = nullptr;
      const long parsed = std::strtol(argv[2], &end, 10);
      if (end != argv[2] && parsed > 0) {
        iterations = static_cast<int>(parsed);
      }
    }
    return run_curl_header_list_benchmark(iterations);
  }

  boost_current_thread_for_hot_path();

  td_json_client_execute(
      nullptr,
      R"({"@type":"setLogVerbosityLevel","new_verbosity_level":0})");

  void *client = td_json_client_create();
  std::atomic<bool> should_stop{false};
  std::atomic<bool> native_listing_mode{false};
  std::atomic<bool> native_buy_mode{false};
  WatchChatRegistry watched_chats;
  BybitNativeBuyer native_buyer;
  NativeMessageDeduper native_deduper;
  const bool flush_listing_events = getenv_truthy("LISTING_TDLIB_FLUSH_LISTING_EVENTS", true);
  const bool emit_listing_events = getenv_truthy("LISTING_TDLIB_EMIT_LISTING_EVENTS", true);

  std::thread stdin_thread([&]() {
    boost_current_thread_for_hot_path();
    std::string line;
    while (std::getline(std::cin, line)) {
      if (!line.empty() && line.back() == '\r') {
        line.pop_back();
      }
      if (line == "__quit__") {
        should_stop.store(true, std::memory_order_relaxed);
        break;
      }
      if (line == "__clock__") {
        std::cout << "__clock__\t" << monotonic_now_ns() << std::endl;
        continue;
      }
      if (line == "__native_listing_on__") {
        native_listing_mode.store(true);
        continue;
      }
      if (line == "__native_listing_off__") {
        native_listing_mode.store(false);
        continue;
      }
      if (line == "__native_buy_on__") {
        native_buyer.begin_activation();
        native_buy_mode.store(true);
        const bool ready = native_buyer.finish_activation_warmup();
        native_buy_mode.store(ready);
        std::cout << "__native_buy_status__\t"
                  << "{\"active\":true,"
                  << "\"ready\":" << (ready ? "true" : "false") << ","
                  << "\"reason\":\"" << json_escape(native_buyer.readiness_reason()) << "\"}"
                  << std::endl;
        continue;
      }
      if (line == "__native_buy_off__") {
        native_buy_mode.store(false);
        native_buyer.set_active(false);
        std::cout << "__native_buy_status__\t"
                  << "{\"active\":false,\"ready\":false,"
                  << "\"reason\":\"tdlib_native_buy_inactive\"}"
                  << std::endl;
        continue;
      }
      if (line.rfind("__native_start__\t", 0) == 0) {
        auto next_watch_chats = std::make_shared<WatchChatSet>(
            parse_watch_map(line.substr(std::string("__native_start__\t").size())));
        watched_chats.publish(std::shared_ptr<const WatchChatSet>(std::move(next_watch_chats)));
        native_buyer.begin_activation();
        native_buy_mode.store(true);
        native_listing_mode.store(true);
        const bool ready = native_buyer.finish_activation_warmup();
        native_buy_mode.store(ready);
        std::cout << "__native_buy_status__\t"
                  << "{\"active\":true,"
                  << "\"ready\":" << (ready ? "true" : "false") << ","
                  << "\"reason\":\"" << json_escape(native_buyer.readiness_reason()) << "\"}"
                  << std::endl;
        continue;
      }
      if (line.rfind("__watch_chats__\t", 0) == 0) {
        auto next_watch_chats = std::make_shared<WatchChatSet>(
            parse_watch_map(line.substr(std::string("__watch_chats__\t").size())));
        watched_chats.publish(std::shared_ptr<const WatchChatSet>(std::move(next_watch_chats)));
        continue;
      }
      if (line.rfind("__selftest_native_preflight_on__\t", 0) == 0) {
        constexpr std::string_view prefix = "__selftest_native_preflight_on__\t";
        const std::string symbol(line.data() + prefix.size(), line.size() - prefix.size());
        if (!symbol.empty()) {
          native_buyer.inject_spot_symbol_for_self_test(symbol);
        }
        native_buyer.enable_order_preflight_for_benchmark();
        native_buy_mode.store(true);
        native_listing_mode.store(true);
        std::cout << "__selftest_native_preflight_status__\t"
                  << "{\"active\":true,\"ready\":true,"
                  << "\"symbol\":\"" << json_escape(symbol) << "\"}"
                  << std::endl;
        continue;
      }
      if (line.rfind("__selftest_update__\t", 0) == 0) {
        constexpr std::string_view prefix = "__selftest_update__\t";
        const long long relay_received_monotonic_ns = monotonic_now_ns();
        const std::string_view payload(line.data() + prefix.size(), line.size() - prefix.size());
        const WatchChatSet* watched_snapshot = watched_chats.load();
        const bool consumed = watched_snapshot != nullptr &&
            maybe_emit_listing_matched(
                payload,
                *watched_snapshot,
                native_listing_mode.load(std::memory_order_relaxed),
                false,
                native_buyer,
                true,
                relay_received_monotonic_ns,
                &native_buy_mode,
                &native_deduper,
                flush_listing_events,
                emit_listing_events,
                !emit_listing_events);
        if (!consumed) {
          std::cout << relay_received_monotonic_ns << '\t'
                    << "{\"@type\":\"selftestUpdateStatus\","
                    << "\"consumed\":false}" << std::endl;
        }
        continue;
      }
      if (!line.empty()) {
        td_json_client_send(client, line.c_str());
      }
    }
    should_stop.store(true, std::memory_order_relaxed);
  });

  const double receive_timeout_sec = getenv_nonnegative_double_or(
      "LISTING_TDLIB_RECEIVE_TIMEOUT_SEC",
      0.001);

  std::cout << "__relay_ready__" << std::endl;

  while (!should_stop.load(std::memory_order_relaxed)) {
    if (const char *result = td_json_client_receive(client, receive_timeout_sec)) {
      const bool listing_enabled = native_listing_mode.load(std::memory_order_relaxed);
      if (listing_enabled) {
        if (!cstr_has_update_new_message_type(result)) {
          continue;
        }
        const std::string_view result_view(result);
        const long long relay_received_monotonic_ns = monotonic_now_ns();
        const WatchChatSet* watched_snapshot = watched_chats.load();
        if (watched_snapshot &&
            maybe_emit_listing_matched(
                result_view,
                *watched_snapshot,
                true,
                false,
                native_buyer,
                true,
                relay_received_monotonic_ns,
                &native_buy_mode,
                &native_deduper,
                flush_listing_events,
                emit_listing_events)) {
          continue;
        }
        continue;
      }
      const long long relay_received_monotonic_ns = monotonic_now_ns();
      std::cout << relay_received_monotonic_ns << '\t' << result << std::endl;
    }
  }

  td_json_client_destroy(client);
  if (stdin_thread.joinable()) {
    stdin_thread.join();
  }
  return 0;
}
