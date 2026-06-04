// IEC 61851 Hardware Abstraction Layer Adapter
// Provides interface for controlling IEC 61851 signals (CP, PP, etc.)

#ifndef HAL_61851_ADAPTER_HH
#define HAL_61851_ADAPTER_HH

#include <string>
#include <vector>
#include <cstdint>
#include <functional>

// IEC 61851 State definitions
enum class IEC61851_State {
    STATE_A,  // Vehicle not connected
    STATE_B,  // Vehicle connected, not charging
    STATE_C,  // Vehicle connected, charging
    STATE_D,  // Vehicle connected, charging with ventilation
    STATE_E,  // Error
    STATE_F   // Fault
};

// Hardware abstraction layer interface
class HAL_61851_Adapter {
public:
    using StateChangeCallback = std::function<void(IEC61851_State, float, uint16_t, uint8_t)>;

    static HAL_61851_Adapter& instance();

    // Initialize the hardware adapter
    bool initialize(const std::string& device_path = "", int baudrate = 115200);

    // Close the hardware connection
    void close();

    // Check if adapter is initialized
    bool is_initialized() const { return initialized_; }

    // Set CP state
    bool set_state(IEC61851_State state);

    // Get current CP state
    IEC61851_State get_state() const { return current_state_; }

    // Set proximity value (0-255)
    bool set_proximity(uint8_t proximity);

    // Get proximity value
    uint8_t get_proximity() const { return current_proximity_; }

    // Set duty cycle and frequency
    bool set_duty_cycle(float duty_cycle, uint16_t frequency);

    // Register state change callback
    void register_state_change_callback(StateChangeCallback callback);

    // Trigger state change event (called when hardware reports change)
    void trigger_state_change(IEC61851_State state, float duty_cycle,
                            uint16_t frequency, uint8_t proximity);

    // Get last error message
    std::string get_last_error() const { return last_error_; }

private:
    HAL_61851_Adapter() : initialized_(false), current_state_(IEC61851_State::STATE_A),
                          current_proximity_(0), current_duty_cycle_(0.0),
                          current_frequency_(0) {}

    bool initialized_;
    IEC61851_State current_state_;
    uint8_t current_proximity_;
    float current_duty_cycle_;
    uint16_t current_frequency_;
    std::string last_error_;
    std::string device_path_;
    int baudrate_;
    StateChangeCallback state_change_callback_;
};

#endif // HAL_61851_ADAPTER_HH
