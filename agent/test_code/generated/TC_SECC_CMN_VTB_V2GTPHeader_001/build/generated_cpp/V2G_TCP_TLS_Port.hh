#ifndef V2G_TCP_TLS_PORT_HH
#define V2G_TCP_TLS_PORT_HH

#include "SECC_TestSystem.hh"
#include <string>
#include <cstdint>
#include <sys/socket.h>
#include <netinet/in.h>
#include <net/if.h>

namespace SECC__TestSystem {

class V2G__TCP__TLS__Port : public V2G__TCP__TLS__Port_BASE {
public:
    V2G__TCP__TLS__Port(const char *par_port_name = NULL);
    virtual ~V2G__TCP__TLS__Port();
    void set_parameter(const char *parameter_name, const char *parameter_value);
    void Handle_Message(const unsigned char *message, size_t length);
    void outgoing_message(const unsigned char *message, size_t length);
    void outgoing_send(const OCTETSTRING& send_par) override;
    void send_data(const OCTETSTRING& send_data, const char* send_fmt, ...);

private:
    std::string port_name_;
    std::string ip_address_;
    std::string network_interface_;
    std::string ipv6_multicast_;
    int port_;
    bool tls_enabled_;
    bool use_ipv6_;
    int socket_fd_;
    bool connected_;
    bool receive_thread_running_;
    pthread_t receive_thread_id_;

    void connect_to_secc();
    void disconnect_from_secc();
    void receive_loop();
    bool send_v2gtp_frame(const unsigned char* data, size_t length);
    int parse_ipv6_address(const std::string& addr_str, ::sockaddr_in6* addr6);

    static void* receive_thread_func(void* arg);
};

}

#endif
