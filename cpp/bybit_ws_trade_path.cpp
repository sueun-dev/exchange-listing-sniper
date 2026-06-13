#include <openssl/hmac.h>
#include <openssl/ssl.h>

#include <boost/asio/connect.hpp>
#include <boost/asio/ip/tcp.hpp>
#include <boost/asio/ssl/context.hpp>
#include <boost/asio/ssl/stream.hpp>
#include <boost/beast/core.hpp>
#include <boost/beast/ssl.hpp>
#include <boost/beast/websocket.hpp>

#include <chrono>
#include <cctype>
#include <cstdlib>
#include <cstring>
#include <functional>
#include <iomanip>
#include <iostream>
#include <memory>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

namespace beast = boost::beast;
namespace http = beast::http;
namespace net = boost::asio;
namespace ssl = net::ssl;
namespace websocket = beast::websocket;
using tcp = net::ip::tcp;

namespace {

using WsStream = websocket::stream<beast::ssl_stream<tcp::socket>>;

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
    std::string normalized(value);
    for (char& ch : normalized) {
        ch = static_cast<char>(std::tolower(static_cast<unsigned char>(ch)));
    }
    return normalized == "1" || normalized == "true" || normalized == "yes" || normalized == "on";
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

std::vector<std::string> split_tab_line(const std::string& line) {
    std::vector<std::string> parts;
    size_t start = 0;
    while (start <= line.size()) {
        size_t end = line.find('\t', start);
        if (end == std::string::npos) {
            parts.push_back(line.substr(start));
            break;
        }
        parts.push_back(line.substr(start, end - start));
        start = end + 1;
    }
    return parts;
}

struct ParsedUrl {
    std::string host;
    std::string port;
    std::string target;
};

ParsedUrl parse_ws_url(const std::string& ws_url) {
    std::string work = ws_url;
    bool secure = true;
    if (work.rfind("wss://", 0) == 0) {
        work = work.substr(6);
    } else if (work.rfind("ws://", 0) == 0) {
        work = work.substr(5);
        secure = false;
    }

    const size_t slash = work.find('/');
    const std::string host_port = slash == std::string::npos ? work : work.substr(0, slash);
    const std::string target = slash == std::string::npos ? "/" : work.substr(slash);
    const size_t colon = host_port.find(':');
    if (colon == std::string::npos) {
        return {host_port, secure ? "443" : "80", target};
    }
    return {
        host_port.substr(0, colon),
        host_port.substr(colon + 1),
        target,
    };
}

class BybitWsTradePath {
public:
    BybitWsTradePath()
        : api_key_(getenv_or("BYBIT_API_KEY")),
          api_secret_(getenv_or("BYBIT_API_SECRET")),
          ws_url_(getenv_or("BYBIT_WS_TRADE_URL", "wss://stream.bybit.com/v5/trade")),
          recv_window_(getenv_or("BYBIT_RECV_WINDOW", "5000")),
          timestamp_bias_ms_(getenv_long_long_or("BYBIT_TIMESTAMP_BIAS_MS", -50)),
          insecure_skip_verify_(getenv_truthy("BYBIT_CPP_WS_INSECURE_SKIP_VERIFY", false)),
          parsed_(parse_ws_url(ws_url_)),
          ssl_ctx_(ssl::context::tls_client),
          resolver_(ioc_) {
        if (insecure_skip_verify_) {
            ssl_ctx_.set_verify_mode(ssl::verify_none);
        } else {
            ssl_ctx_.set_default_verify_paths();
            ssl_ctx_.set_verify_mode(ssl::verify_peer);
        }
    }

    bool self_test() {
        const std::string signature = sign("1700000000000testkey5000{}");
        if (signature.size() != 64) {
            return false;
        }
        return true;
    }

    int run_server() {
        std::string line;
        while (std::getline(std::cin, line)) {
            if (line.empty()) {
                continue;
            }
            std::cout << handle_command(line) << '\n';
            std::cout.flush();
        }
        return 0;
    }

private:
    std::string handle_command(const std::string& line) {
        const auto parts = split_tab_line(line);
        if (parts.empty()) {
            return make_error("invalid_command");
        }
        if (parts[0] == "PING") {
            return "{\"ok\":true,\"pong\":true}";
        }
        if (parts[0] == "WARMUP") {
            try {
                ensure_ready();
                return "{\"ok\":true,\"warmed\":true}";
            } catch (const std::exception& exc) {
                close();
                return make_error(exc.what());
            }
        }
        if (parts[0] == "BUY") {
            if (parts.size() != 5) {
                return make_error("buy_command_requires_4_args");
            }
            return create_market_order(parts[1], "Buy", parts[2], parts[3], parts[4]);
        }
        if (parts[0] == "SELL") {
            if (parts.size() != 4) {
                return make_error("sell_command_requires_3_args");
            }
            return create_market_order(parts[1], "Sell", parts[2], "baseCoin", parts[3]);
        }
        return make_error("unknown_command");
    }

    void ensure_ready() {
        if (ws_ == nullptr || !ws_->is_open()) {
            connect();
            authenticate();
            return;
        }
        if (!authenticated_) {
            authenticate();
        }
    }

    void connect() {
        close();
        ws_ = std::make_unique<WsStream>(ioc_, ssl_ctx_);

        const auto endpoints = resolver_.resolve(parsed_.host, parsed_.port);
        auto& lowest = beast::get_lowest_layer(*ws_);
        net::connect(lowest, endpoints);
        lowest.set_option(tcp::no_delay(true));

        if (!SSL_set_tlsext_host_name(ws_->next_layer().native_handle(), parsed_.host.c_str())) {
            throw std::runtime_error("sni_failed");
        }

        ws_->next_layer().handshake(ssl::stream_base::client);
        ws_->set_option(websocket::stream_base::timeout::suggested(beast::role_type::client));
        ws_->set_option(websocket::stream_base::decorator(
            [](websocket::request_type& req) {
                req.set(http::field::user_agent, "ChainPulse-WSFastPath/1.0");
            }
        ));
        ws_->handshake(parsed_.host, parsed_.target);
        ws_->binary(false);
        authenticated_ = false;
    }

    void authenticate() {
        const auto expires = std::to_string(now_ms_int() + 1000);
        const std::string signature = sign("GET/realtime" + expires);
        const std::string payload =
            "{\"op\":\"auth\",\"args\":[\"" + api_key_ + "\"," + expires + ",\"" + signature + "\"]}";
        send_text(payload);
        const std::string response = read_until(
            [](const std::string& text) {
                return text.find("\"op\":\"auth\"") != std::string::npos;
            }
        );
        const auto ret_code = extract_json_int(response, "retCode").value_or(-1);
        const bool success =
            response.find("\"success\":true") != std::string::npos || ret_code == 0;
        if (!success) {
            throw std::runtime_error(
                extract_json_string(response, "retMsg").value_or("ws_auth_failed")
            );
        }
        authenticated_ = true;
    }

    std::string create_market_order(
        const std::string& symbol,
        const std::string& side,
        const std::string& qty,
        const std::string& market_unit,
        const std::string& order_link_id
    ) {
        if (api_key_.empty() || api_secret_.empty()) {
            return make_error("missing_api_config", symbol);
        }
        try {
            ensure_ready();
            return create_market_order_once(symbol, side, qty, market_unit, order_link_id);
        } catch (const std::exception& first_exc) {
            close();
            try {
                ensure_ready();
                return create_market_order_once(symbol, side, qty, market_unit, order_link_id);
            } catch (const std::exception& retry_exc) {
                close();
                return make_error(retry_exc.what(), symbol, true);
            }
        }
    }

    std::string create_market_order_once(
        const std::string& symbol,
        const std::string& side,
        const std::string& qty,
        const std::string& market_unit,
        const std::string& order_link_id
    ) {
        const std::string request_id = "cws-" + std::to_string(std::chrono::steady_clock::now().time_since_epoch().count());
        const std::string timestamp = std::to_string(now_ms_int(timestamp_bias_ms_));
        const std::string payload =
            "{\"reqId\":\"" + request_id + "\",\"header\":{\"X-BAPI-TIMESTAMP\":\"" + timestamp +
            "\",\"X-BAPI-RECV-WINDOW\":\"" + recv_window_ +
            "\"},\"op\":\"order.create\",\"args\":[{\"category\":\"spot\",\"symbol\":\"" + symbol +
            "\",\"side\":\"" + side + "\",\"orderType\":\"Market\",\"qty\":\"" + qty +
            "\",\"orderFilter\":\"Order\",\"marketUnit\":\"" + market_unit +
            "\",\"orderLinkId\":\"" + json_escape(order_link_id) + "\"}]}";

        send_text(payload);
        const std::string response = read_until(
            [&](const std::string& text) {
                return text.find("\"op\":\"order.create\"") != std::string::npos &&
                       text.find("\"reqId\":\"" + request_id + "\"") != std::string::npos;
            }
        );

        const auto ret_code = extract_json_int(response, "retCode").value_or(-1);
        if (ret_code != 0) {
            return make_error(
                extract_json_string(response, "retMsg").value_or("order_create_failed"),
                symbol,
                true,
                static_cast<int>(ret_code)
            );
        }

        const std::string order_id = extract_json_string(response, "orderId").value_or("");
        const std::string response_order_link_id =
            extract_json_string(response, "orderLinkId").value_or(order_link_id);
        return make_success(symbol, order_id, response_order_link_id, static_cast<int>(ret_code));
    }

    void send_text(const std::string& payload) {
        if (ws_ == nullptr) {
            throw std::runtime_error("ws_not_connected");
        }
        ws_->write(net::buffer(payload));
    }

    std::string read_until(const std::function<bool(const std::string&)>& predicate) {
        if (ws_ == nullptr) {
            throw std::runtime_error("ws_not_connected");
        }
        beast::flat_buffer buffer;
        while (true) {
            buffer.clear();
            ws_->read(buffer);
            const std::string text = beast::buffers_to_string(buffer.data());
            if (predicate(text)) {
                return text;
            }
        }
    }

    void close() {
        if (ws_ == nullptr) {
            return;
        }
        beast::error_code ec;
        if (ws_->is_open()) {
            ws_->close(websocket::close_code::normal, ec);
        }
        ws_.reset();
        authenticated_ = false;
    }

    std::string sign(const std::string& payload) const {
        unsigned char digest[EVP_MAX_MD_SIZE];
        unsigned int digest_len = 0;
        HMAC(
            EVP_sha256(),
            api_secret_.data(),
            static_cast<int>(api_secret_.size()),
            reinterpret_cast<const unsigned char*>(payload.data()),
            payload.size(),
            digest,
            &digest_len
        );
        std::ostringstream hex;
        hex << std::hex << std::setfill('0');
        for (unsigned int i = 0; i < digest_len; ++i) {
            hex << std::setw(2) << static_cast<int>(digest[i]);
        }
        return hex.str();
    }

    static long long now_ms_int(long long bias_ms = 0) {
        const auto now = std::chrono::time_point_cast<std::chrono::milliseconds>(
            std::chrono::system_clock::now()
        );
        return now.time_since_epoch().count() + bias_ms;
    }

    static std::string make_error(
        const std::string& reason,
        const std::string& symbol = "",
        bool attempted = false,
        int ret_code = -1
    ) {
        std::ostringstream out;
        out << "{\"ok\":false,\"executed\":false,\"attempted\":"
            << (attempted ? "true" : "false")
            << ",\"reason\":\"" << json_escape(reason) << "\"";
        if (!symbol.empty()) {
            out << ",\"symbol\":\"" << json_escape(symbol) << "\"";
        }
        if (ret_code != -1) {
            out << ",\"ret_code\":" << ret_code;
        }
        out << ",\"transport\":\"cpp_ws_trade\"}";
        return out.str();
    }

    static std::string make_success(
        const std::string& symbol,
        const std::string& order_id,
        const std::string& order_link_id,
        int ret_code
    ) {
        std::ostringstream out;
        out << "{\"ok\":true,\"executed\":true,\"attempted\":true,"
            << "\"symbol\":\"" << json_escape(symbol) << "\","
            << "\"order_id\":\"" << json_escape(order_id) << "\","
            << "\"order_link_id\":\"" << json_escape(order_link_id) << "\","
            << "\"ret_code\":" << ret_code << ","
            << "\"transport\":\"cpp_ws_trade\"}";
        return out.str();
    }

    std::string api_key_;
    std::string api_secret_;
    std::string ws_url_;
    std::string recv_window_;
    long long timestamp_bias_ms_{-50};
    bool insecure_skip_verify_{false};
    ParsedUrl parsed_;
    net::io_context ioc_;
    ssl::context ssl_ctx_;
    tcp::resolver resolver_;
    std::unique_ptr<WsStream> ws_;
    bool authenticated_{false};
};

}  // namespace

int main(int argc, char** argv) {
    BybitWsTradePath fast_path;

    if (argc > 1 && std::string(argv[1]) == "--self-test") {
        const bool ok = fast_path.self_test();
        std::cout << (ok ? "SELFTEST_OK" : "SELFTEST_FAIL") << '\n';
        return ok ? 0 : 1;
    }

    if (argc > 1 && std::string(argv[1]) == "--server") {
        return fast_path.run_server();
    }

    std::cerr << "usage: bybit_ws_trade_path --server | --self-test\n";
    return 1;
}
