#ifndef V2G_UDP_SDP_PORT_HH
#define V2G_UDP_SDP_PORT_HH

#include "SECC_TestSystem.hh"

namespace SECC__TestSystem {
class V2G__UDP__SDP__Port : public V2G__UDP__SDP__Port_BASE {
public:
    V2G__UDP__SDP__Port(const char *par_port_name = NULL);
    virtual ~V2G__UDP__SDP__Port();
    void set_parameter(const char *parameter_name, const char *parameter_value);
    void Handle_Message(const unsigned char *message, size_t length);
    void outgoing_message(const unsigned char *message, size_t length);
    void outgoing_send(const OCTETSTRING& send_par) override;
    void send(const OCTETSTRING& send_data, const char* send_fmt, ...);
};
}

#endif
