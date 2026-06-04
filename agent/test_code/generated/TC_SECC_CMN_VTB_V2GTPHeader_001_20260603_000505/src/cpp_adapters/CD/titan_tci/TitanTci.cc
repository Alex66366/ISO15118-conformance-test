// TITAN Test Control Interface (TCI) Implementation
// Required by the TITAN runtime

#include "TitanTci.hh"
#include <iostream>
#include <cstdlib>

namespace TCI {

void tciStartTestCase(const char* testcase_name, int ptc_id) {
    std::cout << "[TCI] Test case started: " << testcase_name
              << " (PTC ID: " << ptc_id << ")" << std::endl;
}

void tciEndTestCase(const char* testcase_name, TTCN_VerdictType verdict,
                    const char* reason) {
    const char* verdict_str;
    switch (verdict) {
        case T_R_VERDICT_PASS: verdict_str = "PASS"; break;
        case T_R_VERDICT_INCONC: verdict_str = "INCONCLUSIVE"; break;
        case T_R_VERDICT_FAIL: verdict_str = "FAIL"; break;
        case T_R_VERDICT_ERROR: verdict_str = "ERROR"; break;
        default: verdict_str = "NONE";
    }

    std::cout << "[TCI] Test case finished: " << testcase_name
              << " - Verdict: " << verdict_str
              << " (Reason: " << (reason ? reason : "none") << ")" << std::endl;
}

void tciLog(int severity, const char* source_info, const char* message) {
    const char* level_str;
    switch (severity) {
        case TTCN_log2_debug: level_str = "DEBUG"; break;
        case TTCN_log2_testcase: level_str = "TESTCASE"; break;
        case TTCN_log2_warning: level_str = "WARNING"; break;
        case TTCN_log2_error: level_str = "ERROR"; break;
        case TTCN_log2_exception: level_str = "EXCEPTION"; break;
        default: level_str = "INFO";
    }

    std::cout << "[TCI][" << level_str << "] " << message;
    if (source_info) {
        std::cout << " (" << source_info << ")";
    }
    std::cout << std::endl;
}

void tciSetVerdict(TTCN_VerdictType verdict, const char* reason,
                   const char* source_info, int line_no) {
    const char* verdict_str;
    switch (verdict) {
        case T_R_VERDICT_PASS: verdict_str = "PASS"; break;
        case T_R_VERDICT_INCONC: verdict_str = "INCONCLUSIVE"; break;
        case T_R_VERDICT_FAIL: verdict_str = "FAIL"; break;
        case T_R_VERDICT_ERROR: verdict_str = "ERROR"; break;
        default: verdict_str = "NONE";
    }

    std::cout << "[TCI] Verdict: " << verdict_str;
    if (reason) std::cout << " - " << reason;
    if (source_info) std::cout << " (" << source_info;
    if (line_no > 0) std::cout << ":" << line_no;
    if (source_info) std::cout << ")";
    std::cout << std::endl;
}

void tciComponentCreated(const char* component_name, int component_id, int ptc_id) {
    std::cout << "[TCI] Component created: " << component_name
              << " (ID: " << component_id << ", PTC: " << ptc_id << ")" << std::endl;
}

void tciComponentTerminated(const char* component_name, int component_id) {
    std::cout << "[TCI] Component terminated: " << component_name
              << " (ID: " << component_id << ")" << std::endl;
}

void tciPortsConnected(const char* source_component, int source_port,
                       const char* dest_component, int dest_port) {
    std::cout << "[TCI] Ports connected: " << source_component << ":" << source_port
              << " -> " << dest_component << ":" << dest_port << std::endl;
}

void tciPortsDisconnected(const char* source_component, int source_port,
                          const char* dest_component, int dest_port) {
    std::cout << "[TCI] Ports disconnected: " << source_component << ":" << source_port
              << " -> " << dest_component << ":" << dest_port << std::endl;
}

void tciPortsMapped(const char* component_name, int port_id) {
    std::cout << "[TCI] Port mapped: " << component_name << ":" << port_id << std::endl;
}

void tciPortsUnmapped(const char* component_name, int port_id) {
    std::cout << "[TCI] Port unmapped: " << component_name << ":" << port_id << std::endl;
}

const char* tciGetTestSystemConfig() {
    // Return empty config - will be provided via runtime.cfg
    return "";
}

void tciRuntimeError(const char* message, const char* source_info, int line_no) {
    std::cerr << "[TCI] Runtime error: " << message;
    if (source_info) {
        std::cerr << " (" << source_info;
        if (line_no > 0) std::cerr << ":" << line_no;
        std::cerr << ")";
    }
    std::cerr << std::endl;
}

} // namespace TCI
