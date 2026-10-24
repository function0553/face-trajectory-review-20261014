#pragma once
#include "face_trajectory/state.hpp"
#include <nlohmann/json.hpp>
namespace face_trajectory {
nlohmann::json PlatformV4(State& root, const nlohmann::json& request);
bool ExpirePlatformPersons(State& root, std::int64_t now);
std::string CapturePhotoBase64(const nlohmann::json& value);
}
