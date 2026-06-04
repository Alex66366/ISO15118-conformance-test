// StaticExiDecoder.cc
//
// Purely static, data-driven EXI decoder implementation.
// No external library dependencies.
//

#include "StaticExiDecoder.hh"
#include <cstring>
#include <sstream>
#include <iomanip>
#include <iostream>
#include <fstream>

namespace {

static const size_t V2GTP_HEADER_SIZE = 8;
static const uint8_t V2GTP_VERSION = 1;
static const uint8_t V2GTP_INV_VERSION = 0xFE;

static uint32_t read_be32(const uint8_t* p) {
    return (static_cast<uint32_t>(p[0]) << 24) |
           (static_cast<uint32_t>(p[1]) << 16) |
           (static_cast<uint32_t>(p[2]) << 8) |
           static_cast<uint32_t>(p[3]);
}

static std::string bytes_to_hex(const uint8_t* data, size_t len) {
    std::ostringstream oss;
    for (size_t i = 0; i < len; ++i)
        oss << std::hex << std::setw(2) << std::setfill('0') << (int)data[i];
    return oss.str();
}

// EXI 8-bit string decoder: reads nchars raw bytes from after the header byte.
// Returns them as a hex string for reliable template matching.

// Minimal JSON scanner - no external dependency
static const char* skip_ws(const char* p) {
    while (*p && (*p == ' ' || *p == '\n' || *p == '\r' || *p == '\t')) ++p;
    return p;
}

static std::string parse_json_string(const char*& p) {
    p = skip_ws(p);
    if (*p != '"') return "";
    ++p;
    std::string val;
    while (*p && *p != '"') val += *p++;
    if (*p == '"') ++p;
    return val;
}

static int parse_json_int(const char*& p) {
    p = skip_ws(p);
    int sign = 1;
    if (*p == '-') { sign = -1; ++p; }
    int val = 0;
    while (*p >= '0' && *p <= '9') { val = val * 10 + (*p - '0'); ++p; }
    return sign * val;
}

static int parse_hex_val(const std::string& s) {
    if (s.size() > 2 && s[0] == '0' && (s[1] == 'x' || s[1] == 'X'))
        return std::stoi(s.substr(2), nullptr, 16);
    return std::stoi(s, nullptr, 10);
}

static bool find_messages_object(const char* json, size_t json_len,
                               const char*& out_start, const char*& out_end) {
    const char* end = json + json_len;
    const char* p = json;
    p = skip_ws(p);
    if (*p != '{') return false;
    ++p;
    while (p < end) {
        p = skip_ws(p);
        if (*p == '}') break;
        if (*p != '"') { p = skip_ws(p); break; }
        ++p;
        const char* key_start = p;
        while (p < end && *p && *p != '"') ++p;
        std::string key(key_start, p - key_start);
        if (*p == '"') ++p;
        p = skip_ws(p);
        if (*p != ':') { ++p; continue; }
        ++p;
        p = skip_ws(p);
        if (key == "messages") {
            out_start = p;
            int depth = 0;
            bool in_str = false;
            while (p < end && *p) {
                if (!in_str) {
                    if (*p == '{') ++depth;
                    else if (*p == '}') { --depth; if (depth <= 0) { ++p; break; } }
                    else if (*p == '"') in_str = true;
                } else {
                    if (*p == '"' && *(p-1) != '\\') in_str = false;
                }
                ++p;
            }
            out_end = p;
            return true;
        }
        int depth = 0;
        bool in_str = false;
        while (p < end && *p) {
            if (!in_str) {
                if (*p == '{' || *p == '[') ++depth;
                else if (*p == '}' || *p == ']') { --depth; if (depth < 0) { ++p; break; } }
                else if (*p == '"') in_str = true;
                else if (*p == ',' && depth == 0) { ++p; break; }
            } else {
                if (*p == '"' && *(p-1) != '\\') in_str = false;
            }
            ++p;
        }
    }
    return false;
}

static std::string get_msg_string(const char* obj_start, const char* obj_end, const char* field) {
    const char* p = obj_start;
    const char* obj_lim = obj_end;
    while (p < obj_lim) {
        p = skip_ws(p);
        if (*p != '"') { ++p; continue; }
        ++p;
        const char* k_start = p;
        while (p < obj_lim && *p && *p != '"') ++p;
        size_t klen = p - k_start;
        if (*p == '"') ++p;
        p = skip_ws(p);
        if (*p != ':') { ++p; continue; }
        ++p;
        if (klen == std::strlen(field) && std::strncmp(k_start, field, klen) == 0)
            return parse_json_string(p);
        int depth = 0;
        bool in_str = false;
        while (p < obj_lim && *p) {
            if (!in_str) {
                if (*p == '{' || *p == '[') ++depth;
                else if (*p == '}' || *p == ']') { --depth; if (depth < 0) { ++p; break; } }
                else if (*p == '"') in_str = true;
                else if (*p == ',' && depth == 0) { ++p; break; }
            } else {
                if (*p == '"' && *(p-1) != '\\') in_str = false;
            }
            ++p;
        }
    }
    return "";
}

static int get_msg_int(const char* obj_start, const char* obj_end, const char* field) {
    const char* p = obj_start;
    const char* obj_lim = obj_end;
    while (p < obj_lim) {
        p = skip_ws(p);
        if (*p != '"') { ++p; continue; }
        ++p;
        const char* k_start = p;
        while (p < obj_lim && *p && *p != '"') ++p;
        size_t klen = p - k_start;
        if (*p == '"') ++p;
        p = skip_ws(p);
        if (*p != ':') { ++p; continue; }
        ++p;
        if (klen == std::strlen(field) && std::strncmp(k_start, field, klen) == 0)
            return parse_json_int(p);
        int depth = 0;
        bool in_str = false;
        while (p < obj_lim && *p) {
            if (!in_str) {
                if (*p == '{' || *p == '[') ++depth;
                else if (*p == '}' || *p == ']') { --depth; if (depth < 0) { ++p; break; } }
                else if (*p == '"') in_str = true;
                else if (*p == ',' && depth == 0) { ++p; break; }
            } else {
                if (*p == '"' && *(p-1) != '\\') in_str = false;
            }
            ++p;
        }
    }
    return 0;
}

static bool get_msg_object(const char* obj_start, const char* obj_end,
                         const char* field,
                         const char*& out_start, const char*& out_end) {
    const char* p = obj_start;
    const char* obj_lim = obj_end;
    while (p < obj_lim) {
        p = skip_ws(p);
        if (*p != '"') { ++p; continue; }
        ++p;
        const char* k_start = p;
        while (p < obj_lim && *p && *p != '"') ++p;
        size_t klen = p - k_start;
        if (*p == '"') ++p;
        p = skip_ws(p);
        if (*p != ':') { ++p; continue; }
        ++p;
        p = skip_ws(p);
        if (klen == std::strlen(field) && std::strncmp(k_start, field, klen) == 0) {
            out_start = p;
            int depth = 0;
            bool in_str = false;
            while (p < obj_lim && *p) {
                if (!in_str) {
                    if (*p == '{' || *p == '[') ++depth;
                    else if (*p == '}' || *p == ']') { --depth; if (depth <= 0) { ++p; break; } }
                    else if (*p == '"') in_str = true;
                    else if (*p == ',' && depth == 0) { ++p; break; }
                } else {
                    if (*p == '"' && *(p-1) != '\\') in_str = false;
                }
                ++p;
            }
            out_end = p;
            return true;
        }
        int depth = 0;
        bool in_str = false;
        while (p < obj_lim && *p) {
            if (!in_str) {
                if (*p == '{' || *p == '[') ++depth;
                else if (*p == '}' || *p == ']') { --depth; if (depth < 0) { ++p; break; } }
                else if (*p == '"') in_str = true;
                else if (*p == ',' && depth == 0) { ++p; break; }
            } else {
                if (*p == '"' && *(p-1) != '\\') in_str = false;
            }
            ++p;
        }
    }
    return false;
}

static bool extract_anchor_values(const char* fp_start, const char* fp_end,
                                std::vector<uint8_t>& out) {
    const char* av_start, *av_end;
    if (!get_msg_object(fp_start, fp_end, "anchor_values", av_start, av_end) &&
        !get_msg_object(fp_start, fp_end, "anchor_value", av_start, av_end))
        return false;
    const char* p = skip_ws(av_start);
    if (*p != '[') return false;
    ++p;
    while (*p && *p != ']') {
        p = skip_ws(p);
        if (*p == ']') break;
        int val = 0;
        if (*p == '"') {
            std::string s = parse_json_string(p);
            val = parse_hex_val(s);
        } else {
            val = parse_json_int(p);
        }
        out.push_back(static_cast<uint8_t>(val));
        p = skip_ws(p);
        if (*p == ',') ++p;
    }
    return true;
}

static bool extract_fields(const char* fp_start, const char* fp_end,
                          std::vector<SECC__ExiJni::FieldDescriptor>& out) {
    const char* fields_start, *fields_end;
    if (!get_msg_object(fp_start, fp_end, "fields", fields_start, fields_end))
        return false;
    const char* p = skip_ws(fields_start);
    if (*p != '[') return false;
    ++p;
    while (*p && *p != ']') {
        p = skip_ws(p);
        if (*p == ']') break;
        if (*p == '{') {
            const char* f_start = p;
            ++p;
            int depth = 1;
            bool in_str = false;
            while (p && depth > 0) {
                if (!in_str) {
                    if (*p == '{') ++depth;
                    else if (*p == '}') { --depth; if (depth == 0) { ++p; break; } }
                    else if (*p == '"') in_str = true;
                } else {
                    if (*p == '"' && *(p-1) != '\\') in_str = false;
                }
                ++p;
            }
            const char* f_end = p;
            SECC__ExiJni::FieldDescriptor fd;
            fd.name = get_msg_string(f_start, f_end, "name");
            fd.offset_rel = get_msg_int(f_start, f_end, "offset_relative_to_anchor");
            fd.length = get_msg_int(f_start, f_end, "length");
            fd.encoding = get_msg_string(f_start, f_end, "encoding");
            if (!fd.name.empty()) out.push_back(fd);
        }
        p = skip_ws(p);
        if (*p == ',') ++p;
    }
    return true;
}

static bool parse_patterns_from_json(
        const std::string& json_text,
        std::vector<SECC__ExiJni::MessagePattern>& out) {
    const char *msgs_start, *msgs_end;
    if (!find_messages_object(json_text.c_str(), json_text.size(), msgs_start, msgs_end)) {
        std::cerr << "[JSON] ERROR: messages not found" << std::endl;
        return false;
    }
    const char* p = msgs_start;
    int count = 0;
    while (p < msgs_end) {
        p = skip_ws(p);
        if (*p == '}') break;
        if (*p != '"') { ++p; continue; }
        ++p;
        const char* key_start = p;
        while (p < msgs_end && *p && *p != '"') ++p;
        std::string msg_key(key_start, p - key_start);
        if (*p == '"') ++p;
        p = skip_ws(p);
        if (*p != ':') { ++p; continue; }
        ++p;
        const char* obj_start = skip_ws(p);
        const char* obj_end = obj_start;
        int depth = 0;
        bool in_str = false;
        while (obj_end < msgs_end && *obj_end) {
            if (!in_str) {
                if (*obj_end == '{') ++depth;
                else if (*obj_end == '}') { --depth; if (depth <= 0) { ++obj_end; break; } }
                else if (*obj_end == '"') in_str = true;
                else if (*obj_end == ',' && depth == 0) { ++obj_end; break; }
            } else {
                if (*obj_end == '"' && *(obj_end-1) != '\\') in_str = false;
            }
            ++obj_end;
        }
        p = obj_end;
        if (get_msg_string(obj_start, obj_end, "direction") != "res") continue;
        SECC__ExiJni::MessagePattern pat;
        pat.key = msg_key;
        std::string hex_str = get_msg_string(obj_start, obj_end, "exi_payload_hex");
        pat.min_payload_len = static_cast<uint16_t>(hex_str.size() / 2);
        const char *fp_start, *fp_end;
        if (get_msg_object(obj_start, obj_end, "feature_pattern", fp_start, fp_end)) {
            pat.anchor_offset = get_msg_int(fp_start, fp_end, "anchor_offset");
            extract_anchor_values(fp_start, fp_end, pat.anchor_values);
            extract_fields(fp_start, fp_end, pat.fields);
            out.push_back(pat);
            ++count;
        }
    }
    std::cerr << "[JSON] Total patterns: " << count << std::endl;
    return count > 0;
}

} // anonymous namespace

namespace SECC__ExiJni {

StaticExiDecoder::StaticExiDecoder() : loaded_(false) {}
StaticExiDecoder::~StaticExiDecoder() {}

bool StaticExiDecoder::load_from_json(const std::string& path) {
    std::ifstream file(path.c_str());
    if (!file.is_open()) {
        last_error_ = "Failed to open JSON library: " + path;
        return false;
    }
    std::stringstream buf;
    buf << file.rdbuf();
    patterns_.clear();
    if (!parse_patterns_from_json(buf.str(), patterns_)) {
        last_error_ = "Failed to parse patterns from JSON library";
        return false;
    }
    loaded_ = true;
    return true;
}

bool StaticExiDecoder::decode(const uint8_t* wire_bytes, size_t wire_len,
                             DecodeResult& result) {
    result = DecodeResult();
    last_error_.clear();
    if (wire_len < V2GTP_HEADER_SIZE) {
        last_error_ = "Wire too short for V2GTP header"; return false;
    }
    uint8_t version = wire_bytes[0];
    uint8_t inv_version = wire_bytes[1];
    uint16_t payload_type = (static_cast<uint16_t>(wire_bytes[2]) << 8) | wire_bytes[3];
    uint32_t payload_len = read_be32(wire_bytes + 4);
    if (version != V2GTP_VERSION || inv_version != V2GTP_INV_VERSION) {
        last_error_ = "Invalid V2GTP header"; return false;
    }
    if (payload_type != 0x8001) {
        last_error_ = "Unknown V2GTP payload type"; return false;
    }
    if (wire_len < V2GTP_HEADER_SIZE + payload_len) {
        last_error_ = "Wire too short for payload"; return false;
    }
    return decode_exi(wire_bytes + V2GTP_HEADER_SIZE,
                      static_cast<size_t>(payload_len), result);
}

bool StaticExiDecoder::decode_exi(const uint8_t* exi, size_t exi_len,
                                 DecodeResult& result) {
    result = DecodeResult();
    last_error_.clear();
    if (exi_len < 2) { last_error_ = "EXI too short"; return false; }

    std::cout << "[StaticExiDecoder] decode_exi: trying " << patterns_.size()
              << " patterns, first2=" << std::hex << (int)exi[0] << " " << (int)exi[1] << std::dec << std::endl;

    const MessagePattern* pat = find_pattern(exi, exi_len);
    if (pat == nullptr) {
        std::ostringstream oss;
        oss << "Unknown EXI message type: ";
        for (size_t i = 0; i < std::min(exi_len, (size_t)24); ++i)
            oss << std::hex << std::setw(2) << std::setfill('0') << (int)exi[i];
        last_error_ = oss.str();
        std::cerr << "[StaticExiDecoder] " << last_error_ << std::endl;
        return false;
    }

    std::cout << "[StaticExiDecoder] Matched: " << pat->key << std::endl;

    if (pat->key == "supported_app_protocol_res") {
        if (!parse_app_protocol_res(exi, exi_len, result)) return false;
        result.msg_type = DecodeResult::MSG_SUPPORTED_APP_PROTOCOL_RES;
        return true;
    }
    if (pat->key == "session_setup_res") {
        if (!parse_session_setup_res(exi, exi_len, result)) return false;
        result.msg_type = DecodeResult::MSG_SESSION_SETUP_RES;
        return true;
    }
    if (!parse_generic_res(exi, exi_len, *pat, result)) return false;

    if (pat->key == "service_discovery_res") result.msg_type = DecodeResult::MSG_SERVICE_DISCOVERY_RES;
    else if (pat->key == "service_detail_res") result.msg_type = DecodeResult::MSG_SERVICE_DETAIL_RES;
    else if (pat->key == "payment_service_selection_res") result.msg_type = DecodeResult::MSG_PAYMENT_SERVICE_SELECTION_RES;
    else if (pat->key == "payment_details_res") result.msg_type = DecodeResult::MSG_PAYMENT_DETAILS_RES;
    else if (pat->key == "authorization_res") result.msg_type = DecodeResult::MSG_AUTHORIZATION_RES;
    else if (pat->key == "charge_parameter_discovery_res") result.msg_type = DecodeResult::MSG_CHARGE_PARAMETER_DISCOVERY_RES;
    else if (pat->key == "power_delivery_res") result.msg_type = DecodeResult::MSG_POWER_DELIVERY_RES;
    else if (pat->key == "session_stop_res") result.msg_type = DecodeResult::MSG_SESSION_STOP_RES;
    else if (pat->key == "cable_check_res") result.msg_type = DecodeResult::MSG_CABLE_CHECK_RES;
    else if (pat->key == "precharge_res") result.msg_type = DecodeResult::MSG_PRECHARGE_RES;
    else if (pat->key == "current_demand_res") result.msg_type = DecodeResult::MSG_CURRENT_DEMAND_RES;
    else if (pat->key == "charging_status_res") result.msg_type = DecodeResult::MSG_CHARGING_STATUS_RES;
    else if (pat->key == "metering_receipt_res") result.msg_type = DecodeResult::MSG_METERING_RECEIPT_RES;
    return true;
}

const MessagePattern* StaticExiDecoder::find_pattern(const uint8_t* exi,
                                                  size_t exi_len) const {
    if (exi_len < 2) return nullptr;

    // AppProtocol: first byte 0x80, second byte 0x40/0x44/0x48
    if (exi_len >= 2 && exi[0] == 0x80) {
        uint8_t second = exi[1];
        if (second == 0x40 || second == 0x44 || second == 0x48) {
            for (size_t i = 0; i < patterns_.size(); ++i) {
                if (patterns_[i].key == "supported_app_protocol_res")
                    return &patterns_[i];
            }
        }
    }

    // SessionSetupRes: starts with 0x80 0x98 (V2G_Message container).
    // Real SECC responses may use 8-bit EXI encoding where the body element is at byte[12].
    // Body element codes for SessionSetupRes: 0x11 (10-bit) or 0xE0 (8-bit).
    if (exi_len >= 2 && exi[0] == 0x80 && exi[1] == 0x98) {
        if (exi_len >= 13) {
            if (exi[11] == 0x11 || exi[12] == 0xE0) {
                for (size_t i = 0; i < patterns_.size(); ++i) {
                    if (patterns_[i].key == "session_setup_res")
                        return &patterns_[i];
                }
            }
        }
        // Also try JSON pattern if anchor matches
        for (size_t i = 0; i < patterns_.size(); ++i) {
            if (patterns_[i].key == "session_setup_res") {
                const MessagePattern& pat = patterns_[i];
                if (pat.anchor_offset >= 0 &&
                    pat.anchor_offset < static_cast<int>(exi_len) &&
                    pat.anchor_values.size() > 0 &&
                    exi[pat.anchor_offset] == pat.anchor_values[0]) {
                    return &patterns_[i];
                }
            }
        }
    }

    // Try each loaded pattern for other message types
    for (size_t pi = 0; pi < patterns_.size(); ++pi) {
        const MessagePattern& pat = patterns_[pi];
        if (pat.key == "supported_app_protocol_res") continue;
        if (pat.key == "session_setup_res") continue;
        if (exi_len < pat.min_payload_len) continue;
        if (pat.anchor_offset < 0 || pat.anchor_values.empty()) continue;
        if (pat.anchor_offset >= static_cast<int>(exi_len)) continue;
        uint8_t cand = exi[pat.anchor_offset];
        for (size_t ai = 0; ai < pat.anchor_values.size(); ++ai) {
            if (cand == pat.anchor_values[ai]) return &patterns_[pi];
        }
    }
    return nullptr;
}

bool StaticExiDecoder::slice_field(const uint8_t* base, size_t total_len,
                                  const FieldDescriptor& f,
                                  DecodeResult& result) const {
    int abs_off = f.offset_rel;
    if (abs_off < 0) abs_off = static_cast<int>(total_len) + abs_off;
    if (abs_off < 0 || abs_off + f.length > static_cast<int>(total_len)) return false;
    const uint8_t* fp = base + abs_off;
    if (f.encoding == "session_id") {
        result.session_id.assign(fp, fp + f.length);
    } else if (f.encoding == "v2g_response_code") {
        int rc = V2GResponseCodeMap::from_byte_with_custom(fp[0]);
        if (rc < 0) return false;
        result.v2g_response_code = rc;
    } else if (f.encoding == "app_hand_response_code") {
        result.app_response_code = AppHandResponseCodeMap::from_bytes(fp);
    } else if (f.encoding == "schema_id") {
        result.schema_id = AppHandResponseCodeMap::schema_id_from_bytes(fp);
    }
    return true;
}

bool StaticExiDecoder::parse_by_pattern(const uint8_t* exi, size_t len,
                                       const MessagePattern& pat,
                                       DecodeResult& result) const {
    for (size_t fi = 0; fi < pat.fields.size(); ++fi) {
        if (!slice_field(exi, len, pat.fields[fi], result)) return false;
    }
    return true;
}

bool StaticExiDecoder::parse_app_protocol_res(const uint8_t* exi, size_t len,
                                            DecodeResult& result) const {
    if (len < 4) { last_error_ = "AppProtocolRes: too short"; return false; }
    if (exi[0] != 0x80) { last_error_ = "AppProtocolRes: bad header"; return false; }
    result.app_response_code = AppHandResponseCodeMap::from_bytes(exi);
    result.schema_id = AppHandResponseCodeMap::schema_id_from_bytes(exi);
    std::cout << "[StaticExiDecoder] AppProtocolRes: RC=" << result.app_response_code
              << " SchemaID=" << result.schema_id << std::endl;
    return true;
}

bool StaticExiDecoder::parse_session_setup_res(const uint8_t* exi, size_t len,
                                             DecodeResult& result) const {
    // Real SECC SessionSetupRes layout (8-bit EXI):
    //   Bytes[0-1]:  0x8098 (V2G_Message container)
    //   Bytes[2-10]: SessionID header + partial value
    //   Byte[11]:     SessionID length/value continuation header (0x91)
    //   Byte[12]:     Body element header (0xE0 = SessionSetupRes)
    //   Byte[13]:     ResponseCode (0x20 = OK_NewSessionEstablished)
    //   Byte[14]:     EVSEID length header (0x51 = 10 << 3 | 1)
    //   Bytes[15-19]: EVSEID first 10 chars (value bits)
    //   Byte[20]:     EVSEID continuation length header (0x29 = 5 << 3 | 1)
    //   Bytes[21-25]: EVSEID last 5 chars (value bits)
    //
    // The parser reads ResponseCode from byte[13] and EVSEID from bytes[14-25].
    if (len < 14) { last_error_ = "SessionSetupRes: too short"; return false; }

    result.session_id.resize(8);
    for (int i = 0; i < 8; ++i) result.session_id[i] = exi[3 + i];

    // Try byte[13] first (8-bit EXI SECC: ResponseCode right after body element)
    uint8_t rc_byte = exi[13];
    result.v2g_response_code = V2GResponseCodeMap::from_byte_with_custom(rc_byte);
    // Fallback: try byte[12] (some implementations: body element IS the code)
    if (result.v2g_response_code < 0) {
        uint8_t rc_byte12 = exi[12];
        result.v2g_response_code = V2GResponseCodeMap::from_byte_with_custom(rc_byte12);
    }
    if (result.v2g_response_code < 0) {
        std::ostringstream oss;
        oss << "SessionSetupRes: unknown RC bytes {13:0x"
            << std::hex << (int)rc_byte << ",12:0x" << (int)exi[12] << "}";
        last_error_ = oss.str();
        return false;
    }

    // Extract EVSEID: 15 raw bytes from the EXI payload.
    // The SECC encodes EVSEID as 15 bytes starting at byte[14] (after RC).
    // We store them as a hex string for TTCN-3 template matching.
    if (len >= 15 + 14) {
        ::std::ostringstream evs;
        for (int i = 0; i < 15; ++i)
            evs << ::std::hex << ::std::setw(2) << ::std::setfill('0') << (int)exi[14 + i];
        result.evseid = evs.str();
    }

    std::cout << "[StaticExiDecoder] SessionSetupRes: RC=" << result.v2g_response_code
              << " SessionID=" << bytes_to_hex(result.session_id.data(), 8)
              << " EVSEID=" << result.evseid << std::endl;
    return true;
}

bool StaticExiDecoder::parse_generic_res(const uint8_t* exi, size_t len,
                                         const MessagePattern& pat,
                                         DecodeResult& result) const {
    if (len < 13) { last_error_ = pat.key + ": too short"; return false; }

    if (!pat.fields.empty()) {
        if (parse_by_pattern(exi, len, pat, result)) {
            std::cout << "[StaticExiDecoder] " << pat.key << ": JSON pattern OK" << std::endl;
            return true;
        }
    }

    // Universal V2G layout: ResponseCode at offset 12
    uint8_t rc_byte = exi[12];
    result.v2g_response_code = V2GResponseCodeMap::from_byte_with_custom(rc_byte);
    if (result.v2g_response_code < 0) {
        std::ostringstream oss;
        oss << pat.key << ": unknown RC 0x" << std::hex << (int)rc_byte;
        last_error_ = oss.str();
        return false;
    }
    std::cout << "[StaticExiDecoder] " << pat.key << ": RC=" << result.v2g_response_code
              << " (generic)" << std::endl;
    return true;
}

} // namespace SECC__ExiJni
