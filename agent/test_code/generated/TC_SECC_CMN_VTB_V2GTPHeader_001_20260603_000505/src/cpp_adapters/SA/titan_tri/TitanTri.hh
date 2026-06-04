// TITAN Test Port Interface (TRI) Base Class
// Base class for all test port implementations

#ifndef TITAN_TRI_HH
#define TITAN_TRI_HH

#include <string>
#include <vector>
#include <cstdint>
#include <map>
#include <functional>

// TRI callback interface - implemented by TITAN runtime
class TRI_base {
public:
    virtual ~TRI_base() = default;

    // Called to indicate the port is ready
    virtual void user_init() = 0;

    // Called when the port is being terminated
    virtual void user_term() = 0;

    // Called to map the port to a system port
    virtual void map(const char* system_port) = 0;

    // Called to unmap the port from a system port
    virtual void unmap(const char* system_port) = 0;

    // Called to set the port parameter
    virtual void set_parameter(const char* parameter_name, const char* value) = 0;

    // Called to get the port parameter
    virtual const char* get_parameter(const char* parameter_name) = 0;
};

// Port event types
enum class PortEventType {
    MESSAGE_RECEIVED,
    CONNECTION_OPENED,
    CONNECTION_CLOSED,
    ERROR
};

// Port event data structure
struct PortEvent {
    PortEventType type;
    std::vector<uint8_t> data;
    std::string error_message;
    void* user_data;
};

// Abstract base class for all test ports
class TTCN3_Port {
public:
    virtual ~TTCN3_Port() = default;

    // Initialize the port
    virtual void init_port() = 0;

    // Close and cleanup the port
    virtual void close_port() = 0;

    // Check if port is ready to send
    virtual bool is_ready_to_send() = 0;

    // Handle outgoing message
    virtual void send_message(const std::vector<uint8_t>& data) = 0;

    // Handle outgoing primitive
    virtual void send_primitive(const std::string& primitive_type,
                                const std::vector<uint8_t>& data) = 0;

    // Handle outgoing message with address
    virtual void send_message_with_address(const std::vector<uint8_t>& data,
                                          const std::string& address) = 0;

    // Set up incoming message handler
    void set_incoming_message_handler(
        std::function<void(const std::vector<uint8_t>&)> handler) {
        incoming_handler_ = handler;
    }

    // Set up incoming primitive handler
    void set_incoming_primitive_handler(
        std::function<void(const std::string&, const std::vector<uint8_t>&)> handler) {
        incoming_primitive_handler_ = handler;
    }

public:
    std::function<void(const std::vector<uint8_t>&)> incoming_handler_;
    std::function<void(const std::string&, const std::vector<uint8_t>&)> incoming_primitive_handler_;
};

// Port manager - manages all test ports
class PortManager {
public:
    static PortManager& instance();

    // Register a port
    void register_port(const std::string& port_name, TTCN3_Port* port);

    // Unregister a port
    void unregister_port(const std::string& port_name);

    // Get a port by name
    TTCN3_Port* get_port(const std::string& port_name);

    // Handle incoming data for a port
    void handle_incoming_data(const std::string& port_name,
                             const std::vector<uint8_t>& data);

private:
    PortManager() = default;
    std::map<std::string, TTCN3_Port*> ports_;
};

#endif // TITAN_TRI_HH
