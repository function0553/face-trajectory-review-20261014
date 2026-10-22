#pragma once

#include "face_trajectory/gallery.hpp"
#include "face_trajectory/trajectory.hpp"

#include <set>
#include <tuple>

namespace face_trajectory {

struct FaceObservation {
    std::string observation_id, source_id, camera_id, frame_id;
    std::int64_t captured_at_unix_ms, clock_uncertainty_ms;
    TimeSource time_source;
    std::string clock_domain;
    FeatureSpaceContract feature_space;
    Embedding embedding;
    double detection_score, face_size_px;
    std::string local_track_id, track_session_id, reentry_evidence_id;
};

struct AssociationPolicy {
    std::string policy_id;
    // Explicit deployment inputs; no universal recognition threshold is implied.
    double match_threshold, new_target_threshold, min_margin, template_update_threshold;
    double max_template_similarity;
    double min_detection_score = 0.5, min_face_size_px = 32;
    std::int64_t max_clock_uncertainty_ms = 1000;
    std::size_t max_templates_per_target = 8;
    bool require_topology = false;
    bool topology_is_complete = false;
    std::vector<std::pair<std::string, std::string>> overlapping_cameras;
};

enum class CameraCompatibility { Compatible, Unknown, Conflict };

struct AssociationCandidate {
    PersonHit hit;
    CameraCompatibility compatibility;
};

struct AssociationResult {
    std::string observation_id, decision, reason, policy_id, graph_version;
    std::uint64_t version = 0;
    std::optional<std::string> target_id;
    std::optional<SightingEvidence> sighting;
    std::vector<AssociationCandidate> candidates;
    std::vector<std::string> affected_target_ids;
    bool template_added = false;
    // Exact score bound for unclassified lower candidates, when it suffices
    // to prove the identity margin without visiting every person's history.
    std::optional<float> remaining_similarity_upper_bound;
};

// Internal proof from the immediately preceding Observe call. A retry,
// reconsideration or correction cannot manufacture an append proof.
struct AssociationAppend {
    SightingEvidence sighting;
    std::uint64_t previous_target_revision = 0;
    std::uint64_t target_revision = 0;
};

// One gallery per activity runtime, including explicitly enrolled templates and
// that activity's automatically created anonymous IDs. State scopes and caches
// isolate activity history without changing business person identifiers.
// No 30-second identity expiry. Only accepted observations update identity templates.
class AssociationStore {
public:
    explicit AssociationStore(Gallery& gallery, State* state = nullptr);
    AssociationResult Observe(FaceObservation observation, const AssociationPolicy& policy,
                              const CameraGraph& graph, bool reconsider = false);
    AssociationResult Correct(const std::string& observation_id, const std::string& target_id,
                              const std::string& evidence_id);
    AssociationResult Retract(const std::string& observation_id, const std::string& evidence_id);
    std::vector<SightingEvidence> Sightings(const std::string& target_id) const;
    std::uint64_t TargetRevision(const std::string& target_id) const;
    std::optional<AssociationResult> Result(const std::string& observation_id) const;
    std::optional<AssociationAppend> LastAppend() const { return last_append_; }

private:
    struct Record {
        FaceObservation observation;
        AssociationResult result;
    };
    struct OwnedTemplate {
        std::string observation_id, template_id;
        double quality;
    };
    using TrackKey = std::tuple<std::string, std::string, std::string, std::string>;
    std::optional<Record> LoadRecord(const std::string& id) const;
    void SaveRecord(const Record& record);
    struct CaptureMetadata {
        std::string observation_id, source_id, camera_id, frame_id, local_track_id, track_session_id;
        std::int64_t captured_at_unix_ms, clock_uncertainty_ms;
    };
    std::vector<CaptureMetadata> TargetMetadata(const std::string& target) const;
    struct TargetSummary {
        std::uint64_t revision = 0;
        std::map<std::string, std::uint64_t> camera_counts;
    };
    // Derived indices share the observation transaction. Missing/stale legacy
    // indices are rebuilt from complete authoritative observations.
    TargetSummary EnsureTargetIndex(const std::string& target) const;
    void SaveTargetSummary(const std::string& target, const TargetSummary& summary) const;
    std::map<std::string, std::uint64_t> TrackTargetCounts(const std::string& track) const;
    void SaveTemplates(const std::string& target);
    CameraCompatibility Compatible(const FaceObservation& observation, const std::string& target_id,
                                   const AssociationPolicy& policy, const CameraGraph& graph) const;
    void Assign(Record& record, const std::string& target_id, const std::string& evidence_id,
                const AssociationPolicy& policy, bool update_template);
    void ForgetTemplate(const std::string& observation_id, const std::string& target_id);
    Gallery& gallery_;
    std::map<std::string, Record> records_;
    std::map<std::string, std::set<std::string>> target_observations_;
    std::map<std::string, std::vector<OwnedTemplate>> templates_;
    std::map<TrackKey, std::set<std::string>> track_observations_;
    std::map<std::string, AssociationPolicy> policies_;
    std::map<std::string, CameraGraph> graphs_;
    std::uint64_t version_ = 0;
    std::map<std::string, std::uint64_t> target_revisions_;
    std::optional<AssociationAppend> last_append_;
    State* state_ = nullptr;
};

}  // namespace face_trajectory
