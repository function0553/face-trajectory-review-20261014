#pragma once
#include <cstdint>
#include <functional>
#include <string>
#include <vector>
#include <nlohmann/json.hpp>

namespace face_trajectory {
class ClockAdapter;
using ExpiredReference = std::function<bool(const std::string& activity_id,
    const std::string& source_id, const std::string& event_id)>;
// Operates only on the explicit locally configured SQLite paths. Unknown legacy
// times receive no new retention window. A running compatible P1 consumer
// quiesces on the persisted maintenance request; its lease protects in-flight
// images and delivery bodies until all caches and SQLite readers are dropped.
nlohmann::json PruneP1(const std::vector<std::string>& configured_paths,
                       std::int64_t trusted_now_ms, bool dry_run,
                       const ClockAdapter& clocks,
                       const ExpiredReference& expired_reference = {});
}
