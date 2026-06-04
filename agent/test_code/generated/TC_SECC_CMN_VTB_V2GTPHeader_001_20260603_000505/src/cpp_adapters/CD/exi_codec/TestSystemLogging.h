// Minimal stub for TestSystemLogging.h
// Provides Logging class + macros without pulling in cppcommon (C++17-only).
// The actual logging is done via std::cerr in this minimal build.

#ifndef __TESTSYSTEMLOGGING_H__
#define __TESTSYSTEMLOGGING_H__

#include "fmt/core.h"
#include "fmt/core.h"
#include <string>
#include <iostream>
#include <cstdio>
#include <ctime>
#include <chrono>

typedef enum En_LogLevelType {
    LOG_NONE = 0, LOG_ERROR, LOG_INFO, LOG_DEBUG, LOG_NUM
} LogLevelType;

typedef enum En_LogOutputType {
    LOG_OUT_NONE = 0, LOG_OUT_CONSOLE, LOG_OUT_FILE, LOG_OUT_BOTH, LOG_OUT_NUM
} LogOutputType;

typedef union {
    struct {
        uint16_t LogPAIf : 1; uint16_t LogRunEnv : 1; uint16_t LogSut61851 : 1;
        uint16_t LogSutTCP : 1; uint16_t LogSutUDP : 1; uint16_t LogComponent : 1;
        uint16_t LogCmnFnc : 1; uint16_t LogCfgFnc : 1; uint16_t LogPreFnc : 1;
        uint16_t LogPosFnc : 1; uint16_t LogTbFnc : 1; uint16_t LogTc : 1;
        uint16_t LogCmnLib : 1; uint16_t LogMsgTPDump : 1; uint16_t LogMsgDump : 1;
        uint16_t LogSLAC : 1;
    };
    uint16_t value;
} LogModuleCfg;

#define LogPAIf_ENABLE       ((uint16_t)0x0001)
#define LogRunEnv_ENABLE     ((uint16_t)0x0002)
#define LogSut61851_ENABLE   ((uint16_t)0x0004)
#define LogSutTCP_ENABLE     ((uint16_t)0x0008)
#define LogSutUDP_ENABLE     ((uint16_t)0x0010)
#define LogComponent_ENABLE  ((uint16_t)0x0020)
#define LogCmnFnc_ENABLE     ((uint16_t)0x0040)
#define LogCfgFnc_ENABLE     ((uint16_t)0x0080)
#define LogPreFnc_ENABLE     ((uint16_t)0x0100)
#define LogPosFnc_ENABLE     ((uint16_t)0x0200)
#define LogTbFnc_ENABLE      ((uint16_t)0x0400)
#define LogTc_ENABLE         ((uint16_t)0x0800)
#define LogCmnLib_ENABLE    ((uint16_t)0x1000)
#define LogMsgDump_ENABLE    ((uint16_t)0x2000)
#define LogSLAC_ENABLE       ((uint16_t)0x4000)
#define LogMsgTPDump_ENABLE  ((uint16_t)0x8000)
#define LogAll_ENABLE        ((uint16_t)0xFFFF)

class Logging {
private:
    static LogLevelType logLevel;
    static LogOutputType logOutDest;
    static std::FILE *outputLog;
public:
    static LogModuleCfg LogCfg;
    Logging() { }
    ~Logging() { if (outputLog) fclose(outputLog); }
    static std::string time_inMicroSec(void) {
        auto now = std::chrono::system_clock::now();
        unsigned long us = std::chrono::duration_cast<std::chrono::microseconds>(
            now.time_since_epoch()).count() % 1000000;
        auto timer = std::chrono::system_clock::to_time_t(now);
        char buf[32];
        std::strftime(buf, sizeof(buf), "%H:%M:%S", std::localtime(&timer));
        return std::string(buf) + ":" + std::to_string(us);
    }
    static void setLogLevel(LogLevelType level) { logLevel = level; }
    static void setLogOutput(LogOutputType type) { logOutDest = type; }
    static void setLogFile(const std::string _file) {
        outputLog = fopen(_file.c_str(), "w");
    }
    static void closeLogFile(void) { if (outputLog) { fclose(outputLog); outputLog = nullptr; } }
    static void debug(uint16_t module, const std::string value) {
        if (LOG_INFO < Logging::logLevel && (module == (Logging::LogCfg.value & module))) {
            std::string ts = time_inMicroSec();
            if (LOG_OUT_CONSOLE == Logging::logOutDest || LOG_OUT_BOTH == Logging::logOutDest)
                std::cerr << ts << "[DEBUG]:" << value << std::endl;
            if ((LOG_OUT_FILE == Logging::logOutDest || LOG_OUT_BOTH == Logging::logOutDest) && outputLog)
                fprintf(outputLog, "%s[DEBUG]:%s\n", ts.c_str(), value.c_str());
        }
    }
    static void info(uint16_t module, const std::string value) {
        if (LOG_ERROR < Logging::logLevel && (module == (Logging::LogCfg.value & module))) {
            std::string ts = time_inMicroSec();
            if (LOG_OUT_CONSOLE == Logging::logOutDest || LOG_OUT_BOTH == Logging::logOutDest)
                std::cerr << ts << "[ INFO]:" << value << std::endl;
            if ((LOG_OUT_FILE == Logging::logOutDest || LOG_OUT_BOTH == Logging::logOutDest) && outputLog)
                fprintf(outputLog, "%s[ INFO]:%s\n", ts.c_str(), value.c_str());
        }
    }
    static void error(uint16_t module, const std::string value) {
        if (LOG_NONE < Logging::logLevel && (module == (Logging::LogCfg.value & module))) {
            std::string ts = time_inMicroSec();
            if (LOG_OUT_CONSOLE == Logging::logOutDest || LOG_OUT_BOTH == Logging::logOutDest)
                std::cerr << ts << "[ERROR]:" << value << std::endl;
            if ((LOG_OUT_FILE == Logging::logOutDest || LOG_OUT_BOTH == Logging::logOutDest) && outputLog)
                fprintf(outputLog, "%s[ERROR]:%s\n", ts.c_str(), value.c_str());
        }
    }
};

#endif // __TESTSYSTEMLOGGING_H__
