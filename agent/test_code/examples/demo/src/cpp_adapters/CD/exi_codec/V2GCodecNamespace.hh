// V2GCodecNamespace.hh — OpenV2G namespace aliases for V2gExiMessage.h
//
// V2gExiMessage.h uses 'using namespace DataStructure_V2GTP' which requires
// the DataStructure_V2GTP namespace and its child namespaces (MsgDef, MsgDataTypes, etc.)
// to be defined.
//
// Problem: TsDataType.h normally defines these inside its #else branch (TITAN-absent).
// When TITAN IS present (VERDICTTYPE_HH defined), the #else branch is skipped and
// the namespaces are never defined, causing "namespace not found" errors.
//
// Solution: This header defines the namespace aliases in a separate, unconditionally-
// included header that always works regardless of TITAN presence.
//
// Include order:
//   V2GCodecNamespace.hh  (this file — defines namespace aliases)
//   TsDataType.h           (provides structs, guards vs TITAN)
//   V2gExiMessage.h         (uses the namespace aliases)
//
// Both V2G_TCP_TLS_Port.cc (TITAN-present) and ExiCodecAdapter.cc (OpenV2G-only)
// should include V2GCodecNamespace.hh before V2gExiMessage.h.

#ifndef V2G_CODEC_NAMESPACE_HH
#define V2G_CODEC_NAMESPACE_HH

// Define DataStructure_V2GTP namespace and its using-aliases.
// This is safe to include multiple times (namespaces are idempotent).

// Forward-declare all the underlying namespaces. These may or may not exist
// depending on whether TsDataType.h's #else branch was entered.
// We use a namespace alias trick: if the namespace doesn't exist yet, we create
// an empty one that will later be populated by TsDataType.h's #else block.

namespace urn_iso_15118_2_2013_MsgDef {}
namespace urn_iso_15118_2_2013_MsgDataTypes {}
namespace urn_iso_15118_2_2010_AppProtocol {}
namespace DataStructure_SDP {}

// Primary namespace that V2gExiMessage.h imports
namespace DataStructure_V2GTP {
    using namespace urn_iso_15118_2_2013_MsgDef;
    using namespace urn_iso_15118_2_2013_MsgDataTypes;
    using namespace urn_iso_15118_2_2010_AppProtocol;
    using namespace DataStructure_SDP;
}

#endif // V2G_CODEC_NAMESPACE_HH
