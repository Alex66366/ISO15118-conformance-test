/*
 * V2G_TCP_TLS_Port.cc — Universal EXI Bridge v4 Dumb Terminal (Sprint 2)
 *
 * ISO 15118-2 SECC Test Adapter
 *
 * PHILOSOPHY: Pure transparent byte pipe. Knows nothing about JSON schema,
 * message types, namespaces, or session IDs. TTCN-3 is the sole authority
 * over JSON correctness.
 *
 * Outgoing path:
 *   TTCN-3 send(OCTETSTRING JSON)  [UTF-8 JSON bytes]
 *     -> extract namespace from first line of JSON
 *     -> ENCODE request to microservice (localhost:9000)
 *     -> V2GTP header (8 bytes) + EXI payload -> TCP to SECC
 *
 * Incoming path:
 *   SECC TCP bytes
 *     -> strip V2GTP header
 *     -> DECODE request to microservice
 *     -> microservice returns JSON as UTF-8 octetstring
 *     -> deliver OCTETSTRING to TTCN-3 (no union construction, no parsing)
 *
 * Protocol with microservice (TCP, \n\n-terminated framing):
 *   ENCODE: "ENCODE\n<namespace>\n<json>\n\n"
 *   DECODE: "DECODE\n<namespace>\n<exi_hex>\n\n"
 *
 * Author: Infrastructure Agent — Sprint 2 Dumb Terminal Refactor
 */

#include "V2G_TCP_TLS_Port.hh"

#include <algorithm>
#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <iomanip>
#include <map>
#include <sstream>
#include <thread>
#include <unistd.h>

extern "C" {
#include <arpa/inet.h>
#include <fcntl.h>
#include <ifaddrs.h>
#include <netdb.h>
#include <net/if.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <sys/time.h>
}

// External variables from V2G_UDP_SDP_Port (SDP discovery result)
extern ::std::string V2G__UDP__SDP__Port__last_secc_ip_;
extern int V2G__UDP__SDP__Port__last_secc_port_;

// ============================================================================
// Protocol constants
// ============================================================================
namespace {
    static constexpr size_t  V2GTP_HDR_LEN       = 8;
    static constexpr uint8_t V2GTP_VERSION       = 0x01;
    static constexpr uint8_t V2GTP_INV_VERSION   = 0xFE;
    static constexpr uint16_t V2GTP_EXI_TYPE     = 0x8001;

    // EXI microservice connection
    static constexpr int     EXI_SERVICE_PORT     = 9000;
    static constexpr const char* EXI_SERVICE_HOST = "127.0.0.1";
    static constexpr size_t  RECV_BUF_SIZE        = 8192;
    static constexpr size_t  EXI_WIRE_MAX        = 8192;
    static constexpr long    SELECT_TIMEOUT_US    = 100000;  // 100 ms

    // Microservice protocol framing
    static constexpr const char* FRAME_TERM       = "\n\n";
    static constexpr size_t  FRAME_TERM_LEN       = 2;

    // Largest expected EXI message in bytes
    static constexpr size_t  EXI_PAYLOAD_MAX      = 65535;

    // ==========================================================================
    // Hex <-> bytes utilities
    // ==========================================================================
    static ::std::vector<uint8_t> hex_to_bytes(const ::std::string& hex) {
        ::std::vector<uint8_t> out;
        out.reserve(hex.size() / 2);
        for (size_t i = 0; i + 1 < hex.size(); i += 2) {
            unsigned int byte = 0;
            if (::std::sscanf(hex.c_str() + i, "%02x", &byte) == 1) {
                out.push_back(static_cast<uint8_t>(byte));
            }
        }
        return out;
    }

    static ::std::string bytes_to_hex(const uint8_t* data, size_t len) {
        ::std::string out;
        out.reserve(len * 2);
        for (size_t i = 0; i < len; ++i) {
            char buf[3];
            ::std::snprintf(buf, sizeof(buf), "%02x", data[i]);
            out += buf;
        }
        return out;
    }

    // ==========================================================================
    // V2GTP header helpers
    // ==========================================================================
    static void write_v2gtp_header(uint8_t* hdr, uint32_t payload_len) {
        hdr[0] = V2GTP_VERSION;
        hdr[1] = V2GTP_INV_VERSION;
        hdr[2] = static_cast<uint8_t>((V2GTP_EXI_TYPE >> 8) & 0xFF);
        hdr[3] = static_cast<uint8_t>(V2GTP_EXI_TYPE & 0xFF);
        hdr[4] = static_cast<uint8_t>((payload_len >> 24) & 0xFF);
        hdr[5] = static_cast<uint8_t>((payload_len >> 16) & 0xFF);
        hdr[6] = static_cast<uint8_t>((payload_len >> 8) & 0xFF);
        hdr[7] = static_cast<uint8_t>(payload_len & 0xFF);
    }

    static ::std::string dump_hex(const uint8_t* data, size_t len) {
        ::std::ostringstream oss;
        ::std::size_t show = (len > 64) ? 64 : len;
        for (::std::size_t i = 0; i < show; ++i) {
            oss << ::std::hex << ::std::setw(2) << ::std::setfill('0')
                << static_cast<int>(data[i]) << " ";
        }
        if (len > 64) oss << "...(" << len << " bytes total)";
        return oss.str();
    }

    // ==========================================================================
    // Extract one complete V2GTP frame from a byte buffer.
    // Handles both raw V2GTP and TLS-wrapped V2GTP (0x17 0x03 0x03).
    // Returns 1 on success, 0 if need more data, -1 on error.
    // ==========================================================================
    static int extract_v2gtp_frame(::std::vector<uint8_t>& buf,
                                   ::std::vector<uint8_t>& frame_out) {
        if (buf.empty()) return 0;

        // TLS unwrap: 0x17 0x03 0x03 prefix
        if (buf.size() >= 5 && buf[0] == 0x17 && buf[1] == 0x03 && buf[2] == 0x03) {
            if (buf.size() < 6) return 0;
            frame_out.assign(buf.begin() + 5, buf.end());
            buf.clear();
            return 1;
        }

        // Raw V2GTP
        if (buf.size() < V2GTP_HDR_LEN) return 0;
        uint32_t payload_len =
            (static_cast<uint32_t>(buf[4]) << 24) |
            (static_cast<uint32_t>(buf[5]) << 16) |
            (static_cast<uint32_t>(buf[6]) << 8)  |
            static_cast<uint32_t>(buf[7]);
        size_t total = V2GTP_HDR_LEN + payload_len;
        if (buf.size() < total) return 0;

        frame_out.assign(buf.begin(), buf.begin() + total);
        buf.erase(buf.begin(), buf.begin() + total);
        return 1;
    }

    // ==========================================================================
    // Detect namespace from the incoming JSON bytes.
    // The JSON is UTF-8. We scan for the first top-level key that tells us
    // which message type this is, and return the matching EXI namespace.
    // ==========================================================================
    static ::std::string detect_namespace_from_json(const ::std::string& json_bytes) {
        // V2G_Message path: {"V2G_Message":{"Header":...}}
        if (json_bytes.find("\"V2G_Message\"") != ::std::string::npos) {
            return "urn:iso:15118:2:2013:MsgDef";
        }
        // supportedAppProtocolReq/Res path
        if (json_bytes.find("\"supportedAppProtocolReq\"") != ::std::string::npos ||
            json_bytes.find("\"supportedAppProtocolRes\"") != ::std::string::npos) {
            return "urn:iso:15118:2:2010:AppProtocol";
        }
        // DIN SPEC 70121
        if (json_bytes.find("\"V2G_Message\"") != ::std::string::npos) {
            // For DIN, the JSON format is identical; namespace is different.
            // If we knew it was DIN we'd use urn:din:70121:2012:MsgDef.
            // TTCN-3 should pass the namespace explicitly in a future extension.
            // Default to ISO 15118-2 for now.
            return "urn:iso:15118:2:2013:MsgDef";
        }
        // Fallback: ISO 15118-2
        return "urn:iso:15118:2:2013:MsgDef";
    }

    // ==========================================================================
    // CASE_MAP — defensive camelCase → PascalCase JSON key translation
    // ==========================================================================
    // ISO 15118-2 standard JAXB descriptors register PascalCase root elements
    // (e.g. SessionSetupReq). When the C++ adapter forwards JSON to the EXI
    // microservice (Siemens EXIficient via py4j), the JAXB layer does strict
    // case-matching on the root element name. If the LLM generates camelCase
    // union-field names (sessionSetupReq, serviceDiscoveryReq, etc.), JAXB
    // throws UnmarshalException: "A descriptor with default root element
    // sessionSetupReq was not found in the project".
    //
    // This dictionary maps every camelCase V2G-message body key (and the
    // AppProtocol pair) to its PascalCase ISO 15118-2 equivalent. The
    // apply_case_map() helper below does a text-level replacement on the JSON
    // body BEFORE forwarding to the EXI microservice. This is a defensive
    // belt-and-suspenders guard: the LLM prompt already instructs the model
    // to emit PascalCase, but if a regression sneaks camelCase through, the
    // adapter still produces a valid EXI payload.
    //
    // Reference: BATCH_ANALYSIS_20260711.md §4.2 (LLM Case-Sensitivity
    // Hallucination on JSON Element Names) and §2.3 Issue B (JAXB Case-
    // Sensitive Element Mismatch).
    // ==========================================================================
    static const ::std::map<::std::string, ::std::string> CASE_MAP = {
        // Note: ISO 15118-2 has TWO root-element naming conventions, by namespace:
        //
        //   1. urn:iso:15118:2:2010:AppProtocol  →  XSD uses camelCase
        //        (first letter lowercase, e.g. supportedAppProtocolReq —
        //        see schemas/V2G_CI_AppProtocol.xsd line 95: <xs:element
        //        name="supportedAppProtocolReq">). JAXB descriptors register
        //        these names EXACTLY.  We MUST NOT transform them.
        //
        //   2. urn:iso:15118:2:2013:MsgDef (Body) →  XSD uses PascalCase
        //        (first letter uppercase, e.g. SessionSetupReq —
        //        see schemas/iso15118_2/V2G_CI_MsgBody.xsd line 24:
        //        <xs:element name="SessionSetupReq" ...>). JAXB descriptors
        //        register these names EXACTLY.  If the LLM emits the camelCase
        //        form (sessionSetupReq), JAXB throws UnmarshalException
        //        ("A descriptor with default root element sessionSetupReq was
        //        not found") and ENCODE fails.
        //
        // CASE_MAP therefore contains ONLY V2G body names: camelCase (LLM
        // hallucination) → PascalCase (XSD/JAXB-canonical). AppProtocol
        // names are intentionally absent — they are correct as-is.
        //
        // Reference: BATCH_ANALYSIS_20260711.md §4.2 (LLM Case-Sensitivity
        // Hallucination on JSON Element Names), §2.3 Issue B; corrected
        // by direct XSD inspection during Sprint 3 verification (the
        // earlier doc hypothesized the opposite direction, which would have
        // broken AppProtocol). XSD is the source of truth.

        // ISO 15118-2 V2G body messages (urn:iso:15118:2:2013:MsgDef)
        {"sessionSetupReq",         "SessionSetupReq"},
        {"sessionSetupRes",         "SessionSetupRes"},
        {"serviceDiscoveryReq",     "ServiceDiscoveryReq"},
        {"serviceDiscoveryRes",     "ServiceDiscoveryRes"},
        {"paymentServiceSelectionReq", "PaymentServiceSelectionReq"},
        {"paymentServiceSelectionRes", "PaymentServiceSelectionRes"},
        {"certificateInstallationReq", "CertificateInstallationReq"},
        {"certificateInstallationRes", "CertificateInstallationRes"},
        {"authorizationReq",        "AuthorizationReq"},
        {"authorizationRes",        "AuthorizationRes"},
        {"powerDeliveryReq",        "PowerDeliveryReq"},
        {"powerDeliveryRes",        "PowerDeliveryRes"},
        {"cableCheckReq",           "CableCheckReq"},
        {"cableCheckRes",           "CableCheckRes"},
        {"meteringReceiptReq",      "MeteringReceiptReq"},
        {"meteringReceiptRes",      "MeteringReceiptRes"},

        // ISO 15118-2 AC/DC charging loop extras
        {"chargingStatusReq",       "ChargingStatusReq"},
        {"chargingStatusRes",       "ChargingStatusRes"},
        {"preChargeReq",            "PreChargeReq"},
        {"preChargeRes",            "PreChargeRes"},
        {"currentDemandReq",        "CurrentDemandReq"},
        {"currentDemandRes",        "CurrentDemandRes"},
        {"weldingDetectionReq",     "WeldingDetectionReq"},
        {"weldingDetectionRes",     "WeldingDetectionRes"},

        // NOTE: AppProtocol names (supportedAppProtocolReq/Res) are deliberately
        //       NOT in this map. The XSD registers them as lowercase-first-letter
        //       (supportedAppProtocolReq), and JAXB descriptors match that form
        //       exactly. Transforming them would break AppProtocol encoding —
        //       see verified ERROR during Sprint 3 verification:
        //       "A descriptor with default root element SupportedAppProtocolReq
        //        was not found in the project".
    };

    // ==========================================================================
    // apply_case_map — replace any camelCase JSON keys with their PascalCase
    // ISO 15118-2 equivalents before forwarding to the EXI microservice.
    //
    // Implementation note: this is a TEXT-LEVEL replacement (not a JSON parse),
    // matching each camelCase key (with surrounding double-quotes) and replacing
    // with the PascalCase equivalent. We use the surrounding quotes to ensure
    // we only match KEY positions, not values or substrings.
    //
    // For ISO 15118-2 messages the body keys are unique enough that the
    // text-level replacement is safe: "sessionSetupReq" as a substring is
    // exceedingly unlikely to appear except as the key itself. If a future
    // message introduces a collision risk, switch to a full JSON parser.
    // ==========================================================================
    static ::std::string apply_case_map(const ::std::string& json_bytes) {
        ::std::string out = json_bytes;
        ::std::size_t total_replacements = 0;
        ::std::string log_first_replacement;

        for (const auto& kv : CASE_MAP) {
            const ::std::string& camel = kv.first;
            const ::std::string& pascal = kv.second;
            const ::std::string from = "\"" + camel + "\"";
            const ::std::string to   = "\"" + pascal + "\"";

            ::std::size_t pos = 0;
            ::std::size_t count_for_this_key = 0;
            while ((pos = out.find(from, pos)) != ::std::string::npos) {
                out.replace(pos, from.size(), to);
                pos += to.size();
                ++count_for_this_key;
            }
            if (count_for_this_key > 0) {
                total_replacements += count_for_this_key;
                if (log_first_replacement.empty()) {
                    log_first_replacement = camel + " -> " + pascal +
                                            " (x" + ::std::to_string(count_for_this_key) + ")";
                }
            }
        }

        if (total_replacements > 0) {
            ::std::cerr << "[TCP_TLS_PORT] CASE_MAP: rewrote "
                      << total_replacements << " camelCase JSON key(s); "
                      << "first: " << log_first_replacement << ::std::endl;
        }
        return out;
    }

}  // anonymous namespace

// ============================================================================
// V2G__TCP__TLS__Port implementation
// ============================================================================
namespace SECC__TestSystem {

V2G__TCP__TLS__Port::V2G__TCP__TLS__Port(const char* par_port_name)
    : V2G__TCP__TLS__Port_BASE(par_port_name),
      port_name_(par_port_name ? par_port_name : ""),
      port_(15118),
      tls_enabled_(false),
      use_ipv6_(false),
      socket_fd_(-1),
      connected_(false),
      receive_thread_running_(false),
      receive_thread_id_(0),
      cached_secc_port_(0),
      last_decoded_json_(),
      receive_buffer_(),
      exi_service_fd_(-1),
      exi_service_connected_(false)
{
    ::std::cout << "[TCP_TLS_PORT] Constructed (v4 Dumb Terminal): " << port_name_ << ::std::endl;
    ::std::cout << "[TCP_TLS_PORT]   EXI microservice : " << EXI_SERVICE_HOST
                << ":" << EXI_SERVICE_PORT << ::std::endl;
    ::std::cout << "[TCP_TLS_PORT]   V2G_MessageUnion : NOT LINKED (TTCN-3 owns JSON)" << ::std::endl;
}

V2G__TCP__TLS__Port::~V2G__TCP__TLS__Port() {
    disconnect_from_secc();
    disconnect_exi_service();
    ::std::cout << "[TCP_TLS_PORT] Destructor: " << port_name_ << ::std::endl;
}

// ============================================================================
// set_parameter
// ============================================================================
void V2G__TCP__TLS__Port::set_parameter(const char* parameter_name,
                                         const char* parameter_value) {
    ::std::string name(parameter_name ? parameter_name : "");
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
    } else if (name == "session_id") {
        // v4 Dumb Terminal: session_id is purely for logging.
        // The adapter does NOT inject it into messages.
        ::std::cout << "[TCP_TLS_PORT] session_id (logging only): " << value << ::std::endl;
    } else if (name == "last_decoded_json") {
        last_decoded_json_ = value;
    } else if (name == "test_fail_fast_source") {
        // ========================================================================
        // Sprint 3 / Phase 2 TEST HOOK — verify the fail-fast mechanism
        // ========================================================================
        // When the runtime config sets `test_fail_fast_source` to one of the
        // documented source tags ("exi_decode" | "v2gtp_framing" |
        // "exi_payload_too_large" | "exi_service_unavailable"), the adapter
        // immediately calls push_error_indicator() with that source. This lets
        // BadCase test cases verify end-to-end that:
        //   1. The error_indicator path reaches the port's incoming_message.
        //   2. TTCN-3's alt-block `receive({ error_indicator := ? })` matches.
        //   3. setverdict(fail, ...) fires within ms, not after the per-TC
        //      message timeout (~20 min).
        //
        // Production TCs never set this parameter. It is intended ONLY for
        // BadCase verification of the Sprint 3 fail-fast wiring.
        //
        // Trigger fires at startup (set_parameter is called once during
        // component init). The error_indicator message is queued in the port's
        // incoming queue and is matched by the first receive() call.
        // ========================================================================
        ::std::cerr << "[TCP_TLS_PORT] TEST_HOOK: triggering fail-fast with source="
                  << value << ::std::endl;
        push_error_indicator(
            value,
            "Test-injected fail-fast (BATCH_ANALYSIS §4.3 Sprint 3 verification)",
            "urn:iso:15118:2:2013:MsgDef"
        );
    }
}

void V2G__TCP__TLS__Port::Handle_Message(const unsigned char*, size_t) {}
void V2G__TCP__TLS__Port::outgoing_message(const unsigned char*, size_t) {}

// ============================================================================
// EXI Microservice client
// ============================================================================
void V2G__TCP__TLS__Port::connect_exi_service() {
    if (exi_service_fd_ >= 0) {
        ::close(exi_service_fd_);
        exi_service_fd_ = -1;
    }

    exi_service_fd_ = ::socket(AF_INET, SOCK_STREAM, 0);
    if (exi_service_fd_ < 0) {
        ::std::cerr << "[TCP_TLS_PORT] ERROR: exi_service socket() failed: "
                  << ::strerror(errno) << ::std::endl;
        return;
    }

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(EXI_SERVICE_PORT);
    if (::inet_pton(AF_INET, EXI_SERVICE_HOST, &addr.sin_addr) != 1) {
        ::std::cerr << "[TCP_TLS_PORT] ERROR: inet_pton failed for " << EXI_SERVICE_HOST << ::std::endl;
        ::close(exi_service_fd_);
        exi_service_fd_ = -1;
        return;
    }

    if (::connect(exi_service_fd_, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0) {
        ::std::cerr << "[TCP_TLS_PORT] ERROR: connect to EXI microservice failed: "
                  << ::strerror(errno) << ::std::endl;
        ::close(exi_service_fd_);
        exi_service_fd_ = -1;
        return;
    }

    exi_service_connected_ = true;
    ::std::cout << "[TCP_TLS_PORT] Connected to EXI microservice at "
                << EXI_SERVICE_HOST << ":" << EXI_SERVICE_PORT << ::std::endl;
}

void V2G__TCP__TLS__Port::disconnect_exi_service() {
    if (exi_service_fd_ >= 0) {
        ::shutdown(exi_service_fd_, SHUT_RDWR);
        ::close(exi_service_fd_);
        exi_service_fd_ = -1;
    }
    exi_service_connected_ = false;
}

bool V2G__TCP__TLS__Port::ensure_exi_service() {
    if (exi_service_fd_ < 0 || !exi_service_connected_) {
        connect_exi_service();
    }
    return exi_service_connected_;
}

// Send a protocol frame and receive a \n\n-terminated response.
// If the service closes the connection mid-call (short-lived JVM gateway pattern),
// reconnect once and retry before propagating the error.
::std::string V2G__TCP__TLS__Port::exi_service_call(const ::std::string& op,
                                                   const ::std::string& ns,
                                                   const ::std::string& payload) {
    if (!ensure_exi_service()) {
        return "ERROR\nnot connected\n\n";
    }

    // Build request frame: "OP\n<namespace>\n<payload>\n\n"
    ::std::string frame = op + "\n" + ns + "\n" + payload + "\n\n";

    ssize_t sent = ::send(exi_service_fd_, frame.data(), frame.size(), 0);
    if (sent < 0) {
        ::std::cerr << "[TCP_TLS_PORT] ERROR: send to EXI service failed: "
                  << ::strerror(errno) << ::std::endl;
        exi_service_connected_ = false;
        return "ERROR\nsend failed\n\n";
    }

    // Receive response until \n\n terminator
    ::std::string response;
    response.reserve(8192);
    char buf[1024];
    while (response.find(FRAME_TERM) == ::std::string::npos) {
        ssize_t r = ::recv(exi_service_fd_, buf, sizeof(buf), 0);
        if (r <= 0) {
            if (r < 0) {
                ::std::cerr << "[TCP_TLS_PORT] ERROR: recv from EXI service failed: "
                          << ::strerror(errno) << ::std::endl;
            } else {
                ::std::cerr << "[TCP_TLS_PORT] WARNING: EXI service closed connection mid-call, reconnecting..." << ::std::endl;
            }
            exi_service_connected_ = false;
            // Short-lived JVM gateway: microservice may close after processing a request.
            // Reconnect once and retry before giving up.
            ::std::cerr << "[TCP_TLS_PORT] Retrying call to EXI service (reconnect)..." << ::std::endl;
            connect_exi_service();
            if (!exi_service_connected_) {
                return "ERROR\nreconnect failed\n\n";
            }
            // Retry the request on the fresh connection
            ssize_t r2 = ::send(exi_service_fd_, frame.data(), frame.size(), 0);
            if (r2 < 0) {
                ::std::cerr << "[TCP_TLS_PORT] ERROR: send on reconnected socket failed" << ::std::endl;
                return "ERROR\nretry send failed\n\n";
            }
            response.clear();
            while (response.find(FRAME_TERM) == ::std::string::npos) {
                ssize_t rr = ::recv(exi_service_fd_, buf, sizeof(buf), 0);
                if (rr <= 0) {
                    if (rr < 0) {
                        ::std::cerr << "[TCP_TLS_PORT] ERROR: recv after reconnect failed: "
                                  << ::strerror(errno) << ::std::endl;
                    } else {
                        ::std::cerr << "[TCP_TLS_PORT] ERROR: EXI service closed on retry" << ::std::endl;
                    }
                    return "ERROR\nrecv retry failed\n\n";
                }
                response.append(buf, static_cast<size_t>(rr));
                if (response.size() > EXI_WIRE_MAX * 4) {
                    ::std::cerr << "[TCP_TLS_PORT] ERROR: EXI service response too large" << ::std::endl;
                    return "ERROR\nresponse too large\n\n";
                }
            }
            return response;
        }
        response.append(buf, static_cast<size_t>(r));
        if (response.size() > EXI_WIRE_MAX * 4) {
            ::std::cerr << "[TCP_TLS_PORT] ERROR: EXI service response too large" << ::std::endl;
            return "ERROR\nresponse too large\n\n";
        }
    }

    return response;
}

::std::vector<uint8_t> V2G__TCP__TLS__Port::call_exi_encode(
        const ::std::string& json_str,
        const ::std::string& namespace_uri)
{
    if (!ensure_exi_service()) {
        ::std::cerr << "[TCP_TLS_PORT] ERROR: cannot encode: EXI service unavailable" << ::std::endl;
        return {};
    }

    ::std::string resp = exi_service_call("ENCODE", namespace_uri, json_str);

    // Parse: "OK\n<exi_hex>\n\n" or "ERROR\n<msg>\n\n"
    if (resp.rfind("OK\n", 0) == 0) {
        ::std::string hex_data = resp.substr(3);
        size_t term_pos = hex_data.rfind(FRAME_TERM);
        if (term_pos != ::std::string::npos) {
            hex_data.resize(term_pos);
        }
        ::std::vector<uint8_t> exi_bytes = hex_to_bytes(hex_data);
        ::std::cout << "[TCP_TLS_PORT] ENCODE OK: " << exi_bytes.size()
                  << " bytes  ns=" << namespace_uri << ::std::endl;
        return exi_bytes;
    } else {
        ::std::cerr << "[TCP_TLS_PORT] ENCODE ERROR: " << resp << ::std::endl;
        return {};
    }
}

::std::string V2G__TCP__TLS__Port::call_exi_decode(
        const ::std::vector<uint8_t>& exi_bytes,
        const ::std::string& namespace_uri)
{
    if (!ensure_exi_service()) {
        ::std::cerr << "[TCP_TLS_PORT] ERROR: cannot decode: EXI service unavailable" << ::std::endl;
        return "{}";
    }

    ::std::string hex_str = bytes_to_hex(exi_bytes.data(), exi_bytes.size());
    ::std::string resp = exi_service_call("DECODE", namespace_uri, hex_str);

    if (resp.rfind("OK\n", 0) == 0) {
        ::std::string json_str = resp.substr(3);
        size_t term_pos = json_str.rfind(FRAME_TERM);
        if (term_pos != ::std::string::npos) {
            json_str.resize(term_pos);
        }
        last_decoded_json_ = json_str;
        ::std::cout << "[TCP_TLS_PORT] DECODE OK: " << json_str.size()
                  << " chars  ns=" << namespace_uri << ::std::endl;
        return json_str;
    } else {
        ::std::cerr << "[TCP_TLS_PORT] DECODE ERROR: " << resp << ::std::endl;
        return "{}";
    }
}

// ============================================================================
// Serialize V2G_MessageUnion to JSON string for EXI microservice.
// Uses TITAN's log2str() for value-to-string conversion, then converts
// TTCN-3 value notation to JSON.
// ============================================================================
static ::std::string serialize_v2g_message(
        const SECC__Types::V2G__MessageUnion& msg_union)
{
    ::CHARSTRING cstr = log2str(msg_union);
    ::std::string logged(cstr, cstr.lengthof());

    // Convert TTCN-3 log format to JSON:
    //   "{ fieldName := { Field1 := val1, Field2 := val2 } }"
    // ->
    //   "{ \"fieldName\": { \"field1\": val1, \"field2\": val2 } }"
    ::std::string result;
    result.reserve(logged.size() * 2);

    size_t i = 0;
    size_t len = logged.size();
    bool expecting_key = true;
    ::std::string current_token;

    while (i < len && logged[i] != '{') ++i;
    if (i >= len) return "{}";

    for (; i < len; ++i) {
        char c = logged[i];
        if (c == '{') {
            if (!current_token.empty()) {
                result += "\""; result += current_token; result += "\": ";
                current_token.clear();
            }
            result += "{";
            expecting_key = true;
        } else if (c == '}') {
            if (!current_token.empty()) { result += current_token; current_token.clear(); }
            result += "}";
            expecting_key = false;
        } else if (c == ':' && i + 1 < len && logged[i + 1] == '=') {
            ++i;
            if (!current_token.empty()) {
                result += "\""; result += current_token; result += "\": ";
                current_token.clear();
            }
            expecting_key = false;
        } else if (c == ',') {
            if (!current_token.empty()) { result += current_token; current_token.clear(); }
            result += ",";
            expecting_key = true;
        } else if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
            if (!current_token.empty() && expecting_key) {
                result += "\""; result += current_token; result += "\"";
                current_token.clear();
            }
        } else {
            current_token += c;
        }
    }
    if (!current_token.empty()) result += current_token;

    ::std::cout << "[TCP_TLS_PORT] Serialized V2G message: " << result << ::std::endl;
    return result;
}

// ============================================================================
// Outgoing path: V2G_PortMessage (dual-path) -> microservice -> V2GTP -> SECC
// ============================================================================
void V2G__TCP__TLS__Port::outgoing_send(
        const SECC__TestSystem::V2G__PortMessage& msg)
{
    if (msg.union_selection == SECC__TestSystem::V2G__PortMessage::ALT_v2g) {
        const SECC__Types::V2G__MessageUnion& v2g_msg = msg.v2g();
        ::std::string json_str = serialize_v2g_message(v2g_msg);

        ::std::cout << "[TCP_TLS_PORT] outgoing_send (v2g variant): "
                  << json_str.size() << " JSON bytes" << ::std::endl;
        if (json_str.size() <= 512) {
            ::std::cout << "[TCP_TLS_PORT] JSON: " << json_str << ::std::endl;
        } else {
            ::std::cout << "[TCP_TLS_PORT] JSON: " << json_str.substr(0, 256)
                      << "...(" << json_str.size() << " total)" << ::std::endl;
        }

        ::std::string ns = detect_namespace_from_json(json_str);
        ::std::cout << "[TCP_TLS_PORT] Detected namespace: " << ns << ::std::endl;

        json_str = apply_case_map(json_str);

        ::std::vector<uint8_t> exi_bytes = call_exi_encode(json_str, ns);
        if (exi_bytes.empty()) {
            ::std::cerr << "[TCP_TLS_PORT] ERROR: EXI encode returned empty" << ::std::endl;
            return;
        }

        ::std::cout << "[TCP_TLS_PORT] EXI hex: "
                  << bytes_to_hex(exi_bytes.data(), exi_bytes.size()) << ::std::endl;

        if (!send_v2gtp_framed_exi(exi_bytes)) {
            ::std::cerr << "[TCP_TLS_PORT] ERROR: send_v2gtp_framed_exi failed" << ::std::endl;
        }
    }
    else if (msg.union_selection == SECC__TestSystem::V2G__PortMessage::ALT_raw) {
        const ::OCTETSTRING& raw = msg.raw();
        const unsigned char* data = raw;
        size_t len = raw.lengthof();

        ::std::cout << "[TCP_TLS_PORT] outgoing_send (raw variant): "
                  << len << " bytes" << ::std::endl;

        ::std::vector<uint8_t> wire(data, data + len);
        if (!send_v2gtp_framed_exi(wire)) {
            ::std::cerr << "[TCP_TLS_PORT] ERROR: send_v2gtp_framed_exi (raw) failed" << ::std::endl;
        }
    }
    else {
        ::std::cerr << "[TCP_TLS_PORT] ERROR: outgoing_send called with "
                  << "unsupported union variant" << ::std::endl;
    }
}

bool V2G__TCP__TLS__Port::send_v2gtp_framed_exi(const ::std::vector<uint8_t>& exi_bytes) {
    // Ensure SECC connection
    if (!connected_ || socket_fd_ < 0) {
        connect_to_secc();
    }
    if (!connected_) {
        ::std::cerr << "[TCP_TLS_PORT] ERROR: not connected to SECC" << ::std::endl;
        return false;
    }

    // Build wire frame: V2GTP header (8 bytes) + EXI payload
    uint32_t payload_len = static_cast<uint32_t>(exi_bytes.size());
    if (payload_len > EXI_PAYLOAD_MAX) {
        ::std::cerr << "[TCP_TLS_PORT] ERROR: EXI payload too large: " << payload_len << ::std::endl;
        return false;
    }

    ::std::vector<uint8_t> wire(V2GTP_HDR_LEN + exi_bytes.size());
    write_v2gtp_header(wire.data(), payload_len);
    ::std::memcpy(wire.data() + V2GTP_HDR_LEN, exi_bytes.data(), exi_bytes.size());

    ::std::cout << "[TCP_TLS_PORT] Sending " << wire.size()
              << " bytes to SECC  (V2GTP payload_len=" << payload_len << ")" << ::std::endl;
    ::std::cout << "[TCP_TLS_PORT] V2GTP header: "
              << dump_hex(wire.data(), V2GTP_HDR_LEN) << ::std::endl;

    if (!send_wire_bytes(wire.data(), wire.size())) {
        return false;
    }
    return true;
}

// ============================================================================
// TCP socket to SECC
// ============================================================================
int V2G__TCP__TLS__Port::parse_ipv6_address(const ::std::string& addr_str,
                                            ::sockaddr_in6* addr6) {
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
        ::std::cerr << "[TCP_TLS_PORT] ERROR: inet_pton failed: " << addr_str << ::std::endl;
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
    // ISO 15118-3 guard delay
    ::std::this_thread::sleep_for(::std::chrono::milliseconds(75));

    ::std::string target_ip;
    int target_port = port_;

    // Priority: SECC_IP env > cached port > SDP result > default
    const char* sec_ip_env = ::getenv("SECC_IP");
    if (sec_ip_env != nullptr && ::strlen(sec_ip_env) > 0) {
        target_ip = sec_ip_env;
    } else {
        target_ip = V2G__UDP__SDP__Port__last_secc_ip_;
    }
    if (target_ip.empty()) {
        ::std::cerr << "[TCP_TLS_PORT] ERROR: No SECC target IP available" << ::std::endl;
        return;
    }

    if (cached_secc_port_ > 0) {
        target_port = cached_secc_port_;
    } else if (V2G__UDP__SDP__Port__last_secc_port_ != port_) {
        target_port = V2G__UDP__SDP__Port__last_secc_port_;
    }

    ::std::cout << "[TCP_TLS_PORT] Connecting to SECC at " << target_ip << ":" << target_port << ::std::endl;

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
            ::sockaddr_in6 addr6{};
            if (parse_ipv6_address(target_ip, &addr6) != 0) continue;
            addr6.sin6_port = htons(target_port);
            ok = (::connect(socket_fd_, reinterpret_cast<sockaddr*>(&addr6), sizeof(addr6)) == 0);
        } else {
            ::sockaddr_in addr4{};
            addr4.sin_family = AF_INET;
            addr4.sin_port = htons(target_port);
            if (::inet_pton(AF_INET, target_ip.c_str(), &addr4.sin_addr) != 1) {
                struct hostent* he = ::gethostbyname(target_ip.c_str());
                if (he != nullptr && he->h_addr_list[0] != nullptr)
                    ::memcpy(&addr4.sin_addr, he->h_addr_list[0], static_cast<size_t>(he->h_length));
                else { ::std::cerr << "[TCP_TLS_PORT] ERROR: resolve failed: " << target_ip << ::std::endl; break; }
            }
            ok = (::connect(socket_fd_, reinterpret_cast<sockaddr*>(&addr4), sizeof(addr4)) == 0);
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
    ::std::cout << "[TCP_TLS_PORT] Connected to SECC! port=" << cached_secc_port_ << ::std::endl;

    receive_thread_running_ = true;
    if (::pthread_create(&receive_thread_id_, nullptr,
                         &V2G__TCP__TLS__Port::receive_thread_func, this) != 0) {
        ::std::cerr << "[TCP_TLS_PORT] ERROR: pthread_create failed" << ::std::endl;
        receive_thread_running_ = false;
    }
}

void V2G__TCP__TLS__Port::disconnect_from_secc() {
    ::std::cout << "[TCP_TLS_PORT] Disconnecting from SECC..." << ::std::endl;
    receive_thread_running_ = false;
    if (receive_thread_id_ != 0) { ::pthread_join(receive_thread_id_, nullptr); receive_thread_id_ = 0; }
    if (socket_fd_ >= 0) { ::shutdown(socket_fd_, SHUT_RDWR); ::close(socket_fd_); socket_fd_ = -1; }
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
    return true;
}

// ============================================================================
// Incoming path: SECC wire -> V2GTP strip -> microservice DECODE -> OCTETSTRING
// ============================================================================
void* V2G__TCP__TLS__Port::receive_thread_func(void* arg) {
    static_cast<V2G__TCP__TLS__Port*>(arg)->receive_loop();
    return nullptr;
}

bool V2G__TCP__TLS__Port::strip_v2gtp_header(const ::std::vector<uint8_t>& wire_bytes,
                                              ::std::vector<uint8_t>& exi_out) {
    if (wire_bytes.size() < V2GTP_HDR_LEN) {
        ::std::cerr << "[TCP_TLS_PORT] ERROR: wire too short for V2GTP header ("
                  << wire_bytes.size() << " bytes)" << ::std::endl;
        return false;
    }

    const uint8_t* data = wire_bytes.data();
    uint8_t version = data[0];
    uint8_t inv_version = data[1];
    uint16_t payload_type = (static_cast<uint16_t>(data[2]) << 8) | data[3];
    uint32_t declared_len = (static_cast<uint32_t>(data[4]) << 24) |
                           (static_cast<uint32_t>(data[5]) << 16) |
                           (static_cast<uint32_t>(data[6]) << 8)  |
                           static_cast<uint32_t>(data[7]);

    if (version != V2GTP_VERSION || inv_version != V2GTP_INV_VERSION) {
        ::std::cerr << "[TCP_TLS_PORT] ERROR: Invalid V2GTP version byte[0]="
                  << ::std::hex << (int)version << " byte[1]=" << (int)inv_version
                  << ::std::dec << " (expected 0x01 0xFE)" << ::std::endl;
        return false;
    }
    if (payload_type != V2GTP_EXI_TYPE) {
        ::std::cerr << "[TCP_TLS_PORT] ERROR: Unsupported V2GTP payload type=0x"
                  << ::std::hex << payload_type << ::std::dec << ::std::endl;
        return false;
    }

    size_t exi_start = V2GTP_HDR_LEN;
    size_t exi_len = wire_bytes.size() - V2GTP_HDR_LEN;
    if (exi_len != declared_len) {
        ::std::cerr << "[TCP_TLS_PORT] WARNING: V2GTP declared_len=" << declared_len
                  << " actual=" << exi_len << ::std::endl;
    }

    ::std::cout << "[TCP_TLS_PORT] V2GTP OK: declared_len=" << declared_len
              << " actual=" << exi_len << ::std::endl;

    exi_out.assign(data + exi_start, data + wire_bytes.size());
    return true;
}

void V2G__TCP__TLS__Port::receive_loop() {
    unsigned char buf[RECV_BUF_SIZE];
    ::std::cout << "[TCP_TLS_PORT] Receive thread started" << ::std::endl;

    while (receive_thread_running_) {
        if (socket_fd_ < 0) {
            ::std::this_thread::sleep_for(::std::chrono::milliseconds(100));
            continue;
        }

        // Drain immediately-available data (MSG_DONTWAIT)
        bool had_data = false;
        for (;;) {
            ssize_t r = ::recv(socket_fd_, buf, sizeof(buf), MSG_DONTWAIT);
            if (r > 0) {
                had_data = true;
                ::std::cout << "[TCP_TLS_PORT] recv " << r << " bytes" << ::std::endl;
                receive_buffer_.insert(receive_buffer_.end(), buf, buf + r);
            } else if (r == 0) {
                ::std::cout << "[TCP_TLS_PORT] SECC closed connection" << ::std::endl;
                connected_ = false;
                goto connection_closed;
            } else {
                break;  // EAGAIN/EWOULDBLOCK
            }
        }

        if (had_data) {
            while (receive_thread_running_) {
                ::std::vector<uint8_t> frame;
                int rc = extract_v2gtp_frame(receive_buffer_, frame);
                if (rc < 0) { receive_buffer_.clear(); break; }
                if (rc == 0) break;
                handle_incoming_frame(frame);
            }
            continue;
        }

        // No data: wait with select()
        fd_set fds;
        FD_ZERO(&fds);
        FD_SET(socket_fd_, &fds);
        struct timeval tv{0, SELECT_TIMEOUT_US};
        int n = ::select(socket_fd_ + 1, &fds, nullptr, nullptr, &tv);
        if (n < 0) {
            if (errno == EINTR) continue;
            if (errno == EBADF) break;
            ::std::cerr << "[TCP_TLS_PORT] ERROR: select failed: " << ::strerror(errno) << ::std::endl;
            break;
        }
        if (n == 0) continue;

        ssize_t r = ::recv(socket_fd_, buf, sizeof(buf), 0);
        if (r < 0) { ::std::cerr << "[TCP_TLS_PORT] ERROR: recv failed: " << ::strerror(errno) << ::std::endl; break; }
        if (r == 0) { ::std::cout << "[TCP_TLS_PORT] SECC closed connection" << ::std::endl; connected_ = false; goto connection_closed; }
        receive_buffer_.insert(receive_buffer_.end(), buf, buf + r);

        while (receive_thread_running_) {
            ::std::vector<uint8_t> frame;
            int rc = extract_v2gtp_frame(receive_buffer_, frame);
            if (rc < 0) { receive_buffer_.clear(); break; }
            if (rc == 0) break;
            handle_incoming_frame(frame);
        }
    }

connection_closed:
    while (receive_thread_running_) {
        ::std::vector<uint8_t> frame;
        int rc = extract_v2gtp_frame(receive_buffer_, frame);
        if (rc <= 0) break;
        handle_incoming_frame(frame);
    }
    ::std::cout << "[TCP_TLS_PORT] Receive thread exiting" << ::std::endl;
    connected_ = false;
    receive_buffer_.clear();
}

void V2G__TCP__TLS__Port::handle_incoming_frame(const ::std::vector<uint8_t>& wire_bytes) {
    // Step 1: Strip V2GTP header
    ::std::vector<uint8_t> exi_bytes;
    if (!strip_v2gtp_header(wire_bytes, exi_bytes)) {
        ::std::cerr << "[TCP_TLS_PORT] ERROR: failed to strip V2GTP header" << ::std::endl;
        // Fail-fast: signal the error to TTCN-3 instead of dropping silently.
        push_error_indicator("v2gtp_framing",
                             "failed to strip V2GTP header (invalid bytes 0x"
                             + bytes_to_hex(wire_bytes.data(),
                                            ::std::min<size_t>(wire_bytes.size(), 8))
                             + "...)",
                             "");
        return;
    }

    if (exi_bytes.empty()) {
        ::std::cerr << "[TCP_TLS_PORT] ERROR: empty EXI payload" << ::std::endl;
        push_error_indicator("v2gtp_framing",
                             "empty EXI payload after V2GTP header strip",
                             "");
        return;
    }

    if (exi_bytes.size() > EXI_PAYLOAD_MAX) {
        ::std::cerr << "[TCP_TLS_PORT] ERROR: EXI payload too large ("
                  << exi_bytes.size() << " bytes)" << ::std::endl;
        push_error_indicator("exi_payload_too_large",
                             "EXI payload is " + ::std::to_string(exi_bytes.size())
                             + " bytes (max " + ::std::to_string(EXI_PAYLOAD_MAX) + ")",
                             "");
        return;
    }

    ::std::cout << "[TCP_TLS_PORT] EXI payload: " << bytes_to_hex(exi_bytes.data(), exi_bytes.size()) << ::std::endl;

    // Step 2: DECODE via microservice
    // SECC responses arrive on two possible namespaces:
    //   - AppProtocol namespace: urn:iso:15118:2:2010:AppProtocol
    //     (SupportedAppProtocolRes — the response to our SupportedAppProtocolReq)
    //   - MsgDef namespace: urn:iso:15118:2:2013:MsgDef
    //     (all V2G body messages: SessionSetupRes, ServiceDiscoveryRes, etc.)
    // Try MsgDef first (more common), fall back to AppProtocol.
    static const ::std::string NS_MSGD_ISO2  = "urn:iso:15118:2:2013:MsgDef";
    static const ::std::string NS_APP_ISO2    = "urn:iso:15118:2:2010:AppProtocol";

    ::std::string json_str = call_exi_decode(exi_bytes, NS_MSGD_ISO2);
    ::std::string last_ns   = NS_MSGD_ISO2;
    if (json_str.empty() || json_str == "{}") {
        ::std::cout << "[TCP_TLS_PORT] DECODE (MsgDef) empty, retrying AppProtocol namespace..." << ::std::endl;
        json_str  = call_exi_decode(exi_bytes, NS_APP_ISO2);
        last_ns   = NS_APP_ISO2;
    }

    if (json_str.empty() || json_str == "{}") {
        ::std::cerr << "[TCP_TLS_PORT] ERROR: EXI decode failed for both namespaces" << ::std::endl;
        // Fail-fast: signal DECODE ERROR to TTCN-3 instead of dropping the
        // message. TTCN-3 immediately matches { error_indicator := ? } and
        // sets verdict=fail within seconds (vs ~20 min message-timeout wait).
        push_error_indicator(
            "exi_decode",
            "DECODE ERROR: EXI microservice could not decode SECC payload for "
            "either namespace '" + NS_MSGD_ISO2 + "' or '" + NS_APP_ISO2 + "'. "
            "Common causes: schema-informed grammar mismatch (Issue A from "
            "BATCH_ANALYSIS_20260711.md), JAXB case-sensitivity mismatch "
            "(Issue B), or framing corruption.",
            last_ns
        );
        return;
    }

    last_decoded_json_ = json_str;
    ::std::cout << "[TCP_TLS_PORT] Decoded JSON: " << json_str << ::std::endl;

    // Step 3: Deliver decoded JSON as a V2G_PortMessage{ raw := ... } to TTCN-3.
    // The TITAN-generated incoming_message(V2G_PortMessage) accepts a typed
    // union; we use the `raw()` accessor (returns OCTETSTRING& and selects
    // ALT_raw internally) to carry the JSON bytes (Dumb Terminal receive
    // path — TTCN-3 parse/receive pattern does the JSON -> typed value
    // dispatch).
    const unsigned char* json_uc = reinterpret_cast<const unsigned char*>(json_str.data());
    ::OCTETSTRING decoded_octets(static_cast<int>(json_str.size()), json_uc);

    SECC__TestSystem::V2G__PortMessage port_msg;
    port_msg.raw() = decoded_octets;
    incoming_message(port_msg);
}

// ============================================================================
// Fail-fast error indicator (Sprint 3, 2026-07-11)
// ============================================================================
// Constructs a typed V2G_ErrorInfo and pushes a V2G_PortMessage with the
// error_indicator alternative set. TTCN-3 immediately matches
//   receive({ error_indicator := ? })
// and sets verdict=fail, terminating the run within seconds. Replaces the
// pre-Sprint-3 behavior of silently dropping decode-error messages and
// waiting for the per-TC message timeout (~20 min).
//
// The function uses TITAN's generated V2G__ErrorInfo class (record, in
// namespace SECC__Types) and V2G__PortMessage class (union, in namespace
// SECC__TestSystem). Field accessors follow TITAN's pattern (note the
// DOUBLE underscore in some accessors — TITAN escapes underscore-containing
// identifiers to avoid collision with the auto-generated accessor names):
//   V2G__ErrorInfo::source()           -> CHARSTRING&
//   V2G__ErrorInfo::detail()           -> CHARSTRING&   (renamed from
//                                          `message` because that's a TTCN-3
//                                          reserved keyword; the C++ field
//                                          name tracks the TTCN-3 field name)
//   V2G__ErrorInfo::namespace__uri()   -> OPTIONAL<CHARSTRING>&
//                                         (TITAN escapes the underscore in
//                                          the TTCN-3 field name
//                                          `namespace_uri` to `namespace__uri`)
//   V2G__PortMessage::ALT_raw, ALT_error__indicator   enum values
//   V2G__PortMessage::raw()            -> OCTETSTRING&   (sets ALT_raw)
//   V2G__PortMessage::error__indicator()-> SECC__Types::V2G__ErrorInfo&
//                                         (sets ALT_error__indicator)
// IMPORTANT: union_selection and the field_* pointers are PRIVATE. Use the
// accessor methods (raw() / error__indicator()) to select alternatives.
// ============================================================================
void V2G__TCP__TLS__Port::push_error_indicator(const ::std::string& source,
                                               const ::std::string& message,
                                               const ::std::string& namespace_uri) {
    ::std::cerr << "[TCP_TLS_PORT] FAIL-FAST: pushing error_indicator("
              << source << ", " << message
              << ", " << (namespace_uri.empty() ? "<none>" : namespace_uri)
              << ") to TTCN-3" << ::std::endl;

    // Build the V2G_ErrorInfo record (TITAN-generated class in SECC__Types).
    // The 3-arg constructor takes (source, detail, OPTIONAL<CHARSTRING>
    // namespace__uri). We pass an OPTIONAL that's bound only when the
    // caller supplied a non-empty namespace_uri.
    ::OPTIONAL<::CHARSTRING> ns_opt;
    if (!namespace_uri.empty()) {
        ns_opt = ::CHARSTRING(namespace_uri.c_str());
    }
    SECC__Types::V2G__ErrorInfo err_info(
        ::CHARSTRING(source.c_str()),
        ::CHARSTRING(message.c_str()),
        ns_opt
    );

    // Build a V2G_PortMessage with the error_indicator alternative selected.
    // The error__indicator() accessor (with DOUBLE underscore because TITAN
    // escapes field names to avoid collision with accessor methods) returns
    // a SECC__Types::V2G__ErrorInfo& AND sets union_selection = ALT_error__indicator.
    SECC__TestSystem::V2G__PortMessage port_msg;
    port_msg.error__indicator() = err_info;

    // Push into the port queue. TITAN matches
    //   receive({ error_indicator := ? })
    // immediately and the test case sets verdict=fail.
    incoming_message(port_msg);
}

}  // namespace SECC__TestSystem
