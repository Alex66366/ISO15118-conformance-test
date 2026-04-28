// TCP/TLS Test Port Adapter for V2G Communication
// Handles TCP/TLS connection management and V2GTP message processing

#ifndef TCP_TLS_PORT_ADAPTER_HH
#define TCP_TLS_PORT_ADAPTER_HH

#include <string>
#include <vector>
#include <cstdint>
#include <memory>
#include <functional>
#include <mutex>
#include <queue>

// Connection state
enum class ConnectionState {
    DISCONNECTED,
    CONNECTING,
    CONNECTED,
    TLS_HANDSHAKING,
    TLS_CONNECTED,
    ERROR
};

// V2GTP Header structure (8 bytes)
struct V2GTP_Header {
    uint8_t protocol_version;
    uint8_t inv_protocol_version;
    uint16_t payload_type;
    uint32_t payload_length;
};

// V2GTP Payload types
namespace V2GTP_PayloadType {
    const uint16_t SDP = 12800;
    const uint16_t SUPPORTED_APP_PROTOCOL_REQ = 12801;
    const uint16_t SUPPORTED_APP_PROTOCOL_RES = 12802;
    const uint16_t SESSION_SETUP_REQ = 12803;
    const uint16_t SESSION_SETUP_RES = 12804;
}

// TCP/TLS Port Adapter
class TCP_TLS_Port_Adapter {
public:
    using DataReceivedCallback = std::function<void(const std::vector<uint8_t>&)>;
    using ConnectionCallback = std::function<void(bool success, const std::string& error)>;
    using StatusCallback = std::function<void(const std::string& status)>;

    static TCP_TLS_Port_Adapter& instance();

    // Initialize the port adapter
    bool initialize();

    // Close the port
    void close();

    // Set callbacks
    void set_data_received_callback(DataReceivedCallback callback);
    void set_connection_callback(ConnectionCallback callback);
    void set_status_callback(StatusCallback callback);

    // Connect to SECC
    bool connect(const std::string& ip_address, int port, bool use_tls = true);

    // Disconnect from SECC
    bool disconnect();

    // Send data
    bool send_data(const std::vector<uint8_t>& data);

    // Get connection state
    ConnectionState get_state() const { return state_; }

    // Set TLS parameters
    void set_tls_parameters(const std::string& cipher_suite,
                          const std::string& certificate_path);

    // Get last error
    std::string get_last_error() const { return last_error_; }

    // Get port status
    bool is_connected() const;
    bool is_tls_connected() const;

private:
    TCP_TLS_Port_Adapter();

    ConnectionState state_;
    std::string last_error_;
    std::string ip_address_;
    int port_;
    bool use_tls_;
    std::string cipher_suite_;
    std::string certificate_path_;

    DataReceivedCallback data_callback_;
    ConnectionCallback connection_callback_;
    StatusCallback status_callback_;

    std::mutex state_mutex_;
    std::mutex send_mutex_;
};

#endif // TCP_TLS_PORT_ADAPTER_HH
