#pragma once
#include "face_trajectory/monitoring.hpp"
namespace face_trajectory {
// Called inside the observation/correction transaction. Returns a complete v1
// wire record only when the platform's current attribution needs to change.
std::optional<nlohmann::json> PersonTrackEvent(State& scope, const nlohmann::json& request,
    const nlohmann::json& context, const MonitoringStore& monitoring, const AssociationStore& associations);
}
