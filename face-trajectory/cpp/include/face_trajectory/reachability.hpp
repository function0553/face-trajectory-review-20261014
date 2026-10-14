#pragma once

#include "face_trajectory/trajectory.hpp"

#include <nlohmann/json.hpp>

namespace face_trajectory {

// Conservative, camera-point-level forward candidates from accepted captures.
// Does not use reconstructed paths, future captures, or simulated ground truth.
// as_of_unix_ms is a capture-time cutoff, not an ingestion-time replay clock.
nlohmann::json PredictReachable(const TrajectorySnapshot& snapshot,
                                const nlohmann::json& options);

}  // namespace face_trajectory
