// TITAN Test Control Interface (TCI) Implementation
// This provides the necessary TCI callbacks required by the TITAN runtime

#ifndef TITAN_TCI_HH
#define TITAN_TCI_HH

#include <iostream>
#include <string>
#include <cstring>

// Forward declarations from TITAN runtime headers
extern "C" {

typedef enum {
    T_R_ERROR = 0,
    T_R_NULL = 1,
    T_R_LOG = 2,
    T_R_UNBOUND = 3,
    T_R_MEMORY = 4,
    T_R_INVALID_TESTCASE = 5,
    T_R_INVALID_FUNCTION = 6,
    T_R_INVALID_PORT = 7,
    T_R_PORTERROR = 8,
    T_R_TIMEOUT = 9,
    T_R_DYNAMIC_TESTCASE = 10,
    T_R_STARTUP_ERROR = 11,
    T_R_SHUTDOWN_ERROR = 12,
    T_R_TESTCASE_STARTED = 13,
    T_R_TESTCASE_FINISHED = 14,
    T_R_VERDICT = 15,
    T_R_FINAL = 16,
    T_R_DEFAULTOP = 17,
    T_R_ALT = 18,
    T_R_FUNCTION_STARTED = 19,
    T_R_FUNCTION_FINISHED = 20,
    T_R_COMPONENT_STARTED = 21,
    T_R_COMPONENT_FINISHED = 22,
    T_R_COMPONENT_CONNECTED = 23,
    T_R_COMPONENT_DISCONNECTED = 24,
    T_R_COMPONENT_MAPPED = 25,
    T_R_COMPONENT_UNMAPPED = 26,
    T_R_MTC_CREATED = 27,
    T_R_PTC_CREATED = 28,
    T_R_TIMER_READ = 29,
    T_R_TIMER_TIMEOUT = 30,
    T_R_MQ_READY = 31,
    T_R_PTC_KILLED = 32
} TTCN_TciMessageType;

typedef enum {
    T_R_VERDICT_NONE = 0,
    T_R_VERDICT_PASS = 1,
    T_R_VERDICT_INCONC = 2,
    T_R_VERDICT_FAIL = 3,
    T_R_VERDICT_ERROR = 4
} TTCN_VerdictType;

typedef enum {
    TTCN_log2_debug = 0,
    TTCN_log2_testcase = 1,
    TTCN_log2_priority = 2,
    TTCN_log2_warning = 3,
    TTCN_log2_error = 4,
    TTCN_log2_parset = 5,
    TTCN_log2_reference = 6,
    TTCN_log2_exception = 7,
    TTCN_log2_runtime = 8
} TTCN_Logger_Level;

struct TTCN_TciMessage {
    TTCN_TciMessageType msgtype;
    const char* module_name;
    const char* source_info;
    int line_no;
    union {
        struct { int verdict; const char* reason; } verdict;
        struct { int severity; const char* text; } log;
        struct { const char* testcase_name; int ptcid; } testcase;
        struct { const char* component_name; int compid; } component;
    } msg_data;
};

} // extern "C"

// TCI callback functions that must be implemented
namespace TCI {

// Called when a test case starts
void tciStartTestCase(const char* testcase_name, int ptc_id);

// Called when a test case finishes
void tciEndTestCase(const char* testcase_name, TTCN_VerdictType verdict,
                    const char* reason);

// Called for logging messages
void tciLog(int severity, const char* source_info, const char* message);

// Called to report a verdict
void tciSetVerdict(TTCN_VerdictType verdict, const char* reason,
                   const char* source_info, int line_no);

// Called when a component is created
void tciComponentCreated(const char* component_name, int component_id, int ptc_id);

// Called when a component finishes
void tciComponentTerminated(const char* component_name, int component_id);

// Called when ports are connected
void tciPortsConnected(const char* source_component, int source_port,
                       const char* dest_component, int dest_port);

// Called when ports are disconnected
void tciPortsDisconnected(const char* source_component, int source_port,
                          const char* dest_component, int dest_port);

// Called when ports are mapped
void tciPortsMapped(const char* component_name, int port_id);

// Called when ports are unmapped
void tciPortsUnmapped(const char* component_name, int port_id);

// Get test system configuration
const char* tciGetTestSystemConfig();

// Called for runtime errors
void tciRuntimeError(const char* message, const char* source_info, int line_no);

} // namespace TCI

#endif // TITAN_TCI_HH
