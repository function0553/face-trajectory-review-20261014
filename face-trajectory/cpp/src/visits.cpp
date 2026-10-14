#include "face_trajectory/visits.hpp"

#include <algorithm>
#include <limits>
#include <map>
#include <set>
#include <stdexcept>
#include <unordered_map>
#include <utility>

namespace face_trajectory {

void MergeCaptureTimeBounds(CaptureTimeBounds& into, const CaptureTimeBounds& other) {
    into.first_earliest_unix_ms = std::min(into.first_earliest_unix_ms, other.first_earliest_unix_ms);
    into.first_latest_unix_ms = std::min(into.first_latest_unix_ms, other.first_latest_unix_ms);
    into.last_earliest_unix_ms = std::max(into.last_earliest_unix_ms, other.last_earliest_unix_ms);
    into.last_latest_unix_ms = std::max(into.last_latest_unix_ms, other.last_latest_unix_ms);
}

bool SameSighting(const SightingEvidence& a, const SightingEvidence& b) {
    return a.observation_id == b.observation_id && a.target_id == b.target_id &&
           a.camera_id == b.camera_id && a.captured_at_unix_ms == b.captured_at_unix_ms &&
           a.clock_uncertainty_ms == b.clock_uncertainty_ms &&
           a.attribution_evidence_id == b.attribution_evidence_id &&
           a.link_status == b.link_status && a.time_source == b.time_source &&
           a.source_id == b.source_id && a.clock_domain == b.clock_domain &&
           a.reentry_evidence_id == b.reentry_evidence_id;
}

namespace {

std::string VisitId(const SightingEvidence& first) {
    return "visit:" + std::to_string(first.target_id.size()) + ":" + first.target_id +
           ":" + first.observation_id;
}

CaptureTimeBounds TimeBounds(const SightingEvidence& sighting) {
    const auto time = sighting.captured_at_unix_ms;
    const auto error = sighting.clock_uncertainty_ms;
    const auto lower = time < std::numeric_limits<std::int64_t>::min() + error
        ? std::numeric_limits<std::int64_t>::min() : time - error;
    const auto upper = time > std::numeric_limits<std::int64_t>::max() - error
        ? std::numeric_limits<std::int64_t>::max() : time + error;
    return {lower, upper, lower, upper};
}

}  // namespace

VisitBuildResult BuildCameraVisits(
    const std::vector<SightingEvidence>& sightings,
    std::int64_t max_gap_ms,
    std::int64_t max_clock_uncertainty_ms,
    const std::vector<std::pair<std::string, std::string>>& non_overlapping_cameras) {
    if (max_gap_ms < 0 || max_clock_uncertainty_ms < 0) {
        throw std::invalid_argument("time limits must be nonnegative");
    }
    std::unordered_map<std::string, SightingEvidence> unique;
    VisitBuildResult output;
    for (const auto& sighting : sightings) {
        if (sighting.observation_id.empty() || sighting.target_id.empty() ||
            sighting.camera_id.empty() || sighting.attribution_evidence_id.empty() ||
            sighting.source_id.empty() || sighting.clock_domain.empty()) {
            throw std::invalid_argument("observation, target, camera, source, clock domain and attribution evidence IDs are required");
        }
        auto [found, inserted] = unique.emplace(sighting.observation_id, sighting);
        if (!inserted && !SameSighting(found->second, sighting)) {
            throw std::invalid_argument("conflicting duplicate observation_id");
        }
    }
    std::vector<SightingEvidence> ordered;
    std::set<std::pair<std::string, std::string>> disjoint;
    for (auto pair : non_overlapping_cameras) {
        if (pair.first.empty() || pair.second.empty() || pair.first == pair.second)
            throw std::invalid_argument("non-overlapping camera pair must contain distinct IDs");
        if (pair.second < pair.first) std::swap(pair.first, pair.second);
        disjoint.insert(std::move(pair));
    }
    ordered.reserve(unique.size());
    for (const auto& [id, sighting] : unique) {
        if (sighting.link_status != TargetLinkStatus::Confirmed ||
            sighting.time_source != TimeSource::UtcCapture ||
            sighting.clock_domain != "UTC" ||
            sighting.clock_uncertainty_ms < 0 ||
            sighting.clock_uncertainty_ms > max_clock_uncertainty_ms) {
            output.rejected_observation_ids.push_back(id);
        } else {
            ordered.push_back(sighting);
        }
    }
    std::sort(output.rejected_observation_ids.begin(), output.rejected_observation_ids.end());
    std::sort(ordered.begin(), ordered.end(), [](const auto& a, const auto& b) {
        if (a.target_id != b.target_id) return a.target_id < b.target_id;
        if (a.camera_id != b.camera_id) return a.camera_id < b.camera_id;
        if (a.captured_at_unix_ms != b.captured_at_unix_ms) return a.captured_at_unix_ms < b.captured_at_unix_ms;
        return a.observation_id < b.observation_id;
    });
    std::map<std::string, std::vector<const SightingEvidence*>> target_evidence;
    if (!disjoint.empty()) {
        for (const auto& sighting : ordered) target_evidence[sighting.target_id].push_back(&sighting);
        for (auto& [id, evidence] : target_evidence)
            std::sort(evidence.begin(), evidence.end(), [](auto a, auto b) {
                return std::tie(a->captured_at_unix_ms, a->observation_id) <
                       std::tie(b->captured_at_unix_ms, b->observation_id);
            });
    }
    for (const auto& sighting : ordered) {
        bool join = !output.visits.empty() &&
            output.visits.back().target_id == sighting.target_id &&
            output.visits.back().camera_id == sighting.camera_id &&
            sighting.reentry_evidence_id.empty() &&
            static_cast<std::uint64_t>(sighting.captured_at_unix_ms) -
                static_cast<std::uint64_t>(output.visits.back().last_seen_unix_ms) <=
                static_cast<std::uint64_t>(max_gap_ms);
        std::string boundary;
        if (join && !disjoint.empty()) {
            const auto lower = output.visits.back().time_bounds.last_latest_unix_ms;
            const auto upper = TimeBounds(sighting).first_earliest_unix_ms;
            const auto& evidence = target_evidence.at(sighting.target_id);
            auto begin = std::upper_bound(evidence.begin(), evidence.end(), lower,
                [](auto time, auto item) { return time < item->captured_at_unix_ms; });
            for (auto it = begin; it != evidence.end() && (*it)->captured_at_unix_ms < upper; ++it) {
                const auto& other = **it;
                auto pair = std::minmax(sighting.camera_id, other.camera_id);
                const auto bounds = TimeBounds(other);
                if (disjoint.count({pair.first, pair.second}) &&
                    bounds.first_earliest_unix_ms > lower && bounds.last_latest_unix_ms < upper) {
                    join = false;
                    boundary = other.observation_id;
                    break;
                }
            }
        }
        if (join) {
            auto& visit = output.visits.back();
            MergeCaptureTimeBounds(visit.time_bounds, TimeBounds(sighting));
            if (sighting.captured_at_unix_ms == visit.first_seen_unix_ms) {
                visit.first_seen_clock_uncertainty_ms = std::max(
                    visit.first_seen_clock_uncertainty_ms, sighting.clock_uncertainty_ms);
            }
            if (sighting.captured_at_unix_ms > visit.last_seen_unix_ms) {
                visit.last_seen_unix_ms = sighting.captured_at_unix_ms;
                visit.last_seen_clock_uncertainty_ms = sighting.clock_uncertainty_ms;
            } else {
                visit.last_seen_clock_uncertainty_ms = std::max(
                    visit.last_seen_clock_uncertainty_ms, sighting.clock_uncertainty_ms);
            }
            visit.max_clock_uncertainty_ms = std::max(visit.max_clock_uncertainty_ms,
                                                     sighting.clock_uncertainty_ms);
            visit.observation_ids.push_back(sighting.observation_id);
            visit.attribution_evidence_ids.push_back(sighting.attribution_evidence_id);
            visit.source_ids.push_back(sighting.source_id);
        } else {
            output.visits.push_back({VisitId(sighting), sighting.target_id, sighting.camera_id,
                                     sighting.captured_at_unix_ms, sighting.captured_at_unix_ms,
                                     sighting.clock_uncertainty_ms, sighting.clock_uncertainty_ms,
                                     sighting.clock_uncertainty_ms,
                                     {sighting.observation_id}, {sighting.attribution_evidence_id},
                                     {sighting.source_id},
                                     sighting.reentry_evidence_id.empty()
                                         ? std::vector<std::string>{}
                                         : std::vector<std::string>{sighting.reentry_evidence_id},
                                     TimeBounds(sighting)});
            output.visits.back().boundary_observation_id = boundary;
        }
    }
    return output;
}

std::vector<CameraVisit> BuildRawTrajectory(
    const std::vector<SightingEvidence>& sightings,
    std::int64_t max_gap_ms,
    std::int64_t max_clock_uncertainty_ms,
    const std::string& target_id) {
    if (target_id.empty()) throw std::invalid_argument("target_id is required");
    std::vector<SightingEvidence> matching;
    for (const auto& sighting : sightings) {
        if (sighting.target_id == target_id) matching.push_back(sighting);
    }
    const auto built = BuildCameraVisits(matching, max_gap_ms, max_clock_uncertainty_ms);
    std::vector<CameraVisit> trajectory;
    for (const auto& visit : built.visits) {
        if (visit.target_id != target_id) continue;
        trajectory.push_back(visit);
    }
    std::sort(trajectory.begin(), trajectory.end(), [](const auto& a, const auto& b) {
        if (a.first_seen_unix_ms != b.first_seen_unix_ms) return a.first_seen_unix_ms < b.first_seen_unix_ms;
        if (a.last_seen_unix_ms != b.last_seen_unix_ms) return a.last_seen_unix_ms < b.last_seen_unix_ms;
        return a.camera_id < b.camera_id;
    });
    return trajectory;
}

}  // namespace face_trajectory
