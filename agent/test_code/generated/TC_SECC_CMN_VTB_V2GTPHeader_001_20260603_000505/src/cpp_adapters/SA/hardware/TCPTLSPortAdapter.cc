// TCP/TLS Test Port Adapter Implementation

#include "TCPTLSPortAdapter.hh"
#include "../../common/Common.hh"
#include <iostream>

TCP_TLS_Port_Adapter::TCP_TLS_Port_Adapter()
    : state_(ConnectionState::DISCONNECTED), port_(0), use_tls_(false) {}

TCP_TLS_Port_Adapter& TCP_TLS_Port_Adapter::instance() {
    static TCP_TLS_Port_Adapter instance;
    return instance;
}

bool TCP_TLS_Port_Adapter::initialize() {
    titan_adapter::log_message(titan_adapter::LogLevel::INFO,
        "TCP/TLS Port Adapter initialized");
    return true;
}

void TCP_TLS_Port_Adapter::close() {
    std::lock_guard<std::mutex> lock(state_mutex_);
    disconnect();
    titan_adapter::log_message(titan_adapter::LogLevel::INFO,
        "TCP/TLS Port Adapter closed");
}

void TCP_TLS_Port_Adapter::set_data_received_callback(DataReceivedCallback callback) {
    data_callback_ = callback;
}

void TCP_TLS_Port_Adapter::set_connection_callback(ConnectionCallback callback) {
    connection_callback_ = callback;
}

void TCP_TLS_Port_Adapter::set_status_callback(StatusCallback callback) {
    status_callback_ = callback;
}

bool TCP_TLS_Port_Adapter::connect(const std::string& ip_address, int port, bool use_tls) {
    std::lock_guard<std::mutex> lock(state_mutex_);

    ip_address_ = ip_address;
    port_ = port;
    use_tls_ = use_tls;

    titan_adapter::log_message(titan_adapter::LogLevel::INFO,
        "Connecting to " + ip_address + ":" + std::to_string(port) +
        (use_tls ? " (TLS)" : " (TCP)"));

    // In a real implementation, this would establish the TCP/TLS connection
    // For now, we simulate a successful connection
    state_ = use_tls ? ConnectionState::TLS_CONNECTED : ConnectionState::CONNECTED;

    if (connection_callback_) {
        connection_callback_(true, "");
    }

    if (status_callback_) {
        status_callback_(use_tls ? "TLS_CONNECTED" : "CONNECTED");
    }

    return true;
}

bool TCP_TLS_Port_Adapter::disconnect() {
    std::lock_guard<std::mutex> lock(state_mutex_);

    titan_adapter::log_message(titan_adapter::LogLevel::INFO,
        "Disconnecting from SECC");

    state_ = ConnectionState::DISCONNECTED;

    if (status_callback_) {
        status_callback_("DISCONNECTED");
    }

    return true;
}

bool TCP_TLS_Port_Adapter::send_data(const std::vector<uint8_t>& data) {
    std::lock_guard<std::mutex> lock(send_mutex_);

    if (state_ == ConnectionState::DISCONNECTED) {
        last_error_ = "Not connected";
        return false;
    }

    titan_adapter::log_message(titan_adapter::LogLevel::DEBUG,
        "Sending " + std::to_string(data.size()) + " bytes");

    // In a real implementation, this would send data over the TCP/TLS socket
    return true;
}

bool TCP_TLS_Port_Adapter::is_connected() const {
    return state_ == ConnectionState::CONNECTED ||
           state_ == ConnectionState::TLS_CONNECTED ||
           state_ == ConnectionState::TLS_HANDSHAKING;
}

bool TCP_TLS_Port_Adapter::is_tls_connected() const {
    return state_ == ConnectionState::TLS_CONNECTED;
}

void TCP_TLS_Port_Adapter::set_tls_parameters(const std::string& cipher_suite,
                                             const std::string& certificate_path) {
    cipher_suite_ = cipher_suite;
    certificate_path_ = certificate_path;
}
