// Inline logging stub for ExiCodecAdapter — no external dependency.
// Drop this into any compilation unit that needs the LOG_* macros.

#ifndef EXI_CODEC_LOGGER_HH
#define EXI_CODEC_LOGGER_HH

#include <string>
#include <iostream>

namespace exi_codec_logger {
    enum class LogLevel { DEBUG = 0, INFO = 1, WARN = 2, ERROR = 3 };
    inline LogLevel g_level = LogLevel::INFO;
    inline void set_level(LogLevel l) { g_level = l; }
    inline void log(LogLevel level, const std::string& msg) {
        if (level < g_level) return;
        const char* prefix = "INFO";
        if (level == LogLevel::DEBUG) prefix = "DEBUG";
        else if (level == LogLevel::WARN)  prefix = "WARN";
        else if (level == LogLevel::ERROR)  prefix = "ERROR";
        std::cerr << "[" << prefix << "] " << msg << std::endl;
    }
    inline void logf(LogLevel level, const char* fmt, ...);
}

#define LOG_DEBUG(msg) exi_codec_logger::log(exi_codec_logger::LogLevel::DEBUG, msg)
#define LOG_INFO(msg)  exi_codec_logger::log(exi_codec_logger::LogLevel::INFO, msg)
#define LOG_WARN(msg)  exi_codec_logger::log(exi_codec_logger::LogLevel::WARN, msg)
#define LOG_ERROR(msg) exi_codec_logger::log(exi_codec_logger::LogLevel::ERROR, msg)

#endif // EXI_CODEC_LOGGER_HH
