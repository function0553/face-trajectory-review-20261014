#include "face_trajectory/point_trajectory.hpp"
#include "face_trajectory/state.hpp"
#include <algorithm>
#include <limits>
#include <set>
#include <stdexcept>
namespace face_trajectory {
namespace {
using nlohmann::json;
std::int64_t Integer(const json& v) {
    if (!v.is_number_integer() || (v.is_number_unsigned() && v.get<std::uint64_t>() >
        static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max())))
        throw std::invalid_argument("point graph integer is outside int64 range");
    return v.get<std::int64_t>();
}
CameraGraph Decode(const json& p) {
    CameraGraph graph{p.at("graph_version").get<std::string>(), {}};
    if (!p.at("edges").is_array()) throw std::invalid_argument("point graph edges must be an array");
    for (const auto& e : p.at("edges")) {
        CameraEdge edge{e.at("from_camera").get<std::string>(), e.at("to_camera").get<std::string>(),
            Integer(e.at("min_travel_ms")), Integer(e.at("max_travel_ms")), e.at("cost").get<double>(),
            std::nullopt, std::nullopt, e.at("edge_id").get<std::string>()};
        if (e.contains("open_from_unix_ms")) edge.open_from_unix_ms = Integer(e.at("open_from_unix_ms"));
        if (e.contains("open_until_unix_ms")) edge.open_until_unix_ms = Integer(e.at("open_until_unix_ms"));
        graph.edges.push_back(std::move(edge));
    }
    return NormalizeCameraGraph(std::move(graph));
}
bool Accepted(const SightingEvidence& e, const TrajectorySnapshot& s, const std::set<std::string>& rejected) {
    return !rejected.count(e.observation_id) && e.target_id == s.target_id &&
        e.link_status == TargetLinkStatus::Confirmed && e.time_source == TimeSource::UtcCapture &&
        e.clock_domain == "UTC" && e.clock_uncertainty_ms >= 0 &&
        e.clock_uncertainty_ms <= s.options.max_clock_uncertainty_ms;
}
// A third camera's overlapping time interval can make nominal neighbours
// ambiguous too. Preserve the entire connected interval group's uncertainty.
std::set<std::string> AmbiguousOrder(const std::vector<const SightingEvidence*>& evidence) {
    const auto lower = [](const auto* e) {
        return e->captured_at_unix_ms < std::numeric_limits<std::int64_t>::min() + e->clock_uncertainty_ms
            ? std::numeric_limits<std::int64_t>::min() : e->captured_at_unix_ms - e->clock_uncertainty_ms;
    };
    const auto upper = [](const auto* e) {
        return e->captured_at_unix_ms > std::numeric_limits<std::int64_t>::max() - e->clock_uncertainty_ms
            ? std::numeric_limits<std::int64_t>::max() : e->captured_at_unix_ms + e->clock_uncertainty_ms;
    };
    auto intervals = evidence;
    std::sort(intervals.begin(), intervals.end(), [&](auto a, auto b) {
        if (lower(a) != lower(b)) return lower(a) < lower(b);
        return a->observation_id < b->observation_id;
    });
    std::set<std::string> ambiguous;
    for (std::size_t begin = 0; begin < intervals.size();) {
        auto end = begin + 1;
        auto latest = upper(intervals[begin]);
        std::set<std::string> cameras{intervals[begin]->camera_id};
        while (end < intervals.size() && lower(intervals[end]) <= latest) {
            latest = std::max(latest, upper(intervals[end]));
            cameras.insert(intervals[end++]->camera_id);
        }
        if (cameras.size() > 1)
            for (auto i = begin; i < end; ++i) ambiguous.insert(intervals[i]->observation_id);
        begin = end;
    }
    return ambiguous;
}
void ValidateSceneBinding(const json& packet, const json& site) {
    const bool model_packet = packet.value("schema", "") == "camera-point-candidate-graph@1";
    for (auto key : {"unit", "to_world_matrix", "cameras"})
        if (model_packet && !packet.contains(key))
            throw std::invalid_argument(std::string("model candidate graph requires ") + key);
    if (model_packet && site.contains("model_revision") && !packet.contains("model_binding"))
        throw std::invalid_argument("model candidate graph requires model_binding for a scene with model_revision");
    for (auto key : {"coordinate_frame", "unit", "to_world_matrix", "model_id", "scene_url",
                     "tileset_id", "calibration_profile", "model_revision"})
        if (packet.contains(key) && (!site.contains(key) || packet.at(key) != site.at(key)))
            throw std::invalid_argument(std::string("point graph differs from scene: ") + key);
    if (packet.contains("model_binding")) {
        const auto& binding = packet.at("model_binding");
        if (!binding.is_object()) throw std::invalid_argument("model_binding must be an object");
        for (auto key : {"model_id", "scene_url", "tileset_id", "calibration_profile"})
            if (!binding.contains(key) || binding.at(key) != site.at(key))
                throw std::invalid_argument(std::string("point graph model_binding differs from scene: ") + key);
        if (binding.contains("model_revision") != site.contains("model_revision") ||
            (site.contains("model_revision") && binding.at("model_revision") != site.at("model_revision")))
            throw std::invalid_argument("point graph model_binding differs from scene: model_revision");
    }
    // Legacy hand-authored graphs may omit model provenance. Supplied spatial
    // assertions must still agree with the immutable scene they reference.
    std::map<std::string, const json*> catalog, annotations;
    std::map<std::string, std::set<std::string>> bound;
    for (const auto& c : site.at("cameras")) catalog.emplace(c.at("camera_id"), &c);
    for (const auto& a : site.at("annotations")) {
        const auto id = a.at("annotation_id").get<std::string>();
        annotations.emplace(id, &a);
        const auto camera = a.at("camera_id").get<std::string>();
        if (a.at("role") == "CAMERA" && catalog.count(camera)) bound[camera].insert(id);
    }
    auto marker = [&](const std::string& id, const std::string& camera) -> const json& {
        const auto found = annotations.find(id);
        if (found == annotations.end() || found->second->at("role") != "CAMERA" ||
            found->second->at("camera_id") != camera)
            throw std::invalid_argument("point graph annotation differs from scene camera binding");
        return *found->second;
    };
    if (packet.contains("cameras")) {
        if (!packet.at("cameras").is_array()) throw std::invalid_argument("point graph cameras must be an array");
        std::set<std::string> camera_ids;
        for (const auto& camera : packet.at("cameras")) {
            const auto id = camera.at("camera_id").get<std::string>();
            if (!catalog.count(id) || !camera_ids.insert(id).second)
                throw std::invalid_argument("point graph has unknown or duplicate camera_id");
            if (camera.contains("vcl_channel_id") && camera.at("vcl_channel_id") != catalog.at(id)->at("vcl_channel_id"))
                throw std::invalid_argument("point graph camera channel differs from scene");
            if (!camera.contains("position_candidates")) {
                if (model_packet) throw std::invalid_argument("model candidate camera requires position_candidates");
                continue;
            }
            const auto& positions = camera.at("position_candidates");
            if (!positions.is_array()) throw std::invalid_argument("position_candidates must be an array");
            std::set<std::string> marker_ids;
            for (const auto& p : positions) {
                const auto annotation_id = p.at("annotation_id").get<std::string>();
                const auto& a = marker(annotation_id, id);
                if (!marker_ids.insert(annotation_id).second)
                    throw std::invalid_argument("duplicate point graph camera annotation");
                if (!p.contains("points") && !p.contains("local_xyz"))
                    throw std::invalid_argument("point graph camera candidate requires source coordinates");
                for (auto key : {"source_scene_id", "camera_id", "role", "shape", "visible", "points"}) {
                    if (model_packet && !p.contains(key))
                        throw std::invalid_argument(std::string("model camera candidate requires ") + key);
                    if (p.contains(key) && p.at(key) != a.at(key))
                        throw std::invalid_argument(std::string("point graph camera candidate differs from scene: ") + key);
                }
                if (p.contains("local_xyz") && p.at("local_xyz") != a.at("points").at(0))
                    throw std::invalid_argument("point graph camera coordinates differ from scene");
                if (p.contains("model_diagnostic")) {
                    const auto& d = p.at("model_diagnostic");
                    for (auto key : {"annotation_id", "camera_id"})
                        if (d.contains(key) && d.at(key) != a.at(key))
                            throw std::invalid_argument("point graph model diagnostic has stale camera binding");
                    if (d.contains("marker_local_xyz") && d.at("marker_local_xyz") != a.at("points").at(0))
                        throw std::invalid_argument("point graph model diagnostic has stale camera coordinates");
                }
            }
            if (model_packet && marker_ids != bound[id])
                throw std::invalid_argument("model candidate graph must preserve all scene camera positions");
        }
        if (model_packet) {
            std::set<std::string> expected;
            for (const auto& [id, positions] : bound) expected.insert(id);
            if (camera_ids != expected)
                throw std::invalid_argument("model candidate graph must preserve all bound scene cameras");
        }
    }
    for (const auto& e : packet.at("edges")) {
        for (const auto& endpoint : {std::pair{"from_annotation_id", "from_camera"},
                                     std::pair{"to_annotation_id", "to_camera"}}) {
            if (model_packet && !e.contains(endpoint.first))
                throw std::invalid_argument("model candidate edge requires source annotation IDs");
            if (e.contains(endpoint.first)) marker(e.at(endpoint.first), e.at(endpoint.second));
        }
    }
}
}
void PointGraphStore::Put(json packet, const SiteContextStore& sites) {
    for (auto key : {"graph_version", "scene_version", "source_evidence_id", "coordinate_frame"})
        if (packet.at(key).get<std::string>().empty()) throw std::invalid_argument("point graph metadata is required");
    for (auto key : {"topology_verified", "topology_is_complete"})
        if (!packet.at(key).is_boolean()) throw std::invalid_argument("explicit topology qualification required");
    ValidateSceneBinding(packet, sites.Get(packet.at("scene_version")));
    for (const auto& e : packet.at("edges")) {
        if (e.at("edge_id").get<std::string>().empty()) throw std::invalid_argument("point edge requires stable edge_id");
        if (packet.at("topology_verified") == true && !e.value("travel_time_bound_verified", false))
            throw std::invalid_argument("verified topology requires verified edge time bounds");
    }
    const auto graph = Decode(packet);
    std::map<std::string, json> contexts;
    for (const auto& edge : graph.edges) for (const auto& id : {edge.from_camera, edge.to_camera}) {
        if (!contexts.count(id)) contexts[id] = sites.CameraContext(packet.at("scene_version"), id, "CAMERA_ID", 0);
        const auto& c = contexts.at(id);
        if (c.at("coordinate_frame") != packet.at("coordinate_frame"))
            throw std::invalid_argument("point graph coordinate frame differs from scene");
        if (c.at("status") != "BOUND_MARKER")
            throw std::invalid_argument("operational point graph edge requires one bound camera position; preserve conflicting candidates separately");
    }
    std::sort(packet["edges"].begin(), packet["edges"].end(), [](const auto& a, const auto& b) {
        return a.at("edge_id") < b.at("edge_id");
    });
    const auto version = graph.version, encoded = packet.dump();
    auto old = state_ ? state_->Get("point_graph", version) : std::optional<std::string>{};
    if (!state_) { auto it = packets_.find(version); if (it != packets_.end()) old = it->second; }
    if (old && *old != encoded) throw std::invalid_argument("point graph changed without a new version");
    if (state_) state_->Put("point_graph", version, encoded); else packets_[version] = encoded;
}
nlohmann::json PointGraphStore::Get(const std::string& version) const {
    auto value = state_ ? state_->Get("point_graph", version) : std::optional<std::string>{};
    if (!state_) { auto it = packets_.find(version); if (it != packets_.end()) value = it->second; }
    if (!value) throw std::out_of_range("point graph version not found");
    return json::parse(*value);
}
CameraGraph PointGraphStore::Graph(const std::string& version) const { return Decode(Get(version)); }
nlohmann::json CameraPointTrace(const TrajectorySnapshot& s, const nlohmann::json& p, const SiteContextStore& sites) {
    if (s.graph.version != p.at("graph_version")) throw std::invalid_argument("point trace graph version mismatch");
    const std::set<std::string> rejected(s.rejected_observation_ids.begin(), s.rejected_observation_ids.end());
    std::vector<const SightingEvidence*> evidence;
    for (const auto& e : s.evidence) if (Accepted(e, s, rejected)) evidence.push_back(&e);
    std::sort(evidence.begin(), evidence.end(), [](auto a, auto b) {
        return std::tie(a->captured_at_unix_ms, a->observation_id) < std::tie(b->captured_at_unix_ms, b->observation_id);
    });
    json observations = json::array(), order = json::array(), connections = json::array();
    const auto ambiguous_order = AmbiguousOrder(evidence);
    std::map<std::string, std::string> visit_sessions, observation_sessions, anchor_visits;
    for (const auto& a : s.anchors) anchor_visits[a.anchor_id] = a.visit_id;
    for (const auto& session : s.sessions) for (const auto& id : session.anchor_ids) {
        const auto found = anchor_visits.find(id);
        if (found != anchor_visits.end()) visit_sessions[found->second] = session.session_id;
    }
    for (const auto& visit : s.visits) {
        const auto found = visit_sessions.find(visit.visit_id);
        if (found != visit_sessions.end())
            for (const auto& id : visit.observation_ids) observation_sessions[id] = found->second;
    }
    std::map<std::string, json> contexts;
    const SightingEvidence* previous = nullptr;
    for (const auto* e : evidence) {
        if (!contexts.count(e->camera_id)) contexts[e->camera_id] = sites.CameraContext(p.at("scene_version"), e->camera_id, "CAMERA_ID", 0);
        const auto& context = contexts.at(e->camera_id);
        json positions = json::array();
        for (const auto& marker : context.at("candidate_markers")) positions.push_back({
            {"annotation_id", marker.at("annotation_id")}, {"source_scene_id", marker.at("source_scene_id")},
            {"name", marker.at("name")}, {"local_xyz", marker.at("local_xyz")}});
        observations.push_back({{"observation_id", e->observation_id}, {"camera_id", e->camera_id},
            {"source_id", e->source_id},
            {"captured_at_unix_ms", e->captured_at_unix_ms}, {"clock_uncertainty_ms", e->clock_uncertainty_ms},
            {"attribution_evidence_id", e->attribution_evidence_id}, {"status", "OBSERVED"},
            {"position_status", context.at("status")}, {"position_candidates", positions},
            {"position_semantics", "CAMERA_MARKER_NOT_PERSON_FOOTPOINT"}});
        if (previous) {
            const auto gap = static_cast<std::uint64_t>(e->captured_at_unix_ms) -
                static_cast<std::uint64_t>(previous->captured_at_unix_ms);
            const auto errors = static_cast<std::uint64_t>(e->clock_uncertainty_ms) +
                static_cast<std::uint64_t>(previous->clock_uncertainty_ms);
            order.push_back({{"from_observation_id", previous->observation_id}, {"to_observation_id", e->observation_id},
                {"order_status", gap > errors ? "ORDERED" : "UNCERTAIN_CLOCK_ORDER"},
                {"physical_path_observed", false}, {"meaning", "OBSERVATION_SEQUENCE_ONLY"}});
            if (previous->camera_id != e->camera_id) {
                const auto& from = observations[observations.size() - 2];
                const auto& to = observations.back();
                const auto from_session = observation_sessions.find(previous->observation_id);
                const auto to_session = observation_sessions.find(e->observation_id);
                const bool same_session = from_session != observation_sessions.end() &&
                    to_session != observation_sessions.end() && from_session->second == to_session->second;
                const bool unique_positions = from.at("position_status") == "BOUND_MARKER" &&
                    to.at("position_status") == "BOUND_MARKER" &&
                    from.at("position_candidates").size() == 1 && to.at("position_candidates").size() == 1;
                json coordinates = json::array();
                if (same_session && unique_positions) {
                    coordinates.push_back(from.at("position_candidates")[0].at("local_xyz"));
                    coordinates.push_back(to.at("position_candidates")[0].at("local_xyz"));
                }
                connections.push_back({
                    {"from_observation_id", previous->observation_id}, {"to_observation_id", e->observation_id},
                    {"from_source_id", previous->source_id}, {"to_source_id", e->source_id},
                    {"from_camera_id", previous->camera_id}, {"to_camera_id", e->camera_id},
                    {"order_status", gap > errors && !ambiguous_order.count(previous->observation_id) &&
                        !ambiguous_order.count(e->observation_id) ? "ORDERED" : "UNCERTAIN_CLOCK_ORDER"},
                    {"drawing_status", !same_session ? "SESSION_BREAK" :
                        (unique_positions ? "DRAWABLE" : "UNRESOLVED_POSITION")},
                    {"coordinates", coordinates}, {"physical_path_observed", false},
                    {"meaning", "DIRECT_CAMERA_POINT_CONNECTION"}});
            }
        }
        previous = e;
    }
    return {{"target_id", s.target_id}, {"version", s.version}, {"scene_version", p.at("scene_version")},
        {"graph_version", s.graph.version}, {"coordinate_frame", p.at("coordinate_frame")},
        {"topology_verified", p.at("topology_verified")}, {"topology_is_complete", p.at("topology_is_complete")},
        {"observations", observations}, {"observation_order", order},
        {"drawing_mode", "CAMERA_POINT_CONNECTIONS"}, {"connections", connections},
        {"detailed_walk_path_observed", false}};
}
}
