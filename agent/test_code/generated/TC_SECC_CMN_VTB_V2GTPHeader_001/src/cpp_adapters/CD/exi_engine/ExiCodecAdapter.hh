// EXI Engine Adapter for V2G Message Encoding/Decoding
// Provides EXI codec functionality for ISO 15118 messages

#ifndef EXI_CODEC_ADAPTER_HH
#define EXI_CODEC_ADAPTER_HH

#include <vector>
#include <cstdint>
#include <string>
#include <memory>

// EXI codec options
struct ExiCodecOptions {
    uint8_t value_table_present;
    uint8_t self_contained_present;
    uint8_t element_present;
    uint8_t schema_aware;
    uint8_t fidelity_options;
};

// EXI codec result
struct ExiCodecResult {
    bool success;
    std::string error_message;
    int bytes_consumed;
};

// EXI codec interface
class ExiCodecAdapter {
public:
    static ExiCodecAdapter& instance();

    // Initialize the EXI codec
    bool initialize(bool schema_aware = false);

    // Encode V2G message to EXI
    ExiCodecResult encode(const std::vector<uint8_t>& xml_data,
                         std::vector<uint8_t>& exi_output);

    // Decode EXI to V2G message
    ExiCodecResult decode(const std::vector<uint8_t>& exi_data,
                         std::vector<uint8_t>& xml_output);

    // Set EXI options
    void set_options(const ExiCodecOptions& options);

    // Get last error
    std::string get_last_error() const { return last_error_; }

private:
    ExiCodecAdapter() : initialized_(false) {}
    ExiCodecAdapter(const ExiCodecAdapter&) = delete;
    ExiCodecAdapter& operator=(const ExiCodecAdapter&) = delete;

    bool initialized_;
    std::string last_error_;
    ExiCodecOptions options_;
};

#endif // EXI_CODEC_ADAPTER_HH
