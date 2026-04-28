// Common utilities for TITAN TTCN-3 Test Adapter
// This file provides common helper functions used across all adapter components

#ifndef COMMON_HH
#define COMMON_HH

#include <string>
#include <vector>
#include <cstdint>
#include <iostream>
#include <fstream>
#include <ctime>

namespace titan_adapter {

// Log levels
enum class LogLevel {
    DEBUG = 0,
    INFO = 1,
    WARN = 2,
    ERROR = 3,
    FATAL = 4
};

// Global log level setting
extern LogLevel g_log_level;

// Initialize logging system
void init_logging(const std::string& log_file = "");

// Log a message
void log_message(LogLevel level, const std::string& message);

// Convert octetstring to hex string
std::string octetstring_to_hex(const std::vector<uint8_t>& data);

// Convert hex string to octetstring
std::vector<uint8_t> hex_to_octetstring(const std::string& hex);

// Sleep for specified milliseconds
void sleep_ms(int milliseconds);

// Get current timestamp as string
std::string get_timestamp();

// Load file content into buffer
bool load_file(const std::string& path, std::vector<uint8_t>& buffer);

// Save buffer to file
bool save_file(const std::string& path, const std::vector<uint8_t>& buffer);

} // namespace titan_adapter

#endif // COMMON_HH
