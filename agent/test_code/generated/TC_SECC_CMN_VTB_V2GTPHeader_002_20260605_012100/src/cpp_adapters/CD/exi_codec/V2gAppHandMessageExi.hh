//
// Concrete AppHandMessage that implements dumpMsg()
// to allow direct instantiation in the port adapter.
//
#ifndef V2GAPPHANDMESSAGEEXI_HH
#define V2GAPPHANDMESSAGEEXI_HH

#include "V2gAppHandMessage.h"

class V2gAppHandMessageExi : public V2gAppHandMessage
{
public:
    V2gAppHandMessageExi() : V2gAppHandMessage() {}
    V2gAppHandMessageExi(AppHandMessageType type) : V2gAppHandMessage(type) {}
    virtual ~V2gAppHandMessageExi() {}
    void dumpMsg() override {}
};

#endif // V2GAPPHANDMESSAGEEXI_HH
