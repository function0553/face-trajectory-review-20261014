#pragma once
#include "face_trajectory/monitoring.hpp"
namespace face_trajectory {
// Same activity/person within 5 seconds shares one reported representative.
// Corrections may also promote suppressed evidence, all in one transaction.
std::vector<nlohmann::json> PersonTrackEvents(State& scope, const nlohmann::json& request,
    const nlohmann::json& context, const MonitoringStore& monitoring, const AssociationStore& associations);
}
