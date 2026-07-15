/*
 * V2G_TCP_TLS_Port.hh — Universal EXI Bridge v4 Dumb Terminal (Sprint 2)
 *
 * ISO 15118-2 SECC Test Adapter
 *
 * PHILOSOPHY:
 *   This adapter is a TRANSPARENT BYTE PIPE between TTCN-3 test code and the
 *   Python EXI microservice at localhost:9000. It knows NOTHING about:
 *     - V2G_MessageUnion / message semantics
 *     - JSON field names or namespaces
 *     - ISO 15118 message types (SessionSetupReq, etc.)
 *     - Session IDs or any application-level state
 *
 * WHAT IT DOES:
 *   - outgoing: octetstring (UTF-8 JSON) -> ENCODE via microservice
 *               -> wrap with V2GTP header -> send TCP to SECC
 *   - incoming: SECC TCP bytes -> strip V2GTP header -> DECODE via microservice
 *               -> octetstring (UTF-8 JSON) -> deliver to TTCN-3
 *
 * THE TTCN-3 side is solely responsible for:
 *   - Producing well-formed JSON (PascalCase XSD names, hexBinary as hex strings,
 *     correct namespace selection; see generation_patterns.json::_ironclad_json_payload_rules).
 *
 * Created for the ISO 15118-2/4 TTCN-3 Test Generation Pipeline.
 * Sprint 2 author: Infrastructure Agent — Dumb Terminal Refactor.
 */

#ifndef V2G_TCP_TLS_PORT_HH
#define V2G_TCP_TLS_PORT_HH

// Standard library headers MUST come BEFORE SECC_TestSystem.hh
#include <string>
#include <cstdint>
#include <sys/socket.h>
#include <netinet/in.h>
#include <net/if.h>
#include <ifaddrs.h>
#include <vector>

// TITAN-generated header
#include "SECC_TestSystem.hh"

namespace SECC__TestSystem {

class V2G__TCP__TLS__Port : public V2G__TCP__TLS__Port_BASE {
public:
    V2G__TCP__TLS__Port(const char* par_port_name = nullptr);
    virtual ~V2G__TCP__TLS__Port();

    void set_parameter(const char* parameter_name, const char* parameter_value);
    void Handle_Message(const unsigned char* message, size_t length);
    void outgoing_message(const unsigned char* message, size_t length);

    // SINGLE outgoing interface: V2G_PortMessage (dual-path) -> microservice -> SECC
    //   - v2g variant: structured union -> extract JSON -> EXI encode -> V2GTP -> TCP
    //   - raw variant: raw octetstring -> V2GTP header -> TCP (as-is)
    void outgoing_send(const SECC__TestSystem::V2G__PortMessage& msg);

private:
    ::std::string port_name_;
    ::std::string ip_address_;
    ::std::string network_interface_;
    ::std::string ipv6_multicast_;
    int port_;
    bool tls_enabled_;
    bool use_ipv6_;

    // SECC TCP connection
    int socket_fd_;
    bool connected_;
    bool receive_thread_running_;
    pthread_t receive_thread_id_;
    int cached_secc_port_;

    // Last decoded JSON bytes (cached for inspection; not used by adapter logic)
    ::std::string last_decoded_json_;

    // TCP receive buffer
    ::std::vector<uint8_t> receive_buffer_;

    // Handle one complete V2GTP frame
    void handle_incoming_frame(const ::std::vector<uint8_t>& wire_bytes);

    // Push a typed V2G_ErrorInfo fail-fast error notification into the port
    // queue, so the TTCN-3 side can immediately match
    //   receive({ error_indicator := ? })
    // and set verdict=fail instead of waiting for the per-TC message timeout.
    //
    // source        : "exi_decode" | "exi_encode" | "exi_service_unavailable"
    //                 | "v2gtp_framing" | "exi_payload_too_large"
    // message       : raw diagnostic (microservice response or local stderr)
    // namespace_uri : optional — the namespace URI attempted when the error
    //                 happened (e.g. "urn:iso:15118:2:2013:MsgDef"). Pass ""
    //                 if not applicable.
    void push_error_indicator(const ::std::string& source,
                              const ::std::string& message,
                              const ::std::string& namespace_uri = "");

    // EXI microservice client
    int exi_service_fd_;
    bool exi_service_connected_;

    // TCP socket to SECC
    void connect_to_secc();
    void disconnect_from_secc();
    void receive_loop();
    bool send_wire_bytes(const unsigned char* data, size_t length);
    int parse_ipv6_address(const ::std::string& addr_str, ::sockaddr_in6* addr6);

    // V2GTP helpers (outgoing: prepend; incoming: strip)
    bool send_v2gtp_framed_exi(const ::std::vector<uint8_t>& exi_bytes);
    bool strip_v2gtp_header(const ::std::vector<uint8_t>& wire_bytes,
                            ::std::vector<uint8_t>& exi_out);

    // EXI microservice client
    void connect_exi_service();
    void disconnect_exi_service();
    bool ensure_exi_service();
    ::std::string exi_service_call(const ::std::string& op,
                                   const ::std::string& ns,
                                   const ::std::string& payload);
    ::std::vector<uint8_t> call_exi_encode(const ::std::string& json_str,
                                           const ::std::string& namespace_uri);
    ::std::string call_exi_decode(const ::std::vector<uint8_t>& exi_bytes,
                                  const ::std::string& namespace_uri);

    static void* receive_thread_func(void* arg);
};

}  // namespace SECC__TestSystem

#endif  // V2G_TCP_TLS_PORT_HH
