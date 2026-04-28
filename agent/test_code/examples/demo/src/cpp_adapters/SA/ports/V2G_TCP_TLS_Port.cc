#include "V2G_TCP_TLS_Port.hh"
#include <iostream>
#include <cstring>
#include <unistd.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <arpa/inet.h>
#include <netdb.h>
#include <ifaddrs.h>
#include <net/if.h>
#include <errno.h>
#include <chrono>
#include <thread>

extern std::string V2G__UDP__SDP__Port__last_secc_ip_;
extern int V2G__UDP__SDP__Port__last_secc_port_;

namespace SECC__TestSystem {

V2G__TCP__TLS__Port::V2G__TCP__TLS__Port(const char *par_port_name)
    : V2G__TCP__TLS__Port_BASE(par_port_name),
      port_name_(par_port_name ? par_port_name : ""),
      port_(15118),
      tls_enabled_(false),
      use_ipv6_(false),
      socket_fd_(-1),
      connected_(false),
      receive_thread_running_(false),
      receive_thread_id_(0) {
    std::cerr << "[TCP_TLS_PORT] CONSTRUCTOR: port_name=" << port_name_ << std::endl;
}

V2G__TCP__TLS__Port::~V2G__TCP__TLS__Port() {
    disconnect_from_secc();
}

void V2G__TCP__TLS__Port::set_parameter(const char *parameter_name, const char *parameter_value) {
    std::string name(parameter_name ? parameter_name : "null");
    std::string value(parameter_value ? parameter_value : "null");

    std::cerr << "[TCP_TLS_PORT] set_parameter: " << name << " = " << value << std::endl;

    if (name == "port") {
        port_ = std::stoi(value);
    } else if (name == "network_interface") {
        network_interface_ = value;
    } else if (name == "ipv6_multicast") {
        ipv6_multicast_ = value;
    } else if (name == "ip_address" || name == "ip") {
        ip_address_ = value;
    } else if (name == "tls_enabled") {
        tls_enabled_ = (value == "true" || value == "1" || value == "yes");
    }
}

void V2G__TCP__TLS__Port::Handle_Message(const unsigned char *message, size_t length) {
    std::cout << "[TCP_TLS_PORT] Handle_Message: " << length << " bytes" << std::endl;
}

void V2G__TCP__TLS__Port::outgoing_message(const unsigned char *message, size_t length) {
    std::cout << "[TCP_TLS_PORT] Sending " << length << " bytes via outgoing_message" << std::endl;
    send_v2gtp_frame(message, length);
}

void V2G__TCP__TLS__Port::outgoing_send(const OCTETSTRING& send_par) {
    size_t length = send_par.lengthof();
    const unsigned char* data = send_par;

    std::cerr << "[TCP_TLS_PORT] outgoing_send(): " << length << " bytes, connected_=" << connected_ << std::endl;

    if (ip_address_.empty()) {
        char* env_ip = getenv("SECC_IP");
        if (env_ip) {
            ip_address_ = env_ip;
            std::cerr << "[TCP_TLS_PORT] Using SECC_IP from env: " << ip_address_ << std::endl;
        } else {
            ip_address_ = V2G__UDP__SDP__Port__last_secc_ip_;
            std::cerr << "[TCP_TLS_PORT] Using SDP response IP: " << ip_address_ << std::endl;
        }
    }

    if (port_ == 15118 && V2G__UDP__SDP__Port__last_secc_port_ != 15118) {
        port_ = V2G__UDP__SDP__Port__last_secc_port_;
        std::cerr << "[TCP_TLS_PORT] Using SDP response port: " << port_ << std::endl;
    }

    if (!connected_) {
        std::cerr << "[TCP_TLS_PORT] Attempting to connect to " << ip_address_ << ":" << port_ << std::endl;
        bool connected = false;
        for (int attempt = 0; attempt < 5 && !connected; ++attempt) {
            if (attempt > 0) {
                std::cerr << "[TCP_TLS_PORT] Retry " << attempt << "/5 in 1s..." << std::endl;
                std::this_thread::sleep_for(std::chrono::seconds(1));
            }
            disconnect_from_secc();
            connect_to_secc();
            connected = connected_;
        }
        if (!connected_) {
            std::cerr << "[TCP_TLS_PORT] ERROR: All connection attempts failed" << std::endl;
            return;
        }
    }

    std::cerr << "[TCP_TLS_PORT] Forwarding " << length << " bytes to SECC" << std::endl;
    send_v2gtp_frame(data, length);
}

void V2G__TCP__TLS__Port::send_data(const OCTETSTRING& send_data, const char* send_fmt, ...) {
    size_t length = send_data.lengthof();
    const unsigned char* data = send_data;

    std::cout << "[TCP_TLS_PORT] send_data(): " << length << " bytes" << std::endl;

    if (!connected_ && !ip_address_.empty()) {
        connect_to_secc();
    }

    if (connected_) {
        send_v2gtp_frame(data, length);
    } else {
        std::cerr << "[TCP_TLS_PORT] ERROR: Not connected, cannot send" << std::endl;
    }
}

int V2G__TCP__TLS__Port::parse_ipv6_address(const std::string& addr_str, ::sockaddr_in6* addr6) {
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
        struct addrinfo hints, *result;
        memset(&hints, 0, sizeof(hints));
        hints.ai_family = AF_INET6;
        hints.ai_socktype = SOCK_STREAM;

        int ret = getaddrinfo(addr_copy.c_str(), NULL, &hints, &result);
        if (ret == 0 && result != NULL) {
            memcpy(addr6, result->ai_addr, sizeof(::sockaddr_in6));
            addr6->sin6_port = htons(port_);
            freeaddrinfo(result);
        } else {
            std::cerr << "[TCP_TLS_PORT] ERROR: Failed to parse IPv6 address: " << addr_str << std::endl;
            return -1;
        }
    }

    if (!scope_id.empty()) {
        addr6->sin6_scope_id = if_nametoindex(scope_id.c_str());
        if (addr6->sin6_scope_id == 0) {
            std::cerr << "[TCP_TLS_PORT] ERROR: Invalid interface name: " << scope_id << std::endl;
            return -1;
        }
    } else if (!network_interface_.empty()) {
        unsigned int if_idx = if_nametoindex(network_interface_.c_str());
        if (if_idx != 0) {
            addr6->sin6_scope_id = if_idx;
        }
    }

    return 0;
}

void V2G__TCP__TLS__Port::connect_to_secc() {
    if (ip_address_.empty()) {
        std::cerr << "[TCP_TLS_PORT] ERROR: No IP address configured" << std::endl;
        return;
    }

    std::cout << "[TCP_TLS_PORT] Connecting to SECC at " << ip_address_ << ":" << port_ << std::endl;

    use_ipv6_ = (ip_address_.find(':') != std::string::npos);

    if (use_ipv6_) {
        struct addrinfo hints, *result, *rp;
        memset(&hints, 0, sizeof(hints));
        hints.ai_family = AF_INET6;
        hints.ai_socktype = SOCK_STREAM;
        hints.ai_protocol = IPPROTO_TCP;

        char port_str[8];
        snprintf(port_str, sizeof(port_str), "%d", port_);

        std::string addr_for_gai = ip_address_;
        if (addr_for_gai.find('%') == std::string::npos) {
            if (!network_interface_.empty()) {
                addr_for_gai += "%" + network_interface_;
            } else {
                addr_for_gai += "%ens33";
            }
        }

        int gai_ret = getaddrinfo(addr_for_gai.c_str(), port_str, &hints, &result);
        if (gai_ret != 0) {
            std::cerr << "[TCP_TLS_PORT] ERROR: getaddrinfo() failed: " << gai_strerror(gai_ret) << std::endl;
            return;
        }

        bool connected = false;
        for (rp = result; rp != NULL; rp = rp->ai_next) {
            socket_fd_ = ::socket(rp->ai_family, rp->ai_socktype, rp->ai_protocol);
            if (socket_fd_ < 0) continue;

            int no_delay = 1;
            ::setsockopt(socket_fd_, IPPROTO_TCP, TCP_NODELAY, &no_delay, sizeof(no_delay));

            if (::connect(socket_fd_, rp->ai_addr, rp->ai_addrlen) == 0) {
                connected = true;
                break;
            } else {
                ::close(socket_fd_);
                socket_fd_ = -1;
            }
        }
        freeaddrinfo(result);

        if (!connected) {
            std::cerr << "[TCP_TLS_PORT] ERROR: All connect() attempts failed: " << strerror(errno) << std::endl;
            return;
        }
    } else {
        struct sockaddr_in addr;
        memset(&addr, 0, sizeof(addr));
        addr.sin_family = AF_INET;
        addr.sin_port = htons(port_);

        if (inet_pton(AF_INET, ip_address_.c_str(), &addr.sin_addr) != 1) {
            std::cerr << "[TCP_TLS_PORT] ERROR: Failed to parse IPv4 address: " << ip_address_ << std::endl;
            return;
        }

        socket_fd_ = ::socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
        if (socket_fd_ < 0) {
            std::cerr << "[TCP_TLS_PORT] ERROR: Failed to create IPv4 socket: " << strerror(errno) << std::endl;
            return;
        }

        std::cout << "[TCP_TLS_PORT] Connecting to IPv4 address..." << std::endl;
        if (::connect(socket_fd_, (struct sockaddr*)&addr, sizeof(addr)) < 0) {
            std::cerr << "[TCP_TLS_PORT] ERROR: connect() failed: " << strerror(errno) << std::endl;
            ::close(socket_fd_);
            socket_fd_ = -1;
            return;
        }
    }

    connected_ = true;
    std::cout << "[TCP_TLS_PORT] Connected to SECC successfully!" << std::endl;

    receive_thread_running_ = true;
    if (pthread_create(&receive_thread_id_, NULL, &V2G__TCP__TLS__Port::receive_thread_func, this) != 0) {
        std::cerr << "[TCP_TLS_PORT] ERROR: Failed to create receive thread" << std::endl;
        receive_thread_running_ = false;
    }
}

void V2G__TCP__TLS__Port::disconnect_from_secc() {
    std::cout << "[TCP_TLS_PORT] Disconnecting from SECC..." << std::endl;

    receive_thread_running_ = false;

    if (receive_thread_id_ != 0) {
        pthread_join(receive_thread_id_, NULL);
        receive_thread_id_ = 0;
    }

    if (socket_fd_ >= 0) {
        ::close(socket_fd_);
        socket_fd_ = -1;
    }

    connected_ = false;
}

bool V2G__TCP__TLS__Port::send_v2gtp_frame(const unsigned char* data, size_t length) {
    if (socket_fd_ < 0 || !connected_) {
        std::cerr << "[TCP_TLS_PORT] ERROR: Not connected, cannot send frame" << std::endl;
        return false;
    }

    ssize_t sent = ::send(socket_fd_, data, length, 0);
    if (sent < 0) {
        std::cerr << "[TCP_TLS_PORT] ERROR: send() failed: " << strerror(errno) << std::endl;
        return false;
    }

    std::cout << "[TCP_TLS_PORT] Sent " << sent << " bytes" << std::endl;
    return true;
}

void* V2G__TCP__TLS__Port::receive_thread_func(void* arg) {
    V2G__TCP__TLS__Port* port = static_cast<V2G__TCP__TLS__Port*>(arg);
    port->receive_loop();
    return NULL;
}

void V2G__TCP__TLS__Port::receive_loop() {
    const size_t BUFFER_SIZE = 8192;
    unsigned char buffer[BUFFER_SIZE];

    std::cout << "[TCP_TLS_PORT] Receive thread started" << std::endl;

    while (receive_thread_running_) {
        if (socket_fd_ < 0) {
            break;
        }

        fd_set read_fds;
        FD_ZERO(&read_fds);
        FD_SET(socket_fd_, &read_fds);

        struct timeval timeout;
        timeout.tv_sec = 1;
        timeout.tv_usec = 0;

        int ret = select(socket_fd_ + 1, &read_fds, NULL, NULL, &timeout);

        if (ret < 0) {
            if (errno == EINTR) continue;
            std::cerr << "[TCP_TLS_PORT] ERROR: select() failed: " << strerror(errno) << std::endl;
            break;
        } else if (ret == 0) {
            continue;
        }

        if (FD_ISSET(socket_fd_, &read_fds)) {
            ssize_t received = ::recv(socket_fd_, buffer, BUFFER_SIZE, 0);

            if (received < 0) {
                std::cerr << "[TCP_TLS_PORT] ERROR: recv() failed: " << strerror(errno) << std::endl;
                break;
            } else if (received == 0) {
                std::cout << "[TCP_TLS_PORT] Connection closed by peer" << std::endl;
                break;
            }

            std::cout << "[TCP_TLS_PORT] Received " << received << " bytes" << std::endl;

            OCTETSTRING received_data(received, buffer);
            incoming_message(received_data);
        }
    }

    std::cout << "[TCP_TLS_PORT] Receive thread exiting" << std::endl;
    connected_ = false;
}

}
