#include "face_trajectory/site_context.hpp"
#include "face_trajectory/state.hpp"

#include <algorithm>
#include <array>
#include <cmath>
#include <set>
#include <stdexcept>

namespace face_trajectory {
using nlohmann::json;
namespace {
using Point = std::array<double, 3>;
void Required(const json& j, const char* key) {
    if (j.at(key).get<std::string>().empty()) throw std::invalid_argument(std::string(key) + " is required");
}
Point ReadPoint(const json& j) {
    if (!j.is_array() || j.size() != 3) throw std::invalid_argument("site point must be xyz");
    Point p{};
    for (int i = 0; i < 3; ++i) {
        if (!j[i].is_number() || !std::isfinite(j[i].get<double>())) throw std::invalid_argument("finite site coordinates required");
        p[i] = j[i].get<double>();
    }
    return p;
}
double Distance(const Point& a, const Point& b) {
    return std::hypot(std::hypot(a[0] - b[0], a[1] - b[1]), a[2] - b[2]);
}
Point World(const json& matrix, const Point& p) {
    Point out{};
    for (int r = 0; r < 3; ++r) {
        out[r] = matrix[12 + r].get<double>();
        for (int c = 0; c < 3; ++c) out[r] += matrix[c * 4 + r].get<double>() * p[c];
        if (!std::isfinite(out[r])) throw std::invalid_argument("site world transform overflow");
    }
    return out;
}
// A segment projection preserves heights; it is never interpreted as a walkable edge.
std::pair<double, Point> Closest(const Point& p, const json& annotation) {
    const auto& points = annotation.at("points");
    Point best = ReadPoint(points[0]);
    double distance = Distance(p, best);
    const bool closed = annotation.at("shape") == "polygon";
    for (std::size_t i = 1; i < points.size() + (closed ? 1 : 0); ++i) {
        auto a = ReadPoint(points[i - 1]), b = ReadPoint(points[i % points.size()]);
        double dot = 0, norm = 0;
        for (int d = 0; d < 3; ++d) {dot += (p[d] - a[d]) * (b[d] - a[d]); norm += (b[d] - a[d]) * (b[d] - a[d]);}
        const double t = norm > 0 ? std::clamp(dot / norm, 0.0, 1.0) : 0;
        Point q{}; for (int d = 0; d < 3; ++d) q[d] = a[d] + t * (b[d] - a[d]);
        const double candidate = Distance(p, q);
        if (candidate < distance) {distance = candidate; best = q;}
    }
    return {distance, best};
}
}

void SiteContextStore::Put(json site) {
    for (auto key : {"scene_version", "model_id", "scene_url", "tileset_id", "calibration_profile", "coordinate_frame", "world_coordinate_frame", "unit", "source_evidence_id"}) Required(site, key);
    if (site.contains("model_revision")) Required(site, "model_revision");
    if (site.at("unit") != "m") throw std::invalid_argument("site local coordinates must use metres");
    if (site.at("world_coordinate_frame") != "ECEF_WGS84") throw std::invalid_argument("site world frame must be ECEF_WGS84");
    const auto& matrix = site.at("to_world_matrix");
    if (!matrix.is_array() || matrix.size() != 16) throw std::invalid_argument("site matrix must contain 16 numbers");
    for (const auto& v : matrix) if (!v.is_number() || !std::isfinite(v.get<double>())) throw std::invalid_argument("finite site matrix required");
    for (int c = 0; c < 4; ++c) if (std::abs(matrix[c * 4 + 3].get<double>() - (c == 3 ? 1 : 0)) > 1e-9)
        throw std::invalid_argument("affine site matrix required");
    // Distances are in local metres. Refuse scaled/skewed matrices rather than
    // silently computing the wrong distance in an arbitrary model unit.
    for (int a = 0; a < 3; ++a) for (int b = 0; b < 3; ++b) {
        double dot = 0; for (int r = 0; r < 3; ++r) dot += matrix[a * 4 + r].get<double>() * matrix[b * 4 + r].get<double>();
        if (std::abs(dot - (a == b ? 1 : 0)) > 1e-6) throw std::invalid_argument("rigid metre-preserving site matrix required");
    }
    const double tolerance = site.at("duplicate_tolerance_m").get<double>();
    if (!std::isfinite(tolerance) || tolerance < 0) throw std::invalid_argument("finite nonnegative duplicate tolerance required");
    auto& cameras = site.at("cameras");
    if (!cameras.is_array()) throw std::invalid_argument("site cameras must be an array");
    std::set<std::string> camera_ids;
    for (const auto& c : cameras) {
        Required(c, "camera_id");
        if (!camera_ids.insert(c.at("camera_id").get<std::string>()).second) throw std::invalid_argument("duplicate camera catalog ID");
        if (!c.at("vcl_channel_id").is_null()) c.at("vcl_channel_id").get<std::string>();
    }
    auto& annotations = site.at("annotations");
    if (!annotations.is_array()) throw std::invalid_argument("site annotations must be an array");
    std::set<std::string> ids;
    for (const auto& a : annotations) {
        for (auto key : {"annotation_id", "source_scene_id", "name", "kind_id", "role", "shape"}) Required(a, key);
        if (!ids.insert(a.at("annotation_id").get<std::string>()).second) throw std::invalid_argument("duplicate site annotation ID");
        const auto role = a.at("role").get<std::string>(), shape = a.at("shape").get<std::string>();
        if (role != "CAMERA" && role != "LANDMARK" && role != "BARRIER" && role != "ROUTE_CANDIDATE") throw std::invalid_argument("unsupported site role");
        if (shape != "point" && shape != "polyline" && shape != "polygon") throw std::invalid_argument("unsupported site shape");
        if (role == "CAMERA" && shape != "point") throw std::invalid_argument("camera marker must be a point");
        a.at("visible").get<bool>(); a.at("camera_id").get<std::string>();
        const auto& points = a.at("points");
        const auto minimum = shape == "polygon" ? 3U : shape == "polyline" ? 2U : 1U;
        if (!points.is_array() || points.size() < minimum || (shape == "point" && points.size() != 1)) throw std::invalid_argument("invalid site shape point count");
        for (const auto& p : points) World(matrix, ReadPoint(p));
    }
    std::sort(cameras.begin(), cameras.end(), [](const auto& a, const auto& b) {return a.at("camera_id") < b.at("camera_id");});
    std::sort(annotations.begin(), annotations.end(), [](const auto& a, const auto& b) {return a.at("annotation_id") < b.at("annotation_id");});
    const auto version = site.at("scene_version").get<std::string>(), encoded = site.dump();
    auto old = state_ ? state_->Get("site_context", version) : std::optional<std::string>{};
    if (!state_) {auto it = sites_.find(version); if (it != sites_.end()) old = it->second;}
    if (old && *old != encoded) throw std::invalid_argument("site changed without a new scene_version");
    if (state_) state_->Put("site_context", version, encoded); else sites_[version] = encoded;
}

json SiteContextStore::Get(const std::string& version) const {
    auto encoded = state_ ? state_->Get("site_context", version) : std::optional<std::string>{};
    if (!state_) {auto it = sites_.find(version); if (it != sites_.end()) encoded = it->second;}
    if (!encoded) throw std::out_of_range("site scene version not found");
    return json::parse(*encoded);
}

json SiteContextStore::CameraContext(const std::string& version, const std::string& camera_ref,
                                     const std::string& id_namespace, double radius_m) const {
    if (camera_ref.empty()) throw std::invalid_argument("camera_ref is required");
    if (id_namespace != "CAMERA_ID" && id_namespace != "VCL_CHANNEL_ID") throw std::invalid_argument("explicit CAMERA_ID or VCL_CHANNEL_ID namespace required");
    if (!std::isfinite(radius_m) || radius_m < 0) throw std::invalid_argument("finite nonnegative context radius required");
    const auto site = Get(version);
    json result = {{"scene_version", version}, {"model_id", site.at("model_id")}, {"scene_url", site.at("scene_url")},
        {"calibration_profile", site.at("calibration_profile")}, {"coordinate_frame", site.at("coordinate_frame")},
        {"world_coordinate_frame", site.at("world_coordinate_frame")}, {"unit", "m"},
        {"source_evidence_id", site.at("source_evidence_id")}, {"camera_ref", camera_ref}, {"id_namespace", id_namespace},
        {"person_position_observed", false}, {"navigation_verified", false}, {"candidate_markers", json::array()}, {"status", "UNKNOWN_CAMERA"}};
    std::set<std::string> matches;
    for (const auto& c : site.at("cameras")) {
        const auto& ref = c.at(id_namespace == "CAMERA_ID" ? "camera_id" : "vcl_channel_id");
        if (ref.is_string() && ref == camera_ref) matches.insert(c.at("camera_id").get<std::string>());
    }
    if (matches.empty()) return result;
    if (matches.size() > 1) {result["status"] = "AMBIGUOUS_DEVICE"; result["camera_candidates"] = matches; return result;}
    const auto camera_id = *matches.begin(); result["camera_id"] = camera_id;
    result["status"] = "NO_PUBLISHED_CAMERA_MARKER";
    std::vector<Point> positions;
    for (const auto& a : site.at("annotations")) {
        if (a.at("role") != "CAMERA" || a.at("camera_id") != camera_id || !a.at("visible").get<bool>()) continue;
        const auto p = ReadPoint(a.at("points")[0]); positions.push_back(p);
        json marker = a; marker["local_xyz"] = p; marker["world_ecef_xyz"] = World(site.at("to_world_matrix"), p);
        marker["nearby_annotations"] = json::array();
        for (const auto& other : site.at("annotations")) {
            if (other.at("role") == "CAMERA" || !other.at("visible").get<bool>()) continue;
            const auto nearest = Closest(p, other);
            if (nearest.first > radius_m) continue;
            json nearby = other; nearby["distance_to_geometry_m"] = nearest.first;
            nearby["nearest_local_xyz"] = nearest.second;
            marker["nearby_annotations"].push_back(std::move(nearby));
        }
        auto& nearby = marker["nearby_annotations"];
        std::sort(nearby.begin(), nearby.end(), [](const auto& x, const auto& y) {
            if (x.at("distance_to_geometry_m") != y.at("distance_to_geometry_m")) return x.at("distance_to_geometry_m") < y.at("distance_to_geometry_m");
            return x.at("annotation_id") < y.at("annotation_id");
        });
        result["candidate_markers"].push_back(std::move(marker));
    }
    if (positions.empty()) return result;
    double separation = 0;
    for (const auto& a : positions) for (const auto& b : positions) separation = std::max(separation, Distance(a, b));
    result["max_marker_separation_m"] = separation;
    result["status"] = separation > site.at("duplicate_tolerance_m").get<double>() ? "AMBIGUOUS_POSITION" : "BOUND_MARKER";
    // Never average conflicting annotations or turn a nearby landmark into a
    // confirmed observation region. The caller retains all source candidates.
    return result;
}
}
