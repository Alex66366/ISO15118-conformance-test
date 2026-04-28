#include "V2G_UDP_SDP_Port.hh"
#include <iostream>

namespace SECC__TestSystem {

V2G__UDP__SDP__Port::V2G__UDP__SDP__Port(const char *par_port_name)
    : V2G__UDP__SDP__Port_BASE(par_port_name) {
}

V2G__UDP__SDP__Port::~V2G__UDP__SDP__Port() {
}

void V2G__UDP__SDP__Port::set_parameter(const char *parameter_name, const char *parameter_value) {
}

void V2G__UDP__SDP__Port::Handle_Message(const unsigned char *message, size_t length) {
}

void V2G__UDP__SDP__Port::outgoing_message(const unsigned char *message, size_t length) {
    std::cout << "[UDP_SDP_PORT] Sending " << length << " bytes" << std::endl;
}

void V2G__UDP__SDP__Port::outgoing_send(const OCTETSTRING& send_par) {
    std::cout << "[UDP_SDP_PORT] outgoing_send() called with " << send_par.lengthof() << " bytes" << std::endl;
}

void V2G__UDP__SDP__Port::send(const OCTETSTRING& send_data, const char* send_fmt, ...) {
    std::cout << "[UDP_SDP_PORT] send() called with " << send_data.lengthof() << " bytes" << std::endl;
}

}
