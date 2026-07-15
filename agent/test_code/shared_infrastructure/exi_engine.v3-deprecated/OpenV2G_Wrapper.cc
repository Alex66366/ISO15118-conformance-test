/*
 * OpenV2G_Wrapper.cc
 *
 * ISO 15118-2 EXI Dynamic Encoder Bridge Layer — Implementation
 *
 * This .cc file is the ONLY place in the project that includes OpenV2G
 * headers.  It is physically separated from all TITAN-generated code.
 *
 * Include order (enforced by guard):
 *   1. OpenV2G headers  (all inside this file)
 *   2. Standard C/C++   (exit, memcpy, memset, etc.)
 *
 * OpenV2G headers pulled in:
 *   - iso1EXIDatatypes.h       : iso1EXIDocument, iso1AnonType_V2G_Message,
 *                                 iso1MessageHeaderType, iso1ServiceDiscoveryResType,
 *                                 enum iso1responseCodeType, etc.
 *   - iso1EXIDatatypesEncoder.h: encode_iso1ExiDocument()
 *   - EXIHeaderEncoder.h       : writeEXIHeader()
 *   - EXITypes.h               : bitstream_t, exi_bytes_t, exi_string_t
 *   - v2gtp.h                 : write_v2gtpHeader()
 *
 * Key compatibility facts about OpenV2G (verified against source):
 *   - STRING_REPRESENTATION = STRING_REPRESENTATION_ASCII  → char is 1 byte
 *   - SAVE_MEMORY_WITH_UNNAMED_UNION = UNION_YES         → struct fields are
 *                                                           named directly (not via union)
 *   - EXI_STREAM = BYTE_ARRAY                         → stream.pos is size_t*
 *   - SessionID in iso1MessageHeaderType is:
 *       struct { uint8_t bytes[8]; uint16_t bytesLen; } SessionID;
 *     (NOT exi_bytes_t, so fill with memcpy directly)
 *   - EVSEID in iso1SessionSetupResType is:
 *       struct { exi_string_character_t chars[N]; uint16_t charsLen; } EVSEID;
 *     (NOT exi_string_t, so fill with memcpy directly)
 *
 * All functions return std::vector<uint8_t> with the complete V2GTP+EXI frame.
 *
 * Created for the ISO 15118-2/4 TTCN-3 Test Generation Pipeline.
 * Author: Infrastructure Agent — Phase 2 Architecture
 */

#include "OpenV2G_Wrapper.hh"

#include <algorithm>
#include <cstring>
#include <cstdlib>
#include <cstdio>

/* ========================================================================
 * OpenV2G headers — ONLY in this .cc file
 * ======================================================================== */
extern "C" {

#include "iso1EXIDatatypes.h"
#include "iso1EXIDatatypesEncoder.h"
#include "iso1EXIDatatypesDecoder.h"
#include "EXIHeaderEncoder.h"
#include "EXIHeaderDecoder.h"
#include "EXITypes.h"
#include "v2gtp.h"

}  // extern "C"

/* ========================================================================
 * Compile-time constants
 *
 * CRITICAL: Do NOT name these identically to OpenV2G macros (v2gtp.h).
 * OpenV2G uses:  #define V2GTP_VERSION (unsigned char)0x01
 * Our anonymous namespace vars use different names to avoid macro clash.
 * ======================================================================== */
namespace {

    constexpr size_t V2GTP_HDR_LEN_RAW    = 8;
    constexpr uint8_t  V2GTP_VER_BYTE        = 0x01;
    constexpr uint8_t  V2GTP_INV_VER_BYTE   = 0xFE;
    constexpr uint16_t V2GTP_EXI_TYPE_CODE   = 0x8001;

    /* EXI output buffer — generous for all ISO 15118-2 messages */
    constexpr size_t EXI_BUFFER_SIZE = 8192;

    /* SessionID field: exactly 8 bytes per ISO 15118-2 schema */
    constexpr size_t SESSION_ID_FIELD_BYTES = 8;
}

/* ========================================================================
 * hex_to_bytes — convert "0011223344556677" -> {0x00,0x11,...,0x66,0x77}
 * ======================================================================== */
namespace OpenV2G {

std::vector<uint8_t> hex_to_bytes(const std::string& hex) {
    std::vector<uint8_t> out;
    out.reserve(hex.size() / 2);
    for (size_t i = 0; i + 1 < hex.size(); i += 2) {
        unsigned int byte = 0;
        if (std::sscanf(hex.c_str() + i, "%02x", &byte) == 1) {
            out.push_back(static_cast<uint8_t>(byte));
        }
    }
    return out;
}

}  // namespace OpenV2G

/* ========================================================================
 * Internal helpers (anonymous namespace — invisible outside this translation unit)
 * ======================================================================== */
namespace {

/* ------------------------------------------------------------------------
 * fill_exi_string_field — fill a UTF-8 string field in an EXI struct.
 *
 * OpenV2G string fields are unnamed structs:
 *   struct { exi_string_character_t chars[N]; uint16_t charsLen; } FieldName;
 * NOT exi_string_t, so we fill them directly.
 *
 * Input: pointer to the unnamed struct field, source UTF-8 string.
 * ------------------------------------------------------------------------ */
static void fill_exi_string_field(
    void* field_ptr,
    const std::string& src)
{
    if (!field_ptr) return;
    using char_t = exi_string_character_t;
    char_t* chars = reinterpret_cast<char_t*>(field_ptr);
    std::memset(chars, 0, sizeof(char_t) * 64);  /* generous clear */

    if (src.empty()) {
        /* Leave all zeros (empty string) */
        return;
    }

    /* ascii mode: char_t == char, 1 byte per character */
    for (size_t i = 0; i < src.size() && i < 63; ++i) {
        chars[i] = static_cast<char_t>(static_cast<unsigned char>(src[i]));
    }
    /* Write charsLen at offset sizeof(char_t)*64 from field_ptr base */
    uint16_t* len_ptr = reinterpret_cast<uint16_t*>(
        static_cast<char*>(field_ptr) + sizeof(char_t) * 64);
    *len_ptr = static_cast<uint16_t>(std::min(src.size(), static_cast<size_t>(63)));
}

/* ------------------------------------------------------------------------
 * fill_session_id_field — fill the SessionID field of iso1MessageHeaderType.
 *
 * iso1MessageHeaderType has:
 *   struct { uint8_t bytes[8]; uint16_t bytesLen; } SessionID;
 * NOT exi_bytes_t — so we fill directly with memcpy.
 *
 * ISO 15118-2 §8.3.4 — new session establishment:
 *   The SessionID of the first EVCC→SECC message (SessionSetupReq) MUST be
 *   8 bytes of 0x00.  This signals to the SECC that a new session is being
 *   initiated and the SECC will assign a proper SessionID in SessionSetupRes.
 *
 *   The field MUST have bytesLen = 8 with all 8 bytes set to 0x00.
 *   Setting bytesLen = 0 (absence encoding) causes the SECC's exi-codec to
 *   mis-align subsequent elements, resulting in "Premature EOS" errors because
 *   the EXI grammar consumes zero bytes where it expected 8.
 *
 * For subsequent messages (after the SECC has assigned a SessionID), pass the
 * full 16-hex-char session_id (e.g. "A1B2C3D4E5F60718") — bytesLen will be 8
 * and the actual session bytes will be used.
 * ------------------------------------------------------------------------ */
static void fill_session_id_field(
    void* sid_ptr,
    const std::string& hex_session_id)
{
    if (!sid_ptr) return;
    uint8_t* bytes = reinterpret_cast<uint8_t*>(sid_ptr);
    uint16_t* len_ptr = reinterpret_cast<uint16_t*>(
        static_cast<uint8_t*>(sid_ptr) + SESSION_ID_FIELD_BYTES);

    /* Always zero-fill first — this also provides the correct default for
     * a new-session SessionID (8 bytes of 0x00). */
    std::memset(bytes, 0, SESSION_ID_FIELD_BYTES);

    /* Detect "new session" sentinel: empty string or canonical all-zeros.
     * ISO 15118-2 §8.3.4 mandates exactly 8 bytes of 0x00 (bytesLen = 8). */
    bool is_new_session = (hex_session_id.empty() ||
                            hex_session_id == "0000000000000000");
    if (is_new_session) {
        *len_ptr = SESSION_ID_FIELD_BYTES;  /* 8 — NOT 0 */
        return;
    }

    std::vector<uint8_t> raw = OpenV2G::hex_to_bytes(hex_session_id);
    const size_t n = std::min(raw.size(), SESSION_ID_FIELD_BYTES);
    if (n > 0) {
        std::memcpy(bytes, raw.data(), n);
    }
    *len_ptr = static_cast<uint16_t>(n);
}

/* ------------------------------------------------------------------------
 * build_message_header — populate iso1MessageHeaderType from a hex session ID.
 * Notification and Signature are left absent (their isUsed flags stay 0).
 * ------------------------------------------------------------------------ */
static void build_message_header(
    struct iso1MessageHeaderType* header,
    const std::string& session_id_hex)
{
    std::memset(header, 0, sizeof(struct iso1MessageHeaderType));
    /* SessionID field is at offset 0 of iso1MessageHeaderType */
    fill_session_id_field(header, session_id_hex);
    /* Notification_isUsed and Signature_isUsed remain 0 (absent) */
}

/* ------------------------------------------------------------------------
 * build_payment_option_list — populate PaymentOptionList from a vector.
 * ISO 15118-2: 1–2 options.
 * ------------------------------------------------------------------------ */
static void build_payment_option_list(
    struct iso1PaymentOptionListType* pol,
    const std::vector<OpenV2G::PaymentOption>& options)
{
    std::memset(pol, 0, sizeof(struct iso1PaymentOptionListType));
    const size_t n = std::min(options.size(), static_cast<size_t>(2));
    for (size_t i = 0; i < n; ++i) {
        pol->PaymentOption.array[i] = static_cast<iso1paymentOptionType>(
            static_cast<uint8_t>(options[i]));
    }
    pol->PaymentOption.arrayLen = static_cast<uint16_t>(n);
}

/* ------------------------------------------------------------------------
 * build_service_entry — populate one iso1ServiceType entry.
 * ServiceName and ServiceScope are optional.
 * ------------------------------------------------------------------------ */
static void build_service_entry(
    struct iso1ServiceType* entry,
    const OpenV2G::ServiceEntry& src)
{
    std::memset(entry, 0, sizeof(struct iso1ServiceType));

    entry->ServiceID = src.service_id;

    if (src.service_name_present && !src.service_name.empty()) {
        fill_exi_string_field(&entry->ServiceName, src.service_name);
        entry->ServiceName_isUsed = 1;
    }

    entry->ServiceCategory = static_cast<iso1serviceCategoryType>(
        static_cast<uint8_t>(src.category));

    if (src.service_scope_present && !src.service_scope.empty()) {
        fill_exi_string_field(&entry->ServiceScope, src.service_scope);
        entry->ServiceScope_isUsed = 1;
    }

    entry->FreeService = src.free_service ? 1 : 0;
}

/* ------------------------------------------------------------------------
 * build_charge_service — populate iso1ChargeServiceType.
 * This is the mandatory ChargeService element in ServiceDiscoveryRes.
 * ------------------------------------------------------------------------ */
static void build_charge_service(
    struct iso1ChargeServiceType* cs,
    const OpenV2G::ServiceDiscoveryRes_Params& src)
{
    std::memset(cs, 0, sizeof(struct iso1ChargeServiceType));

    cs->ServiceID = src.charge_service_id;

    if (src.charge_service_name_present && !src.charge_service_name.empty()) {
        fill_exi_string_field(&cs->ServiceName, src.charge_service_name);
        cs->ServiceName_isUsed = 1;
    }

    cs->ServiceCategory = static_cast<iso1serviceCategoryType>(
        static_cast<uint8_t>(src.charge_service_category));

    if (src.charge_service_scope_present && !src.charge_service_scope.empty()) {
        fill_exi_string_field(&cs->ServiceScope, src.charge_service_scope);
        cs->ServiceScope_isUsed = 1;
    }

    cs->FreeService = src.charge_service_free ? 1 : 0;

    {
        const size_t n = std::min(src.supported_energy_modes.size(),
                                   static_cast<size_t>(6));
        const size_t safe_n = std::max(size_t(1), n);
        for (size_t i = 0; i < n; ++i) {
            cs->SupportedEnergyTransferMode.EnergyTransferMode.array[i] =
                static_cast<iso1EnergyTransferModeType>(
                    static_cast<uint8_t>(src.supported_energy_modes[i]));
        }
        cs->SupportedEnergyTransferMode.EnergyTransferMode.arrayLen =
            static_cast<uint16_t>(safe_n);
    }
}

/* ------------------------------------------------------------------------
 * build_service_list — populate iso1ServiceListType (optional field).
 * ------------------------------------------------------------------------ */
static void build_service_list(
    struct iso1ServiceListType* sl,
    const std::vector<OpenV2G::ServiceEntry>& entries)
{
    std::memset(sl, 0, sizeof(struct iso1ServiceListType));
    const size_t n = std::min(entries.size(), static_cast<size_t>(8));
    for (size_t i = 0; i < n; ++i) {
        build_service_entry(&sl->Service.array[i], entries[i]);
    }
    sl->Service.arrayLen = static_cast<uint16_t>(n);
}

/* ------------------------------------------------------------------------
 * build_service_discovery_res_body — fill iso1ServiceDiscoveryResType.
 * ------------------------------------------------------------------------ */
static void build_service_discovery_res_body(
    struct iso1ServiceDiscoveryResType* body,
    const OpenV2G::ServiceDiscoveryRes_Params& src)
{
    std::memset(body, 0, sizeof(struct iso1ServiceDiscoveryResType));

    body->ResponseCode = static_cast<iso1responseCodeType>(
        static_cast<uint8_t>(src.response_code));

    build_payment_option_list(&body->PaymentOptionList, src.payment_options);
    build_charge_service(&body->ChargeService, src);

    if (!src.service_list.empty()) {
        build_service_list(&body->ServiceList, src.service_list);
        body->ServiceList_isUsed = 1;
    }
}

/* ------------------------------------------------------------------------
 * encode_exi_document_v2 — run OpenV2G encoder, return raw EXI bytes.
 *
 * Uses OpenV2G's BYTE_ARRAY mode where stream.pos is a size_t*.
 * OpenV2G increments *stream.pos as it writes each byte.
 * We set stream.pos to our own counter so we can read back the final length.
 * ------------------------------------------------------------------------ */
static bool encode_exi_document_v2(
    struct iso1EXIDocument* doc,
    uint8_t*               out_buf,
    size_t*                out_len,
    const char*            msg_name)
{
    size_t pos = 0;

    bitstream_t stream;
    std::memset(&stream, 0, sizeof(bitstream_t));
    stream.size     = EXI_BUFFER_SIZE;
    stream.data    = out_buf;
    stream.pos     = &pos;       /* OpenV2G increments *pos as it writes */
    stream.buffer  = 0;
    stream.capacity = 8;          /* start with fresh byte */

    if (writeEXIHeader(&stream) != 0) {
        std::fprintf(stderr,
            "[OpenV2G_Wrapper] %s: writeEXIHeader failed\n", msg_name);
        return false;
    }

    if (encode_iso1ExiDocument(&stream, doc) != 0) {
        std::fprintf(stderr,
            "[OpenV2G_Wrapper] %s: encode_iso1ExiDocument failed\n", msg_name);
        return false;
    }

    *out_len = pos;

    /* Wire diagnostics: dump raw EXI bytes as hex so we can compare against SECC logs */
    std::fprintf(stderr,
        "[OpenV2G_Wrapper] %s EXI wire (%zu bytes): ", msg_name, pos);
    for (size_t i = 0; i < pos && i < 128; ++i) {
        std::fprintf(stderr, "%02x ", out_buf[i]);
    }
    if (pos > 128) std::fprintf(stderr, "...(%zu more)", pos - 128);
    std::fprintf(stderr, "\n");

    return true;
}

}  // anonymous namespace

/* ========================================================================
 * Public API — encode_service_discovery_res
 *
 * Wire format returned:
 *   Bytes [0..7]   : V2GTP header
 *   Bytes [8..N-1]: EXI-encoded message
 *
 * iso1EXIDocument union layout (SAVE_MEMORY_WITH_UNNAMED_UNION = UNION_YES):
 *   doc.V2G_Message_isUsed = 1
 *   doc.V2G_Message.Header.SESSION_ID filled
 *   doc.V2G_Message.Body.ServiceDiscoveryRes_isUsed = 1
 *   doc.V2G_Message.Body.ServiceDiscoveryRes filled
 * ======================================================================== */
namespace OpenV2G {

std::vector<uint8_t>
encode_service_discovery_res(
    const std::string&                   session_id,
    const ServiceDiscoveryRes_Params&     params)
{
    /* ---- Step 1: Build iso1EXIDocument in memory ---- */
    struct iso1EXIDocument doc;
    std::memset(&doc, 0, sizeof(doc));
    init_iso1EXIDocument(&doc);

    /* Activate V2G_Message variant in the union */
    doc.V2G_Message_isUsed = 1;

    /* Fill Header */
    build_message_header(&doc.V2G_Message.Header, session_id);

    /* Fill Body — ServiceDiscoveryRes variant */
    doc.V2G_Message.Body.ServiceDiscoveryRes_isUsed = 1;
    std::memset(&doc.V2G_Message.Body.ServiceDiscoveryRes, 0,
                 sizeof(doc.V2G_Message.Body.ServiceDiscoveryRes));
    build_service_discovery_res_body(
        &doc.V2G_Message.Body.ServiceDiscoveryRes, params);

    /* ---- Step 2: EXI encode ---- */
    uint8_t exi_buf[EXI_BUFFER_SIZE];
    size_t  exi_len = 0;

    if (!encode_exi_document_v2(&doc, exi_buf, &exi_len, "ServiceDiscoveryRes")) {
        std::fprintf(stderr,
            "[OpenV2G_Wrapper] encode_service_discovery_res: EXI encode failed\n");
        return {};
    }

    if (exi_len == 0) {
        std::fprintf(stderr,
            "[OpenV2G_Wrapper] encode_service_discovery_res: zero EXI length\n");
        return {};
    }

    /* ---- Step 3: Allocate wire buffer and prepend V2GTP header ---- */
    std::vector<uint8_t> wire(V2GTP_HDR_LEN_RAW + exi_len);

    /* Copy EXI bytes after the 8-byte header slot */
    std::memcpy(wire.data() + V2GTP_HDR_LEN_RAW, exi_buf, exi_len);

    /* Write V2GTP header */
    write_v2gtpHeader(wire.data(),
                       static_cast<uint32_t>(exi_len),
                       V2GTP_EXI_TYPE_CODE);

    /* ---- Step 4: Diagnostic log ---- */
    std::fprintf(stderr,
        "[OpenV2G_Wrapper] Encoded ServiceDiscoveryRes: "
        "session_id=%s response_code=%u "
        "payment_options=%zu charge_service_id=%u "
        "service_list_size=%zu  ->  wire=%zu bytes\n",
        session_id.c_str(),
        static_cast<unsigned>(params.response_code),
        params.payment_options.size(),
        params.charge_service_id,
        params.service_list.size(),
        wire.size());

    return wire;
}

/* ========================================================================
 * encode_session_setup_res — fill SessionSetupRes body.
 *
 * iso1SessionSetupResType:
 *   ResponseCode : iso1responseCodeType
 *   EVSEID      : { exi_string_character_t chars[38]; uint16_t charsLen; }
 *   EVSETimeStamp: int64_t (optional)
 * ======================================================================== */
std::vector<uint8_t>
encode_session_setup_res(
    const std::string&  session_id,
    ResponseCode        rc,
    const std::string&  evse_id)
{
    struct iso1EXIDocument doc;
    std::memset(&doc, 0, sizeof(doc));
    init_iso1EXIDocument(&doc);

    doc.V2G_Message_isUsed = 1;
    build_message_header(&doc.V2G_Message.Header, session_id);

    doc.V2G_Message.Body.SessionSetupRes_isUsed = 1;
    std::memset(&doc.V2G_Message.Body.SessionSetupRes, 0,
                 sizeof(doc.V2G_Message.Body.SessionSetupRes));
    doc.V2G_Message.Body.SessionSetupRes.ResponseCode =
        static_cast<iso1responseCodeType>(static_cast<uint8_t>(rc));

    /* EVSEID: fill the unnamed string struct directly */
    fill_exi_string_field(&doc.V2G_Message.Body.SessionSetupRes.EVSEID,
                           evse_id);

    uint8_t exi_buf[EXI_BUFFER_SIZE];
    size_t  exi_len = 0;
    if (!encode_exi_document_v2(&doc, exi_buf, &exi_len, "SessionSetupRes"))
        return {};

    std::vector<uint8_t> wire(V2GTP_HDR_LEN_RAW + exi_len);
    std::memcpy(wire.data() + V2GTP_HDR_LEN_RAW, exi_buf, exi_len);
    write_v2gtpHeader(wire.data(),
                       static_cast<uint32_t>(exi_len),
                       V2GTP_EXI_TYPE_CODE);
    return wire;
}

/* ========================================================================
 * encode_session_setup_req — fill SessionSetupReq body.
 *
 * iso1SessionSetupReqType (per OpenV2G iso1EXIDatatypes.h):
 *   EVCCID : unnamed struct { uint8_t bytes[6]; uint16_t bytesLen; }
 *            MANDATORY — schema facet maxLength=6 (urn:iso:15118:2:2013:MsgDataTypes,evccIDType)
 *
 * Real SECC behaviour observed: an all-zero EVCCID (or empty bytes) causes
 * the SECC parser to raise "Premature EOS" / EOFException — the EXI body
 * is rejected because EVCCID is a mandatory element of the SessionSetupReq.
 *
 * To prevent silent truncation:
 *   1. Reject params.evcc_id_bytes.size() != 6 BEFORE touching OpenV2G.
 *   2. Use the EXACTLY 6-byte fill (not a zero-padded shorter source) so the
 *      SECC always sees a valid 6-byte EVCCID.
 *   3. Log an ERROR on encode_iso1ExiDocument failure with the byte length
 *      actually produced (or attempted) — never send a partial frame.
 * ======================================================================== */
std::vector<uint8_t>
encode_session_setup_req(
    const std::string&              session_id,
    const SessionSetupReq_Params&   params)
{
    /* ---- Step 0: Reject if EVCCID is not exactly 6 bytes ---- */
    constexpr size_t EVCCID_REQUIRED_BYTES = 6;
    if (params.evcc_id_bytes.size() != EVCCID_REQUIRED_BYTES) {
        std::fprintf(stderr,
            "[OpenV2G_Wrapper] ERROR encode_session_setup_req: "
            "EVCCID must be exactly %zu bytes, got %zu — refusing to encode "
            "(would produce truncated EXI / SECC Premature EOS)\n",
            EVCCID_REQUIRED_BYTES, params.evcc_id_bytes.size());
        return {};
    }

    /* ---- Step 1: Build iso1EXIDocument in memory ---- */
    struct iso1EXIDocument doc;
    std::memset(&doc, 0, sizeof(doc));
    init_iso1EXIDocument(&doc);

    /* Activate V2G_Message variant in the union */
    doc.V2G_Message_isUsed = 1;

    /* Fill Header */
    build_message_header(&doc.V2G_Message.Header, session_id);

    /* Fill Body — SessionSetupReq variant (MANDATORY EVCCID) */
    doc.V2G_Message.Body.SessionSetupReq_isUsed = 1;
    std::memset(&doc.V2G_Message.Body.SessionSetupReq, 0,
                 sizeof(doc.V2G_Message.Body.SessionSetupReq));
    {
        /* EVCCID field has shape { uint8_t bytes[6]; uint16_t bytesLen; } */
        uint8_t* evcc_bytes = reinterpret_cast<uint8_t*>(
            &doc.V2G_Message.Body.SessionSetupReq.EVCCID);
        uint16_t* evcc_len = reinterpret_cast<uint16_t*>(
            evcc_bytes + EVCCID_REQUIRED_BYTES);

        std::memset(evcc_bytes, 0, EVCCID_REQUIRED_BYTES);
        std::memcpy(evcc_bytes, params.evcc_id_bytes.data(), EVCCID_REQUIRED_BYTES);
        *evcc_len = static_cast<uint16_t>(EVCCID_REQUIRED_BYTES);
    }

    /* ---- Step 2: EXI encode ---- */
    uint8_t exi_buf[EXI_BUFFER_SIZE];
    size_t  exi_len = 0;

    if (!encode_exi_document_v2(&doc, exi_buf, &exi_len, "SessionSetupReq")) {
        std::fprintf(stderr,
            "[OpenV2G_Wrapper] ERROR encode_session_setup_req: "
            "OpenV2G encode_iso1ExiDocument failed "
            "(session_id=%s, evcc_id_bytes=%zu, attempted_exi_len>=0) — "
            "refusing to send truncated bitstream\n",
            session_id.c_str(), params.evcc_id_bytes.size());
        return {};
    }

    if (exi_len == 0) {
        std::fprintf(stderr,
            "[OpenV2G_Wrapper] ERROR encode_session_setup_req: "
            "OpenV2G produced ZERO-length EXI body — refusing to send\n");
        return {};
    }

    /* ---- Step 3: Allocate wire buffer and prepend V2GTP header ---- */
    std::vector<uint8_t> wire(V2GTP_HDR_LEN_RAW + exi_len);
    std::memcpy(wire.data() + V2GTP_HDR_LEN_RAW, exi_buf, exi_len);
    write_v2gtpHeader(wire.data(),
                       static_cast<uint32_t>(exi_len),
                       V2GTP_EXI_TYPE_CODE);

    /* ---- Step 4: Diagnostic log ---- */
    std::fprintf(stderr,
        "[OpenV2G_Wrapper] Encoded SessionSetupReq: "
        "session_id=%s evcc_id=[%02x %02x %02x %02x %02x %02x] -> wire=%zu bytes\n",
        session_id.c_str(),
        params.evcc_id_bytes[0], params.evcc_id_bytes[1],
        params.evcc_id_bytes[2], params.evcc_id_bytes[3],
        params.evcc_id_bytes[4], params.evcc_id_bytes[5],
        wire.size());

    return wire;
}

/* ========================================================================
 * encode_service_discovery_req — fill ServiceDiscoveryReq body.
 *
 * iso1ServiceDiscoveryReqType:
 *   ServiceScope  : { exi_string_t chars; }  (optional)
 *   ServiceCategory: iso1serviceCategoryType  (optional)
 * ======================================================================== */
std::vector<uint8_t>
encode_service_discovery_req(
    const std::string&  session_id,
    const std::string&  service_scope,
    bool                service_scope_present,
    ServiceCategory     category,
    bool                category_present)
{
    struct iso1EXIDocument doc;
    std::memset(&doc, 0, sizeof(doc));
    init_iso1EXIDocument(&doc);

    doc.V2G_Message_isUsed = 1;
    build_message_header(&doc.V2G_Message.Header, session_id);

    doc.V2G_Message.Body.ServiceDiscoveryReq_isUsed = 1;
    std::memset(&doc.V2G_Message.Body.ServiceDiscoveryReq, 0,
                 sizeof(doc.V2G_Message.Body.ServiceDiscoveryReq));

    if (service_scope_present && !service_scope.empty()) {
        fill_exi_string_field(
            &doc.V2G_Message.Body.ServiceDiscoveryReq.ServiceScope,
            service_scope);
        doc.V2G_Message.Body.ServiceDiscoveryReq.ServiceScope_isUsed = 1;
    }

    if (category_present) {
        doc.V2G_Message.Body.ServiceDiscoveryReq.ServiceCategory_isUsed = 1;
        doc.V2G_Message.Body.ServiceDiscoveryReq.ServiceCategory =
            static_cast<iso1serviceCategoryType>(
                static_cast<uint8_t>(category));
    }

    uint8_t exi_buf[EXI_BUFFER_SIZE];
    size_t  exi_len = 0;
    if (!encode_exi_document_v2(&doc, exi_buf, &exi_len, "ServiceDiscoveryReq"))
        return {};

    std::vector<uint8_t> wire(V2GTP_HDR_LEN_RAW + exi_len);
    std::memcpy(wire.data() + V2GTP_HDR_LEN_RAW, exi_buf, exi_len);
    write_v2gtpHeader(wire.data(),
                       static_cast<uint32_t>(exi_len),
                       V2GTP_EXI_TYPE_CODE);
    return wire;
}

}  // namespace OpenV2G

/* ========================================================================
 * decode_session_setup_res — decode EXI bit-stream using OpenV2G decoder
 *
 * Canonical, spec-compliant SessionID extraction. Replaces broken heuristic
 * that read raw EXI bytes as a byte array (exi[3..10]), which fails for EXI
 * bit-packed encoding where SessionID is distributed across bit positions.
 *
 * Wire format: V2GTP header (8 bytes) + EXI payload.
 * ======================================================================== */
namespace OpenV2G {

SessionSetupRes_DecodeResult
decode_session_setup_res(const std::vector<uint8_t>& exi_payload) {
    SessionSetupRes_DecodeResult result;

    /* exi_payload = pure EXI bytes (V2GTP header must be stripped by caller) */
    if (exi_payload.size() < 8) {
        result.error_message = "EXI payload too short (< 8 bytes)";
        return result;
    }

    size_t read_pos = 0;
    bitstream_t stream;
    std::memset(&stream, 0, sizeof(bitstream_t));
    stream.size     = exi_payload.size();
    stream.data    = const_cast<uint8_t*>(exi_payload.data());
    stream.pos     = &read_pos;
    /* buffer=0, capacity=0 — forces readBuffer() on first readBit/readByte call,
     * so the decoder starts at the true first bit of the EXI payload */
    stream.buffer  = 0;
    stream.capacity = 0;

    if (readEXIHeader(&stream) != 0) {
        result.error_message = "readEXIHeader failed";
        return result;
    }

    struct iso1EXIDocument doc;
    std::memset(&doc, 0, sizeof(doc));

    if (decode_iso1ExiDocument(&stream, &doc) != 0) {
        result.error_message = "decode_iso1ExiDocument failed";
        return result;
    }

    if (!doc.V2G_Message_isUsed) {
        result.error_message = "V2G_Message not present";
        return result;
    }
    if (!doc.V2G_Message.Body.SessionSetupRes_isUsed) {
        result.error_message = "SessionSetupRes not present";
        return result;
    }

    /* Extract SessionID: { uint8_t bytes[8]; uint16_t bytesLen; } */
    const uint8_t* sid_bytes = doc.V2G_Message.Header.SessionID.bytes;
    uint16_t       sid_len   = doc.V2G_Message.Header.SessionID.bytesLen;

    {
        char hex_buf[17];
        const size_t n = std::min<size_t>(sid_len, 8);
        for (size_t i = 0; i < n; ++i) {
            std::snprintf(&hex_buf[i * 2], 3, "%02X",
                          static_cast<unsigned int>(sid_bytes[i]));
        }
        for (size_t i = n; i < 8; ++i) {
            std::snprintf(&hex_buf[i * 2], 3, "%02X", 0);
        }
        hex_buf[16] = '\0';
        result.session_id = hex_buf;
    }

    /* Extract ResponseCode */
    result.response_code = static_cast<ResponseCode>(
        static_cast<uint8_t>(doc.V2G_Message.Body.SessionSetupRes.ResponseCode));

    /* Extract EVSEID: { exi_string_character_t chars[N]; uint16_t charactersLen; } */
    {
        const exi_string_character_t* chars =
            doc.V2G_Message.Body.SessionSetupRes.EVSEID.characters;
        uint16_t chars_len = doc.V2G_Message.Body.SessionSetupRes.EVSEID.charactersLen;
        result.evse_id.clear();
        result.evse_id.reserve(chars_len);
        for (uint16_t i = 0; i < chars_len; ++i) {
            result.evse_id.push_back(static_cast<char>(chars[i]));
        }
    }

    std::fprintf(stderr,
        "[OpenV2G_Wrapper] decode_session_setup_res: "
        "SessionID=%s ResponseCode=%u EVSEID=%s\n",
        result.session_id.c_str(),
        static_cast<unsigned>(result.response_code),
        result.evse_id.c_str());

    result.success = true;
    return result;
}

}  // namespace OpenV2G
