/*
 * OpenV2G_Wrapper.h
 *
 * ISO 15118-2 EXI Dynamic Encoder Bridge Layer
 *
 * PURPOSE: Provide a zero-pollution interface between the TITAN C++ adapter
 * (V2G_TCP_TLS_Port.cc) and the OpenV2G C library.  This header file must
 * NEVER #include any OpenV2G or TITAN-generated header.  All cross-library
 * communication uses only standard C++ types (std::vector, std::string, etc.).
 *
 * WHY THIS LAYER EXISTS:
 * Both OpenV2G (struct iso1V2G_Message / enum iso1responseCodeType) and the
 * TITAN auto-generated C++ (struct V2G__MessageUnion / enum V2G__ResponseCode__)
 * define symbols named V2G_Message, ResponseCode, and SessionID.  Including
 * both sets of headers in the same translation unit produces O(100) macro/
 * struct/enum redefinition errors.  This pimpl bridge physically separates
 * the two worlds: .cc files that #include OpenV2G headers are encapsulated
 * here and never seen by TITAN code; TITAN code includes only this header.
 *
 * DESIGN RULES:
 * 1. No OpenV2G #include directives in this file.
 * 2. No TITAN-generated #include directives in this file.
 * 3. No macros that collide with OpenV2G or TITAN namespaces.
 * 4. Every function returns std::vector<uint8_t> — the raw wire bytes ready
 *    to be handed directly to the TCP send() call (V2GTP header included).
 * 5. All string parameters use std::string (UTF-8).  Hex session IDs are
 *    passed as 16-character std::string ("0011223344556677").
 * 6. Each encode function is standalone; no shared state between calls.
 *
 * USAGE from V2G_TCP_TLS_Port.cc:
 *   #include "OpenV2G_Wrapper.hh"   // no OpenV2G or TITAN headers above this
 *   auto wire = OpenV2G::encode_service_discovery_res(session_id, response_code, ...);
 *   send_wire_bytes(wire.data(), wire.size());
 *
 * Created for the ISO 15118-2/4 TTCN-3 Test Generation Pipeline.
 * Author: Infrastructure Agent — Phase 2 Architecture
 */

#ifndef OPENV2G_WRAPPER_HH
#define OPENV2G_WRAPPER_HH

#include <cstdint>
#include <string>
#include <vector>

namespace OpenV2G {

/* ========================================================================
 * Enumerations mirrored from ISO 15118-2 (uint8_t to avoid OpenV2G includes)
 * These must stay in sync with the actual enum values in iso1EXIDatatypes.h
 * ======================================================================== */

/* ResponseCode values (iso1responseCodeType) */
enum class ResponseCode : uint8_t {
    OK                              = 0,
    OK_NewSessionEstablished        = 1,
    OK_OldSessionJoined             = 2,
    OK_CertificateExpiresSoon       = 3,
    FAILED                          = 4,
    FAILED_SequenceError            = 5,
    FAILED_ServiceIDInvalid         = 6,
    FAILED_UnknownSession           = 7,
    FAILED_ServiceSelectionInvalid  = 8,
    FAILED_PaymentSelectionInvalid  = 9,
    FAILED_CertificateExpired       = 10,
    FAILED_SignatureError           = 11,
    FAILED_NoCertificateAvailable   = 12,
    FAILED_CertChainError           = 13,
    FAILED_ChallengeInvalid         = 14,
    FAILED_ContractCanceled         = 15,
    FAILED_WrongChargeParameter     = 16,
    FAILED_PowerDeliveryNotApplied  = 17,
    FAILED_TariffSelectionInvalid   = 18,
    FAILED_ChargingProfileInvalid   = 19,
    FAILED_MeteringSignatureNotValid= 20,
    FAILED_NoChargeServiceSelected  = 21,
    FAILED_WrongEnergyTransferMode  = 22,
    FAILED_ContactorError           = 23,
    FAILED_CertificateNotAllowedAtThisEVSE = 24,
    FAILED_CertificateRevoked       = 25
};

/* PaymentOption values (iso1paymentOptionType) */
enum class PaymentOption : uint8_t {
    Contract          = 0,
    ExternalPayment   = 1
};

/* ServiceCategory values (iso1serviceCategoryType) */
enum class ServiceCategory : uint8_t {
    EVCharging           = 0,
    Internet             = 1,
    ContractCertificate  = 2,
    OtherCustom          = 3
};

/* EnergyTransferMode values (iso1EnergyTransferModeType) */
enum class EnergyTransferMode : uint8_t {
    AC_single_phase_core = 0,
    AC_three_phase_core = 1,
    DC_core             = 2,
    DC_extended         = 3,
    DC_combo_core       = 4,
    DC_unique           = 5
};

/* ========================================================================
 * Structured input types (plain C++ data, no OpenV2G structs)
 * These are the ONLY types the V2G_TCP_TLS_Port.cc ever talks to.
 * ======================================================================== */

/* One entry in the ServiceList.Service array */
struct ServiceEntry {
    uint16_t service_id = 0;
    std::string service_name;              // UTF-8, max 32 chars
    bool service_name_present = false;
    ServiceCategory category = ServiceCategory::EVCharging;
    std::string service_scope;             // UTF-8, max 64 chars
    bool service_scope_present = false;
    bool free_service = false;              // false = not free, true = free
};

/* SessionSetupReq body parameters.
 *
 * ISO 15118-2 SessionSetupReq has exactly ONE mandatory field:
 *   EVCCID : octet string of EXACTLY 6 bytes (urn:iso:15118:2:2013:MsgDataTypes,evccIDType)
 *
 * Common SECC implementations (Java ISDP/IS, OpenV2G demo SECC) parse EVCCID as 6
 * raw bytes. If the field is missing or zero-filled, the SECC will raise
 * "Premature EOS" or "EVCCID invalid" — the EXI body must carry valid 6 bytes.
 *
 * Source: the EVCCID byte sequence is typically derived from an ASCII identifier
 * such as the TTCN-3 template constant "PLATFORM123456" — the adapter is
 * responsible for converting that ASCII string into 6 raw bytes (see
 * V2G_TCP_TLS_Port.cc SessionSetupReq branch).
 */
struct SessionSetupReq_Params {
    std::vector<uint8_t> evcc_id_bytes;    // MUST be exactly 6 bytes; empty = reject
};

/* The full ServiceDiscoveryRes body parameters */
struct ServiceDiscoveryRes_Params {
    ResponseCode response_code = ResponseCode::OK;

    /* PaymentOptionList: 1–2 payment options (Contract, ExternalPayment) */
    std::vector<PaymentOption> payment_options;

    /* ChargeService (mandatory) */
    uint16_t charge_service_id = 0;
    std::string charge_service_name;       // UTF-8, max 32 chars
    bool charge_service_name_present = false;
    ServiceCategory charge_service_category = ServiceCategory::EVCharging;
    std::string charge_service_scope;      // UTF-8, max 64 chars
    bool charge_service_scope_present = false;
    bool charge_service_free = false;
    std::vector<EnergyTransferMode> supported_energy_modes;  // 1–6 modes

    /* ServiceList (optional) */
    std::vector<ServiceEntry> service_list;
};

/* ========================================================================
 * EXI Encoders — public API
 *
 * Each function:
 *   - Takes pure C++ types (no OpenV2G structs)
 *   - Builds the full V2GTP+EXI wire bytes internally
 *   - Returns std::vector<uint8_t> with the complete frame (ready for send())
 *
 * Wire format per call:
 *   Bytes [0..7]   : V2GTP header
 *                      [0] = 0x01 (version)
 *                      [1] = 0xFE (inverse version)
 *                      [2..3] = 0x8001 (payload type = EXI)
 *                      [4..7] = payload length (network byte order)
 *   Bytes [8..N-1] : EXI-encoded message body
 *
 * Return value: empty vector on error; caller MUST check .empty().
 * ======================================================================== */

/* Encode a ServiceDiscoveryRes message.
 *
 * @param session_id  8-byte SessionID as 16 hex chars, e.g. "0011223344556677".
 *                    Use "0000000000000000" for a new session.
 * @param params      Structured ServiceDiscoveryRes parameters.
 * @return            Complete V2GTP+EXI wire bytes, or empty vector on error.
 */
std::vector<uint8_t>
encode_service_discovery_res(
    const std::string&                  session_id,
    const ServiceDiscoveryRes_Params&   params
);

/* Placeholder stubs for other messages (to be implemented in Phase 3) */
std::vector<uint8_t> encode_session_setup_res(
    const std::string& session_id,
    ResponseCode rc,
    const std::string& evse_id
);

/* Encode SessionSetupReq.
 *
 * @param session_id  8-byte SessionID as 16 hex chars, e.g. "0011223344556677".
 * @param params      Structured SessionSetupReq parameters (must contain 6 EVCCID bytes).
 * @return            Complete V2GTP+EXI wire bytes, or empty vector on error
 *                    (including missing/short EVCCID, encode failure).
 *
 * Returns EMPTY (not partial bytes) when:
 *   - params.evcc_id_bytes.size() != 6
 *   - writeEXIHeader fails
 *   - encode_iso1ExiDocument fails (with stderr ERROR log)
 */
std::vector<uint8_t> encode_session_setup_req(
    const std::string& session_id,
    const SessionSetupReq_Params& params
);

std::vector<uint8_t> encode_service_discovery_req(
    const std::string& session_id,
    const std::string& service_scope,
    bool service_scope_present,
    ServiceCategory category,
    bool category_present
);

/* ========================================================================
 * EXI Decoders — public API
 *
 * Decode raw wire bytes (V2GTP header + EXI payload) into plain C++ structs.
 * These replace the heuristic bit-slice approach (exi[N] byte extraction) that
 * fails for EXI bit-packed encoding.  OpenV2G's decode_iso1ExiDocument() handles
 * all bit-level parsing internally.
 * ======================================================================== */

/* Decoded SessionSetupRes fields */
struct SessionSetupRes_DecodeResult {
    std::string session_id;       // 16 hex chars, e.g. "D73E33B128610EA7"
    ResponseCode response_code = ResponseCode::OK;
    std::string evse_id;         // raw EVSEID string, e.g. "UK123E1234"
    bool        success = false;
    std::string error_message;
};

/* Decode a SessionSetupRes from raw EXI bytes.
 *
 * @param exi_payload  Pure EXI bit-stream bytes (V2GTP header stripped by caller).
 *                    This is exactly the bytes starting at wire[8] through wire[end].
 * @return            Decoded fields; .success==true only on full decode.
 */
SessionSetupRes_DecodeResult
decode_session_setup_res(const std::vector<uint8_t>& wire_bytes);

/* ========================================================================
 * Utility — validate and convert a hex string to bytes
 * ======================================================================== */
std::vector<uint8_t> hex_to_bytes(const std::string& hex);

}  // namespace OpenV2G

#endif  // OPENV2G_WRAPPER_HH
