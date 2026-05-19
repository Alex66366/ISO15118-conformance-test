// TITAN Test Port Interface (TRI) Implementation

#include "TitanTri.hh"

// PortManager implementation
PortManager& PortManager::instance() {
    static PortManager instance;
    return instance;
}

void PortManager::register_port(const std::string& port_name, TTCN3_Port* port) {
    ports_[port_name] = port;
}

void PortManager::unregister_port(const std::string& port_name) {
    ports_.erase(port_name);
}

TTCN3_Port* PortManager::get_port(const std::string& port_name) {
    auto it = ports_.find(port_name);
    if (it != ports_.end()) {
        return it->second;
    }
    return nullptr;
}

void PortManager::handle_incoming_data(const std::string& port_name,
                                       const std::vector<uint8_t>& data) {
    TTCN3_Port* port = get_port(port_name);
    if (port && port->incoming_handler_) {
        port->incoming_handler_(data);
    }
}
