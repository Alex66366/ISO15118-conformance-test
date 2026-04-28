// Common utilities implementation for TITAN TTCN-3 Test Adapter

#include "Common.hh"
#include <chrono>
#include <thread>
#include <iomanip>

namespace titan_adapter {

LogLevel g_log_level = LogLevel::INFO;
static std::ofstream g_log_file;

void init_logging(const std::string& log_file) {
    if (!log_file.empty()) {
        g_log_file.open(log_file, std::ios::app);
    }
}

void log_message(LogLevel level, const std::string& message) {
    if (level < g_log_level) return;

    std::string level_str;
    switch (level) {
        case LogLevel::DEBUG: level_str = "DEBUG"; break;
        case LogLevel::INFO:  level_str = "INFO";  break;
        case LogLevel::WARN:  level_str = "WARN";  break;
        case LogLevel::ERROR: level_str = "ERROR"; break;
        case LogLevel::FATAL: level_str = "FATAL"; break;
    }

    std::string log_entry = "[" + get_timestamp() + "] [" + level_str + "] " + message;

    std::cout << log_entry << std::endl;
    if (g_log_file.is_open()) {
        g_log_file << log_entry << std::endl;
        g_log_file.flush();
    }
}

std::string octetstring_to_hex(const std::vector<uint8_t>& data) {
    std::stringstream ss;
    ss << std::hex << std::setfill('0');
    for (auto byte : data) {
        ss << std::setw(2) << static_cast<int>(byte);
    }
    return ss.str();
}

std::vector<uint8_t> hex_to_octetstring(const std::string& hex) {
    std::vector<uint8_t> result;
    for (size_t i = 0; i < hex.length(); i += 2) {
        std::string byte_str = hex.substr(i, 2);
        uint8_t byte = static_cast<uint8_t>(std::stoul(byte_str, nullptr, 16));
        result.push_back(byte);
    }
    return result;
}

void sleep_ms(int milliseconds) {
    std::this_thread::sleep_for(std::chrono::milliseconds(milliseconds));
}

std::string get_timestamp() {
    auto now = std::chrono::system_clock::now();
    auto time_t_now = std::chrono::system_clock::to_time_t(now);
    auto ms = std::chrono::duration_cast<std::chrono::milliseconds>(
        now.time_since_epoch()) % 1000;

    std::stringstream ss;
    ss << std::put_time(std::localtime(&time_t_now), "%Y-%m-%d %H:%M:%S");
    ss << '.' << std::setfill('0') << std::setw(3) << ms.count();
    return ss.str();
}

bool load_file(const std::string& path, std::vector<uint8_t>& buffer) {
    std::ifstream file(path, std::ios::binary);
    if (!file) return false;

    file.seekg(0, std::ios::end);
    size_t size = file.tellg();
    file.seekg(0, std::ios::beg);

    buffer.resize(size);
    file.read(reinterpret_cast<char*>(buffer.data()), size);
    return file.good();
}

bool save_file(const std::string& path, const std::vector<uint8_t>& buffer) {
    std::ofstream file(path, std::ios::binary);
    if (!file) return false;

    file.write(reinterpret_cast<const char*>(buffer.data()), buffer.size());
    return file.good();
}

} // namespace titan_adapter
