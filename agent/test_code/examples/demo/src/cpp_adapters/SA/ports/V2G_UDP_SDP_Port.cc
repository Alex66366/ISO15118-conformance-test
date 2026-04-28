#include "V2G_UDP_SDP_Port.hh"
#include <iostream>
#include <cstring>
#include <unistd.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <netdb.h>
#include <ifaddrs.h>
#include <net/if.h>
#include <errno.h>

std::string V2G__UDP__SDP__Port__last_secc_ip_;
int V2G__UDP__SDP__Port__last_secc_port_ = 15118;

namespace SECC__TestSystem {

V2G__UDP__SDP__Port::V2G__UDP__SDP__Port(const char *par_port_name)
    : V2G__UDP__SDP__Port_BASE(par_port_name),
      port_name_(par_port_name ? par_port_name : ""),
      port_(15118),
      socket_fd_(-1),
      bound_(false),
      receive_thread_running_(false),
      receive_thread_id_(0) {
}

V2G__UDP__SDP__Port::~V2G__UDP__SDP__Port() {
    close_socket();
}

void V2G__UDP__SDP__Port::set_parameter(const char *parameter_name, const char *parameter_value) {
    std::string name(parameter_name);
    std::string value(parameter_value ? parameter_value : "");

    std::cout << "[UDP_SDP_PORT] set_parameter(" << name << " = " << value << ")" << std::endl;

    if (name == "port") {
        port_ = std::stoi(value);
    } else if (name == "network_interface") {
        network_interface_ = value;
    } else if (name == "ipv6_multicast") {
        ipv6_multicast_ = value;
    }
}

void V2G__UDP__SDP__Port::Handle_Message(const unsigned char *message, size_t length) {
    std::cout << "[UDP_SDP_PORT] Handle_Message: " << length << " bytes" << std::endl;
}

void V2G__UDP__SDP__Port::outgoing_message(const unsigned char *message, size_t length) {
    std::cout << "[UDP_SDP_PORT] Sending " << length << " bytes via outgoing_message" << std::endl;
    send_sdp_request(message, length);
}

void V2G__UDP__SDP__Port::outgoing_send(const OCTETSTRING& send_par) {
    size_t length = send_par.lengthof();
    const unsigned char* data = send_par;

    std::cerr << "[UDP_SDP_PORT] outgoing_send() called with " << length << " bytes" << std::endl;

    // Ensure socket is initialized
    if (!bound_) {
        std::cerr << "[UDP_SDP_PORT] Socket not bound, initializing..." << std::endl;
        initialize_socket();
    }

    // Check if we need to construct a proper SDP request
    // If the data is too short or zero, construct a proper SDP request ourselves
    if (length < 12) {
        std::cerr << "[UDP_SDP_PORT] Data too short (" << length << " bytes), constructing SDP request..." << std::endl;

        // SDP Request: V2GTP header (8 bytes) + SDP body (2 bytes) = 10 bytes total
        // V2GTP Header (8 bytes):
        //   [0]: protocol version = 1 (0x01)
        //   [1]: inverse version = 254 (0xFE)
        //   [2-3]: payload type = 0x9000 (SDP_REQUEST for ISO 15118-2)
        //   [4-7]: payload length = 2 (big-endian)
        // SDP Body (2 bytes):
        //   [0]: security = 0x10 (NO_TLS = plain TCP)
        //   [1]: transport = 0x00 (TCP)
        unsigned char sdp_req[10] = {
            0x01,             // protocol version
            0xFE,             // inverse protocol version
            0x90, 0x00,      // payload type = 0x9000 (SDP_REQUEST)
            0x00, 0x00, 0x00, 0x02,  // payload length = 2 (big-endian)
            0x10,             // security = 0x10 (NO_TLS = plain TCP)
            0x00              // transport = 0x00 (TCP)
        };

        std::cerr << "[UDP_SDP_PORT] Sending constructed SDP request (10 bytes)" << std::endl;
        send_sdp_request(sdp_req, 10);
    } else {
        std::cerr << "[UDP_SDP_PORT] Sending provided SDP request (" << length << " bytes)" << std::endl;
        send_sdp_request(data, length);
    }
}

void V2G__UDP__SDP__Port::send_data(const OCTETSTRING& send_data, const char* send_fmt, ...) {
    size_t length = send_data.lengthof();
    const unsigned char* data = send_data;

    std::cout << "[UDP_SDP_PORT] send_data() called with " << length << " bytes" << std::endl;

    if (!bound_) {
        initialize_socket();
    }

    send_sdp_request(data, length);
}

int V2G__UDP__SDP__Port::parse_ipv6_address(const std::string& addr_str, ::sockaddr_in6* addr6) {
    if (!addr6) return -1;

    memset(addr6, 0, sizeof(::sockaddr_in6));
    addr6->sin6_family = AF_INET6;
    addr6->sin6_port = htons(port_);

    std::string addr_copy = addr_str;
    std::string scope_id;

    size_t scope_pos = addr_copy.find('%');
    if (scope_pos != std::string::npos) {
        scope_id = addr_copy.substr(scope_pos + 1);
        addr_copy = addr_copy.substr(0, scope_pos);
    }

    if (inet_pton(AF_INET6, addr_copy.c_str(), &addr6->sin6_addr) != 1) {
        std::cerr << "[UDP_SDP_PORT] ERROR: Failed to parse IPv6 address: " << addr_str << std::endl;
        return -1;
    }

    if (!scope_id.empty()) {
        addr6->sin6_scope_id = if_nametoindex(scope_id.c_str());
        if (addr6->sin6_scope_id == 0) {
            std::cerr << "[UDP_SDP_PORT] ERROR: Invalid interface name: " << scope_id << std::endl;
            return -1;
        }
    } else if (!network_interface_.empty()) {
        addr6->sin6_scope_id = if_nametoindex(network_interface_.c_str());
    }

    return 0;
}

void V2G__UDP__SDP__Port::initialize_socket() {
    std::cout << "[UDP_SDP_PORT] Initializing UDP socket..." << std::endl;

    socket_fd_ = ::socket(AF_INET6, SOCK_DGRAM, IPPROTO_UDP);
    if (socket_fd_ < 0) {
        std::cerr << "[UDP_SDP_PORT] ERROR: Failed to create socket: " << strerror(errno) << std::endl;
        return;
    }

    int reuse = 1;
    ::setsockopt(socket_fd_, SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof(reuse));

    if (!ipv6_multicast_.empty()) {
        struct ipv6_mreq mreq;
        memset(&mreq, 0, sizeof(mreq));

        std::string addr_copy = ipv6_multicast_;
        size_t scope_pos = addr_copy.find('%');
        if (scope_pos != std::string::npos) {
            addr_copy = addr_copy.substr(0, scope_pos);
        }

        if (inet_pton(AF_INET6, addr_copy.c_str(), &mreq.ipv6mr_multiaddr) == 1) {
            mreq.ipv6mr_interface = if_nametoindex(network_interface_.c_str());

            if (::setsockopt(socket_fd_, IPPROTO_IPV6, IPV6_JOIN_GROUP, &mreq, sizeof(mreq)) < 0) {
                std::cerr << "[UDP_SDP_PORT] WARNING: Failed to join multicast group: " << strerror(errno) << std::endl;
            } else {
                std::cout << "[UDP_SDP_PORT] Joined multicast group " << ipv6_multicast_ << " on interface " << network_interface_ << std::endl;
            }
        }
    }

    bound_ = true;
    std::cout << "[UDP_SDP_PORT] UDP socket initialized successfully" << std::endl;

    receive_thread_running_ = true;
    if (pthread_create(&receive_thread_id_, NULL, &V2G__UDP__SDP__Port::receive_thread_func, this) != 0) {
        std::cerr << "[UDP_SDP_PORT] ERROR: Failed to create receive thread" << std::endl;
        receive_thread_running_ = false;
    }
}

void V2G__UDP__SDP__Port::close_socket() {
    std::cout << "[UDP_SDP_PORT] Closing UDP socket..." << std::endl;

    receive_thread_running_ = false;

    if (receive_thread_id_ != 0) {
        pthread_join(receive_thread_id_, NULL);
        receive_thread_id_ = 0;
    }

    if (socket_fd_ >= 0) {
        ::close(socket_fd_);
        socket_fd_ = -1;
    }

    bound_ = false;
    std::cout << "[UDP_SDP_PORT] Socket closed." << std::endl;
}

bool V2G__UDP__SDP__Port::send_sdp_request(const unsigned char* data, size_t length) {
    if (socket_fd_ < 0) {
        std::cerr << "[UDP_SDP_PORT] ERROR: Socket not initialized" << std::endl;
        return false;
    }

    std::string target_addr = ipv6_multicast_.empty() ? "ff02::1" : ipv6_multicast_;

    ::sockaddr_in6 dest_addr;
    if (parse_ipv6_address(target_addr, &dest_addr) != 0) {
        std::cerr << "[UDP_SDP_PORT] ERROR: Failed to parse destination address" << std::endl;
        return false;
    }

    std::cout << "[UDP_SDP_PORT] Sending SDP request to " << target_addr << "%" << network_interface_ << ":" << port_ << std::endl;

    ssize_t sent = ::sendto(socket_fd_, data, length, 0,
                          (struct sockaddr*)&dest_addr, sizeof(dest_addr));

    if (sent < 0) {
        std::cerr << "[UDP_SDP_PORT] ERROR: sendto() failed: " << strerror(errno) << std::endl;
        return false;
    }

    std::cout << "[UDP_SDP_PORT] Sent " << sent << " bytes to multicast address" << std::endl;
    return true;
}

bool V2G__UDP__SDP__Port::parse_sdp_response(const unsigned char* data, size_t length,
                                              std::string& secc_ip, int& secc_port) {
    if (length < 8) {
        std::cerr << "[UDP_SDP_PORT] ERROR: Response too short for V2GTP header" << std::endl;
        return false;
    }

    uint8_t version = data[0];
    uint8_t inv_version = data[1];
    uint16_t payload_type = (data[2] << 8) | data[3];
    uint32_t payload_length = (data[4] << 24) | (data[5] << 16) | (data[6] << 8) | data[7];

    std::cout << "[UDP_SDP_PORT] V2GTP Header: version=" << (int)version
              << ", inv_version=" << (int)inv_version
              << ", payload_type=" << payload_type
              << ", payload_length=" << payload_length << std::endl;

    if (version != 1 || inv_version != 254) {
        std::cerr << "[UDP_SDP_PORT] ERROR: Invalid V2GTP version header" << std::endl;
        return false;
    }

    if (payload_type != 0x9001) {
        std::cerr << "[UDP_SDP_PORT] ERROR: Unexpected payload type: " << payload_type << std::endl;
        return false;
    }

    size_t header_len = 8;
    if (length < header_len + 20) {
        std::cerr << "[UDP_SDP_PORT] ERROR: Response too short for SDP body" << std::endl;
        return false;
    }

    const unsigned char* body = data + header_len;

    uint8_t security_protocol = body[0];
    uint8_t transport_protocol = body[1];

    std::cout << "[UDP_SDP_PORT] SDP Body: security=" << (int)security_protocol
              << ", transport=" << (int)transport_protocol << std::endl;

    secc_port = (body[16] << 8) | body[17];

    char ip_str[INET6_ADDRSTRLEN];
    struct in6_addr addr;
    memcpy(&addr, body + 0, 16);

    if (inet_ntop(AF_INET6, &addr, ip_str, sizeof(ip_str)) == NULL) {
        std::cerr << "[UDP_SDP_PORT] ERROR: Failed to convert IPv6 address" << std::endl;
        return false;
    }

    secc_ip = ip_str;
    if (!network_interface_.empty()) {
        secc_ip += "%" + network_interface_;
    }

    std::cout << "[UDP_SDP_PORT] Parsed SECC address: " << secc_ip << ":" << secc_port << std::endl;

    V2G__UDP__SDP__Port__last_secc_ip_ = ip_str;
    V2G__UDP__SDP__Port__last_secc_port_ = secc_port;

    return true;
}

void* V2G__UDP__SDP__Port::receive_thread_func(void* arg) {
    V2G__UDP__SDP__Port* port = static_cast<V2G__UDP__SDP__Port*>(arg);
    port->receive_loop();
    return NULL;
}

void V2G__UDP__SDP__Port::receive_loop() {
    const size_t BUFFER_SIZE = 8192;
    unsigned char buffer[BUFFER_SIZE];
    ::sockaddr_in6 sender_addr;
    socklen_t sender_len = sizeof(sender_addr);

    std::cout << "[UDP_SDP_PORT] Receive thread started" << std::endl;

    while (receive_thread_running_) {
        fd_set read_fds;
        FD_ZERO(&read_fds);
        FD_SET(socket_fd_, &read_fds);

        struct timeval timeout;
        timeout.tv_sec = 1;
        timeout.tv_usec = 0;

        int ret = select(socket_fd_ + 1, &read_fds, NULL, NULL, &timeout);

        if (ret < 0) {
            if (errno == EINTR) continue;
            std::cerr << "[UDP_SDP_PORT] ERROR: select() failed: " << strerror(errno) << std::endl;
            break;
        } else if (ret == 0) {
            continue;
        }

        if (FD_ISSET(socket_fd_, &read_fds)) {
            ssize_t received = ::recvfrom(socket_fd_, buffer, BUFFER_SIZE, 0,
                                       (struct sockaddr*)&sender_addr, &sender_len);

            if (received < 0) {
                std::cerr << "[UDP_SDP_PORT] ERROR: recvfrom() failed: " << strerror(errno) << std::endl;
                break;
            }

            char sender_ip[INET6_ADDRSTRLEN];
            inet_ntop(AF_INET6, &sender_addr.sin6_addr, sender_ip, sizeof(sender_ip));
            std::cout << "[UDP_SDP_PORT] Received " << received << " bytes from " << sender_ip << std::endl;

            std::string secc_ip;
            int secc_port;
            if (parse_sdp_response(buffer, received, secc_ip, secc_port)) {
                std::cout << "[UDP_SDP_PORT] Valid SDP response from SECC!" << std::endl;
            }

            OCTETSTRING received_data(received, buffer);
            incoming_message(received_data);
        }
    }

    std::cout << "[UDP_SDP_PORT] Receive thread exiting" << std::endl;
}

}
