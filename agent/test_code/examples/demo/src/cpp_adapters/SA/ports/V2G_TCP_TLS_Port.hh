#ifndef V2G_TCP_TLS_PORT_HH
#define V2G_TCP_TLS_PORT_HH

// Standard library headers MUST come BEFORE SECC_TestSystem.hh to avoid
// namespace pollution from TITAN-generated headers defining types that
// conflict with stdlib names (e.g., string, vector, map).
#include <string>
#include <cstdint>
#include <sys/socket.h>
#include <netinet/in.h>
#include <net/if.h>
#include <ifaddrs.h>
#include <vector>

// Forward declarations (in global namespace, before SECC_TestSystem.hh)
namespace SECC__TestSystem {
class ExiCodecAdapter;
}

// ExiJniDecoder lives in the SECC__ExiJni namespace
namespace SECC__ExiJni {
class StaticExiDecoder;
}

// TITAN-generated header -- must come AFTER stdlib includes and forward decl.
#include "SECC_TestSystem.hh"

namespace SECC__TestSystem {

class V2G__TCP__TLS__Port : public V2G__TCP__TLS__Port_BASE {
public:
    V2G__TCP__TLS__Port(const char *par_port_name = NULL);
    virtual ~V2G__TCP__TLS__Port();
    void set_parameter(const char *parameter_name, const char *parameter_value);
    void Handle_Message(const unsigned char *message, size_t length);
    void outgoing_message(const unsigned char *message, size_t length);
    // V2G_PortMessage variant -- TITAN calls this when port out type is V2G_PortMessage
    // V2G__PortMessage is in SECC__TestSystem namespace (TITAN-generated from ComponentsAndPorts.ttcn)
    void outgoing_send(const V2G__PortMessage& send_par);
    // V2G_MessageUnion variant -- for normal V2G messages (EXI-encoded via JSON)
    void outgoing_send(const ::SECC__Types::V2G__MessageUnion& send_par);
    // OCTETSTRING raw variant -- V2GTP header BadCase tests (no EXI, no V2GTP patching)
    void outgoing_send(const ::OCTETSTRING& raw_bytes);
    void send_data(const ::SECC__Types::V2G__MessageUnion& send_data, const char* send_fmt, ...);

private:
    ::std::string port_name_;
    ::std::string ip_address_;
    ::std::string network_interface_;
    ::std::string ipv6_multicast_;
    int port_;
    bool tls_enabled_;
    bool use_ipv6_;
    int socket_fd_;
    bool connected_;
    bool receive_thread_running_;
    pthread_t receive_thread_id_;
    ::std::string current_session_id_;
    ::SECC__ExiJni::StaticExiDecoder* exi_decoder_;
    // TCP buffer: accumulate bytes across multiple recv() calls
    ::std::vector<uint8_t> receive_buffer_;
    // Cached port from successful connection (needed when SECC closes and we reconnect)
    int cached_secc_port_;

    void connect_to_secc();
    void disconnect_from_secc();
    void receive_loop();
    bool send_wire_bytes(const unsigned char* data, size_t length);
    int parse_ipv6_address(const ::std::string& addr_str, ::sockaddr_in6* addr6);
    bool encode_union_to_wire(const ::SECC__Types::V2G__MessageUnion& msg_union,
                               ::std::vector<uint8_t>& wire_output);

    // Decode received EXI bytes into a V2G__MessageUnion and deliver to TTCN-3
    bool decode_and_deliver(const ::std::vector<uint8_t>& wire_bytes);

    static void* receive_thread_func(void* arg);
};

}  // namespace SECC__TestSystem

#endif  // V2G_TCP_TLS_PORT_HH
