#include "V2G_TCP_TLS_Port.hh"
#include <iostream>

namespace SECC__TestSystem {

V2G__TCP__TLS__Port::V2G__TCP__TLS__Port(const char *par_port_name)
    : V2G__TCP__TLS__Port_BASE(par_port_name) {
}

V2G__TCP__TLS__Port::~V2G__TCP__TLS__Port() {
}

void V2G__TCP__TLS__Port::set_parameter(const char *parameter_name, const char *parameter_value) {
}

void V2G__TCP__TLS__Port::Handle_Message(const unsigned char *message, size_t length) {
}

void V2G__TCP__TLS__Port::outgoing_message(const unsigned char *message, size_t length) {
    std::cout << "[TCP_TLS_PORT] Sending " << length << " bytes" << std::endl;
}

void V2G__TCP__TLS__Port::outgoing_send(const OCTETSTRING& send_par) {
    std::cout << "[TCP_TLS_PORT] outgoing_send() called with " << send_par.lengthof() << " bytes" << std::endl;
}

void V2G__TCP__TLS__Port::send(const OCTETSTRING& send_data, const char* send_fmt, ...) {
    std::cout << "[TCP_TLS_PORT] send() called with " << send_data.lengthof() << " bytes" << std::endl;
}

}
