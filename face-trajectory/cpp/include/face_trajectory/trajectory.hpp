#pragma once

#include "face_trajectory/visits.hpp"
#include <nlohmann/json.hpp>

#include <cstddef>
#include <cstdint>
#include <deque>
#include <functional>
#include <map>
#include <optional>
#include <string>
#include <utility>
#include <vector>

namespace face_trajectory {
class State;

struct CameraEdge {
    std::string from_camera;
    std::string to_camera;
    std::int64_t min_travel_ms;
    std::int64_t max_travel_ms;
    double cost;
    // Inclusive departure-time window. Repeated edges can express multiple windows.
    std::optional<std::int64_t> open_from_unix_ms;
    std::optional<std::int64_t> open_until_unix_ms;
    // Stable topology identity; later 3D navigation geometry can bind to this ID.
    std::string edge_id;
};

struct CameraGraph {
    std::string version;
    std::vector<CameraEdge> edges;
};

// Shared validation/canonical ordering for trajectory and identity constraints.
CameraGraph NormalizeCameraGraph(CameraGraph graph);

struct TrajectoryOptions {
    std::int64_t max_visit_gap_ms = 30000;
    std::int64_t max_clock_uncertainty_ms = 1000;
    std::int64_t session_gap_ms = 3600000;
    std::size_t max_hops = 8;
    std::size_t max_expansions = 10000;
    std::size_t max_candidates = 8;
    double backup_score_margin = 1.0;
    double travel_time_weight_per_second = 0.0;
    // Verified disjoint observation regions, not merely different camera IDs.
    std::vector<std::pair<std::string, std::string>> non_overlapping_cameras;
};

enum class NodeStatus { Observed, Inferred, Unknown };

struct Anchor {
    std::string anchor_id;
    std::string visit_id;
    std::string camera_id;
    std::int64_t first_seen_unix_ms;
    std::int64_t last_seen_unix_ms;
    std::int64_t first_seen_clock_uncertainty_ms;
    std::int64_t last_seen_clock_uncertainty_ms;
    CaptureTimeBounds time_bounds;
};

struct PathNode {
    std::string camera_id;
    NodeStatus status;
    // Envelope of possible times, conditional on this route; not a precise fix.
    std::int64_t earliest_unix_ms;
    std::int64_t latest_unix_ms;
};

struct PathTimeRange {
    std::int64_t earliest_unix_ms;
    std::int64_t latest_unix_ms;

    bool operator==(const PathTimeRange& other) const {
        return earliest_unix_ms == other.earliest_unix_ms && latest_unix_ms == other.latest_unix_ms;
    }
};

struct PathCandidate {
    std::vector<PathNode> nodes;
    std::vector<std::string> edge_ids;
    double score;
    double edge_cost;
    double time_penalty;
    // One possible traversal envelope per edge_id, in the same order.
    std::vector<PathTimeRange> edge_time_bounds;
};

struct TrajectorySegment {
    std::string segment_id;
    std::string graph_version;
    std::string from_anchor_id;
    std::string to_anchor_id;
    std::int64_t from_unix_ms;
    std::int64_t to_unix_ms;
    NodeStatus status;
    bool search_complete;
    bool unique_path;
    std::optional<PathCandidate> primary;
    std::vector<PathCandidate> backups;
};

struct TrajectorySession {
    std::string session_id;
    std::int64_t first_seen_unix_ms;
    std::int64_t last_seen_unix_ms;
    std::int64_t first_seen_clock_uncertainty_ms;
    std::int64_t last_seen_clock_uncertainty_ms;
    std::vector<std::string> anchor_ids;
    std::vector<std::string> segment_ids;
    CaptureTimeBounds time_bounds;
};

struct TrajectoryRevision {
    std::string target_id;
    std::uint64_t previous_version;
    std::uint64_t version;
    std::string reason;
    std::vector<std::string> affected_segment_ids;
};

struct TrajectorySnapshot {
    std::string target_id;
    std::uint64_t version;
    CameraGraph graph;
    TrajectoryOptions options;
    std::vector<SightingEvidence> evidence;
    std::vector<CameraVisit> visits;
    std::vector<Anchor> anchors;
    std::vector<TrajectorySession> sessions;
    std::vector<TrajectorySegment> segments;
    std::vector<std::string> rejected_observation_ids;
    TrajectoryRevision revision;
    std::uint64_t association_revision = 0;
};

struct TrajectoryReference {
    std::uint64_t version, association_revision;
    std::size_t observation_count, anchor_count;
    std::string graph_version;
};

struct TimelineObservation {
    std::string anchor_id;
    SightingEvidence observation;
};

struct TimelinePathLocation {
    std::string segment_id;
    // 0 is the primary route; n > 0 refers to backups[n-1].
    std::size_t route_index;
    std::vector<std::size_t> node_indices;
    std::vector<std::size_t> edge_indices;
};

struct TimelineResult {
    std::string target_id;
    std::uint64_t version;
    std::int64_t at_unix_ms;
    NodeStatus status;
    std::vector<std::string> observed_anchor_ids;
    std::vector<std::string> possible_observed_anchor_ids;
    std::optional<std::string> previous_anchor_id;
    std::optional<std::string> next_anchor_id;
    std::optional<std::string> segment_id;
    std::vector<Anchor> anchors_in_range;
    std::vector<TrajectorySegment> segments_in_range;
    std::optional<TimelineObservation> previous_observation;
    std::optional<TimelineObservation> next_observation;
    std::vector<std::string> active_segment_ids;
    std::vector<TimelinePathLocation> path_locations;
};

// Every rebuild uses a complete authoritative observation set for one target.
// Durable snapshots remain queryable across restarts. A Get reference in durable
// mode is valid until the next different version/target query or rebuild.
class TrajectoryStore {
public:
    explicit TrajectoryStore(State* state = nullptr) : state_(state) {}
    void SetAssociationRevisionReader(std::function<std::uint64_t(const std::string&)> reader) {
        association_revision_reader_ = std::move(reader);
    }
    std::uint64_t LatestVersion(const std::string& target_id) const;
    TrajectoryRevision Rebuild(const std::string& target_id,
                               const std::vector<SightingEvidence>& sightings,
                               const CameraGraph& graph,
                               const TrajectoryOptions& options,
                               const std::string& reason,
                               std::optional<std::uint64_t> expected_version = {});
    // Internal intake optimization. Caller must supply the association store's
    // pure-new-record proof, never an externally asserted observation delta.
    std::optional<TrajectoryRevision> TryAppend(const SightingEvidence& sighting,
                               std::uint64_t previous_association_revision,
                               const CameraGraph& graph, const TrajectoryOptions& options,
                               const std::string& reason, std::uint64_t expected_version);
    TrajectoryReference Reference(const std::string& target_id, std::uint64_t version) const;
    const TrajectorySnapshot& Get(const std::string& target_id,
                                  std::uint64_t version = 0) const;
    TimelineResult Query(const std::string& target_id, std::uint64_t version,
                         std::int64_t at_unix_ms, std::int64_t from_unix_ms,
                         std::int64_t to_unix_ms) const;
    // Retention rewrites every persisted version from its remaining evidence,
    // preserving the current version/head scalar and deleting empty older
    // versions. The caller owns the transaction and shared-object mark/sweep;
    // explicit IDs cover quarantine expiry. Legacy unqualified sightings have
    // no quarantine provenance in snapshots and are removed. Dry runs never write.
    nlohmann::json Prune(std::int64_t cutoff_unix_ms, bool dry_run,
                        const std::vector<std::string>& expired_observation_ids = {});

private:
    struct TimedAnchor {
        std::int64_t time;
        std::size_t anchor_index;
        std::size_t evidence_index;
    };
    struct StoredSnapshot {
        TrajectorySnapshot snapshot;
        std::vector<TimedAnchor> timed_anchors;
    };
    struct History {
        std::deque<StoredSnapshot> snapshots;
    };
    static std::vector<TimedAnchor> BuildTimelineIndex(const TrajectorySnapshot& snapshot);
    const StoredSnapshot& Find(const std::string& target_id, std::uint64_t version) const;
    std::map<std::string, History> histories_;
    State* state_ = nullptr;
    // Durable mode keeps one decoded snapshot; historical versions are cold.
    mutable std::optional<StoredSnapshot> cold_cache_;
    std::function<std::uint64_t(const std::string&)> association_revision_reader_;
};

}  // namespace face_trajectory
