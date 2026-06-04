#ifndef V2G_UDP_SDP_PORT_HH
#define V2G_UDP_SDP_PORT_HH

#include "SECC_TestSystem.hh"
#include <string>
#include <cstdint>
#include <sys/socket.h>
#include <netinet/in.h>
#include <net/if.h>

extern ::std::string V2G__UDP__SDP__Port__last_secc_ip_;
extern int V2G__UDP__SDP__Port__last_secc_port_;

namespace SECC__TestSystem {

class V2G__UDP__SDP__Port : public V2G__UDP__SDP__Port_BASE {
public:
    V2G__UDP__SDP__Port(const char *par_port_name = NULL);
    virtual ~V2G__UDP__SDP__Port();
    void set_parameter(const char *parameter_name, const char *parameter_value);
    void Handle_Message(const unsigned char *message, size_t length);
    void outgoing_message(const unsigned char *message, size_t length);
    void outgoing_send(const OCTETSTRING& send_par) override;
    void send_data(const OCTETSTRING& send_data, const char* send_fmt, ...);

private:
    ::std::string port_name_;
    ::std::string network_interface_;
    ::std::string ipv6_multicast_;
    int port_;
    int socket_fd_;
    bool bound_;
    bool receive_thread_running_;
    pthread_t receive_thread_id_;

    void initialize_socket();
    void close_socket();
    void receive_loop();
    bool send_sdp_request(const unsigned char* data, size_t length);
    int parse_ipv6_address(const ::std::string& addr_str, ::sockaddr_in6* addr6);
    bool parse_sdp_response(const unsigned char* data, size_t length,
                           ::std::string& secc_ip, int& secc_port);

    static void* receive_thread_func(void* arg);
};

}

#endif
