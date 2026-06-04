#include "V2G_TCP_TLS_Port.hh"
#include "../../src/cpp_adapters/CD/exi_engine/ExiCodecAdapter.hh"
#include "../../src/cpp_adapters/common/StaticExiDecoder.hh"
#include <iostream>
#include <sstream>
#include <iomanip>
#include <cstring>
#include <unistd.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <arpa/inet.h>
#include <netdb.h>
#include <net/if.h>
#include <errno.h>
#include <chrono>
#include <thread>

extern ::std::string V2G__UDP__SDP__Port__last_secc_ip_;
extern int V2G__UDP__SDP__Port__last_secc_port_;

// ============================================================================
// V2GTP constants and helper functions
// ============================================================================
namespace {
    static const size_t V2GTP_HEADER_SIZE = 8;
    static const uint8_t V2GTP_VERSION = 1;
    static const uint8_t V2GTP_INV_VERSION = 0xFE;
    static const uint16_t V2GTP_PAYLOAD_TYPE_EXI = 0x8001;

    static ::std::string hex_bytes(const unsigned char* data, size_t len) {
        ::std::ostringstream oss;
        for (size_t i = 0; i < len; ++i) {
            oss << ::std::hex << ::std::setw(2) << ::std::setfill('0') << (int)data[i] << " ";
        }
        return oss.str();
    }

    // TCP reassembly: extract one complete frame from a byte buffer.
    //
    // TLS unwrapping: if buffer starts with 0x17 0x03 0x03, treat the rest as
    // V2GTP data (trust the actual bytes received, not the TLS length field, because
    // some SECC implementations send incorrect TLS length values).
    //
    // For raw TCP: use V2GTP header's length field as usual.
    //
    // Returns 1 on success (frame extracted), 0 if need more data, -1 on error.
    static int extract_v2gtp_frame(::std::vector<uint8_t>& buf,
                                   ::std::vector<uint8_t>& frame_out) {
        if (buf.empty()) return 0;

        // If TLS record (0x17 0x03 0x03), strip the 5-byte header and use all
        // remaining bytes as the V2GTP frame.  Trust actual bytes received.
        if (buf.size() >= 5 && buf[0] == 0x17 && buf[1] == 0x03 && buf[2] == 0x03) {
            // Strip TLS header (5 bytes): [0x17][0x03][0x03][len_hi][len_lo]
            // The rest is the V2GTP frame (may include trailing data from next record)
            if (buf.size() < 6) return 0;  // Need at least the header + 1 byte of payload

            size_t inner_len = buf.size() - 5;  // Actual bytes after TLS header
            ::std::cout << "[TCP_TLS_PORT] TLS: inner=" << inner_len << "B" << ::std::endl;

            // Trust actual bytes received; ignore V2GTP header's payload_len
            // because some SECC implementations send incorrect length fields.
            if (inner_len < V2GTP_HEADER_SIZE) {
                ::std::cerr << "[TCP_TLS_PORT] TLS: inner too short: " << inner_len << ::std::endl;
                return -1;
            }

            // Read V2GTP payload_len just for logging
            uint32_t v2gtp_payload = (static_cast<uint32_t>(buf[8]) << 24) |
                                    (static_cast<uint32_t>(buf[9]) << 16) |
                                    (static_cast<uint32_t>(buf[10]) << 8) |
                                    static_cast<uint32_t>(buf[11]);
            ::std::cout << "[TCP_TLS_PORT] V2GTP: payload_len field=" << v2gtp_payload
                      << " using actual inner=" << inner_len << ::std::endl;

            // Use ALL remaining bytes as the V2GTP frame. The TLS record's declared length
            // may be wrong; the actual received bytes are always correct.
            // We will let the V2GTP payload_len field be trusted via actual_exi_len
            // in decode_and_deliver().
            frame_out.assign(buf.begin() + 5, buf.end());
            buf.clear();
            return 1;
        }

    // Raw V2GTP over TCP (or TLS inner)
    if (buf.size() < V2GTP_HEADER_SIZE) return 0;
    uint32_t v2gtp_payload_len = (static_cast<uint32_t>(buf[4]) << 24) |
                              (static_cast<uint32_t>(buf[5]) << 16) |
                              (static_cast<uint32_t>(buf[6]) << 8) |
                              static_cast<uint32_t>(buf[7]);
    size_t v2gtp_declared_total = V2GTP_HEADER_SIZE + v2gtp_payload_len;

    // Use the minimum of declared total and actual buffer size.
    // Some SECC implementations send incorrect V2GTP payload_len fields (off by a few bytes).
    // We never wait for data that will never arrive — use what we have.
    size_t frame_len = (buf.size() < v2gtp_declared_total) ? buf.size() : v2gtp_declared_total;
    if (frame_len < V2GTP_HEADER_SIZE) {
        ::std::cerr << "[TCP_TLS_PORT] V2GTP: frame too short: " << frame_len << ::std::endl;
        return -1;
    }

    ::std::cout << "[TCP_TLS_PORT] V2GTP raw: declared_total=" << v2gtp_declared_total
              << " using=" << frame_len << ::std::endl;

    frame_out.assign(buf.begin(), buf.begin() + frame_len);
    buf.erase(buf.begin(), buf.begin() + frame_len);
    return 1;
    }
}

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
      receive_thread_id_(0),
      exi_decoder_(nullptr),
      receive_buffer_(),
      cached_secc_port_(0) {
    exi_decoder_ = new ::SECC__ExiJni::StaticExiDecoder();
    if (!::ExiCodecAdapter::instance().initialize()) {
        ::std::cerr << "[TCP_TLS_PORT] WARNING: ExiCodecAdapter init failed: "
                  << ::ExiCodecAdapter::instance().get_last_error() << ::std::endl;
    } else {
        ::std::cout << "[TCP_TLS_PORT] ExiCodecAdapter: "
                  << ::ExiCodecAdapter::instance().library_count()
                  << " payloads from " << ::ExiCodecAdapter::instance().library_path() << ::std::endl;
    }
    // Load pattern descriptors from JSON (pure data-driven approach)
    // Binary runs from build/bin, so JSON is at ../../exi_payload_library.json
    ::std::string json_path = "../../exi_payload_library.json";
    if (!exi_decoder_->load_from_json(json_path)) {
        ::std::cerr << "[TCP_TLS_PORT] WARNING: StaticExiDecoder JSON load failed: "
                  << exi_decoder_->last_error() << ::std::endl;
    } else {
        ::std::cout << "[TCP_TLS_PORT] Static decoder: JSON patterns loaded OK" << ::std::endl;
    }
    ::std::cout << "[TCP_TLS_PORT] Constructor: " << port_name_ << " initialized" << ::std::endl;
}

V2G__TCP__TLS__Port::~V2G__TCP__TLS__Port() {
    disconnect_from_secc();
    delete exi_decoder_;
    exi_decoder_ = nullptr;
    ::std::cout << "[TCP_TLS_PORT] Destructor: " << port_name_ << ::std::endl;
}

void V2G__TCP__TLS__Port::set_parameter(const char *parameter_name, const char *parameter_value) {
    ::std::string name(parameter_name);
    ::std::string value(parameter_value ? parameter_value : "");
    ::std::cout << "[TCP_TLS_PORT] set_parameter(" << name << "=" << value << ")" << ::std::endl;
    if (name == "port") {
        port_ = ::std::stoi(value);
    } else if (name == "network_interface") {
        network_interface_ = value;
    } else if (name == "ipv6_multicast") {
        ipv6_multicast_ = value;
    } else if (name == "ip_address" || name == "ip") {
        ip_address_ = value;
    } else if (name == "tls_enabled") {
        tls_enabled_ = (value == "1" || value == "true" || value == "yes");
    }
}

void V2G__TCP__TLS__Port::Handle_Message(const unsigned char *message, size_t length) {
    ::std::cout << "[TCP_TLS_PORT] Handle_Message: " << length << " bytes" << ::std::endl;
}

void V2G__TCP__TLS__Port::outgoing_message(const unsigned char *message, size_t length) {
    ::std::cout << "[TCP_TLS_PORT] outgoing_message: " << length << " bytes" << ::std::endl;
}

bool V2G__TCP__TLS__Port::encode_union_to_wire(
        const ::SECC__Types::V2G__MessageUnion& msg_union,
        ::std::vector<uint8_t>& wire_output) {
    using UT = ::SECC__Types::V2G__MessageUnion;
    UT::union_selection_type sel = msg_union.get_selection();
    const char* key = NULL;

    switch (sel) {
        case UT::ALT_supportedAppProtocolReq:    key = "supported_app_protocol_req"; break;
        case UT::ALT_supportedAppProtocolRes:    key = "supported_app_protocol_res"; break;
        case UT::ALT_sessionSetupReq:            key = "session_setup_req"; break;
        case UT::ALT_sessionSetupRes:            key = "session_setup_res"; break;
        case UT::ALT_serviceDiscoveryReq:        key = "service_discovery_req"; break;
        case UT::ALT_serviceDiscoveryRes:        key = "service_discovery_res"; break;
        case UT::ALT_servicePaymentSelectionReq: key = "service_payment_selection_req"; break;
        case UT::ALT_servicePaymentSelectionRes: key = "service_payment_selection_res"; break;
        case UT::ALT_paymentDetailsReq:         key = "payment_details_req"; break;
        case UT::ALT_paymentDetailsRes:          key = "payment_details_res"; break;
        case UT::ALT_authorizationReq:          key = "authorization_req"; break;
        case UT::ALT_authorizationRes:           key = "authorization_res"; break;
        case UT::ALT_certificateInstallationReq:   key = "certificate_installation_req"; break;
        case UT::ALT_certificateInstallationRes:  key = "certificate_installation_res"; break;
        case UT::ALT_certificateUpdateReq:        key = "certificate_update_req"; break;
        case UT::ALT_certificateUpdateRes:        key = "certificate_update_res"; break;
        case UT::ALT_cableCheckReq:             key = "cable_check_req"; break;
        case UT::ALT_cableCheckRes:             key = "cable_check_res"; break;
        case UT::ALT_chargeParameterDiscoveryReq: key = "charge_parameter_discovery_req"; break;
        case UT::ALT_chargeParameterDiscoveryRes: key = "charge_parameter_discovery_res"; break;
        case UT::ALT_powerDeliveryReq:          key = "power_delivery_req"; break;
        case UT::ALT_powerDeliveryRes:          key = "power_delivery_res"; break;
        case UT::ALT_sessionStopReq:            key = "session_stop_req"; break;
        case UT::ALT_sessionStopRes:            key = "session_stop_res"; break;
        case UT::ALT_chargingStatusReq:         key = "charging_status_req"; break;
        case UT::ALT_chargingStatusRes:         key = "charging_status_res"; break;
        case UT::ALT_currentDemandReq:          key = "current_demand_req"; break;
        case UT::ALT_currentDemandRes:           key = "current_demand_res"; break;
        case UT::ALT_meterReceiptReq:           key = "meter_receipt_req"; break;
        case UT::ALT_meterReceiptRes:           key = "meter_receipt_res"; break;
        case UT::ALT_preChargeReq:              key = "pre_charge_req"; break;
        case UT::ALT_preChargeRes:              key = "pre_charge_res"; break;
        case UT::ALT_weldingDetectionReq:       key = "welding_detection_req"; break;
        case UT::ALT_weldingDetectionRes:        key = "welding_detection_res"; break;
        case UT::ALT_serviceDetailsReq:         key = "service_details_req"; break;
        case UT::ALT_serviceDetailsRes:         key = "service_details_res"; break;
        default: key = NULL;
    }

    if (key == NULL) {
        ::std::cerr << "[TCP_TLS_PORT] ERROR: Unknown union selection: " << (int)sel << ::std::endl;
        return false;
    }

    ::std::cout << "[TCP_TLS_PORT] Encoding: " << key << ::std::endl;
    ::ExiCodecAdapter::instance().encode(key, current_session_id_, wire_output);

    if (wire_output.empty()) {
        ::std::cerr << "[TCP_TLS_PORT] ERROR: ExiCodecAdapter returned empty" << ::std::endl;
        return false;
    }

    // Log V2GTP header of the encoded wire output
    if (wire_output.size() >= 8) {
        uint32_t v2gtp_len = (static_cast<uint32_t>(wire_output[4]) << 24) |
                            (static_cast<uint32_t>(wire_output[5]) << 16) |
                            (static_cast<uint32_t>(wire_output[6]) << 8) |
                            static_cast<uint32_t>(wire_output[7]);
        ::std::cout << "[TCP_TLS_PORT] Encoded V2GTP: total=" << wire_output.size()
                  << " payload_len=" << v2gtp_len << ::std::endl;
    }

    ::std::cout << "[TCP_TLS_PORT] Encoded: " << wire_output.size() << " bytes" << ::std::endl;
    return true;
}

void V2G__TCP__TLS__Port::outgoing_send(const ::SECC__Types::V2G__MessageUnion& send_par) {
    ::std::vector<uint8_t> wire_bytes;
    ::std::cout << "[TCP_TLS_PORT] outgoing_send() called" << ::std::endl;
    if (!encode_union_to_wire(send_par, wire_bytes)) return;

    // Do NOT reconnect if already connected. The receive thread is listening on the
    // existing socket. Creating a new socket orphans that socket and the SECC's
    // response will never be received.
    if (!connected_ || socket_fd_ < 0) {
        ::std::cerr << "[TCP_TLS_PORT] WARNING: Not connected, connecting..." << ::std::endl;
        connect_to_secc();
    }
    if (connected_) {
        ::std::cout << "[TCP_TLS_PORT] Sending " << wire_bytes.size() << " bytes over TCP (socket=" << socket_fd_ << ")" << ::std::endl;
        send_wire_bytes(wire_bytes.data(), wire_bytes.size());
    } else {
        ::std::cerr << "[TCP_TLS_PORT] ERROR: Not connected" << ::std::endl;
    }
}

void V2G__TCP__TLS__Port::send_data(const ::SECC__Types::V2G__MessageUnion& send_data,
                                      const char* /* send_fmt */, ...) {
    outgoing_send(send_data);
}

int V2G__TCP__TLS__Port::parse_ipv6_address(const ::std::string& addr_str, ::sockaddr_in6* addr6) {
    if (!addr6) return -1;
    ::std::memset(addr6, 0, sizeof(::sockaddr_in6));
    addr6->sin6_family = AF_INET6;
    addr6->sin6_port = htons(port_);

    ::std::string addr_copy = addr_str;
    ::std::string scope_id;
    size_t scope_pos = addr_copy.find('%');
    if (scope_pos != ::std::string::npos) {
        scope_id = addr_copy.substr(scope_pos + 1);
        addr_copy = addr_copy.substr(0, scope_pos);
    }

    if (scope_id.empty() && !network_interface_.empty()) {
        scope_id = network_interface_;
    } else if (scope_id.empty()) {
        ::ifaddrs* ifaddr = nullptr;
        if (::getifaddrs(&ifaddr) == 0 && ifaddr != nullptr) {
            for (::ifaddrs* ifa = ifaddr; ifa != nullptr; ifa = ifa->ifa_next) {
                if (ifa->ifa_addr == nullptr || ifa->ifa_addr->sa_family != AF_INET6) continue;
                ::sockaddr_in6* a6 = reinterpret_cast<::sockaddr_in6*>(ifa->ifa_addr);
                char buf[INET6_ADDRSTRLEN];
                if (::inet_ntop(AF_INET6, &a6->sin6_addr, buf, sizeof(buf)) &&
                    ::strncmp(buf, "fe80::", 6) == 0) {
                    scope_id = ifa->ifa_name;
                    break;
                }
            }
            ::freeifaddrs(ifaddr);
        }
    }

    if (::inet_pton(AF_INET6, addr_copy.c_str(), &addr6->sin6_addr) != 1) {
        ::std::cerr << "[TCP_TLS_PORT] ERROR: parse_ipv6 failed: " << addr_str << ::std::endl;
        return -1;
    }
    if (!scope_id.empty()) {
        addr6->sin6_scope_id = if_nametoindex(scope_id.c_str());
        if (addr6->sin6_scope_id == 0) {
            ::std::cerr << "[TCP_TLS_PORT] ERROR: Invalid interface: " << scope_id << ::std::endl;
            return -1;
        }
    }
    return 0;
}

void V2G__TCP__TLS__Port::connect_to_secc() {
    // Guard delay: allow the SECC socket to become fully active after SDP discovery.
    // Per ISO 15118-3, the SECC may need a brief moment to open its listening socket.
    ::std::this_thread::sleep_for(::std::chrono::milliseconds(75));

    ::std::string target_ip;
    int target_port = 0;

    const char* sec_ip_env = ::getenv("SECC_IP");
    if (sec_ip_env != NULL && ::strlen(sec_ip_env) > 0) {
        target_ip = sec_ip_env;
    } else {
        target_ip = V2G__UDP__SDP__Port__last_secc_ip_;
    }

    if (target_ip.empty()) {
        ::std::cerr << "[TCP_TLS_PORT] ERROR: No target IP" << ::std::endl;
        return;
    }

    // Determine which port to connect to, in priority order:
    // 1. If already connected and socket is alive: use getpeername (SECC's actual port)
    // 2. If cached_secc_port_ is set from a previous successful connection: use it
    // 3. If SDP returned a non-default dynamic port: use it
    // 4. Fallback: use the configured port (15118)
    if (connected_ && socket_fd_ >= 0) {
        struct sockaddr_in6 peer_addr;
        socklen_t addrlen = sizeof(peer_addr);
        if (::getpeername(socket_fd_, (struct sockaddr*)&peer_addr, &addrlen) == 0) {
            target_port = ntohs(peer_addr.sin6_port);
            ::std::cout << "[TCP_TLS_PORT] Already connected, SECC port: " << target_port << ::std::endl;
        } else {
            // Socket is dead (SECC closed the connection). Clean up and reconnect.
            ::std::cerr << "[TCP_TLS_PORT] WARNING: Old socket dead, closing and reconnecting" << ::std::endl;
            connected_ = false;
            receive_thread_running_ = false;
            if (socket_fd_ >= 0) {
                ::shutdown(socket_fd_, SHUT_RDWR);
                ::close(socket_fd_);
                socket_fd_ = -1;
            }
            if (receive_thread_id_ != 0) {
                ::pthread_join(receive_thread_id_, NULL);
                receive_thread_id_ = 0;
            }
            // Reconnect: try cached port first, then SDP port, then default
            if (cached_secc_port_ > 0) {
                target_port = cached_secc_port_;
            } else if (V2G__UDP__SDP__Port__last_secc_port_ != port_) {
                target_port = V2G__UDP__SDP__Port__last_secc_port_;
            } else {
                target_port = port_;
            }
        }
    } else {
        // Not connected: use the best available port
        if (cached_secc_port_ > 0) {
            target_port = cached_secc_port_;
            ::std::cout << "[TCP_TLS_PORT] Using cached SECC port: " << target_port << ::std::endl;
        } else if (V2G__UDP__SDP__Port__last_secc_port_ != port_) {
            target_port = V2G__UDP__SDP__Port__last_secc_port_;
            ::std::cout << "[TCP_TLS_PORT] Using SDP dynamic port: " << target_port << ::std::endl;
        } else {
            target_port = port_;
        }
    }

    ::std::cout << "[TCP_TLS_PORT] Connecting to " << target_ip << ":" << target_port << ::std::endl;

    bool is_ipv6 = (target_ip.find(':') != ::std::string::npos);
    use_ipv6_ = is_ipv6;
    int domain = is_ipv6 ? AF_INET6 : AF_INET;
    socket_fd_ = ::socket(domain, SOCK_STREAM, IPPROTO_TCP);
    if (socket_fd_ < 0) {
        ::std::cerr << "[TCP_TLS_PORT] ERROR: socket() failed: " << ::strerror(errno) << ::std::endl;
        return;
    }

    bool ok = false;
    for (int retry = 0; retry < 5 && !ok; ++retry) {
        if (retry > 0) {
            ::std::cout << "[TCP_TLS_PORT] Retry " << retry + 1 << "/5" << ::std::endl;
            ::std::this_thread::sleep_for(::std::chrono::milliseconds(500));
        }
        if (is_ipv6) {
            ::sockaddr_in6 addr6;
            if (parse_ipv6_address(target_ip, &addr6) != 0) continue;
            addr6.sin6_port = htons(target_port);
            ::std::cout << "[TCP_TLS_PORT] IPv6 connect to " << target_ip << ":" << target_port << ::std::endl;
            ok = (::connect(socket_fd_, (struct sockaddr*)&addr6, sizeof(addr6)) == 0);
            if (!ok) ::std::cerr << "[TCP_TLS_PORT] IPv6 connect failed: " << ::strerror(errno) << ::std::endl;
        } else {
            ::sockaddr_in addr4;
            ::std::memset(&addr4, 0, sizeof(addr4));
            addr4.sin_family = AF_INET;
            addr4.sin_port = htons(target_port);
            if (::inet_pton(AF_INET, target_ip.c_str(), &addr4.sin_addr) != 1) {
                struct hostent* he = ::gethostbyname(target_ip.c_str());
                if (he != NULL && he->h_addr_list[0] != NULL)
                    ::memcpy(&addr4.sin_addr, he->h_addr_list[0], he->h_length);
                else { ::std::cerr << "[TCP_TLS_PORT] ERROR: resolve failed: " << target_ip << ::std::endl; break; }
            }
            ::std::cout << "[TCP_TLS_PORT] IPv4 connect to " << target_ip << ":" << target_port << ::std::endl;
            ok = (::connect(socket_fd_, (struct sockaddr*)&addr4, sizeof(addr4)) == 0);
            if (!ok) ::std::cerr << "[TCP_TLS_PORT] IPv4 connect failed: " << ::strerror(errno) << ::std::endl;
        }
    }

    if (!ok) {
        ::std::cerr << "[TCP_TLS_PORT] ERROR: connect failed after 5 attempts" << ::std::endl;
        ::close(socket_fd_);
        socket_fd_ = -1;
        connected_ = false;
        return;
    }

    int nodelay = 1;
    ::setsockopt(socket_fd_, IPPROTO_TCP, TCP_NODELAY, &nodelay, sizeof(nodelay));
    connected_ = true;
    cached_secc_port_ = target_port;
    ::std::cout << "[TCP_TLS_PORT] Connected! Cached SECC port=" << cached_secc_port_ << ::std::endl;

    receive_thread_running_ = true;
    if (::pthread_create(&receive_thread_id_, NULL,
                         &V2G__TCP__TLS__Port::receive_thread_func, this) != 0) {
        ::std::cerr << "[TCP_TLS_PORT] ERROR: pthread_create failed" << ::std::endl;
        receive_thread_running_ = false;
    }
}

void V2G__TCP__TLS__Port::disconnect_from_secc() {
    ::std::cout << "[TCP_TLS_PORT] Disconnecting..." << ::std::endl;
    receive_thread_running_ = false;
    if (receive_thread_id_ != 0) {
        ::pthread_join(receive_thread_id_, NULL);
        receive_thread_id_ = 0;
    }
    if (socket_fd_ >= 0) {
        ::shutdown(socket_fd_, SHUT_RDWR);
        ::close(socket_fd_);
        socket_fd_ = -1;
    }
    connected_ = false;
}

bool V2G__TCP__TLS__Port::send_wire_bytes(const unsigned char* data, size_t length) {
    if (socket_fd_ < 0) return false;
    ssize_t sent = ::send(socket_fd_, data, length, 0);
    if (sent < 0) {
        ::std::cerr << "[TCP_TLS_PORT] ERROR: send() failed: " << ::strerror(errno) << ::std::endl;
        connected_ = false;
        return false;
    }
    ::std::cout << "[TCP_TLS_PORT] Sent " << sent << " bytes" << ::std::endl;
    if (sent > 0 && sent <= 128) {
        ::std::cout << "[TCP_TLS_PORT] Wire: ";
        for (ssize_t i = 0; i < sent; ++i)
            ::std::cout << ::std::hex << ::std::setw(2) << ::std::setfill('0') << (int)data[i] << " ";
        ::std::cout << ::std::dec << ::std::endl;
    }
    ::std::cout.flush();  // Force-flush so logs appear before any hang
    for (size_t i = 0; i < length; i += 16) {
        ::std::cout << "[TCP_TLS_PORT]   ";
        for (size_t j = 0; j < 16; ++j) {
            if (i + j < length)
                ::std::cout << ::std::hex << ::std::setw(2) << ::std::setfill('0') << (int)data[i+j] << " ";
            else
                ::std::cout << "   ";
        }
        ::std::cout << " |";
        for (size_t j = 0; j < 16 && i + j < length; ++j) {
            unsigned char c = data[i+j];
            ::std::cout << (c >= 32 && c < 127 ? (char)c : '.');
        }
        ::std::cout << "|" << ::std::endl;
    }
    return true;
}

void* V2G__TCP__TLS__Port::receive_thread_func(void* arg) {
    static_cast<V2G__TCP__TLS__Port*>(arg)->receive_loop();
    return NULL;
}

// ============================================================================
// Pipeline: Receive Hex -> Match JSON Feature Pattern -> Slice Dynamic Fields -> Deliver Union
// ============================================================================
bool V2G__TCP__TLS__Port::decode_and_deliver(const ::std::vector<uint8_t>& wire_bytes) {
    if (wire_bytes.size() < V2GTP_HEADER_SIZE) {
        ::std::cerr << "[TCP_TLS_PORT] ERROR: wire too short for V2GTP header" << ::std::endl;
        return false;
    }

    const uint8_t* data = wire_bytes.data();
    uint8_t version = data[0];
    uint8_t inv_version = data[1];
    uint16_t payload_type = (static_cast<uint16_t>(data[2]) << 8) | data[3];
    uint32_t payload_len = (static_cast<uint32_t>(data[4]) << 24) |
                           (static_cast<uint32_t>(data[5]) << 16) |
                           (static_cast<uint32_t>(data[6]) << 8) |
                           static_cast<uint32_t>(data[7]);

    // Some SECC implementations send incorrect V2GTP payload_len fields.
    // Always trust the actual wire_bytes size over the V2GTP header's length field.
    size_t exi_len = wire_bytes.size() - V2GTP_HEADER_SIZE;
    if (exi_len > 8192) exi_len = static_cast<size_t>(payload_len);

    if (version != V2GTP_VERSION || inv_version != V2GTP_INV_VERSION) {
        ::std::cerr << "[TCP_TLS_PORT] ERROR: Invalid V2GTP version" << ::std::endl;
        return false;
    }
    if (payload_type != V2GTP_PAYLOAD_TYPE_EXI) {
        ::std::cerr << "[TCP_TLS_PORT] ERROR: Unknown payload type=0x"
                  << ::std::hex << payload_type << ::std::dec << ::std::endl;
        return false;
    }

    const uint8_t* exi_payload = data + V2GTP_HEADER_SIZE;

    ::std::cout << "[TCP_TLS_PORT] V2GTP: header_payload_len=" << payload_len
              << " actual_exi_len=" << exi_len
              << " EXI: " << hex_bytes(exi_payload, exi_len) << ::std::endl;

    // ---- Static EXI decode using the member decoder (JSON-driven) ----
    ::SECC__ExiJni::StaticExiDecoder::DecodeResult result;
    ::std::cout << "[TCP_TLS_PORT] Calling exi_decoder_->decode_exi()..." << ::std::endl;
    if (!exi_decoder_->decode_exi(exi_payload, exi_len, result)) {
        ::std::cerr << "[TCP_TLS_PORT] ERROR: Static decode failed: " << exi_decoder_->last_error() << ::std::endl;
        return false;
    }
    ::std::cout << "[TCP_TLS_PORT] Static decode OK, msg_type=" << result.msg_type << ::std::endl;

    // ---- Build V2G__MessageUnion ----
    ::SECC__Types::V2G__MessageUnion v2g_msg;

    switch (result.msg_type) {
        case ::SECC__ExiJni::StaticExiDecoder::DecodeResult::MSG_SUPPORTED_APP_PROTOCOL_RES: {
            ::SECC__Types::SupportedAppProtocolRes& res = v2g_msg.supportedAppProtocolRes();
            res.ResponseCode() = result.app_response_code;
            res.SchemaID() = result.schema_id > 0 ? INTEGER(result.schema_id) : OMIT_VALUE;
            res.SchemaURI() = OMIT_VALUE;
            ::std::cout << "[TCP_TLS_PORT] Built SupportedAppProtocolRes: "
                      << "RC=" << result.app_response_code << " SchemaID=" << result.schema_id << ::std::endl;
            break;
        }
        case ::SECC__ExiJni::StaticExiDecoder::DecodeResult::MSG_SESSION_SETUP_RES: {
            ::SECC__Types::SessionSetupRes& res = v2g_msg.sessionSetupRes();
            res.ResponseCode() = result.v2g_response_code;
            // Use extracted EVSEID from SECC response, fallback to canonical
            if (!result.evseid.empty()) {
                res.EVSEID() = result.evseid.c_str();
            } else {
                res.EVSEID() = "EVSE12345678";
            }
            res.EVSETimeStamp() = OMIT_VALUE;
            if (result.session_id.size() == 8) {
                ::std::ostringstream sid;
                for (size_t i = 0; i < 8; ++i)
                    sid << ::std::hex << ::std::setw(2) << ::std::setfill('0') << (int)result.session_id[i];
                current_session_id_ = sid.str();
                ::std::cout << "[TCP_TLS_PORT] SessionID: " << current_session_id_ << ::std::endl;
            }
            ::std::cout << "[TCP_TLS_PORT] Built SessionSetupRes: RC=" << result.v2g_response_code << ::std::endl;
            break;
        }
        default:
            ::std::cerr << "[TCP_TLS_PORT] ERROR: Unsupported message type" << ::std::endl;
            return false;
    }

    ::std::cout << "[TCP_TLS_PORT] Calling incoming_message()" << ::std::endl;
    incoming_message(v2g_msg);
    return true;
}

void V2G__TCP__TLS__Port::receive_loop() {
    unsigned char buf[8192];
    ::std::cout << "[TCP_TLS_PORT] Receive thread started" << ::std::endl;

    while (receive_thread_running_) {
        if (socket_fd_ < 0) {
            ::std::this_thread::sleep_for(::std::chrono::milliseconds(100));
            continue;
        }

        // Drain all immediately-available data using MSG_DONTWAIT.
        // On Linux, MSG_DONTWAIT recv returns data if the kernel has buffered it.
        // Retry until we get EAGAIN (no more data immediately available).
        bool had_data = false;
        for (;;) {
            ssize_t r = ::recv(socket_fd_, buf, sizeof(buf), MSG_DONTWAIT);
            if (r > 0) {
                had_data = true;
                ::std::cout << "[TCP_TLS_PORT] Received " << r << " bytes (buffer: "
                          << receive_buffer_.size() << ")" << ::std::endl;
                receive_buffer_.insert(receive_buffer_.end(), buf, buf + r);
            } else if (r == 0) {
                ::std::cout << "[TCP_TLS_PORT] Connection closed" << ::std::endl;
                connected_ = false;
                goto connection_closed;
            } else {
                // r < 0: EAGAIN/EWOULDBLOCK means socket buffer is empty for now.
                break;
            }
        }

        if (had_data) {
            // Process all complete frames from the buffered data
            while (receive_thread_running_) {
                ::std::vector<uint8_t> frame;
                int rc = extract_v2gtp_frame(receive_buffer_, frame);
                if (rc < 0) {
                    ::std::cerr << "[TCP_TLS_PORT] ERROR: Invalid V2GTP frame" << ::std::endl;
                    receive_buffer_.clear();
                    break;
                }
                if (rc == 0) break;  // Partial frame — need more data
                ::std::cout << "[TCP_TLS_PORT] Decoding frame of " << frame.size() << " bytes" << ::std::endl;
                if (!decode_and_deliver(frame)) {
                    ::std::cerr << "[TCP_TLS_PORT] WARNING: decode failed" << ::std::endl;
                }
            }
            // Loop to drain any more data that may have arrived in the meantime
            continue;
        }

        // No data buffered: wait for socket to become readable with a short timeout.
        fd_set fds;
        FD_ZERO(&fds);
        FD_SET(socket_fd_, &fds);
        struct timeval tv = {0, 100000};  // 100ms timeout

        int n = ::select(socket_fd_ + 1, &fds, NULL, NULL, &tv);
        if (n < 0) {
            if (errno == EINTR) continue;
            if (errno == EBADF) break;
            ::std::cerr << "[TCP_TLS_PORT] ERROR: select failed: " << ::strerror(errno) << ::std::endl;
            break;
        }
        if (n == 0) {
            // Timeout: no data arrived. If we have partial data buffered,
            // it means the SECC's frame is incomplete — keep waiting.
            continue;
        }

        // Socket has data: recv without MSG_DONTWAIT (blocking).
        // This is the slow path — data is arriving from the network.
        ssize_t r = ::recv(socket_fd_, buf, sizeof(buf), 0);
        if (r < 0) {
            ::std::cerr << "[TCP_TLS_PORT] ERROR: recv failed: " << ::strerror(errno) << ::std::endl;
            break;
        }
        if (r == 0) {
            ::std::cout << "[TCP_TLS_PORT] Connection closed" << ::std::endl;
            connected_ = false;
            goto connection_closed;
        }

        ::std::cout << "[TCP_TLS_PORT] Received " << r << " bytes (buffer: "
                  << receive_buffer_.size() << ")" << ::std::endl;
        receive_buffer_.insert(receive_buffer_.end(), buf, buf + r);

        while (receive_thread_running_) {
            ::std::vector<uint8_t> frame;
            int rc = extract_v2gtp_frame(receive_buffer_, frame);
            if (rc < 0) {
                ::std::cerr << "[TCP_TLS_PORT] ERROR: Invalid V2GTP frame" << ::std::endl;
                receive_buffer_.clear();
                break;
            }
            if (rc == 0) break;
            if (!decode_and_deliver(frame)) {
                ::std::cerr << "[TCP_TLS_PORT] WARNING: decode failed" << ::std::endl;
            }
        }
    }

connection_closed:
    // Drain any remaining buffered data on connection close
    while (receive_thread_running_) {
        ::std::vector<uint8_t> frame;
        int rc = extract_v2gtp_frame(receive_buffer_, frame);
        if (rc < 0) break;
        if (rc == 0) break;
        if (!decode_and_deliver(frame)) {
            ::std::cerr << "[TCP_TLS_PORT] WARNING: decode failed" << ::std::endl;
        }
    }

    ::std::cout << "[TCP_TLS_PORT] Receive thread exiting" << ::std::endl;
    connected_ = false;
    receive_buffer_.clear();
}

} // namespace SECC__TestSystem
