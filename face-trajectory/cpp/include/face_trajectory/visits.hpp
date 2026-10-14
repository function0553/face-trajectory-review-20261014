#pragma once

#include <cstdint>
#include <string>
#include <vector>
#include <utility>

namespace face_trajectory {

// Confirmed means that upstream evidence assigns this observation to target_id.
// It does not assert a real-world identity or a person record.
enum class TargetLinkStatus { Unassigned, Provisional, Confirmed, Conflict };
enum class TimeSource { Unknown, UtcCapture, MediaOffset, ReceivedAt };

struct SightingEvidence {
    std::string observation_id;
    std::string target_id;
    std::string camera_id;
    std::string attribution_evidence_id;
    std::int64_t captured_at_unix_ms;
    std::int64_t clock_uncertainty_ms;
    TargetLinkStatus link_status;
    TimeSource time_source;
    std::string source_id;
    std::string clock_domain;
    // A verified local departure/reentry boundary before this observation.
    // Another camera's simultaneous capture alone must not set this field.
    std::string reentry_evidence_id;
};

bool SameSighting(const SightingEvidence& a, const SightingEvidence& b);

// Bounds of the actual first/last capture over a set of observations.
// Unlike nominal endpoint errors, these retain uncertainty from every capture.
struct CaptureTimeBounds {
    std::int64_t first_earliest_unix_ms;
    std::int64_t first_latest_unix_ms;
    std::int64_t last_earliest_unix_ms;
    std::int64_t last_latest_unix_ms;
};

void MergeCaptureTimeBounds(CaptureTimeBounds& into, const CaptureTimeBounds& other);

struct CameraVisit {
    std::string visit_id;
    std::string target_id;
    std::string camera_id;
    std::int64_t first_seen_unix_ms;
    std::int64_t last_seen_unix_ms;
    std::int64_t first_seen_clock_uncertainty_ms;
    std::int64_t last_seen_clock_uncertainty_ms;
    std::int64_t max_clock_uncertainty_ms;
    std::vector<std::string> observation_ids;
    std::vector<std::string> attribution_evidence_ids;
    std::vector<std::string> source_ids;
    std::vector<std::string> reentry_evidence_ids;
    CaptureTimeBounds time_bounds;
    std::string boundary_observation_id;
};

struct VisitBuildResult {
    std::vector<CameraVisit> visits;
    std::vector<std::string> rejected_observation_ids;
};

// Deterministic rebuild from immutable sightings. Visit IDs are derived from
// the first observation in each visit and stay fixed when the visit extends.
// A retroactive merge/split may replace an ID and is reported by a revision.
// Cross-camera sightings do not split visits without explicit reentry evidence.
// max_gap_ms compares nominal capture timestamps; clock uncertainty is reported
// separately and does not establish a guaranteed real-world dwell duration.
VisitBuildResult BuildCameraVisits(
    const std::vector<SightingEvidence>& sightings,
    std::int64_t max_gap_ms,
    std::int64_t max_clock_uncertainty_ms,
    const std::vector<std::pair<std::string, std::string>>& non_overlapping_cameras = {});

// Rebuilds from the same evidence gate as BuildCameraVisits; callers cannot
// inject an arbitrary CameraVisit into an observed trajectory.
std::vector<CameraVisit> BuildRawTrajectory(
    const std::vector<SightingEvidence>& sightings,
    std::int64_t max_gap_ms,
    std::int64_t max_clock_uncertainty_ms,
    const std::string& target_id);

}  // namespace face_trajectory
