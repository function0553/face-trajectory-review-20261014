#include "face_trajectory/reachability.hpp"

#include <algorithm>
#include <cstdint>
#include <functional>
#include <limits>
#include <map>
#include <queue>
#include <set>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace face_trajectory {
namespace {
using nlohmann::json;
using Time = std::int64_t;
constexpr auto kMinTime = std::numeric_limits<Time>::min();
constexpr auto kMaxTime = std::numeric_limits<Time>::max();

Time Integer(const json& value) {
    if (value.is_number_unsigned()) {
        const auto number = value.get<std::uint64_t>();
        if (number > static_cast<std::uint64_t>(kMaxTime))
            throw std::invalid_argument("reachability integer is outside int64 range");
        return static_cast<Time>(number);
    }
    if (!value.is_number_integer())
        throw std::invalid_argument("reachability times and limits must be integers");
    return value.get<Time>();
}

Time Limit(const json& options, const char* key, Time fallback, Time maximum) {
    const auto value = options.contains(key) ? Integer(options.at(key)) : fallback;
    if (value <= 0 || value > maximum)
        throw std::invalid_argument(std::string(key) + " is outside its supported positive range");
    return value;
}

Time Lower(Time time, Time uncertainty) {
    return time < kMinTime + uncertainty ? kMinTime : time - uncertainty;
}

Time Upper(Time time, Time uncertainty) {
    return time > kMaxTime - uncertainty ? kMaxTime : time + uncertainty;
}

std::uint64_t Difference(Time later, Time earlier) {
    // Unsigned subtraction is exact over the full int64 range when later >= earlier.
    return static_cast<std::uint64_t>(later) - static_cast<std::uint64_t>(earlier);
}

bool Accepted(const SightingEvidence& event, const TrajectorySnapshot& snapshot) {
    return event.target_id == snapshot.target_id && !event.observation_id.empty() &&
           !event.camera_id.empty() && !event.attribution_evidence_id.empty() &&
           !event.source_id.empty() && event.link_status == TargetLinkStatus::Confirmed &&
           event.time_source == TimeSource::UtcCapture && event.clock_domain == "UTC" &&
           event.clock_uncertainty_ms >= 0 &&
           event.clock_uncertainty_ms <= snapshot.options.max_clock_uncertainty_ms;
}
}  // namespace

nlohmann::json PredictReachable(const TrajectorySnapshot& snapshot, const json& options) {
    if (!options.is_object()) throw std::invalid_argument("reachability options must be an object");
    const auto as_of = Integer(options.at("as_of_unix_ms"));
    const auto at = Integer(options.at("at_unix_ms"));
    if (at < as_of) throw std::invalid_argument("at_unix_ms must be at or after as_of_unix_ms");
    const auto horizon = Limit(options, "max_horizon_ms", 300000, 86400000);
    const auto max_expansions = static_cast<std::size_t>(Limit(options, "max_expansions", 10000, 1000000));
    const auto max_candidates = static_cast<std::size_t>(Limit(options, "max_candidates", 10000, 100000));
    const auto max_edges = static_cast<std::size_t>(Limit(options, "max_graph_edges", 100000, 1000000));
    const auto max_evidence = static_cast<std::size_t>(Limit(options, "max_evidence", 100000, 1000000));
    const auto verified = options.value("topology_verified", false);
    const auto complete = options.value("topology_is_complete", false);
    const auto edge_wait = options.value("allow_edge_wait", true);
    json result = {{"target_id", snapshot.target_id}, {"version", snapshot.version},
        {"graph_version", snapshot.graph.version}, {"as_of_unix_ms", as_of}, {"at_unix_ms", at},
        {"status", "UNKNOWN"}, {"reason", "NO_ACCEPTED_OBSERVATIONS"},
        {"search_complete", false}, {"coverage_complete", false},
        {"topology_verified", verified}, {"topology_is_complete", complete},
        {"waiting_at_camera_allowed", true}, {"allow_edge_wait", edge_wait},
        {"has_calibrated_probabilities", false}, {"point_position_estimated", false},
        {"max_horizon_ms", horizon}, {"expansions", 0},
        {"knowledge_basis", "SNAPSHOT_VERSION_AND_NOMINAL_CAPTURE_CUTOFF"},
        {"ingestion_time_history_available", false},
        {"candidate_cameras", json::array()}, {"candidate_edges", json::array()},
        {"based_on_observation_ids", json::array()}, {"latest_observation_candidates", json::array()},
        {"origins_without_graph_edges", json::array()}, {"excluded_future_observation_count", 0}};
    if (snapshot.evidence.size() > max_evidence || snapshot.graph.edges.size() > max_edges) {
        result["status"] = "TRUNCATED";
        result["reason"] = "INPUT_RESOURCE_LIMIT";
        return result;
    }

    std::vector<const SightingEvidence*> accepted;
    Time latest_lower = kMinTime;
    Time latest_upper = kMinTime;
    Time latest_nominal = kMinTime;
    std::size_t future_count = 0;
    const std::set<std::string> rejected(snapshot.rejected_observation_ids.begin(),
                                         snapshot.rejected_observation_ids.end());
    for (const auto& event : snapshot.evidence) {
        if (rejected.count(event.observation_id) || !Accepted(event, snapshot)) continue;
        if (event.captured_at_unix_ms > as_of) { ++future_count; continue; }
        accepted.push_back(&event);
        latest_lower = std::max(latest_lower, Lower(event.captured_at_unix_ms, event.clock_uncertainty_ms));
        latest_upper = std::max(latest_upper, Upper(event.captured_at_unix_ms, event.clock_uncertainty_ms));
        latest_nominal = std::max(latest_nominal, event.captured_at_unix_ms);
    }
    result["excluded_future_observation_count"] = future_count;
    if (accepted.empty()) return result;
    std::sort(accepted.begin(), accepted.end(), [](auto a, auto b) {
        return a->observation_id < b->observation_id;
    });
    std::set<std::string> origin_cameras;
    for (const auto* event : accepted) {
        // Any event whose upper bound reaches the greatest lower bound can be last.
        // Starting it before that lower bound would contradict another known event.
        result["based_on_observation_ids"].push_back(event->observation_id);
        const auto upper = Upper(event->captured_at_unix_ms, event->clock_uncertainty_ms);
        if (upper < latest_lower) continue;
        origin_cameras.insert(event->camera_id);
        result["latest_observation_candidates"].push_back({
            {"observation_id", event->observation_id}, {"camera_id", event->camera_id},
            {"captured_at_unix_ms", event->captured_at_unix_ms},
            {"earliest_unix_ms", latest_lower}, {"latest_unix_ms", upper}});
    }
    result["last_observation_at_unix_ms"] = latest_nominal;
    result["latest_capture_bounds"] = {{"earliest_unix_ms", latest_lower}, {"latest_unix_ms", latest_upper}};
    result["observation_age_ms"] = Difference(as_of, latest_nominal);
    result["forecast_horizon_ms"] = Difference(at, latest_lower);
    result["latest_camera_is_ambiguous"] = origin_cameras.size() > 1;
    if (at < latest_upper) {
        result["reason"] = "CAPTURE_TIME_OVERLAPS_QUERY";
        return result;
    }
    if (Difference(at, latest_lower) > static_cast<std::uint64_t>(horizon)) {
        result["reason"] = "OBSERVATION_TOO_OLD_FOR_HORIZON";
        return result;
    }

    // The snapshot graph was already normalized on rebuild. Normalize here too
    // because this function is also a public library entry point.
    const auto graph = NormalizeCameraGraph(snapshot.graph);
    std::map<std::string, std::vector<const CameraEdge*>> outgoing;
    std::set<std::string> graph_cameras;
    for (const auto& edge : graph.edges) {
        if (edge.edge_id.empty())
            throw std::invalid_argument("reachability requires a stable nonempty edge_id on every edge");
        outgoing[edge.from_camera].push_back(&edge);
        graph_cameras.insert(edge.from_camera);
        graph_cameras.insert(edge.to_camera);
    }
    using Pending = std::pair<Time, std::string>;
    std::priority_queue<Pending, std::vector<Pending>, std::greater<Pending>> pending;
    std::map<std::string, Time> arrival;
    std::map<std::string, json> channels;
    bool truncated = false;
    for (const auto& origin : origin_cameras) {
        if (!graph_cameras.count(origin)) result["origins_without_graph_edges"].push_back(origin);
        if (arrival.size() == max_candidates) { truncated = true; break; }
        arrival.emplace(origin, latest_lower);
        pending.emplace(latest_lower, origin);
    }
    std::size_t expansions = 0;
    while (!pending.empty() && !truncated) {
        auto [time, camera] = pending.top();
        pending.pop();
        if (time != arrival.at(camera)) continue;
        const auto edges = outgoing.find(camera);
        if (edges == outgoing.end()) continue;
        for (const auto* edge : edges->second) {
            if (expansions == max_expansions) { truncated = true; break; }
            ++expansions;
            const auto departure = std::max(time, edge->open_from_unix_ms.value_or(kMinTime));
            const auto last_departure = std::min(at, edge->open_until_unix_ms.value_or(kMaxTime));
            if (departure > last_departure) continue;

            const auto occupancy_from = edge_wait ? departure
                : std::max(departure, Lower(at, edge->max_travel_ms));
            if (occupancy_from <= last_departure) {
                if (!channels.count(edge->edge_id) && arrival.size() + channels.size() == max_candidates) {
                    truncated = true; break;
                }
                channels[edge->edge_id] = {{"edge_id", edge->edge_id},
                    {"from_camera", edge->from_camera}, {"to_camera", edge->to_camera},
                    {"earliest_departure_unix_ms", occupancy_from},
                    {"latest_departure_unix_ms", last_departure},
                    {"possible_at_unix_ms", at}};
            }
            if (departure > kMaxTime - edge->min_travel_ms) continue;
            const auto next = departure + edge->min_travel_ms;
            if (next > at) continue;
            const auto old = arrival.find(edge->to_camera);
            if (old == arrival.end()) {
                if (arrival.size() + channels.size() == max_candidates) { truncated = true; break; }
                arrival.emplace(edge->to_camera, next);
                pending.emplace(next, edge->to_camera);
            } else if (next < old->second) {
                old->second = next;
                pending.emplace(next, edge->to_camera);
            }
        }
    }
    for (const auto& [camera, time] : arrival)
        result["candidate_cameras"].push_back({{"camera_id", camera}, {"earliest_arrival_unix_ms", time}});
    for (const auto& [id, channel] : channels) result["candidate_edges"].push_back(channel);
    const bool missing_origins = !result["origins_without_graph_edges"].empty();
    result["expansions"] = expansions;
    result["search_complete"] = !truncated;
    result["coverage_complete"] = !truncated && verified && complete && !missing_origins;
    result["status"] = truncated ? "TRUNCATED"
        : verified && complete && !missing_origins ? "CANDIDATE" : "CONDITIONAL_CANDIDATE";
    result["reason"] = truncated ? "SEARCH_RESOURCE_LIMIT"
        : missing_origins ? "ORIGIN_NOT_REPRESENTED_IN_GRAPH"
        : verified && complete ? "REACHABLE_UNDER_DECLARED_ASSUMPTIONS" : "UNVERIFIED_OR_INCOMPLETE_TOPOLOGY";
    return result;
}

}  // namespace face_trajectory
