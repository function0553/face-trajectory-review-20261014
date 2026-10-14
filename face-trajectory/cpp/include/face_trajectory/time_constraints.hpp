#pragma once
#include "face_trajectory/trajectory.hpp"
namespace face_trajectory {
struct CaptureInterval { std::string camera_id; std::int64_t lower, upper; };
enum class ChainFeasibility { Feasible, Infeasible, Unknown };
// Exact earliest-arrival DP across all possible orders of overlapping captures.
// Exhausted resource bounds mean Unknown, never a fabricated feasibility proof.
ChainFeasibility CheckCaptureChain(std::vector<CaptureInterval> captures,
    const CameraGraph& graph, const std::vector<std::pair<std::string,std::string>>& overlaps,
    bool complete, std::size_t budget = 200000);
}
