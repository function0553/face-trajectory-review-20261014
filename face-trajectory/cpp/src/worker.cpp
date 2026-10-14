#include "face_trajectory/gallery.hpp"
#include "face_trajectory/association.hpp"
#include "face_trajectory/trajectory.hpp"
#include "face_trajectory/visits.hpp"
#include "face_trajectory/state.hpp"
#include "face_trajectory/geometry.hpp"
#include "face_trajectory/site_context.hpp"
#include "face_trajectory/clock_adapter.hpp"
#include "face_trajectory/reachability.hpp"
#include "face_trajectory/point_trajectory.hpp"
#include "face_trajectory/monitoring.hpp"
#include "face_trajectory/activity.hpp"
#include "face_trajectory/retention.hpp"
#include "face_trajectory/p1_retention.hpp"

#include <nlohmann/json.hpp>

#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>
#include <memory>
#include <random>
#include <chrono>
#ifdef _WIN32
#define NOMINMAX
#include <windows.h>
#endif

namespace {

using face_trajectory::Embedding;
using face_trajectory::FeatureSpaceContract;
using face_trajectory::Gallery;
using face_trajectory::TargetLinkStatus;
using face_trajectory::SightingEvidence;
using face_trajectory::TimeSource;
using face_trajectory::TrajectoryStore;
using nlohmann::json;

Embedding ReadEmbedding(const json& value) {
    if (!value.is_array() || value.size() != face_trajectory::kEmbeddingDim) {
        throw std::invalid_argument("embedding must contain exactly 512 numbers");
    }
    Embedding embedding{};
    for (std::size_t i = 0; i < embedding.size(); ++i) {
        if (!value[i].is_number()) throw std::invalid_argument("embedding must contain only numbers");
        embedding[i] = value[i].get<float>();
    }
    return embedding;
}

FeatureSpaceContract ReadSpace(const json& value) {
    return {value.at("recognition_model_sha256").get<std::string>(),
            value.value("detector_model_sha256", std::string{}),
            value.value("detector_profile", std::string{}),
            value.at("alignment_version").get<std::string>(),
            value.at("preprocessing_version").get<std::string>(),
            value.at("output_version").get<std::string>()};
}

std::int64_t ReadInt64(const json& value) {
    if (value.is_number_unsigned()) {
        const auto number = value.get<std::uint64_t>();
        if (number > static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max())) {
            throw std::invalid_argument("integer is outside int64 range");
        }
        return static_cast<std::int64_t>(number);
    }
    if (value.is_number_integer()) return value.get<std::int64_t>();
    throw std::invalid_argument("integer value is required");
}

TargetLinkStatus ReadStatus(const std::string& value) {
    if (value == "CONFIRMED") return TargetLinkStatus::Confirmed;
    if (value == "PROVISIONAL") return TargetLinkStatus::Provisional;
    if (value == "UNASSIGNED") return TargetLinkStatus::Unassigned;
    if (value == "CONFLICT") return TargetLinkStatus::Conflict;
    throw std::invalid_argument("unsupported link_status");
}

TimeSource ReadTimeSource(const std::string& value) {
    if (value == "UTC_CAPTURE") return TimeSource::UtcCapture;
    if (value == "MEDIA_OFFSET") return TimeSource::MediaOffset;
    if (value == "RECEIVED_AT") return TimeSource::ReceivedAt;
    if (value == "UNKNOWN") return TimeSource::Unknown;
    throw std::invalid_argument("unsupported time_source");
}

std::vector<SightingEvidence> ReadSightings(const json& value, bool legacy = false) {
    if (!value.is_array()) throw std::invalid_argument("sightings must be an array");
    std::vector<SightingEvidence> result;
    result.reserve(value.size());
    for (const auto& item : value) {
        const auto target_id = legacy && !item.contains("target_id")
            ? item.at("person_entity_id").get<std::string>()
            : item.at("target_id").get<std::string>();
        const auto attribution_id = legacy && !item.contains("attribution_evidence_id")
            ? item.at("resolution_id").get<std::string>()
            : item.at("attribution_evidence_id").get<std::string>();
        const auto status = legacy && !item.contains("link_status")
            ? item.at("identity_status").get<std::string>()
            : item.at("link_status").get<std::string>();
        result.push_back({item.at("observation_id").get<std::string>(),
                          target_id,
                          item.at("camera_id").get<std::string>(),
                          attribution_id,
                          ReadInt64(item.at("captured_at_unix_ms")),
                          ReadInt64(item.at("clock_uncertainty_ms")),
                          ReadStatus(status),
                          ReadTimeSource(item.at("time_source").get<std::string>()),
                          legacy ? item.value("source_id", std::string("legacy"))
                                 : item.at("source_id").get<std::string>(),
                          legacy ? item.value("clock_domain", std::string("UTC"))
                                 : item.at("clock_domain").get<std::string>(),
                          item.value("reentry_evidence_id", std::string{})});
    }
    return result;
}

json TimeBoundsToJson(const face_trajectory::CaptureTimeBounds& bounds) {
    return {{"first_earliest_unix_ms", bounds.first_earliest_unix_ms},
            {"first_latest_unix_ms", bounds.first_latest_unix_ms},
            {"last_earliest_unix_ms", bounds.last_earliest_unix_ms},
            {"last_latest_unix_ms", bounds.last_latest_unix_ms}};
}

json VisitsToJson(const std::vector<face_trajectory::CameraVisit>& visits) {
    json result = json::array();
    for (const auto& visit : visits) {
        result.push_back({{"visit_id", visit.visit_id},
                          {"target_id", visit.target_id},
                          {"person_entity_id", visit.target_id},
                          {"camera_id", visit.camera_id},
                          {"first_seen_unix_ms", visit.first_seen_unix_ms},
                          {"last_seen_unix_ms", visit.last_seen_unix_ms},
                          {"first_seen_clock_uncertainty_ms", visit.first_seen_clock_uncertainty_ms},
                          {"last_seen_clock_uncertainty_ms", visit.last_seen_clock_uncertainty_ms},
                          {"max_clock_uncertainty_ms", visit.max_clock_uncertainty_ms},
                          {"time_bounds", TimeBoundsToJson(visit.time_bounds)},
                          {"observation_ids", visit.observation_ids},
                          {"attribution_evidence_ids", visit.attribution_evidence_ids},
                          {"resolution_ids", visit.attribution_evidence_ids},
                          {"source_ids", visit.source_ids},
                          {"reentry_evidence_ids", visit.reentry_evidence_ids},
                          {"boundary_observation_id", visit.boundary_observation_id}});
    }
    return result;
}

std::size_t ReadSize(const json& value) {
    const auto number = ReadInt64(value);
    if (number < 1 || static_cast<std::uint64_t>(number) >
            static_cast<std::uint64_t>(std::numeric_limits<std::size_t>::max())) {
        throw std::invalid_argument("positive size is required");
    }
    return static_cast<std::size_t>(number);
}

face_trajectory::TrajectoryOptions ReadOptions(const json& request) {
    face_trajectory::TrajectoryOptions options;
    if (!request.contains("options")) return options;
    const auto& value = request.at("options");
    if (value.contains("non_overlapping_cameras"))
        options.non_overlapping_cameras = value.at("non_overlapping_cameras").get<std::vector<std::pair<std::string, std::string>>>();
    if (value.contains("max_visit_gap_ms")) options.max_visit_gap_ms = ReadInt64(value.at("max_visit_gap_ms"));
    if (value.contains("max_clock_uncertainty_ms")) options.max_clock_uncertainty_ms = ReadInt64(value.at("max_clock_uncertainty_ms"));
    if (value.contains("session_gap_ms")) options.session_gap_ms = ReadInt64(value.at("session_gap_ms"));
    if (value.contains("max_hops")) options.max_hops = ReadSize(value.at("max_hops"));
    if (value.contains("max_expansions")) options.max_expansions = ReadSize(value.at("max_expansions"));
    if (value.contains("max_candidates")) options.max_candidates = ReadSize(value.at("max_candidates"));
    if (value.contains("backup_score_margin")) options.backup_score_margin = value.at("backup_score_margin").get<double>();
    if (value.contains("travel_time_weight_per_second")) {
        options.travel_time_weight_per_second = value.at("travel_time_weight_per_second").get<double>();
    }
    return options;
}

face_trajectory::CameraGraph ReadGraph(const json& value) {
    face_trajectory::CameraGraph graph;
    graph.version = value.at("version").get<std::string>();
    for (const auto& item : value.at("edges")) {
        face_trajectory::CameraEdge edge{
            item.at("from_camera").get<std::string>(),
            item.at("to_camera").get<std::string>(),
            ReadInt64(item.at("min_travel_ms")), ReadInt64(item.at("max_travel_ms")),
            item.at("cost").get<double>(), std::nullopt, std::nullopt,
            item.value("edge_id", std::string{})};
        if (item.contains("open_from_unix_ms")) edge.open_from_unix_ms = ReadInt64(item.at("open_from_unix_ms"));
        if (item.contains("open_until_unix_ms")) edge.open_until_unix_ms = ReadInt64(item.at("open_until_unix_ms"));
        graph.edges.push_back(std::move(edge));
    }
    return graph;
}

face_trajectory::FaceObservation ReadFaceObservation(const json& o) {
    return {o.at("observation_id").get<std::string>(), o.at("source_id").get<std::string>(),
        o.at("camera_id").get<std::string>(), o.at("frame_id").get<std::string>(),
        ReadInt64(o.at("captured_at_unix_ms")), ReadInt64(o.at("clock_uncertainty_ms")),
        ReadTimeSource(o.at("time_source").get<std::string>()), o.at("clock_domain").get<std::string>(),
        ReadSpace(o.at("feature_space")), ReadEmbedding(o.at("embedding")),
        o.at("detection_score").get<double>(), o.at("face_size_px").get<double>(),
        o.value("local_track_id", std::string{}), o.value("track_session_id", std::string{}),
        o.value("reentry_evidence_id", std::string{})};
}

face_trajectory::AssociationPolicy ReadAssociationPolicy(const json& p) {
    face_trajectory::AssociationPolicy policy;
    policy.policy_id = p.at("policy_id").get<std::string>();
    policy.match_threshold = p.at("match_threshold").get<double>();
    policy.new_target_threshold = p.at("new_target_threshold").get<double>();
    policy.min_margin = p.at("min_margin").get<double>();
    policy.template_update_threshold = p.at("template_update_threshold").get<double>();
    policy.max_template_similarity = p.at("max_template_similarity").get<double>();
    if (p.contains("min_detection_score")) policy.min_detection_score = p.at("min_detection_score").get<double>();
    if (p.contains("min_face_size_px")) policy.min_face_size_px = p.at("min_face_size_px").get<double>();
    if (p.contains("max_clock_uncertainty_ms")) policy.max_clock_uncertainty_ms = ReadInt64(p.at("max_clock_uncertainty_ms"));
    if (p.contains("max_templates_per_target")) policy.max_templates_per_target = ReadSize(p.at("max_templates_per_target"));
    policy.require_topology = p.value("require_topology", false);
    policy.topology_is_complete = p.value("topology_is_complete", false);
    if (p.contains("overlapping_cameras")) {
        for (const auto& pair : p.at("overlapping_cameras")) {
            if (!pair.is_array() || pair.size() != 2) throw std::invalid_argument("camera overlap must be a pair");
            policy.overlapping_cameras.emplace_back(pair[0].get<std::string>(), pair[1].get<std::string>());
        }
    }
    return policy;
}

const char* StatusName(face_trajectory::NodeStatus status) {
    switch (status) {
        case face_trajectory::NodeStatus::Observed: return "OBSERVED";
        case face_trajectory::NodeStatus::Inferred: return "INFERRED";
        case face_trajectory::NodeStatus::Unknown: return "UNKNOWN";
    }
    return "UNKNOWN";
}

const char* LinkStatusName(TargetLinkStatus status) {
    switch (status) {
        case TargetLinkStatus::Confirmed: return "CONFIRMED";
        case TargetLinkStatus::Provisional: return "PROVISIONAL";
        case TargetLinkStatus::Unassigned: return "UNASSIGNED";
        case TargetLinkStatus::Conflict: return "CONFLICT";
    }
    return "UNASSIGNED";
}

const char* TimeSourceName(TimeSource source) {
    switch (source) {
        case TimeSource::UtcCapture: return "UTC_CAPTURE";
        case TimeSource::MediaOffset: return "MEDIA_OFFSET";
        case TimeSource::ReceivedAt: return "RECEIVED_AT";
        case TimeSource::Unknown: return "UNKNOWN";
    }
    return "UNKNOWN";
}

json EvidenceToJson(const SightingEvidence& sighting) {
    return {{"observation_id", sighting.observation_id},
            {"target_id", sighting.target_id}, {"camera_id", sighting.camera_id},
            {"attribution_evidence_id", sighting.attribution_evidence_id},
            {"captured_at_unix_ms", sighting.captured_at_unix_ms},
            {"clock_uncertainty_ms", sighting.clock_uncertainty_ms},
            {"link_status", LinkStatusName(sighting.link_status)},
            {"time_source", TimeSourceName(sighting.time_source)},
            {"source_id", sighting.source_id}, {"clock_domain", sighting.clock_domain},
            {"reentry_evidence_id", sighting.reentry_evidence_id}};
}

const char* CompatibilityName(face_trajectory::CameraCompatibility value) {
    switch (value) {
        case face_trajectory::CameraCompatibility::Compatible: return "COMPATIBLE";
        case face_trajectory::CameraCompatibility::Unknown: return "UNKNOWN";
        case face_trajectory::CameraCompatibility::Conflict: return "CONFLICT";
    }
    return "UNKNOWN";
}

json AssociationToJson(const face_trajectory::AssociationResult& result) {
    json candidates = json::array();
    for (const auto& candidate : result.candidates) {
        candidates.push_back({{"target_id", candidate.hit.person_entity_id},
            {"template_id", candidate.hit.template_id}, {"similarity", candidate.hit.similarity},
            {"camera_compatibility", CompatibilityName(candidate.compatibility)}});
    }
    return {{"observation_id", result.observation_id}, {"decision", result.decision},
        {"reason", result.reason}, {"version", result.version}, {"policy_id", result.policy_id},
        {"graph_version", result.graph_version},
        {"target_id", result.target_id ? json(*result.target_id) : json(nullptr)},
        {"sighting", result.sighting ? EvidenceToJson(*result.sighting) : json(nullptr)},
        {"candidates", candidates}, {"affected_target_ids", result.affected_target_ids},
        {"template_added", result.template_added},
        {"remaining_similarity_upper_bound", result.remaining_similarity_upper_bound ? json(*result.remaining_similarity_upper_bound) : json(nullptr)}};
}

json RevisionToJson(const face_trajectory::TrajectoryRevision& revision) {
    return {{"target_id", revision.target_id}, {"previous_version", revision.previous_version},
            {"version", revision.version}, {"reason", revision.reason},
            {"affected_segment_ids", revision.affected_segment_ids}};
}

json GraphToJson(const face_trajectory::CameraGraph& graph) {
    json edges = json::array();
    for (const auto& edge : graph.edges) {
        json item = {{"from_camera", edge.from_camera}, {"to_camera", edge.to_camera},
                     {"min_travel_ms", edge.min_travel_ms},
                     {"max_travel_ms", edge.max_travel_ms}, {"cost", edge.cost}};
        if (!edge.edge_id.empty()) item["edge_id"] = edge.edge_id;
        if (edge.open_from_unix_ms) item["open_from_unix_ms"] = *edge.open_from_unix_ms;
        if (edge.open_until_unix_ms) item["open_until_unix_ms"] = *edge.open_until_unix_ms;
        edges.push_back(std::move(item));
    }
    return {{"version", graph.version}, {"edges", edges}};
}

json OptionsToJson(const face_trajectory::TrajectoryOptions& options) {
    return {{"max_visit_gap_ms", options.max_visit_gap_ms},
            {"max_clock_uncertainty_ms", options.max_clock_uncertainty_ms},
            {"session_gap_ms", options.session_gap_ms},
            {"max_hops", options.max_hops},
            {"max_expansions", options.max_expansions},
            {"max_candidates", options.max_candidates},
            {"backup_score_margin", options.backup_score_margin},
            {"travel_time_weight_per_second", options.travel_time_weight_per_second},
            {"non_overlapping_cameras", options.non_overlapping_cameras}};
}

json AnchorToJson(const face_trajectory::Anchor& anchor) {
    return {{"anchor_id", anchor.anchor_id}, {"visit_id", anchor.visit_id},
            {"camera_id", anchor.camera_id},
            {"first_seen_unix_ms", anchor.first_seen_unix_ms},
            {"last_seen_unix_ms", anchor.last_seen_unix_ms},
            {"first_seen_clock_uncertainty_ms", anchor.first_seen_clock_uncertainty_ms},
            {"last_seen_clock_uncertainty_ms", anchor.last_seen_clock_uncertainty_ms},
            {"time_bounds", TimeBoundsToJson(anchor.time_bounds)}};
}

json CandidateToJson(const face_trajectory::PathCandidate& candidate) {
    json nodes = json::array();
    for (const auto& node : candidate.nodes) {
        nodes.push_back({{"camera_id", node.camera_id}, {"status", StatusName(node.status)},
                         {"earliest_unix_ms", node.earliest_unix_ms},
                         {"latest_unix_ms", node.latest_unix_ms}});
    }
    json bounds = json::array();
    for (const auto& bound : candidate.edge_time_bounds) {
        bounds.push_back({{"earliest_unix_ms", bound.earliest_unix_ms},
                          {"latest_unix_ms", bound.latest_unix_ms}});
    }
    return {{"nodes", nodes}, {"edge_ids", candidate.edge_ids}, {"edge_time_bounds", bounds},
            {"score", candidate.score},
            {"edge_cost", candidate.edge_cost}, {"time_penalty", candidate.time_penalty}};
}

json SegmentToJson(const face_trajectory::TrajectorySegment& segment) {
    json backups = json::array();
    for (const auto& backup : segment.backups) backups.push_back(CandidateToJson(backup));
    return {{"segment_id", segment.segment_id},
            {"graph_version", segment.graph_version},
            {"from_anchor_id", segment.from_anchor_id},
            {"to_anchor_id", segment.to_anchor_id},
            {"from_unix_ms", segment.from_unix_ms},
            {"to_unix_ms", segment.to_unix_ms},
            {"status", StatusName(segment.status)},
            {"search_complete", segment.search_complete},
            {"unique_path", segment.unique_path},
            {"primary", segment.primary ? CandidateToJson(*segment.primary) : json(nullptr)},
            {"backups", backups}};
}

json TimelineObservationToJson(const std::optional<face_trajectory::TimelineObservation>& value) {
    if (!value) return nullptr;
    return {{"anchor_id", value->anchor_id}, {"observation", EvidenceToJson(value->observation)}};
}

json SnapshotToJson(const face_trajectory::TrajectorySnapshot& snapshot) {
    json evidence = json::array();
    for (const auto& sighting : snapshot.evidence) evidence.push_back(EvidenceToJson(sighting));
    json anchors = json::array();
    for (const auto& anchor : snapshot.anchors) anchors.push_back(AnchorToJson(anchor));
    json sessions = json::array();
    for (const auto& session : snapshot.sessions) {
        sessions.push_back({{"session_id", session.session_id},
                            {"first_seen_unix_ms", session.first_seen_unix_ms},
                            {"last_seen_unix_ms", session.last_seen_unix_ms},
                            {"first_seen_clock_uncertainty_ms", session.first_seen_clock_uncertainty_ms},
                            {"last_seen_clock_uncertainty_ms", session.last_seen_clock_uncertainty_ms},
                            {"time_bounds", TimeBoundsToJson(session.time_bounds)},
                            {"anchor_ids", session.anchor_ids},
                            {"segment_ids", session.segment_ids}});
    }
    json segments = json::array();
    for (const auto& segment : snapshot.segments) segments.push_back(SegmentToJson(segment));
    return {{"target_id", snapshot.target_id}, {"version", snapshot.version}, {"association_revision", snapshot.association_revision},
            {"graph_version", snapshot.graph.version},
            {"graph", GraphToJson(snapshot.graph)},
            {"options", OptionsToJson(snapshot.options)},
            {"revision", RevisionToJson(snapshot.revision)},
            {"evidence", evidence}, {"visits", VisitsToJson(snapshot.visits)},
            {"anchors", anchors},
            {"sessions", sessions}, {"segments", segments},
            {"rejected_observation_ids", snapshot.rejected_observation_ids}};
}

std::optional<std::uint64_t> ExpectedVersion(const json& request) {
    if (!request.contains("expected_version")) return {};
    const auto version = ReadInt64(request.at("expected_version"));
    if (version < 0) throw std::invalid_argument("expected_version must be nonnegative");
    return static_cast<std::uint64_t>(version);
}

void ValidateAssociatedSightings(const std::string& target, const std::vector<SightingEvidence>& supplied,
                                const face_trajectory::AssociationStore& associations) {
    if (associations.TargetRevision(target) == 0) return;
    const auto canonical = [](const auto& sightings) {
        std::map<std::string, json> by_id;
        for (const auto& sighting : sightings) {
            const auto value = EvidenceToJson(sighting);
            auto [it, inserted] = by_id.emplace(sighting.observation_id, value);
            if (!inserted && it->second != value) throw std::invalid_argument("conflicting duplicate observation_id");
        }
        return by_id;
    };
    if (canonical(supplied) != canonical(associations.Sightings(target)))
        throw std::runtime_error("SOURCE_CONFLICT: supplied set differs from current association; use association_rebuild");
}

json SnapshotFreshness(const face_trajectory::TrajectorySnapshot& snapshot, TrajectoryStore& trajectories,
                       const face_trajectory::AssociationStore& associations, bool historical) {
    const auto current_revision = associations.TargetRevision(snapshot.target_id);
    const auto latest_version = trajectories.LatestVersion(snapshot.target_id);
    const bool matches = snapshot.association_revision == current_revision;
    return {{"snapshot_association_revision", snapshot.association_revision},
        {"current_association_revision", current_revision}, {"latest_version", latest_version},
        {"association_matches_current", matches}, {"requested_scope", historical ? "HISTORICAL" : "CURRENT"},
        {"valid_for_current_use", matches && snapshot.version == latest_version},
        {"status", !matches ? "STALE_ASSOCIATION" : snapshot.version != latest_version ? "HISTORICAL" : "CURRENT"}};
}

void AddResultFreshness(json& response, const json& request, TrajectoryStore& trajectories,
                        const face_trajectory::AssociationStore& associations) {
    const auto op = request.value("op", "");
    if (op == "association_observe" || op == "association_correct") {
        auto effects = json::array();
        for (const auto& target_json : response.at("association").at("affected_target_ids")) {
            const auto target = target_json.get<std::string>();
            const auto head = trajectories.LatestVersion(target);
            const auto revision = associations.TargetRevision(target);
            const bool required = head == 0 || trajectories.Get(target, head).association_revision != revision;
            effects.push_back({{"target_id", target}, {"current_association_revision", revision},
                {"latest_trajectory_version", head}, {"rebuild_required", required}});
        }
        response["association"]["trajectory_effects"] = effects;
        return;
    }
    const bool rebuild = op == "association_rebuild" || op == "trajectory_rebuild" || op == "point_trajectory_rebuild";
    if (!rebuild && !(op=="monitoring_get" && request.value("include_point_trace",false)) && op != "trajectory_get" && op != "point_trace" && op != "trajectory_predict" &&
        op != "trajectory_geometry" && op != "timeline_query" && op != "point_prediction_compare") return;
    const auto target = request.at("target_id").get<std::string>();
    if(op=="monitoring_get" && trajectories.LatestVersion(target)==0)return;
    const auto version = rebuild ? response.at("revision").at("version").get<std::uint64_t>()
        : static_cast<std::uint64_t>(op!="monitoring_get" && request.contains("version") ? ReadInt64(request.at("version")) : 0);
    const auto freshness = SnapshotFreshness(trajectories.Get(target, version), trajectories, associations, !rebuild && version != 0);
    response["freshness"] = freshness;
    for (const auto* key : {"trajectory", "point_trace", "prediction", "geometry", "timeline", "comparison", "monitoring"})
        if (response.contains(key)) response[key]["freshness"] = freshness;
}

json MonitoringTime(face_trajectory::ClockAdapter& clocks, const json& request,
                    const std::string& camera = "", const std::string& source = "") {
    const json unavailable={{"status","HOLD"},{"reason_codes",json::array({"CAPTURE_TIME_UNAVAILABLE"})},
        {"provenance",{{"time_policy","CAMERA_CAPTURE_60S"},{"camera_id",camera},{"profile_version",""},
            {"raw_time",""},{"offset_convention","POSITIVE_CAMERA_AHEAD"}}}};
    if(!request.contains("time_record") || request.at("time_record").is_null()) return unavailable;
    auto record=request.at("time_record");
    if(!record.is_object()) throw std::invalid_argument("time_record must be an object or null");
    if(!camera.empty()) {
        if(record.contains("camera_id") && record.at("camera_id")!=camera)
            throw std::invalid_argument("time_record camera differs from observation");
        record["camera_id"]=camera;
    }
    if(!source.empty() && record.contains("source_id") && record.at("source_id")!=source)
        throw std::invalid_argument("time_record source differs from observation");
    if(record.contains("time_policy") && record.at("time_policy")!="CAMERA_CAPTURE_60S")
        throw std::invalid_argument("monitoring time_policy is fixed to CAMERA_CAPTURE_60S");
    for(const auto* field:{"profile_version","camera_id","raw_time"})
        if(!record.contains(field) || record.at(field).is_null() || record.at(field)=="") return unavailable;
    record["time_policy"]="CAMERA_CAPTURE_60S";
    auto normalized=clocks.Normalize(record);
    if(normalized.value("status","")=="NORMALIZED" && request.contains("_trusted_clock")) {
        const auto capture=normalized.at("captured_at_unix_ms").get<std::int64_t>();
        const auto now=request.at("_trusted_clock").at("now_unix_ms").get<std::int64_t>();
        if(capture>now && capture-now>face_trajectory::ClockAdapter::CameraCaptureErrorLimitMs){
            normalized["status"]="HOLD";normalized["reason_codes"]=json::array({"FUTURE_CAPTURE_TIME_OVER_60S"});
        }
    }
    return normalized;
}

json Execute(Gallery& gallery, TrajectoryStore& trajectories,
             face_trajectory::AssociationStore& associations, face_trajectory::GeometryStore& geometry,
             face_trajectory::SiteContextStore& site_context, face_trajectory::ClockAdapter& clocks,
             face_trajectory::PointGraphStore& point_graphs, face_trajectory::MonitoringStore& monitoring,
             const json& request) {
    const auto op = request.at("op").get<std::string>();
    if(op=="monitoring_person_put") return monitoring.PutPerson(request,gallery);
    if(op=="monitoring_get") {
        auto result=monitoring.Get(request.at("target_id"),associations);
        if(request.value("include_point_trace",false)) {
            const auto target=request.at("target_id").get<std::string>();
            if(trajectories.LatestVersion(target)==0) {
                result["point_connections"]=json::array();result["point_geometry"]={{"status","NO_CONFIRMED_OBSERVATIONS"}};
            } else {
                const auto snapshot=trajectories.Get(target,0);
                const auto packet=point_graphs.Get(snapshot.graph.version);
                const auto trace=face_trajectory::CameraPointTrace(snapshot,packet,site_context);
                result["point_connections"]=trace.at("connections");
                result["point_geometry"]={{"version",snapshot.version},{"scene_version",packet.at("scene_version")},
                    {"graph_version",snapshot.graph.version},{"coordinate_frame",trace.at("coordinate_frame")},
                    {"topology_verified",trace.at("topology_verified")},{"topology_is_complete",trace.at("topology_is_complete")},
                    {"physical_path_observed",false}};
            }
        }
        return {{"ok",true},{"monitoring",result}};
    }
    if(op=="monitoring_gate_event") {
        monitoring.RequireDurable();
        return monitoring.PutGate(request,MonitoringTime(clocks,request),associations);
    }
    if(op=="monitoring_observe") {
        monitoring.RequireDurable();
        if(const auto previous=monitoring.ObservationReceipt(request)) return *previous;
        auto observation=request.at("observation");
        if(request.contains("target_id") || observation.contains("target_id") || observation.contains("person_id"))
            throw std::invalid_argument("monitoring observation cannot assert target identity");
        const auto id=observation.at("observation_id").get<std::string>();
        const auto camera=observation.at("camera_id").get<std::string>();
        const auto source=observation.at("source_id").get<std::string>();
        if(id.empty() || camera.empty() || source.empty() || observation.at("frame_id").get<std::string>().empty())
            throw std::invalid_argument("observation/source/camera/frame ID is required");
        Gallery::ValidateQuery(ReadSpace(observation.at("feature_space")),ReadEmbedding(observation.at("embedding")));
        auto policy=ReadAssociationPolicy(request.at("policy"));
        if(!request.at("policy").contains("max_clock_uncertainty_ms")) policy.max_clock_uncertainty_ms=60000;
        auto options=ReadOptions(request);
        if(!request.contains("options") || !request.at("options").contains("max_clock_uncertainty_ms"))
            options.max_clock_uncertainty_ms=60000;
        if(policy.max_clock_uncertainty_ms<0 || policy.max_clock_uncertainty_ms>60000 ||
           options.max_clock_uncertainty_ms<0 || options.max_clock_uncertainty_ms>60000)
            throw std::invalid_argument("monitoring clock policy must be within 0..60000");
        const auto graph=ReadGraph(request.at("graph"));
        auto normalized=MonitoringTime(clocks,request,camera,source);
        auto reasons=normalized.at("reason_codes");
        if(normalized.at("status")=="NORMALIZED" && ReadInt64(normalized.at("clock_uncertainty_ms"))>
           std::min(policy.max_clock_uncertainty_ms,options.max_clock_uncertainty_ms))
            reasons.push_back("CLOCK_UNCERTAINTY_EXCEEDS_POLICY");
        json response={{"ok",true},{"observation_id",id},{"status","HOLD"},{"normalization",normalized},
            {"time_admitted",reasons.empty()},
            {"receipt_scope","ORIGINAL_TRANSACTION"},
            {"reason_codes",reasons},{"association",nullptr},{"monitoring",nullptr},{"trajectories",json::array()}};
        if(!reasons.empty()) { monitoring.SaveObservation(request,response); return response; }
        for(const auto* field:{"captured_at_unix_ms","clock_uncertainty_ms","time_source","clock_domain"})
            observation[field]=normalized.at(field);
        const auto result=associations.Observe(ReadFaceObservation(observation),policy,graph,false);
        const auto append=associations.LastAppend();
        response["association"]=AssociationToJson(result);
        if(result.sighting) response["status"]="ACCEPTED";
        else response["reason_codes"].push_back("FACE_"+result.decision);
        monitoring.SaveObservation(request,response);
        if(result.sighting) {
            for(const auto& target:result.affected_target_ids) {
                const auto head=trajectories.LatestVersion(target);
                auto appended=append && append->sighting.target_id==target
                    ? trajectories.TryAppend(append->sighting,append->previous_target_revision,graph,options,"MONITORING_OBSERVATION:"+id,head)
                    : std::optional<face_trajectory::TrajectoryRevision>{};
                const auto revision=appended?*appended:trajectories.Rebuild(target,associations.Sightings(target),graph,options,
                    "MONITORING_OBSERVATION:"+id,head);
                const auto snapshot=trajectories.Reference(target,revision.version);
                response["trajectories"].push_back({{"revision",RevisionToJson(revision)},{"target_id",target},
                    {"version",revision.version},{"association_revision",snapshot.association_revision},
                    {"observation_count",snapshot.observation_count},{"anchor_count",snapshot.anchor_count},
                    {"graph_version",snapshot.graph_version},{"detail_query","POST /v1/trajectories/get"}});
            }
            response["monitoring"]=monitoring.ObserveSummary(*result.sighting,
                append?std::optional<std::uint64_t>(append->previous_target_revision):std::nullopt,associations);
        }
        monitoring.CompleteObservationReceipt(id,response);
        return response;
    }
    if (op == "clock_profile_put") { clocks.Put(request.at("profile")); return {{"ok", true}}; }
    if (op == "clock_normalize") return {{"ok", true}, {"normalization", clocks.Normalize(request)}};
    if (op == "point_graph_put") {
        point_graphs.Put(request.at("point_graph"), site_context);
        return {{"ok", true}, {"graph_version", request.at("point_graph").at("graph_version")}};
    }
    if (op == "point_graph_get") return {{"ok", true}, {"point_graph", point_graphs.Get(request.at("graph_version"))}};
    if (op == "point_trajectory_rebuild") {
        const auto target = request.at("target_id").get<std::string>();
        const auto graph_version = request.at("graph_version").get<std::string>();
        const auto packet = point_graphs.Get(graph_version);
        auto options = ReadOptions(request);
        if (!request.contains("options") || !request.at("options").contains("max_clock_uncertainty_ms"))
            options.max_clock_uncertainty_ms = face_trajectory::ClockAdapter::CameraCaptureErrorLimitMs;
        if (options.max_clock_uncertainty_ms < 0 ||
            options.max_clock_uncertainty_ms > face_trajectory::ClockAdapter::CameraCaptureErrorLimitMs)
            throw std::invalid_argument("point trajectory max_clock_uncertainty_ms must be within 0..60000");
        json admissions = json::array(), sightings = json::array();
        if (!request.at("records").is_array()) throw std::invalid_argument("records must be an array");
        std::map<std::string, std::string> unique_records;
        for (const auto& record : request.at("records")) {
            const auto id = record.at("observation_id").get<std::string>();
            if (id.empty()) throw std::invalid_argument("observation_id is required");
            const auto encoded = record.dump();
            const auto old = unique_records.find(id);
            if (old != unique_records.end()) {
                if (old->second != encoded) throw std::invalid_argument("conflicting duplicate raw observation_id");
                continue;
            }
            unique_records[id] = encoded;
            if (record.value("target_id", target) != target) throw std::invalid_argument("record target differs from rebuild target");
            json admission = {{"observation_id", id}, {"status", "HOLD"}, {"reason_codes", json::array()}};
            if (record.value("link_status", "UNASSIGNED") != "CONFIRMED" ||
                record.value("attribution_evidence_id", "").empty()) {
                admission["reason_codes"].push_back("EXISTING_IDENTITY_LINK_UNCONFIRMED");
                admissions.push_back(std::move(admission)); continue;
            }
            const auto camera = record.at("camera_id").get<std::string>();
            const auto context = site_context.CameraContext(packet.at("scene_version"), camera, "CAMERA_ID", 0);
            if (context.at("status") == "UNKNOWN_CAMERA" || context.at("status") == "AMBIGUOUS_DEVICE") {
                admission["reason_codes"].push_back("CAMERA_ID_UNRESOLVED");
                admissions.push_back(std::move(admission)); continue;
            }
            const auto normalized = clocks.Normalize({{"profile_version", record.at("clock_profile_version")},
                {"camera_id", camera}, {"raw_time", record.at("raw_time")}, {"time_policy", "CAMERA_CAPTURE_60S"}});
            admission["normalization"] = normalized;
            if (normalized.at("status") != "NORMALIZED") {
                admission["reason_codes"] = normalized.at("reason_codes");
                admissions.push_back(std::move(admission)); continue;
            }
            if (ReadInt64(normalized.at("clock_uncertainty_ms")) > options.max_clock_uncertainty_ms) {
                admission["reason_codes"].push_back("CLOCK_UNCERTAINTY_EXCEEDS_POLICY");
                admissions.push_back(std::move(admission)); continue;
            }
            json s = {{"observation_id", id}, {"target_id", target}, {"camera_id", camera},
                {"source_id", record.at("source_id")}, {"attribution_evidence_id", record.at("attribution_evidence_id")},
                {"link_status", "CONFIRMED"}, {"time_source", "UTC_CAPTURE"}, {"clock_domain", "UTC"},
                {"captured_at_unix_ms", normalized.at("captured_at_unix_ms")},
                {"clock_uncertainty_ms", normalized.at("clock_uncertainty_ms")}};
            if (record.contains("reentry_evidence_id")) s["reentry_evidence_id"] = record.at("reentry_evidence_id");
            admission["status"] = "ACCEPTED"; admission["sighting"] = s;
            admissions.push_back(std::move(admission)); sightings.push_back(std::move(s));
        }
        const auto admitted = ReadSightings(sightings);
        ValidateAssociatedSightings(target, admitted, associations);
        const auto revision = trajectories.Rebuild(target, admitted, point_graphs.Graph(graph_version),
            options, request.at("reason").get<std::string>(), ExpectedVersion(request));
        const auto& snapshot = trajectories.Get(target, revision.version);
        auto serialized = SnapshotToJson(snapshot);
        serialized["inference_scope"] = "CONDITIONAL_ON_REGISTERED_POINT_GRAPH";
        serialized["topology_verified"] = packet.at("topology_verified");
        serialized["topology_is_complete"] = packet.at("topology_is_complete");
        for (auto& segment : serialized["segments"]) {
            segment["unique_path_scope"] = "SUPPLIED_GRAPH_ONLY";
            segment["physical_path_observed"] = false;
        }
        return {{"ok", true}, {"admissions", admissions}, {"revision", RevisionToJson(revision)},
            {"trajectory", serialized}, {"point_trace", face_trajectory::CameraPointTrace(snapshot, packet, site_context)}};
    }
    if (op == "point_trace" || op == "trajectory_predict") {
        const auto version = request.contains("version") ? ReadInt64(request.at("version")) : 0;
        if (version < 0) throw std::invalid_argument("version must be nonnegative");
        const auto& snapshot = trajectories.Get(request.at("target_id"), static_cast<std::uint64_t>(version));
        const auto packet = point_graphs.Get(snapshot.graph.version);
        if (GraphToJson(point_graphs.Graph(snapshot.graph.version)) != GraphToJson(snapshot.graph))
            throw std::invalid_argument("snapshot graph differs from registered point graph");
        if (op == "point_trace") return {{"ok", true}, {"point_trace", face_trajectory::CameraPointTrace(snapshot, packet, site_context)}};
        auto prediction_options = request.at("prediction");
        prediction_options["topology_verified"] = packet.at("topology_verified");
        prediction_options["topology_is_complete"] = packet.at("topology_is_complete");
        return {{"ok", true}, {"prediction", face_trajectory::PredictReachable(snapshot, prediction_options)}};
    }
    if (op == "point_prediction_compare") {
        const auto target = request.at("target_id").get<std::string>();
        const auto old_version = ReadInt64(request.at("previous_version"));
        const auto new_version = ReadInt64(request.at("version"));
        if (old_version < 1 || new_version <= old_version) throw std::invalid_argument("explicit increasing snapshot versions required");
        // Durable Get has one cold cache: retain independent copies across calls.
        const auto previous = trajectories.Get(target, static_cast<std::uint64_t>(old_version));
        const auto current = trajectories.Get(target, static_cast<std::uint64_t>(new_version));
        if (current.association_revision != associations.TargetRevision(target))
            throw std::runtime_error("REBUILD_REQUIRED: comparison current version uses outdated association");
        const auto id = request.at("observation_id").get<std::string>();
        const auto accepted = [](const auto& e, const auto& s) { return e.link_status == TargetLinkStatus::Confirmed &&
            e.time_source == TimeSource::UtcCapture && e.clock_domain == "UTC" && e.clock_uncertainty_ms >= 0 &&
            e.clock_uncertainty_ms <= s.options.max_clock_uncertainty_ms &&
            std::find(s.rejected_observation_ids.begin(), s.rejected_observation_ids.end(), e.observation_id) == s.rejected_observation_ids.end(); };
        const SightingEvidence* next = nullptr;
        for (const auto& e : current.evidence) if (e.observation_id == id && accepted(e, current)) next = &e;
        if (!next) throw std::invalid_argument("new observation is not accepted in current snapshot");
        for (const auto& e : previous.evidence) if (e.observation_id == id) throw std::invalid_argument("observation already present in previous snapshot");
        json result = {{"previous_version", old_version}, {"version", new_version}, {"observation_id", id},
            {"observed_camera_id", next->camera_id}, {"status", "UNRESOLVED"}, {"observation_discarded", false}};
        bool any = false; std::int64_t cutoff = std::numeric_limits<std::int64_t>::min();
        bool changed_basis = false;
        for (const auto& e : previous.evidence) if (accepted(e, previous)) {
            any = true; cutoff = std::max(cutoff, e.captured_at_unix_ms);
            const auto found = std::find_if(current.evidence.begin(), current.evidence.end(), [&](const auto& c) {
                return c.observation_id == e.observation_id && accepted(c, current) && face_trajectory::SameSighting(c, e);
            });
            if (found == current.evidence.end()) changed_basis = true;
        }
        for (const auto& e : current.evidence) if (e.observation_id != id && accepted(e, current) && e.captured_at_unix_ms <= cutoff) {
            if (std::none_of(previous.evidence.begin(), previous.evidence.end(), [&](const auto& old) {
                return old.observation_id == e.observation_id && accepted(old, previous);
            })) changed_basis = true;
        }
        if (!any) result["reason"] = "NO_PREVIOUS_ACCEPTED_OBSERVATION";
        else if (changed_basis) result["reason"] = "PRIOR_EVIDENCE_CHANGED_OR_RETRACTED";
        else if (previous.graph.version != current.graph.version) result["reason"] = "GRAPH_CHANGED_REBUILD_REQUIRED";
        else if (next->captured_at_unix_ms < cutoff) result["reason"] = "LATE_OBSERVATION_HISTORY_REBUILT";
        else {
            if (next->captured_at_unix_ms > std::numeric_limits<std::int64_t>::max() - next->clock_uncertainty_ms)
                throw std::invalid_argument("capture uncertainty overflows query time");
            const auto packet = point_graphs.Get(previous.graph.version);
            if (GraphToJson(point_graphs.Graph(previous.graph.version)) != GraphToJson(previous.graph) ||
                GraphToJson(point_graphs.Graph(current.graph.version)) != GraphToJson(current.graph))
                throw std::invalid_argument("snapshot graph differs from registered point graph");
            auto o = request.value("prediction", json::object());
            o["as_of_unix_ms"] = cutoff;
            o["at_unix_ms"] = next->captured_at_unix_ms + next->clock_uncertainty_ms;
            o["topology_verified"] = packet.at("topology_verified"); o["topology_is_complete"] = packet.at("topology_is_complete");
            const auto prediction = face_trajectory::PredictReachable(previous, o);
            result["prior_prediction"] = prediction;
            bool included = false;
            for (const auto& c : prediction.at("candidate_cameras")) if (c.at("camera_id") == next->camera_id) included = true;
            if (included) result["status"] = "CONSISTENT_WITH_PRIOR_CANDIDATES";
            else if (prediction.at("status") == "CANDIDATE" && prediction.at("search_complete") == true &&
                     packet.at("topology_verified") == true && packet.at("topology_is_complete") == true)
                result["status"] = "INCONSISTENT_WITH_PRIOR_MODEL";
            else result["reason"] = "INCOMPLETE_UNVERIFIED_OR_EXPIRED_PREDICTION";
        }
        return {{"ok", true}, {"comparison", result}};
    }
    if(op=="geometry_put"){geometry.Put(request.at("geometry"));return {{"ok",true}};}
    if (op == "site_scene_put") {
        site_context.Put(request.at("site"));
        return {{"ok", true}, {"scene_version", request.at("site").at("scene_version")}};
    }
    if (op == "site_camera_context") {
        return {{"ok", true}, {"context", site_context.CameraContext(
            request.at("scene_version").get<std::string>(), request.at("camera_ref").get<std::string>(),
            request.at("id_namespace").get<std::string>(), request.at("radius_m").get<double>())}};
    }
    if(op=="trajectory_geometry") {
        const auto version=request.contains("version")?ReadInt64(request.at("version")):0;
        if(version<0)throw std::invalid_argument("version must be nonnegative");
        return {{"ok",true},{"geometry",geometry.Render(trajectories.Get(request.at("target_id").get<std::string>(),static_cast<std::uint64_t>(version)),request.at("geometry_version").get<std::string>())}};
    }
    if (op == "health") return {{"ok", true}, {"template_count", gallery.Size()},
                                 {"gallery_version", gallery.Version()}};
    if (op == "add_template") {
        const auto id = request.at("template_id").get<std::string>();
        const auto target = request.at("person_entity_id").get<std::string>();
        if (id.rfind("assoc-template:", 0) == 0 || target.rfind("auto:", 0) == 0)
            throw std::invalid_argument("association template/target namespace is reserved");
        gallery.Add({id, target,
                     ReadSpace(request.at("feature_space")),
                     ReadEmbedding(request.at("embedding"))});
        return {{"ok", true}, {"gallery_version", gallery.Version()}};
    }
    if (op == "remove_template") {
        const auto id = request.at("template_id").get<std::string>();
        if (id.rfind("assoc-template:", 0) == 0)
            throw std::invalid_argument("association templates are managed by attribution correction");
        gallery.Remove(id);
        return {{"ok", true}, {"gallery_version", gallery.Version()}};
    }
    if (op == "association_observe") {
        return {{"ok", true}, {"association", AssociationToJson(associations.Observe(
            ReadFaceObservation(request.at("observation")), ReadAssociationPolicy(request.at("policy")),
            ReadGraph(request.at("graph")), request.value("reconsider", false)))},
            {"gallery_version", gallery.Version()}};
    }
    if (op == "association_correct") {
        return {{"ok", true}, {"association", AssociationToJson(associations.Correct(
            request.at("observation_id").get<std::string>(), request.at("target_id").get<std::string>(),
            request.at("evidence_id").get<std::string>()))}, {"gallery_version", gallery.Version()}};
    }
    if (op == "search") {
        const auto top_k = ReadInt64(request.at("top_k"));
        if (top_k < 1 || top_k > 100) throw std::invalid_argument("top_k must be between 1 and 100");
        json hits = json::array();
        for (const auto& hit : gallery.Search(ReadSpace(request.at("feature_space")),
                                              ReadEmbedding(request.at("embedding")), top_k)) {
            hits.push_back({{"person_entity_id", hit.person_entity_id},
                            {"template_id", hit.template_id}, {"similarity", hit.similarity}});
        }
        return {{"ok", true}, {"hits", hits}, {"gallery_version", gallery.Version()}};
    }
    if (op == "build_visits" || op == "raw_trajectory") {
        const auto sightings = ReadSightings(request.at("sightings"), true);
        const auto max_gap_ms = ReadInt64(request.at("max_gap_ms"));
        const auto max_uncertainty_ms = ReadInt64(request.at("max_clock_uncertainty_ms"));
        if (op == "build_visits") {
            const auto result = face_trajectory::BuildCameraVisits(sightings, max_gap_ms, max_uncertainty_ms);
            return {{"ok", true}, {"visits", VisitsToJson(result.visits)},
                    {"rejected_observation_ids", result.rejected_observation_ids}};
        }
        const auto person_id = request.contains("target_id")
            ? request.at("target_id").get<std::string>()
            : request.at("person_entity_id").get<std::string>();
        return {{"ok", true}, {"visits", VisitsToJson(face_trajectory::BuildRawTrajectory(
                                     sightings, max_gap_ms, max_uncertainty_ms, person_id))}};
    }
    if (op == "trajectory_rebuild" || op == "association_rebuild") {
        const auto target_id = request.at("target_id").get<std::string>();
        const auto sightings = op == "association_rebuild" ? associations.Sightings(target_id)
                                                         : ReadSightings(request.at("sightings"));
        ValidateAssociatedSightings(target_id, sightings, associations);
        const auto revision = trajectories.Rebuild(
            target_id, sightings,
            ReadGraph(request.at("graph")), ReadOptions(request),
            request.at("reason").get<std::string>(), ExpectedVersion(request));
        return {{"ok", true},
                {"revision", RevisionToJson(revision)},
                {"trajectory", SnapshotToJson(trajectories.Get(target_id, revision.version))}};
    }
    if (op == "trajectory_get") {
        const auto target_id = request.at("target_id").get<std::string>();
        const auto version = request.contains("version") ? ReadInt64(request.at("version")) : 0;
        if (version < 0) throw std::invalid_argument("version must be nonnegative");
        return {{"ok", true}, {"trajectory", SnapshotToJson(
            trajectories.Get(target_id, static_cast<std::uint64_t>(version)))}};
    }
    if (op == "timeline_query") {
        const auto target_id = request.at("target_id").get<std::string>();
        const auto version = request.contains("version") ? ReadInt64(request.at("version")) : 0;
        if (version < 0) throw std::invalid_argument("version must be nonnegative");
        const auto at = ReadInt64(request.at("at_unix_ms"));
        const auto from = request.contains("from_unix_ms") ? ReadInt64(request.at("from_unix_ms")) : at;
        const auto to = request.contains("to_unix_ms") ? ReadInt64(request.at("to_unix_ms")) : at;
        const auto result = trajectories.Query(target_id, static_cast<std::uint64_t>(version),
                                               at, from, to);
        json anchors = json::array();
        for (const auto& anchor : result.anchors_in_range) anchors.push_back(AnchorToJson(anchor));
        json segments = json::array();
        for (const auto& segment : result.segments_in_range) segments.push_back(SegmentToJson(segment));
        json locations = json::array();
        for (const auto& location : result.path_locations) {
            locations.push_back({{"segment_id", location.segment_id}, {"route_index", location.route_index},
                                 {"node_indices", location.node_indices}, {"edge_indices", location.edge_indices}});
        }
        return {{"ok", true}, {"timeline", {{"target_id", result.target_id},
                             {"version", result.version}, {"at_unix_ms", result.at_unix_ms},
                             {"status", StatusName(result.status)},
                             {"observed_anchor_ids", result.observed_anchor_ids},
                             {"possible_observed_anchor_ids", result.possible_observed_anchor_ids},
                             {"previous_anchor_id", result.previous_anchor_id},
                             {"next_anchor_id", result.next_anchor_id},
                             {"segment_id", result.segment_id},
                             {"previous_observation", TimelineObservationToJson(result.previous_observation)},
                             {"next_observation", TimelineObservationToJson(result.next_observation)},
                             {"active_segment_ids", result.active_segment_ids},
                             {"path_locations", locations},
                             {"anchors_in_range", anchors},
                             {"segments_in_range", segments}}}};
    }
    throw std::invalid_argument("unsupported operation");
}

bool ReadBoundedLine(std::string& line, bool& too_large) {
    constexpr std::size_t kMaxLine = 4 * 1024 * 1024;
    line.clear();
    too_large = false;
    bool saw_input = false;
    for (;;) {
        const int next = std::cin.get();
        if (next == EOF) return saw_input;
        saw_input = true;
        if (next == '\n') return true;
        if (line.size() < kMaxLine && !too_large) {
            line.push_back(static_cast<char>(next));
        } else {
            too_large = true;
        }
    }
}

bool PushOperation(const std::string& op) {
    return op == "association_observe" || op == "association_correct" ||
        op == "association_rebuild" || op == "trajectory_rebuild" || op == "point_trajectory_rebuild" ||
        op == "monitoring_observe" || op == "monitoring_gate_event" || op == "monitoring_person_put";
}

std::uint64_t PushCounter(face_trajectory::State& state, const std::string& key) {
    return std::stoull(state.Get("push_meta", key).value_or("0"));
}

json PushControl(face_trajectory::State* state, bool enabled, const json& request) {
    const auto op = request.at("op").get<std::string>();
    if (op == "push_status") return {{"ok", true}, {"push", {{"enabled", enabled},
        {"durable", bool(state)}, {"pending", state ? PushCounter(*state, "pending") : 0},
        {"max_pending", 10000}, {"max_pending_per_activity", 256}}}};
    if (!state) throw std::invalid_argument("push requires durable state");
    if (op == "push_next") {
        std::map<std::string,json> heads;
        const auto excluded=request.value("exclude_activity_ids",std::vector<std::string>{});
        for(const auto& activity:state->References("push_outbox")) {
            if(std::find(excluded.begin(),excluded.end(),activity)!=excluded.end())continue;
            state->ScanAfter("push_outbox","",1,[&](const std::string&,const std::string& text){
                heads.emplace(activity,json::parse(text));
            },activity);
        }
        auto next=heads.upper_bound(state->Get("push_meta","last_activity").value_or(""));
        if(next==heads.end())next=heads.begin();
        if(next==heads.end())return {{"ok",true},{"event",nullptr}};
        state->Put("push_meta","last_activity",next->first);
        return {{"ok",true},{"event",next->second}};
    }
    if (op == "push_ack") {
        std::optional<std::string> selected;
        state->Scan("push_outbox",[&](const std::string& key,const std::string& text){
            const auto event=json::parse(text);const auto activity=event.at("activity_id").get<std::string>();
            state->ScanAfter("push_outbox","",1,[&](const std::string& first,const std::string&){
                if(first==key)selected=key;
            },activity);
        },{},false,request.at("event_id").get<std::string>());
        if(!selected)throw std::invalid_argument("push acknowledgement must match oldest pending event in its activity");
        state->Erase("push_outbox",*selected);
        state->Put("push_meta", "pending", std::to_string(PushCounter(*state, "pending") - 1));
        return {{"ok", true}};
    }
    throw std::invalid_argument("unsupported push operation");
}

void EnqueuePush(face_trajectory::State& state, const std::string& op, const json& response,const json& request) {
    const auto activity=response.at("activity_id").get<std::string>();
    const auto activity_pending=state.Count("push_outbox",activity,true);
    if(activity_pending>=256||PushCounter(state,"pending")>=10000)
        throw std::runtime_error("CAPACITY: backend push quota exhausted; retry after delivery");
    auto stream = state.Get("push_meta", "stream_id");
    if (!stream) {
        std::random_device rng;
        const char* hex = "0123456789abcdef";
        std::string id;
        for (int i = 0; i < 32; ++i) id += hex[rng() & 15];
        state.Put("push_meta", "stream_id", id);
        stream = id;
    }
    const auto sequence = PushCounter(state, "sequence") + 1;
    if (sequence == 0) throw std::runtime_error("push sequence exhausted");
    const auto number = std::to_string(sequence);
    const auto key = std::string(20 - number.size(), '0') + number;
    const auto now=request.at("_trusted_clock").at("now_unix_ms").get<std::int64_t>();
    json event = {{"schema_version", "1"}, {"event_id", *stream + ":" + number},
        {"sequence", sequence}, {"operation", op}, {"created_at_unix_ms", now}, {"result", response}};
    for(const auto* k:{"activity_id","activity_num","config_version","binding_version","monitoring_version"})
        if(response.contains(k))event[k]=response.at(k);
    event["retention_deadline_unix_ms"]=response.value("retention_deadline_unix_ms",now+face_trajectory::ActivityStore::RetentionMs);
    // Store only results: no input face embedding, raw image or credentials.
    state.Put("push_outbox", key, event.dump(),activity,event.at("event_id"));
    state.Put("push_meta", "sequence", number);
    state.Put("push_meta", "pending", std::to_string(PushCounter(state, "pending") + 1));
}

}  // namespace

namespace {
struct ActivityRuntime {
    face_trajectory::State scope;
    Gallery gallery;
    TrajectoryStore trajectories;
    face_trajectory::AssociationStore associations;
    face_trajectory::MonitoringStore monitoring;
    ActivityRuntime(face_trajectory::State& root,const std::string& id,std::size_t capacity)
        :scope(root,id),gallery(&scope,capacity),trajectories(&scope),associations(gallery,&scope),monitoring(&scope) {
        trajectories.SetAssociationRevisionReader([this](const auto& target){return associations.TargetRevision(target);});
    }
};
bool GlobalOperation(const std::string& op) {
    return op=="health" || op=="clock_profile_put" || op=="clock_normalize" || op=="site_scene_put" ||
        op=="site_camera_context" || op=="point_graph_put" || op=="point_graph_get" ||
        op=="geometry_put" || op=="push_status" || op=="push_next" || op=="push_ack";
}
}

int Run(const std::vector<std::string>& args) {
    std::ios::sync_with_stdio(false);
    std::cin.tie(nullptr);
    std::unique_ptr<face_trajectory::State> state;
    std::string state_path;std::size_t max_templates=500000;std::uint64_t max_state_mb=8192;
    bool push_results = false,retention_enabled=false;std::vector<std::string> p1_retention_paths;
    for(std::size_t i=0;i<args.size();i+=2){
        if(i+1>=args.size())throw std::invalid_argument("worker option needs a value");
        if(args[i]=="--state-file")state_path=args[i+1];
        else if(args[i]=="--max-templates")max_templates=std::stoull(args[i+1]);
        else if(args[i]=="--max-state-mb")max_state_mb=std::stoull(args[i+1]);
        else if(args[i]=="--retention-enabled") {if(args[i+1]!="true"&&args[i+1]!="false")throw std::invalid_argument("retention-enabled boolean required");retention_enabled=args[i+1]=="true";}
        else if(args[i]=="--p1-retention-db")p1_retention_paths.push_back(args[i+1]);
        else if(args[i]=="--push-results") {
            if (args[i+1] != "true" && args[i+1] != "false") throw std::invalid_argument("push-results must be true or false");
            push_results = args[i+1] == "true";
        }
        else throw std::invalid_argument("unsupported worker option");
    }
    if(max_state_mb<1 || max_state_mb>1024*1024)throw std::invalid_argument("max-state-mb outside supported range");
    if(!state_path.empty())state=std::make_unique<face_trajectory::State>(state_path,max_state_mb*1024*1024);
    if (push_results && !state) throw std::invalid_argument("push-results requires state-file");
    auto gallery=std::make_unique<Gallery>(state.get(),max_templates);
    auto trajectories=std::make_unique<TrajectoryStore>(state.get());
    auto associations=std::make_unique<face_trajectory::AssociationStore>(*gallery,state.get());
    trajectories->SetAssociationRevisionReader([&](const auto& target) { return associations->TargetRevision(target); });
    face_trajectory::GeometryStore geometry(state.get());
    face_trajectory::SiteContextStore site_context(state.get());
    face_trajectory::ClockAdapter clocks(state.get());
    face_trajectory::PointGraphStore point_graphs(state.get());
    face_trajectory::MonitoringStore monitoring(state.get());
    std::unique_ptr<face_trajectory::ActivityStore> activities;
    if(state)activities=std::make_unique<face_trajectory::ActivityStore>(*state);
    std::map<std::string,std::unique_ptr<ActivityRuntime>> activity_runtimes;
    const auto invalidate=[&](){activity_runtimes.clear();associations.reset();trajectories.reset();gallery.reset();gallery=std::make_unique<Gallery>(state.get(),max_templates);trajectories=std::make_unique<TrajectoryStore>(state.get());associations=std::make_unique<face_trajectory::AssociationStore>(*gallery,state.get());trajectories->SetAssociationRevisionReader([&](const auto& target){return associations->TargetRevision(target);});};
    std::string line;
    bool too_large = false;
    while (ReadBoundedLine(line, too_large)) {
        json request;
        try {
            if (too_large) throw std::invalid_argument("request is too large");
            request=json::parse(line);
            if(state)state->Begin();
            const auto op = request.value("op", "");
            bool maintenance=false;
            if(state&&retention_enabled&&op!="health"&&op!="retention_run"&&op!="retention_status") {
                const auto now=activities->TrustedNow(request);
                const auto due=std::stoll(state->Get("retention_meta","next_deadline").value_or("0"));
                if(now>due){face_trajectory::Retain(*state,now,false);invalidate();maintenance=true;}
            }
            const bool enqueue = push_results && PushOperation(op);
            json response;
            if(op=="retention_run"||op=="retention_status") {
                if(!state)throw std::invalid_argument("retention requires durable state");
                if(op=="retention_status")response={{"ok",true},{"enabled",retention_enabled},{"storage",face_trajectory::StorageReport(state->Stats())},{"last_report",json::parse(state->Get("retention_meta","last_report").value_or("null"))}};
                else {const bool dry=request.at("dry_run").get<bool>();if(!dry&&!retention_enabled)throw std::invalid_argument("retention deletion requires startup enable flag");
                    const auto now=activities->TrustedNow(request,!dry);response=face_trajectory::Retain(*state,now,dry);
                    std::unique_ptr<face_trajectory::State> reference_plan;
                    if(dry&&!p1_retention_paths.empty()){
                        reference_plan=state->PlanningCopy();reference_plan->Begin();
                        face_trajectory::Retain(*reference_plan,now,false);
                    }
                    const face_trajectory::ExpiredReference expired_reference=[&](const auto& id,const auto& source,const auto& event){
                        face_trajectory::State scope(reference_plan?*reference_plan:*state,id);
                        return scope.Get("retention_tombstone",face_trajectory::ActivityStore::Digest(json::array({source,event}).dump())).has_value();
                    };
                    response["p1"]=face_trajectory::PruneP1(p1_retention_paths,now,dry,clocks,expired_reference);response["complete"]=response.at("p1").at("status")=="COMPLETE";response["ok"]=response.at("complete");
                    if(!dry){invalidate();maintenance=true;}
                }
            } else if(op.rfind("activity_",0)==0) {
                if(!activities)throw std::invalid_argument("activities require durable state-file");
                response=activities->Control(request);
                if(op!="activity_get" && op!="activity_results" &&
                   (!response.value("enabled",false) || !response.value("duplicate",false)))
                    activity_runtimes.erase(request.at("activity_id").get<std::string>());
            } else if(GlobalOperation(op)) {
                if(op=="push_next" || op=="push_ack")activities->TrustedNow(request);
                response=op.rfind("push_",0)==0 ? PushControl(state.get(),push_results,request)
                    : Execute(*gallery,*trajectories,*associations,geometry,site_context,clocks,point_graphs,monitoring,request);
            } else {
                if(!activities || !request.contains("activity_id") || !request.at("activity_id").is_string() || request.at("activity_id")=="")
                    throw std::invalid_argument("ACTIVITY_REQUIRED: explicit authorized activity_id required; legacy records remain unassigned");
                // Bare UTC assertions and arbitrary raw sightings are no longer
                // production admission paths. Use qualified monitoring intake.
                if(op=="association_observe" || op=="trajectory_rebuild" || op=="raw_trajectory" || op=="build_visits" || op=="point_trajectory_rebuild")
                    throw std::invalid_argument("SOURCE_CONFLICT: use clock-qualified monitoring intake for activity records");
                const auto id=request.at("activity_id").get<std::string>();
                auto& runtime=activity_runtimes[id];
                if(!runtime)runtime=std::make_unique<ActivityRuntime>(*state,id,max_templates);
                if(op=="monitoring_observe")request["_normalization"]=MonitoringTime(clocks,request,request.at("observation").at("camera_id"),request.at("observation").at("source_id"));
                else if(op=="monitoring_gate_event")request["_normalization"]=MonitoringTime(clocks,request);
                const auto early=activities->Before(request,runtime->scope);
                response=early?*early:Execute(runtime->gallery,runtime->trajectories,runtime->associations,geometry,site_context,clocks,point_graphs,runtime->monitoring,request);
                if(!early)AddResultFreshness(response,request,runtime->trajectories,runtime->associations);
                face_trajectory::ActivityStore::ExternalIds(response);
                const auto context=activities->Context(id);
                if(!early)for(const auto* k:{"activity_id","activity_num","venue_id","config_version","binding_version","monitoring_version"})response[k]=context.at(k);
                if(request.contains("route_rule_id"))response["route_rule_id"]=request.at("route_rule_id");
                if(request.contains("_source_event_key") && response.value("status","")!="EXPIRED") {
                    const auto rec=json::parse(runtime->scope.Get("activity_intake",request.at("_source_event_key")).value());
                    const auto start=rec.value("capture_trusted",false)?rec.at("captured_at_unix_ms").get<std::int64_t>():rec.at("first_quarantined_unix_ms").get<std::int64_t>();
                    const auto base=rec.value("had_untrusted_capture",!rec.value("capture_trusted",false))?std::min(start,rec.at("first_quarantined_unix_ms").get<std::int64_t>()):start;
                    response["retention_deadline_unix_ms"]=base+face_trajectory::ActivityStore::RetentionMs;
                    if(!early)activities->Receipt(request,response,runtime->scope);
                }
                if(!early && !response.value("duplicate",false))activities->Notify(request,response,runtime->scope);
                if(enqueue && !early && !response.value("duplicate",false))EnqueuePush(*state,op,response,request);
            }
            if(request.value("op","")=="health")response["durable_state"]=bool(state);
            if(state)state->Commit();
            if(maintenance){try{response["after_physical"]=face_trajectory::StorageReport(state->Maintenance(true));response["maintenance_status"]="COMPLETE";}catch(const std::exception& e){response["maintenance_status"]="PENDING";response["maintenance_error"]=e.what();if(op=="retention_run"){response["complete"]=false;response["ok"]=false;}}}
            std::cout << response.dump() << std::endl;
        } catch (const std::exception& error) {
            if(state){
                const bool dirty=state->Dirty();
                state->Rollback();
                // A failed write may already have changed hot state. Reconstruct
                // all hot indexes from the last committed database transaction.
                if(dirty){
                activity_runtimes.clear();
                associations.reset();trajectories.reset();gallery.reset();
                gallery=std::make_unique<Gallery>(state.get(),max_templates);
                trajectories=std::make_unique<TrajectoryStore>(state.get());
                associations=std::make_unique<face_trajectory::AssociationStore>(*gallery,state.get());
                trajectories->SetAssociationRevisionReader([&](const auto& target) { return associations->TargetRevision(target); });
                }
            }
            json failure = {{"ok", false}, {"error", error.what()}};
            const std::string message = error.what();
            for (const auto* code : {"VERSION_CONFLICT", "SOURCE_CONFLICT", "REBUILD_REQUIRED"})
                if (message.rfind(std::string(code) + ":", 0) == 0) failure["error_code"] = code;
            if (failure.contains("error_code") && request.contains("target_id") && request.at("target_id").is_string()) {
                const auto target = request.at("target_id").get<std::string>();
                failure["conflict"] = {{"target_id", target}, {"latest_version", trajectories->LatestVersion(target)},
                    {"current_association_revision", associations->TargetRevision(target)}};
            }
            std::cout << failure.dump() << std::endl;
        }
    }
    return 0;
}
#ifdef _WIN32
int wmain(int argc,wchar_t** argv) {
    try {
        std::vector<std::string> args;
        for(int i=1;i<argc;++i){int n=WideCharToMultiByte(CP_UTF8,0,argv[i],-1,nullptr,0,nullptr,nullptr);
            std::string s(n,'\0');WideCharToMultiByte(CP_UTF8,0,argv[i],-1,s.data(),n,nullptr,nullptr);s.pop_back();args.push_back(std::move(s));}
        return Run(args);
    } catch(const std::exception& e){std::cerr<<e.what()<<std::endl;return 1;}
}
#else
int main(int argc,char** argv){try{return Run({argv+1,argv+argc});}catch(const std::exception& e){std::cerr<<e.what()<<std::endl;return 1;}}
#endif
