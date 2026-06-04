// IEC 61851 Hardware Abstraction Layer Adapter Implementation

#include "HAL61851Adapter.hh"
#include "../../common/Common.hh"

HAL_61851_Adapter& HAL_61851_Adapter::instance() {
    static HAL_61851_Adapter instance;
    return instance;
}

bool HAL_61851_Adapter::initialize(const std::string& device_path, int baudrate) {
    device_path_ = device_path;
    baudrate_ = baudrate;

    if (!device_path.empty()) {
        // In a real implementation, this would open the serial port
        // For now, we just mark as initialized
        titan_adapter::log_message(titan_adapter::LogLevel::INFO,
            "HAL 61851 adapter initialized (simulated mode)");
    } else {
        titan_adapter::log_message(titan_adapter::LogLevel::INFO,
            "HAL 61851 adapter initialized (no hardware)");
    }

    initialized_ = true;
    return true;
}

void HAL_61851_Adapter::close() {
    if (initialized_) {
        titan_adapter::log_message(titan_adapter::LogLevel::INFO,
            "HAL 61851 adapter closed");
        initialized_ = false;
    }
}

bool HAL_61851_Adapter::set_state(IEC61851_State state) {
    if (!initialized_) {
        last_error_ = "Adapter not initialized";
        return false;
    }

    current_state_ = state;
    titan_adapter::log_message(titan_adapter::LogLevel::INFO,
        "IEC 61851 state set to: " + std::to_string(static_cast<int>(state)));

    // Trigger callback if registered
    if (state_change_callback_) {
        state_change_callback_(current_state_, current_duty_cycle_,
                              current_frequency_, current_proximity_);
    }

    return true;
}

bool HAL_61851_Adapter::set_proximity(uint8_t proximity) {
    if (!initialized_) {
        last_error_ = "Adapter not initialized";
        return false;
    }

    current_proximity_ = proximity;
    titan_adapter::log_message(titan_adapter::LogLevel::DEBUG,
        "Proximity set to: " + std::to_string(proximity));

    return true;
}

bool HAL_61851_Adapter::set_duty_cycle(float duty_cycle, uint16_t frequency) {
    if (!initialized_) {
        last_error_ = "Adapter not initialized";
        return false;
    }

    current_duty_cycle_ = duty_cycle;
    current_frequency_ = frequency;

    titan_adapter::log_message(titan_adapter::LogLevel::DEBUG,
        "Duty cycle set to: " + std::to_string(duty_cycle) +
        "%, Frequency: " + std::to_string(frequency) + " Hz");

    return true;
}

void HAL_61851_Adapter::register_state_change_callback(StateChangeCallback callback) {
    state_change_callback_ = callback;
}

void HAL_61851_Adapter::trigger_state_change(IEC61851_State state, float duty_cycle,
                                            uint16_t frequency, uint8_t proximity) {
    current_state_ = state;
    current_duty_cycle_ = duty_cycle;
    current_frequency_ = frequency;
    current_proximity_ = proximity;

    if (state_change_callback_) {
        state_change_callback_(state, duty_cycle, frequency, proximity);
    }
}
