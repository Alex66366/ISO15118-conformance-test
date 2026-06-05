// Minimal concrete V2gExiMessage subclass for use in ExiCodecAdapter.
// This provides a concrete type to replace the abstract V2gExiMessage
// without pulling in TestSystemLogging.h -> cppcommon (C++17-only) dependencies.
//
// V2gExiMessage.h provides:
//   - V2gExiMessageType enum
//   - iso1EXIDocument wrapper
//   - serialize() / deserialize() methods
// We inherit publicly and provide a no-op dumpMsg().

#ifndef CONCRETE_V2GEXIMESSAGE_HH
#define CONCRETE_V2GEXIMESSAGE_HH

#include "../exi_codec/V2gExiMessage.h"

// Minimal concrete subclass: provides dumpMsg() stub
class ConcreteV2gExiMessage : public V2gExiMessage
{
public:
    explicit ConcreteV2gExiMessage(V2gExiMessageType type) : V2gExiMessage(type) { }

    void dumpMsg(void) override { /* no-op logging for codec adapter use */ }
};

#endif // CONCRETE_V2GEXIMESSAGE_HH
