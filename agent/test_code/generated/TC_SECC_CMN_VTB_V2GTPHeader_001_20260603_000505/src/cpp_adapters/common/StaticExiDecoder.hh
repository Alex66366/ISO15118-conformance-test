// StaticExiDecoder.hh
//
// Purely static, data-driven EXI decoder for V2G messages.
// No external library dependencies (no JNI, no JVM, no Python).
//
// Architecture: The decoder is a pure runtime engine that reads pattern
// descriptors from exi_payload_library.json at startup.  All byte-level
// knowledge (anchor offsets, field offsets, encoding types) lives in JSON.
// The C++ source contains only the slicing and mapping logic.
//
// Pipeline: Receive Hex -> Match JSON Feature Pattern -> Slice Dynamic Fields -> Deliver Union
//

#ifndef STATIC_EXI_DECODER_HH
#define STATIC_EXI_DECODER_HH

#include <string>
#include <vector>
#include <map>
#include <cstdint>

namespace SECC__ExiJni {

// ============================================================================
// TTCN-3 ResponseCode enum values (must match SECC_TestSystem.hh)
// ============================================================================
// appHandresponseCodeType:
//   0 = OK_SuccessfulNegotiation
//   1 = OK_SuccessfulNegotiationWithMinorDeviation
//   2 = Failed_NoNegotiation
//
// responseCodeType (V2G body):
//   0  = OK
//   1  = OK_NewSessionEstablished
//   2  = OK_OldSessionJoined
//   3  = FAILED
//   4  = FAILED_SequenceError
//   5  = FAILED_ServiceIDInvalid
//   6  = FAILED_UnknownSession
//   7  = FAILED_PaymentSelectionInvalid
//   8  = FAILED_CertificateExpired
//   9  = FAILED_CertificateRevoked
//   10 = FAILED_ChallengeInvalid
//   11 = FAILED_SignatureError
//   12 = FAILED_NoCertificateAvailable
//   13 = FAILED_UnknownVendor
//   14 = FAILED_NotConfigured
//   15 = FAILED_CertStoreError
//   16 = FAILED_CertExpired
//   17 = FAILED_CertNotYetValid
//   18 = FAILED_CertWrongHs
//   19 = FAILED_CertRevoked
//   20 = FAILED_ContractCanceled
//   21 = FAILED_ScheduleExchangeError
//   22 = FAILED_PowerDeliveryNotApplied
//   23 = FAILED_TariffSelectionError
//   24 = FAILED_MeteringSignatureError
//   25 = FAILED_ChargeLoopSequenceError
// ============================================================================

struct AppHandResponseCodeMap {
    static int from_bytes(const uint8_t* exi) {
        // Byte[1] low 4 bits: (byte1 & 0x0C) >> 2
        //   0x40 -> 0 (OK_SuccessfulNegotiation)
        //   0x44 -> 1 (MinorDeviation)
        //   0x48 -> 2 (Failed_NoNegotiation)
        uint8_t rc_encoded = exi[1] & 0x0C;
        if (rc_encoded == 0x00) return 0;
        if (rc_encoded == 0x04) return 1;
        if (rc_encoded == 0x08) return 2;
        return -1;
    }

    static int schema_id_from_bytes(const uint8_t* exi) {
        // 10-bit value: ((byte2 & 0x03) << 8) | byte3, then >> 6
        int encoded = (static_cast<int>(exi[2] & 0x03) << 8) |
                       static_cast<int>(exi[3] & 0xFF);
        return encoded >> 6;
    }
};

struct V2GResponseCodeMap {
    static int from_byte(uint8_t b) { return from_byte_lookup(b); }

    static int from_byte_lookup(uint8_t b) {
        switch (b) {
            case 0x00: return 0;   // OK
            case 0x20: return 1;   // OK_NewSessionEstablished
            case 0x40: return 2;   // OK_OldSessionJoined
            case 0x80: return 3;   // FAILED
            case 0xA0: return 4;   // FAILED_SequenceError
            case 0xC0: return 5;   // FAILED_ServiceIDInvalid
            case 0xE0: return 6;   // FAILED_UnknownSession
            case 0x90: return 7;   // FAILED_PaymentSelectionInvalid
            case 0xB0: return 8;   // FAILED_CertificateExpired
            case 0xD0: return 9;   // FAILED_CertificateRevoked
            case 0xF0: return 10;  // FAILED_ChallengeInvalid
            case 0xA8: return 11;  // FAILED_SignatureError
            case 0xB8: return 12;  // FAILED_NoCertificateAvailable
            case 0xC8: return 13;  // FAILED_UnknownVendor
            case 0x88: return 14;  // FAILED_NotConfigured
            case 0x98: return 15;  // FAILED_CertStoreError
            case 0xA4: return 16;  // FAILED_CertExpired
            case 0xA2: return 17;  // FAILED_CertNotYetValid
            case 0xB4: return 18;  // FAILED_CertWrongHs
            case 0xB2: return 19;  // FAILED_CertRevoked
            case 0xA6: return 20;  // FAILED_ContractCanceled
            case 0xC6: return 21;  // FAILED_ScheduleExchangeError
            case 0xE6: return 22;  // FAILED_PowerDeliveryNotApplied
            case 0xF6: return 23;  // FAILED_TariffSelectionError
            case 0xD6: return 24;  // FAILED_MeteringSignatureError
            case 0xE8: return 25;  // FAILED_ChargeLoopSequenceError
            default:   return -1;  // Unknown
        }
    }

    static int from_byte_with_custom(uint8_t b) {
        const std::map<uint8_t,int>& custom = s_custom_map();
        typename std::map<uint8_t,int>::const_iterator it = custom.find(b);
        if (it != custom.end()) return it->second;
        return from_byte_lookup(b);
    }

    static void register_code(uint8_t byte_val, int code) {
        s_custom_map()[byte_val] = code;
    }

private:
    static std::map<uint8_t,int>& s_custom_map() {
        static std::map<uint8_t,int> m;
        return m;
    }
};

// ============================================================================
// Pattern descriptors loaded from exi_payload_library.json
// ============================================================================

enum FieldEncoding {
    ENC_UNKNOWN = 0,
    ENC_APP_HAND_RESPONSE_CODE,
    ENC_SCHEMA_ID,
    ENC_V2G_RESPONSE_CODE,
    ENC_SESSION_ID,
    ENC_BYTES
};

struct FieldDescriptor {
    std::string name;
    int offset_rel;       // relative to anchor byte
    int length;
    std::string encoding;  // "session_id", "v2g_response_code", etc.
    enum FieldEncoding enc_type;

    FieldDescriptor() : offset_rel(0), length(0), enc_type(ENC_UNKNOWN) {}
};

struct MessagePattern {
    std::string             key;
    uint16_t                min_payload_len;
    int                     anchor_offset;
    std::vector<uint8_t>    anchor_values;
    std::vector<FieldDescriptor> fields;

    MessagePattern() : min_payload_len(0), anchor_offset(-1) {}
};

// ============================================================================
// Static EXI Decoder
// ============================================================================

class StaticExiDecoder {
public:
    struct DecodeResult {
        enum MsgType {
            MSG_UNKNOWN,
            MSG_SUPPORTED_APP_PROTOCOL_RES,
            MSG_SESSION_SETUP_RES,
            MSG_SERVICE_DISCOVERY_RES,
            MSG_SERVICE_DETAIL_RES,
            MSG_PAYMENT_SERVICE_SELECTION_RES,
            MSG_PAYMENT_DETAILS_RES,
            MSG_AUTHORIZATION_RES,
            MSG_CHARGE_PARAMETER_DISCOVERY_RES,
            MSG_POWER_DELIVERY_RES,
            MSG_SESSION_STOP_RES,
            MSG_CABLE_CHECK_RES,
            MSG_PRECHARGE_RES,
            MSG_CURRENT_DEMAND_RES,
            MSG_CHARGING_STATUS_RES,
            MSG_METERING_RECEIPT_RES,
            MSG_WELDING_DETECTION_RES,
        } msg_type;

        int app_response_code;
        int schema_id;
        int v2g_response_code;
        std::vector<uint8_t> session_id;
        std::string evseid;  // extracted EVSEID string for SessionSetupRes

        DecodeResult() : msg_type(MSG_UNKNOWN), app_response_code(0),
                         schema_id(0), v2g_response_code(0) {}
    };

    StaticExiDecoder();
    ~StaticExiDecoder();

    bool load_from_json(const std::string& path);
    bool decode(const uint8_t* wire_bytes, size_t wire_len, DecodeResult& result);
    bool decode_exi(const uint8_t* exi_payload, size_t exi_len, DecodeResult& result);

    const char* last_error() const { return last_error_.c_str(); }
    bool is_available() const { return !patterns_.empty(); }
    bool is_loaded() const { return loaded_; }

private:
    mutable std::string last_error_;
    mutable bool loaded_;
    mutable std::vector<MessagePattern> patterns_;

    const MessagePattern* find_pattern(const uint8_t* exi_payload, size_t exi_len) const;
    bool slice_field(const uint8_t* base, size_t total_len,
                     const FieldDescriptor& f, DecodeResult& result) const;
    bool parse_by_pattern(const uint8_t* exi, size_t len,
                         const MessagePattern& pat, DecodeResult& result) const;
    bool parse_app_protocol_res(const uint8_t* exi, size_t len, DecodeResult& result) const;
    bool parse_session_setup_res(const uint8_t* exi, size_t len, DecodeResult& result) const;
    bool parse_generic_res(const uint8_t* exi, size_t len,
                          const MessagePattern& pat, DecodeResult& result) const;
};

} // namespace SECC__ExiJni

#endif // STATIC_EXI_DECODER_HH
