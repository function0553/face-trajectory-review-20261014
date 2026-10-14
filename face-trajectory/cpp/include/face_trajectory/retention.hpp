#pragma once
#include "face_trajectory/state.hpp"
#include <nlohmann/json.hpp>
namespace face_trajectory {
nlohmann::json Retain(State& root,std::int64_t now,bool dry_run);
nlohmann::json StorageReport(const State::StorageStats& stats);
}
