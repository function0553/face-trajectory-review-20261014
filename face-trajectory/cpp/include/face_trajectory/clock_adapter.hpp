#pragma once

#include <map>
#include <cstdint>
#include <string>
#include <nlohmann/json.hpp>

namespace face_trajectory {
class State;

// Converts declared device capture times using an immutable calibration profile.
// An unverified profile or uncertain calibration selection never emits UTC_CAPTURE.
class ClockAdapter {
public:
    static constexpr std::int64_t CameraCaptureErrorLimitMs = 60000;
    explicit ClockAdapter(State* state = nullptr) : state_(state) {}
    void Put(const nlohmann::json& profile);
    nlohmann::json Normalize(const nlohmann::json& request) const;
private:
    State* state_;
    std::map<std::string, std::string> profiles_;
};
}
