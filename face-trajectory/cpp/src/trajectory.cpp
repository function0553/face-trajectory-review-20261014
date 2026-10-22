#include "face_trajectory/trajectory.hpp"
#include "face_trajectory/state.hpp"
#include "face_trajectory/storage_json.hpp"
#include "face_trajectory/shared_json.hpp"

#include <algorithm>
#include <cmath>
#include <functional>
#include <limits>
#include <queue>
#include <set>
#include <stdexcept>
#include <tuple>
#include <utility>

namespace face_trajectory {
namespace {

std::vector<SightingEvidence> CanonicalSightings(
    const std::string& target_id, const std::vector<SightingEvidence>& sightings) {
    std::map<std::string, SightingEvidence> by_id;
    for (const auto& sighting : sightings) {
        if (sighting.target_id != target_id) {
            throw std::invalid_argument("all sightings must belong to target_id");
        }
        auto [it, inserted] = by_id.emplace(sighting.observation_id, sighting);
        if (!inserted && !SameSighting(it->second, sighting)) {
            throw std::invalid_argument("conflicting duplicate observation_id");
        }
    }
    std::vector<SightingEvidence> result;
    result.reserve(by_id.size());
    for (const auto& [id, sighting] : by_id) result.push_back(sighting);
    return result;
}

bool SameSightings(const std::vector<SightingEvidence>& a,
                   const std::vector<SightingEvidence>& b) {
    if (a.size() != b.size()) return false;
    for (std::size_t i = 0; i < a.size(); ++i) {
        if (!SameSighting(a[i], b[i])) return false;
    }
    return true;
}

auto EdgeKey(const CameraEdge& edge) {
    return std::tie(edge.from_camera, edge.to_camera, edge.min_travel_ms,
                    edge.max_travel_ms, edge.cost, edge.open_from_unix_ms,
                    edge.open_until_unix_ms, edge.edge_id);
}

CameraGraph CanonicalGraph(CameraGraph graph) {
    if (graph.version.empty()) throw std::invalid_argument("graph version is required");
    std::set<std::string> edge_ids;
    for (const auto& edge : graph.edges) {
        if (edge.from_camera.empty() || edge.to_camera.empty() ||
            edge.min_travel_ms < 0 || edge.max_travel_ms < edge.min_travel_ms ||
            !std::isfinite(edge.cost) || edge.cost < 0 ||
            (edge.open_from_unix_ms && edge.open_until_unix_ms &&
             *edge.open_from_unix_ms > *edge.open_until_unix_ms)) {
            throw std::invalid_argument("invalid camera edge");
        }
        if (!edge.edge_id.empty() && !edge_ids.insert(edge.edge_id).second) {
            throw std::invalid_argument("duplicate camera edge_id");
        }
    }
    std::sort(graph.edges.begin(), graph.edges.end(), [](const auto& a, const auto& b) {
        return EdgeKey(a) < EdgeKey(b);
    });
    graph.edges.erase(std::unique(graph.edges.begin(), graph.edges.end(),
                                  [](const auto& a, const auto& b) {
                                      return EdgeKey(a) == EdgeKey(b);
                                  }), graph.edges.end());
    return graph;
}

bool SameGraph(const CameraGraph& a, const CameraGraph& b) {
    if (a.version != b.version || a.edges.size() != b.edges.size()) return false;
    for (std::size_t i = 0; i < a.edges.size(); ++i) {
        if (EdgeKey(a.edges[i]) != EdgeKey(b.edges[i])) return false;
    }
    return true;
}

bool SameOptions(const TrajectoryOptions& a, const TrajectoryOptions& b) {
    return std::tie(a.max_visit_gap_ms, a.max_clock_uncertainty_ms,
                    a.session_gap_ms, a.max_hops, a.max_expansions,
                    a.max_candidates, a.backup_score_margin,
                    a.travel_time_weight_per_second, a.non_overlapping_cameras) ==
           std::tie(b.max_visit_gap_ms, b.max_clock_uncertainty_ms,
                    b.session_gap_ms, b.max_hops, b.max_expansions,
                    b.max_candidates, b.backup_score_margin,
                    b.travel_time_weight_per_second, b.non_overlapping_cameras);
}

void ValidateOptions(const TrajectoryOptions& options) {
    if (options.max_visit_gap_ms < 0 || options.max_clock_uncertainty_ms < 0 ||
        options.session_gap_ms < 0 || options.max_hops == 0 ||
        options.max_expansions == 0 || options.max_candidates == 0 ||
        !std::isfinite(options.backup_score_margin) || options.backup_score_margin < 0 ||
        !std::isfinite(options.travel_time_weight_per_second) ||
        options.travel_time_weight_per_second < 0) {
        throw std::invalid_argument("invalid trajectory options");
    }
}

bool Add(std::int64_t a, std::int64_t b, std::int64_t& result) {
    if (b > 0 && a > std::numeric_limits<std::int64_t>::max() - b) return false;
    result = a + b;
    return true;
}

bool Subtract(std::int64_t a, std::int64_t b, std::int64_t& result) {
    if (b > 0 && a < std::numeric_limits<std::int64_t>::min() + b) return false;
    result = a - b;
    return true;
}

std::optional<PathCandidate> EvaluatePath(const std::vector<const CameraEdge*>& path,
                                          const Anchor& from, const Anchor& to,
                                          double time_weight) {
    // Search gates the route with conservative endpoints. Timing uses the outer
    // feasible endpoints so actual traversals are not excluded from the output.
    const auto start = from.time_bounds.last_earliest_unix_ms;
    const auto end = to.time_bounds.first_latest_unix_ms;
    if (end < start) return std::nullopt;

    std::vector<std::int64_t> earliest(path.size() + 1);
    std::vector<std::int64_t> latest(path.size() + 1);
    earliest[0] = start;
    double edge_cost = 0;
    long double max_travel = 0;
    for (std::size_t i = 0; i < path.size(); ++i) {
        const auto& edge = *path[i];
        const auto depart = std::max(earliest[i], edge.open_from_unix_ms.value_or(
            std::numeric_limits<std::int64_t>::min()));
        if (depart > edge.open_until_unix_ms.value_or(
                std::numeric_limits<std::int64_t>::max()) ||
            !Add(depart, edge.min_travel_ms, earliest[i + 1]) || earliest[i + 1] > end) {
            return std::nullopt;
        }
        edge_cost += edge.cost;
        max_travel += edge.max_travel_ms;
    }
    latest.back() = end;
    for (std::size_t i = path.size(); i-- > 0;) {
        std::int64_t depart;
        if (!Subtract(latest[i + 1], path[i]->min_travel_ms, depart)) return std::nullopt;
        latest[i] = std::min(depart, path[i]->open_until_unix_ms.value_or(
            std::numeric_limits<std::int64_t>::max()));
        if (latest[i] < path[i]->open_from_unix_ms.value_or(
                std::numeric_limits<std::int64_t>::min()) || latest[i] < earliest[i]) {
            return std::nullopt;
        }
    }

    PathCandidate candidate;
    candidate.nodes.push_back({from.camera_id, NodeStatus::Observed,
                               from.time_bounds.first_earliest_unix_ms,
                               from.time_bounds.last_latest_unix_ms});
    for (std::size_t i = 0; i < path.size(); ++i) {
        candidate.edge_ids.push_back(path[i]->edge_id);
        candidate.edge_time_bounds.push_back({
            std::max(earliest[i], path[i]->open_from_unix_ms.value_or(
                std::numeric_limits<std::int64_t>::min())), latest[i + 1]});
        if (i + 1 == path.size()) {
            candidate.nodes.push_back({to.camera_id, NodeStatus::Observed,
                                       to.time_bounds.first_earliest_unix_ms,
                                       to.time_bounds.last_latest_unix_ms});
        } else {
            candidate.nodes.push_back({path[i]->to_camera, NodeStatus::Inferred,
                                       earliest[i + 1], latest[i + 1]});
        }
    }
    const auto available = static_cast<long double>(to.time_bounds.first_earliest_unix_ms) -
                           static_cast<long double>(from.time_bounds.last_latest_unix_ms);
    candidate.edge_cost = edge_cost;
    candidate.time_penalty = static_cast<double>(
        std::max(0.0L, available - max_travel) / 1000.0L * time_weight);
    candidate.score = -candidate.edge_cost - candidate.time_penalty;
    return candidate;
}

using RouteIdentity = std::vector<std::tuple<std::string, std::string, std::string>>;

RouteIdentity PathIdentity(const PathCandidate& candidate) {
    RouteIdentity result;
    result.reserve(candidate.edge_ids.size());
    for (std::size_t i = 0; i < candidate.edge_ids.size(); ++i) {
        result.emplace_back(candidate.nodes[i].camera_id, candidate.nodes[i + 1].camera_id,
                            candidate.edge_ids[i]);
    }
    return result;
}

using CameraAdjacency = std::map<std::string, std::vector<const CameraEdge*>>;

CameraAdjacency BuildAdjacency(const CameraGraph& graph) {
    CameraAdjacency adjacency;
    for (const auto& edge : graph.edges) adjacency[edge.from_camera].push_back(&edge);
    return adjacency;
}

// Reverse Dijkstra gives a lower bound without coordinates or opening windows.
// Ignoring windows makes the bound optimistic, so pruning cannot remove a route.
std::map<std::string, std::int64_t> MinimumRemainingTravel(
    const CameraGraph& graph, const std::string& destination) {
    CameraAdjacency incoming;
    for (const auto& edge : graph.edges) incoming[edge.to_camera].push_back(&edge);
    using Entry = std::pair<std::int64_t, std::string>;
    std::priority_queue<Entry, std::vector<Entry>, std::greater<Entry>> pending;
    std::map<std::string, std::int64_t> minimum{{destination, 0}};
    pending.push({0, destination});
    while (!pending.empty()) {
        const auto [distance, camera] = pending.top();
        pending.pop();
        if (distance != minimum.at(camera)) continue;
        for (const auto* edge : incoming[camera]) {
            std::int64_t next;
            if (!Add(distance, edge->min_travel_ms, next)) continue;
            const auto found = minimum.find(edge->from_camera);
            if (found == minimum.end() || next < found->second) {
                minimum[edge->from_camera] = next;
                pending.push({next, edge->from_camera});
            }
        }
    }
    return minimum;
}

TrajectorySegment BuildSegment(const Anchor& from, const Anchor& to,
                               const CameraGraph& graph, const CameraAdjacency& adjacency,
                               const TrajectoryOptions& options) {
    const auto from_earliest = from.time_bounds.last_earliest_unix_ms;
    const auto from_latest = from.time_bounds.last_latest_unix_ms;
    const auto to_earliest = to.time_bounds.first_earliest_unix_ms;
    const auto to_latest = to.time_bounds.first_latest_unix_ms;
    const bool overlapping = to_earliest <= from_latest;
    TrajectorySegment segment{"segment:" + from.anchor_id + ":" + to.anchor_id,
                              graph.version,
                              from.anchor_id, to.anchor_id,
                              std::min(from_earliest, to_earliest),
                              std::max(from_latest, to_latest),
                              NodeStatus::Unknown, true, false, std::nullopt, {}};
    // Clock-overlapping observation intervals do not establish a movement order.
    if (overlapping) return segment;
    const auto minimum_remaining = MinimumRemainingTravel(graph, to.camera_id);
    std::vector<PathCandidate> found;
    std::vector<const CameraEdge*> path;
    std::set<std::string> visited{from.camera_id};
    std::size_t expansions = 0;
    bool expansion_limit = false;
    std::function<void(const std::string&, std::int64_t)> search =
        [&](const std::string& current, std::int64_t earliest) {
        auto it = adjacency.find(current);
        if (it == adjacency.end()) return;
        for (const auto* edge : it->second) {
            const bool at_destination = edge->to_camera == to.camera_id;
            if (!at_destination && visited.count(edge->to_camera)) continue;
            if (at_destination && edge->to_camera != from.camera_id &&
                visited.count(edge->to_camera)) continue;
            const auto depart = std::max(earliest, edge->open_from_unix_ms.value_or(
                std::numeric_limits<std::int64_t>::min()));
            std::int64_t arrival;
            if (depart > edge->open_until_unix_ms.value_or(
                    std::numeric_limits<std::int64_t>::max()) ||
                !Add(depart, edge->min_travel_ms, arrival) || arrival > to_earliest) {
                continue;
            }
            const auto remaining = minimum_remaining.find(edge->to_camera);
            std::int64_t earliest_finish;
            if (remaining == minimum_remaining.end() ||
                !Add(arrival, remaining->second, earliest_finish) || earliest_finish > to_earliest) {
                continue;
            }
            if (expansions == options.max_expansions) {
                segment.search_complete = false;
                expansion_limit = true;
                return;
            }
            ++expansions;
            if (path.size() >= options.max_hops) {
                // The hop cap is a resource limit, never evidence of uniqueness.
                segment.search_complete = false;
                continue;
            }
            path.push_back(edge);
            if (at_destination) {
                auto candidate = EvaluatePath(path, from, to,
                                              options.travel_time_weight_per_second);
                if (candidate) found.push_back(std::move(*candidate));
            } else {
                visited.insert(edge->to_camera);
                search(edge->to_camera, arrival);
                visited.erase(edge->to_camera);
            }
            path.pop_back();
            if (expansion_limit) return;
        }
    };
    search(from.camera_id, from_latest);

    std::sort(found.begin(), found.end(), [](const auto& a, const auto& b) {
        if (a.score != b.score) return a.score > b.score;
        return PathIdentity(a) < PathIdentity(b);
    });
    std::set<RouteIdentity> seen_paths;
    std::vector<PathCandidate> ranked;
    for (auto& candidate : found) {
        if (seen_paths.insert(PathIdentity(candidate)).second) {
            ranked.push_back(std::move(candidate));
        }
    }
    segment.unique_path = ranked.size() == 1 && segment.search_complete;
    if (!ranked.empty()) {
        segment.status = NodeStatus::Inferred;
        segment.primary = std::move(ranked[0]);
        for (std::size_t i = 1; i < ranked.size() &&
                                i < options.max_candidates; ++i) {
            if (segment.primary->score - ranked[i].score > options.backup_score_margin) break;
            segment.backups.push_back(std::move(ranked[i]));
        }
    }
    return segment;
}

TrajectorySnapshot BuildSnapshot(const std::string& target_id,
                                 const std::vector<SightingEvidence>& sightings,
                                 const CameraGraph& graph,
                                 const TrajectoryOptions& options,
                                 std::uint64_t version) {
    auto built = BuildCameraVisits(sightings, options.max_visit_gap_ms,
                                   options.max_clock_uncertainty_ms, options.non_overlapping_cameras);
    TrajectorySnapshot result{target_id, version, graph, options, sightings,
                              std::move(built.visits), {}, {}, {},
                              std::move(built.rejected_observation_ids)};
    const auto adjacency = BuildAdjacency(graph);
    std::sort(result.visits.begin(), result.visits.end(), [](const auto& a, const auto& b) {
        return std::tie(a.first_seen_unix_ms, a.last_seen_unix_ms, a.camera_id, a.visit_id) <
               std::tie(b.first_seen_unix_ms, b.last_seen_unix_ms, b.camera_id, b.visit_id);
    });
    for (const auto& visit : result.visits) {
        result.anchors.push_back({"anchor:" + visit.visit_id, visit.visit_id,
                                  visit.camera_id, visit.first_seen_unix_ms,
                                  visit.last_seen_unix_ms,
                                  visit.first_seen_clock_uncertainty_ms,
                                  visit.last_seen_clock_uncertainty_ms, visit.time_bounds});
    }
    std::size_t frontier = 0;
    std::vector<std::size_t> tied_frontiers;
    const auto refresh_frontiers = [&](std::size_t index) {
        tied_frontiers.push_back(index);
        std::int64_t latest_lower = std::numeric_limits<std::int64_t>::min();
        for (const auto candidate : tied_frontiers) {
            const auto& anchor = result.anchors[candidate];
            latest_lower = std::max(latest_lower, anchor.time_bounds.last_earliest_unix_ms);
        }
        tied_frontiers.erase(std::remove_if(tied_frontiers.begin(), tied_frontiers.end(),
            [&](std::size_t candidate) {
                const auto& anchor = result.anchors[candidate];
                return anchor.time_bounds.last_latest_unix_ms < latest_lower;
            }), tied_frontiers.end());
        frontier = tied_frontiers.front();
    };
    for (std::size_t i = 0; i < result.anchors.size(); ++i) {
        const auto& anchor = result.anchors[i];
        bool new_session = result.sessions.empty();
        if (!new_session) {
            const auto& session = result.sessions.back();
            const auto anchor_earliest = anchor.time_bounds.first_earliest_unix_ms;
            const auto session_latest = session.time_bounds.last_latest_unix_ms;
            if (anchor_earliest > session_latest) {
                const auto difference = static_cast<std::uint64_t>(anchor_earliest) -
                                        static_cast<std::uint64_t>(session_latest);
                new_session = difference > static_cast<std::uint64_t>(options.session_gap_ms);
            }
        }
        if (new_session) {
            frontier = i;
            tied_frontiers = {i};
            result.sessions.push_back({"session:" + anchor.anchor_id,
                                       anchor.first_seen_unix_ms, anchor.last_seen_unix_ms,
                                       anchor.first_seen_clock_uncertainty_ms,
                                       anchor.last_seen_clock_uncertainty_ms,
                                       {anchor.anchor_id}, {}, anchor.time_bounds});
            continue;
        }
        auto& session = result.sessions.back();
        MergeCaptureTimeBounds(session.time_bounds, anchor.time_bounds);
        auto segment = BuildSegment(result.anchors[frontier], anchor, graph, adjacency, options);
        if (tied_frontiers.size() > 1) {
            std::optional<TrajectorySegment> sole_feasible;
            bool all_complete = true;
            std::size_t feasible_origins = 0;
            for (const auto index : tied_frontiers) {
                const auto& origin = result.anchors[index];
                auto candidate = index == frontier ? segment
                                                    : BuildSegment(origin, anchor, graph, adjacency, options);
                all_complete &= candidate.search_complete;
                if (candidate.primary) {
                    ++feasible_origins;
                    if (feasible_origins == 1) sole_feasible = std::move(candidate);
                }
            }
            if (feasible_origins == 1) {
                segment = std::move(*sole_feasible);
                segment.search_complete = all_complete;
                segment.unique_path &= all_complete;
            } else {
                // Clock-overlapping last observations give no supported departure camera.
                segment.status = NodeStatus::Unknown;
                segment.search_complete = all_complete;
                segment.unique_path = false;
                segment.primary.reset();
                segment.backups.clear();
            }
        }
        session.segment_ids.push_back(segment.segment_id);
        result.segments.push_back(std::move(segment));
        session.anchor_ids.push_back(anchor.anchor_id);
        if (anchor.last_seen_unix_ms > session.last_seen_unix_ms) {
            session.last_seen_unix_ms = anchor.last_seen_unix_ms;
            session.last_seen_clock_uncertainty_ms = anchor.last_seen_clock_uncertainty_ms;
        } else if (anchor.last_seen_unix_ms == session.last_seen_unix_ms) {
            session.last_seen_clock_uncertainty_ms = std::max(
                session.last_seen_clock_uncertainty_ms,
                anchor.last_seen_clock_uncertainty_ms);
        }
        refresh_frontiers(i);
    }
    return result;
}

bool SamePath(const PathCandidate& a, const PathCandidate& b) {
    if (a.score != b.score || a.edge_cost != b.edge_cost ||
        a.time_penalty != b.time_penalty || a.edge_ids != b.edge_ids ||
        a.edge_time_bounds != b.edge_time_bounds ||
        a.nodes.size() != b.nodes.size()) return false;
    for (std::size_t i = 0; i < a.nodes.size(); ++i) {
        if (std::tie(a.nodes[i].camera_id, a.nodes[i].status,
                     a.nodes[i].earliest_unix_ms, a.nodes[i].latest_unix_ms) !=
            std::tie(b.nodes[i].camera_id, b.nodes[i].status,
                     b.nodes[i].earliest_unix_ms, b.nodes[i].latest_unix_ms)) return false;
    }
    return true;
}

bool SameSegment(const TrajectorySegment& a, const TrajectorySegment& b) {
    if (std::tie(a.graph_version, a.from_unix_ms, a.to_unix_ms, a.status, a.search_complete,
                 a.unique_path) !=
        std::tie(b.graph_version, b.from_unix_ms, b.to_unix_ms, b.status, b.search_complete,
                 b.unique_path) || a.primary.has_value() != b.primary.has_value() ||
        a.backups.size() != b.backups.size()) return false;
    if (a.primary && !SamePath(*a.primary, *b.primary)) return false;
    for (std::size_t i = 0; i < a.backups.size(); ++i) {
        if (!SamePath(a.backups[i], b.backups[i])) return false;
    }
    return true;
}

std::vector<std::string> ChangedSegments(const TrajectorySnapshot* previous,
                                         const TrajectorySnapshot& next) {
    std::map<std::string, const TrajectorySegment*> old;
    if (previous) {
        for (const auto& segment : previous->segments) old[segment.segment_id] = &segment;
    }
    std::set<std::string> changed;
    for (const auto& segment : next.segments) {
        auto it = old.find(segment.segment_id);
        if (it == old.end() || !SameSegment(*it->second, segment)) {
            changed.insert(segment.segment_id);
        }
        old.erase(segment.segment_id);
    }
    for (const auto& [id, segment] : old) changed.insert(id);
    return {changed.begin(), changed.end()};
}

nlohmann::json AppendTailProof(const TrajectorySnapshot& snapshot) {
    if(snapshot.visits.empty() || !snapshot.rejected_observation_ids.empty() ||
       snapshot.visits.size()!=snapshot.anchors.size() || snapshot.sessions.empty()) return nullptr;
    const auto index=snapshot.anchors.size()-1;
    const auto& tail=snapshot.anchors.back();
    // A strictly later observation region excludes old interleaving evidence
    // that could split/merge the visit or change the incoming frontier.
    for(std::size_t i=0;i<index;++i)
        if(snapshot.anchors[i].time_bounds.last_latest_unix_ms>=tail.time_bounds.first_earliest_unix_ms) return nullptr;
    if(snapshot.sessions.back().anchor_ids.empty() || snapshot.sessions.back().anchor_ids.back()!=tail.anchor_id) return nullptr;
    nlohmann::json incoming=nullptr;
    for(std::size_t i=0;i<snapshot.segments.size();++i) {
        const auto& segment=snapshot.segments[i];
        if(segment.from_anchor_id==tail.anchor_id) return nullptr;
        if(segment.to_anchor_id==tail.anchor_id) { if(!incoming.is_null()) return nullptr; incoming=i; }
    }
    return {{"visit_index",index},{"anchor_index",index},{"session_index",snapshot.sessions.size()-1},{"incoming_segment_index",incoming}};
}

}  // namespace

CameraGraph NormalizeCameraGraph(CameraGraph graph) {
    return CanonicalGraph(std::move(graph));
}

std::vector<TrajectoryStore::TimedAnchor> TrajectoryStore::BuildTimelineIndex(
    const TrajectorySnapshot& snapshot) {
    std::map<std::string, std::size_t> observation_anchors;
    for (std::size_t i = 0; i < snapshot.visits.size(); ++i) {
        for (const auto& id : snapshot.visits[i].observation_ids) {
            observation_anchors.emplace(id, i);
        }
    }
    std::vector<TimedAnchor> timed;
    timed.reserve(observation_anchors.size());
    // Evidence is in observation-ID order. Stable sorting retains its tie rule.
    for (std::size_t i = 0; i < snapshot.evidence.size(); ++i) {
        const auto& sighting = snapshot.evidence[i];
        const auto it = observation_anchors.find(sighting.observation_id);
        if (it != observation_anchors.end()) {
            timed.push_back({sighting.captured_at_unix_ms, it->second, i});
        }
    }
    std::stable_sort(timed.begin(), timed.end(), [](const auto& a, const auto& b) {
        return a.time < b.time;
    });
    return timed;
}

TrajectoryRevision TrajectoryStore::Rebuild(
    const std::string& target_id, const std::vector<SightingEvidence>& sightings,
    const CameraGraph& graph, const TrajectoryOptions& input_options,
    const std::string& reason, std::optional<std::uint64_t> expected_version) {
    if (target_id.empty() || reason.empty()) {
        throw std::invalid_argument("target_id and revision reason are required");
    }
    auto options=input_options;
    for(auto& pair:options.non_overlapping_cameras)if(pair.second<pair.first)std::swap(pair.first,pair.second);
    std::sort(options.non_overlapping_cameras.begin(),options.non_overlapping_cameras.end());
    options.non_overlapping_cameras.erase(std::unique(options.non_overlapping_cameras.begin(),options.non_overlapping_cameras.end()),options.non_overlapping_cameras.end());
    ValidateOptions(options);
    auto canonical_input = CanonicalSightings(target_id, sightings);
    auto canonical_graph = NormalizeCameraGraph(graph);
    const auto association_revision = association_revision_reader_ ? association_revision_reader_(target_id) : 0;
    const auto check_version = [&](std::uint64_t current, bool identical) {
        if (expected_version && *expected_version > current)
            throw std::runtime_error("VERSION_CONFLICT: expected_version is ahead of current trajectory version");
        // Same current input/binding is a safe retry after an acknowledgement loss.
        if (identical) return;
        if ((!expected_version && current != 0) || (expected_version && *expected_version != current))
            throw std::runtime_error("VERSION_CONFLICT: rebuild requires current expected_version=" + std::to_string(current));
    };
    if(state_) {
        // 相机图文本只序列化一次；库里已有逐字节相同的文本时，既不必解析比较，也不必重写同一行。
        const auto graph_text=nlohmann::json(canonical_graph).dump();
        auto known=state_->Get("trajectory_graph",canonical_graph.version);
        if(known && *known!=graph_text && !SameGraph(nlohmann::json::parse(*known).get<CameraGraph>(),canonical_graph))
            throw std::invalid_argument("graph edges changed without a new graph version");
        auto head=state_->Get("trajectory_head",target_id);
        std::optional<TrajectorySnapshot> previous;
        if(head)previous=Get(target_id,std::stoull(*head));
        const bool identical = previous && previous->association_revision == association_revision &&
            SameSightings(previous->evidence,canonical_input) && SameGraph(previous->graph,canonical_graph) && SameOptions(previous->options,options);
        check_version(previous ? previous->version : 0, identical);
        if(identical)
            return {target_id,previous->version,previous->version,reason,{}};
        auto version=previous?previous->version+1:1;
        auto next=BuildSnapshot(target_id,canonical_input,canonical_graph,options,version);
        next.association_revision = association_revision;
        TrajectoryRevision revision{target_id,version-1,version,reason,ChangedSegments(previous?&*previous:nullptr,next)};
        next.revision=revision;
        const auto key=nlohmann::json::array({target_id,version}).dump();
        SharedJson shared(*state_);
        std::optional<nlohmann::json> shared_previous;
        if(previous) {
            const auto text=state_->Get("trajectory",nlohmann::json::array({target_id,previous->version}).dump());
            if(text) { auto encoded=nlohmann::json::parse(*text); if(encoded.value("storage_format","")=="refs-v2") shared_previous=std::move(encoded); }
        }
        auto payload=shared.Encode(nlohmann::json(next),false,shared_previous);
        payload["storage_format"]="refs-v2";
        payload["append_tail"]=AppendTailProof(next);
        state_->Put("trajectory",key,payload.dump(),target_id);
        state_->Put("trajectory_head",target_id,std::to_string(version));
        if(!known || *known!=graph_text) state_->Put("trajectory_graph",canonical_graph.version,graph_text);
        auto timed=BuildTimelineIndex(next);
        cold_cache_=StoredSnapshot{std::move(next),std::move(timed)};
        return revision;
    }
    auto it = histories_.find(target_id);
    if (it != histories_.end()) {
        for (const auto& stored : it->second.snapshots) {
            const auto& snapshot = stored.snapshot;
            if (snapshot.graph.version == canonical_graph.version &&
                !SameGraph(snapshot.graph, canonical_graph)) {
                throw std::invalid_argument("graph edges changed without a new graph version");
            }
        }
    }
    const bool identical = it != histories_.end() &&
        it->second.snapshots.back().snapshot.association_revision == association_revision &&
        SameSightings(it->second.snapshots.back().snapshot.evidence, canonical_input) &&
        SameGraph(it->second.snapshots.back().snapshot.graph, canonical_graph) &&
        SameOptions(it->second.snapshots.back().snapshot.options, options);
    const auto previous_version = it == histories_.end() ? 0 : it->second.snapshots.back().snapshot.version;
    check_version(previous_version, identical);
    if (identical) {
        const auto version = it->second.snapshots.back().snapshot.version;
        return {target_id, version, version, reason, {}};
    }
    auto next = BuildSnapshot(target_id, canonical_input, canonical_graph, options,
                              previous_version + 1);
    next.association_revision = association_revision;
    const auto previous = it == histories_.end() ? nullptr : &it->second.snapshots.back().snapshot;
    auto affected = ChangedSegments(previous, next);
    TrajectoryRevision revision{target_id, previous_version, previous_version + 1, reason,
                                std::move(affected)};
    next.revision = revision;
    auto timed = BuildTimelineIndex(next);
    auto& history = histories_[target_id];
    history.snapshots.push_back({std::move(next), std::move(timed)});
    return revision;
}

std::optional<TrajectoryRevision> TrajectoryStore::TryAppend(
    const SightingEvidence& sighting, std::uint64_t previous_association_revision,
    const CameraGraph& graph, const TrajectoryOptions& input_options,
    const std::string& reason, std::uint64_t expected_version) {
    if(!state_ || expected_version==0 || reason.empty() ||
       LatestVersion(sighting.target_id)!=expected_version) return {};
    const auto text=state_->Get("trajectory",nlohmann::json::array({sighting.target_id,expected_version}).dump());
    if(!text) return {};
    auto payload=nlohmann::json::parse(*text);
    if(payload.value("storage_format","")!="refs-v2" ||
       payload.at("association_revision").get<std::uint64_t>()!=previous_association_revision) return {};
    auto options=input_options;
    for(auto& pair:options.non_overlapping_cameras) if(pair.second<pair.first) std::swap(pair.first,pair.second);
    std::sort(options.non_overlapping_cameras.begin(),options.non_overlapping_cameras.end());
    options.non_overlapping_cameras.erase(std::unique(options.non_overlapping_cameras.begin(),options.non_overlapping_cameras.end()),options.non_overlapping_cameras.end());
    ValidateOptions(options);
    SharedJson shared(*state_);
    if(!SameGraph(shared.Decode(payload.at("graph")).get<CameraGraph>(),NormalizeCameraGraph(graph)) ||
       !SameOptions(shared.Decode(payload.at("options")).get<TrajectoryOptions>(),options)) return {};
    // The complete builder certifies the last visit as strictly later than
    // every other anchor. Missing/ambiguous certificates use the complete core.
    if(!payload.contains("append_tail") || payload.at("append_tail").is_null() ||
       SharedJson::Count(payload.at("rejected_observation_ids"))!=0 || !sighting.reentry_evidence_id.empty()) return {};
    const auto& proof=payload.at("append_tail");
    const auto visit_index=proof.at("visit_index").get<std::size_t>();
    const auto anchor_index=proof.at("anchor_index").get<std::size_t>();
    const auto session_index=proof.at("session_index").get<std::size_t>();
    auto visit=shared.At(payload.at("visits"),visit_index);
    const auto last=visit.at("last_seen_unix_ms").get<std::int64_t>();
    if(visit.at("camera_id")!=sighting.camera_id || sighting.captured_at_unix_ms<=last ||
       static_cast<std::uint64_t>(sighting.captured_at_unix_ms)-static_cast<std::uint64_t>(last)>
         static_cast<std::uint64_t>(options.max_visit_gap_ms)) return {};
    const auto admitted=BuildCameraVisits({sighting},options.max_visit_gap_ms,options.max_clock_uncertainty_ms,options.non_overlapping_cameras);
    if(admitted.visits.size()!=1 || !admitted.rejected_observation_ids.empty()) return {};
    const auto association_revision=association_revision_reader_?association_revision_reader_(sighting.target_id):0;
    if(association_revision<=previous_association_revision) return {};
    const auto old_bounds=visit.at("time_bounds").get<CaptureTimeBounds>();
    auto bounds=old_bounds;
    MergeCaptureTimeBounds(bounds,admitted.visits.front().time_bounds);
    // First bounds drive incoming travel feasibility, edge windows, scores and
    // session partitioning. Changing either requires full reconstruction.
    if(visit_index!=0 && (bounds.first_earliest_unix_ms!=old_bounds.first_earliest_unix_ms ||
        bounds.first_latest_unix_ms!=old_bounds.first_latest_unix_ms)) return {};
    visit["time_bounds"]=bounds;
    visit["last_seen_unix_ms"]=sighting.captured_at_unix_ms;
    visit["last_seen_clock_uncertainty_ms"]=sighting.clock_uncertainty_ms;
    visit["max_clock_uncertainty_ms"]=std::max(visit.at("max_clock_uncertainty_ms").get<std::int64_t>(),sighting.clock_uncertainty_ms);
    shared.Append(visit["observation_ids"],sighting.observation_id);
    shared.Append(visit["attribution_evidence_ids"],sighting.attribution_evidence_id);
    shared.Append(visit["source_ids"],sighting.source_id);
    shared.Set(payload["visits"],visit_index,visit);
    auto anchor=shared.At(payload.at("anchors"),anchor_index), session=shared.At(payload.at("sessions"),session_index);
    auto session_bounds=session.at("time_bounds").get<CaptureTimeBounds>();
    MergeCaptureTimeBounds(session_bounds,bounds);
    for(auto* item:{&anchor,&session}) {
        (*item)["last_seen_unix_ms"]=sighting.captured_at_unix_ms;
        (*item)["last_seen_clock_uncertainty_ms"]=sighting.clock_uncertainty_ms;
    }
    anchor["time_bounds"]=bounds; session["time_bounds"]=session_bounds;
    shared.Set(payload["anchors"],anchor_index,anchor); shared.Set(payload["sessions"],session_index,session);
    shared.InsertEvidence(payload["evidence"],nlohmann::json(sighting));
    TrajectoryRevision revision{sighting.target_id,expected_version,expected_version+1,reason,{}};
    if(!proof.at("incoming_segment_index").is_null()) {
        const auto segment_index=proof.at("incoming_segment_index").get<std::size_t>();
        const auto encoded=shared.At(payload.at("segments"),segment_index);
        const auto old_segment=shared.Decode(encoded).get<TrajectorySegment>();
        auto segment=old_segment;
        const auto extend=[&](PathCandidate& candidate) {
            if(candidate.nodes.empty() || candidate.nodes.back().camera_id!=sighting.camera_id)
                throw std::logic_error("invalid certified incoming trajectory endpoint");
            candidate.nodes.back().latest_unix_ms=bounds.last_latest_unix_ms;
        };
        if(segment.primary) extend(*segment.primary);
        for(auto& candidate:segment.backups) extend(candidate);
        if(!SameSegment(old_segment,segment)) {
            revision.affected_segment_ids.push_back(segment.segment_id);
            shared.Set(payload["segments"],segment_index,shared.Encode(nlohmann::json(segment),false,std::optional<nlohmann::json>(encoded)));
        }
    }
    payload["revision"]=shared.Encode(nlohmann::json(revision));
    payload["version"]=revision.version; payload["association_revision"]=association_revision;
    state_->Put("trajectory",nlohmann::json::array({sighting.target_id,revision.version}).dump(),payload.dump(),sighting.target_id);
    state_->Put("trajectory_head",sighting.target_id,std::to_string(revision.version));
    cold_cache_.reset();
    return revision;
}

TrajectoryReference TrajectoryStore::Reference(const std::string& target_id,std::uint64_t version) const {
    if(state_) {
        if(version==0) version=LatestVersion(target_id);
        const auto text=state_->Get("trajectory",nlohmann::json::array({target_id,version}).dump());
        if(!text) throw std::out_of_range("trajectory version not found");
        const auto payload=nlohmann::json::parse(*text);
        if(payload.value("storage_format","")=="refs-v2") return {version,payload.at("association_revision"),
            SharedJson::Count(payload.at("evidence")),SharedJson::Count(payload.at("anchors")),payload.at("graph").at("version")};
    }
    const auto& snapshot=Get(target_id,version);
    return {snapshot.version,snapshot.association_revision,snapshot.evidence.size(),snapshot.anchors.size(),snapshot.graph.version};
}

const TrajectorySnapshot& TrajectoryStore::Get(const std::string& target_id,
                                                std::uint64_t version) const {
    if (version == 0 && association_revision_reader_) {
        const auto source_revision = association_revision_reader_(target_id);
        if (source_revision != 0 && LatestVersion(target_id) == 0)
            throw std::runtime_error("REBUILD_REQUIRED: no trajectory for current association; target=" + target_id);
        const auto& snapshot = Find(target_id, 0).snapshot;
        if (snapshot.association_revision != source_revision)
            throw std::runtime_error("REBUILD_REQUIRED: association revision changed; target=" + target_id +
                "; current trajectory version=" + std::to_string(snapshot.version));
        return snapshot;
    }
    return Find(target_id, version).snapshot;
}

std::uint64_t TrajectoryStore::LatestVersion(const std::string& target) const {
    if (state_) return std::stoull(state_->Get("trajectory_head", target).value_or("0"));
    const auto found = histories_.find(target);
    return found == histories_.end() ? 0 : found->second.snapshots.back().snapshot.version;
}

nlohmann::json TrajectoryStore::Prune(std::int64_t cutoff_unix_ms, bool dry_run,
                                    const std::vector<std::string>& expired_observation_ids) {
    using J=nlohmann::json;
    // Snapshot versions are immutable to writers, but retention redacts all
    // of them. Removing only the head would leave queryable historical points.
    struct Candidate {
        std::string key;
        TrajectorySnapshot before;
        std::vector<SightingEvidence> retained;
    };
    std::vector<Candidate> candidates;
    if(state_) {
        std::vector<std::pair<std::string,std::string>> keys;
        state_->Scan("trajectory",[&](const std::string& key,const std::string& value){keys.emplace_back(key,value);});
        for(const auto& item:keys) {
            const auto key=J::parse(item.first);
            if(!key.is_array() || key.size()!=2)throw std::runtime_error("invalid persisted trajectory key");
            // Explicit versions avoid the association current-head assertion.
            candidates.push_back({item.first,Get(key.at(0).get<std::string>(),key.at(1).get<std::uint64_t>()),{}});
        }
    } else {
        for(const auto& target:histories_)for(const auto& stored:target.second.snapshots)
            candidates.push_back({J::array({target.first,stored.snapshot.version}).dump(),stored.snapshot,{}});
    }
    std::set<std::string> removed(expired_observation_ids.begin(),expired_observation_ids.end());
    std::set<std::string> removed_from_snapshots;
    std::uint64_t evidence_before=0,evidence_after=0,unknown_removed=0;
    for(auto& item:candidates) {
        for(const auto& sighting:item.before.evidence) {
            ++evidence_before;
            const bool qualified=sighting.time_source==TimeSource::UtcCapture && sighting.clock_domain=="UTC" &&
                sighting.clock_uncertainty_ms>=0 && sighting.clock_uncertainty_ms<=60000;
            // An old snapshot has no first-quarantine provenance. Unqualified
            // evidence was never usable as a point, so remove it immediately
            // rather than inventing a fresh retention lease during migration.
            if(!qualified || removed.count(sighting.observation_id) || sighting.captured_at_unix_ms<cutoff_unix_ms) {
                removed.insert(sighting.observation_id);
                removed_from_snapshots.insert(sighting.observation_id);
                if(!qualified)++unknown_removed;
            } else {
                item.retained.push_back(sighting);
                ++evidence_after;
            }
        }
    }
    std::uint64_t rewritten=0,deleted=0,empty_versions=0,rejected_removed=0,revision_ids_removed=0,reentry_removed=0;
    auto earliest=std::numeric_limits<std::int64_t>::max();
    for(auto& item:candidates) {
        // A removal discovered in another historical version is authoritative
        // for the same immutable observation ID throughout this scope.
        item.retained.erase(std::remove_if(item.retained.begin(),item.retained.end(),[&](const auto& sighting){
            if(!removed.count(sighting.observation_id))return false;
            --evidence_after;
            return true;
        }),item.retained.end());
        bool scrubbed_reentry=false;
        for(auto& sighting:item.retained) {
            earliest=std::min(earliest,sighting.captured_at_unix_ms);
            if(!sighting.reentry_evidence_id.empty() && removed.count(sighting.reentry_evidence_id)) {
                sighting.reentry_evidence_id.clear();scrubbed_reentry=true;++reentry_removed;
            }
        }
        auto next=BuildSnapshot(item.before.target_id,item.retained,item.before.graph,item.before.options,item.before.version);
        next.association_revision=item.before.association_revision;
        next.revision=item.before.revision;
        std::set<std::string> surviving_segments;
        for(const auto& segment:next.segments)surviving_segments.insert(segment.segment_id);
        auto& affected=next.revision.affected_segment_ids;
        const auto affected_before=affected.size();
        affected.erase(std::remove_if(affected.begin(),affected.end(),[&](const auto& id){return !surviving_segments.count(id);}),affected.end());
        revision_ids_removed+=affected_before-affected.size();
        std::set<std::string> surviving_rejected(next.rejected_observation_ids.begin(),next.rejected_observation_ids.end());
        std::uint64_t dropped_rejected=0;
        for(const auto& id:item.before.rejected_observation_ids)if(!surviving_rejected.count(id))++dropped_rejected;
        rejected_removed+=dropped_rejected;
        bool expired_reason=false;
        for(const auto& id:removed)if(!id.empty() && next.revision.reason.find(id)!=std::string::npos){expired_reason=true;break;}
        const bool changed=item.retained.size()!=item.before.evidence.size() || scrubbed_reentry ||
            dropped_rejected!=0 || affected_before!=affected.size() || expired_reason;
        if(next.evidence.empty()) {
            ++empty_versions;
            if(next.version!=LatestVersion(next.target_id)) {
                ++deleted;
                if(!dry_run) {
                    if(state_)state_->Erase("trajectory",item.key);
                    else {
                        auto& snapshots=histories_.at(next.target_id).snapshots;
                        snapshots.erase(std::remove_if(snapshots.begin(),snapshots.end(),[&](const auto& stored){
                            return stored.snapshot.version==next.version;
                        }),snapshots.end());
                    }
                }
                continue;
            }
        }
        if(!changed)continue;
        ++rewritten;
        // Historical revision free text can embed raw observation IDs. A
        // stable reason replaces it rather than retaining expired provenance.
        next.revision.reason="RETENTION_PRUNE";
        if(dry_run)continue;
        if(state_) {
            SharedJson shared(*state_);
            auto payload=shared.Encode(J(next));
            payload["storage_format"]="refs-v2";
            payload["append_tail"]=AppendTailProof(next);
            state_->Put("trajectory",item.key,payload.dump(),next.target_id);
        } else {
            auto& snapshots=histories_.at(next.target_id).snapshots;
            const auto found=std::find_if(snapshots.begin(),snapshots.end(),[&](const auto& stored){return stored.snapshot.version==next.version;});
            if(found==snapshots.end())throw std::logic_error("trajectory history disappeared during retention");
            auto timed=BuildTimelineIndex(next);
            *found=StoredSnapshot{std::move(next),std::move(timed)};
        }
    }
    if(!dry_run)cold_cache_.reset();
    return {{"cutoff_unix_ms",cutoff_unix_ms},{"dry_run",dry_run},{"versions_scanned",candidates.size()},
        {"earliest_retained_capture_unix_ms",earliest},
        {"versions_rewritten",rewritten},{"versions_deleted",deleted},{"empty_versions",empty_versions},{"evidence_before",evidence_before},
        {"evidence_after",evidence_after},{"evidence_removed",evidence_before-evidence_after},
        {"distinct_removed_observations",removed_from_snapshots.size()},{"unknown_time_evidence_removed",unknown_removed},
        {"rejected_ids_removed",rejected_removed},{"revision_ids_removed",revision_ids_removed},{"reentry_ids_removed",reentry_removed}};
}

const TrajectoryStore::StoredSnapshot& TrajectoryStore::Find(
    const std::string& target_id, std::uint64_t version) const {
    if(state_) {
        if(version==0){auto head=state_->Get("trajectory_head",target_id);if(!head)throw std::out_of_range("target trajectory not found");version=std::stoull(*head);}
        if(cold_cache_ && cold_cache_->snapshot.target_id==target_id && cold_cache_->snapshot.version==version)return *cold_cache_;
        auto text=state_->Get("trajectory",nlohmann::json::array({target_id,version}).dump());
        if(!text)throw std::out_of_range("trajectory version not found");
        auto payload=nlohmann::json::parse(*text);
        if(payload.value("storage_format","")=="refs-v2") {
            SharedJson shared(*state_); payload=shared.Decode(payload);
        } else if(payload.contains("storage_format")) {
            if(payload.at("storage_format")!="refs-v1")throw std::runtime_error("unsupported trajectory storage format");
            auto object=[&](const std::string& key){auto value=state_->Get("object",key);if(!value)throw std::runtime_error("missing immutable trajectory object");return nlohmann::json::parse(*value);};
            payload["graph"]=object(payload.at("graph_ref").get<std::string>());
            payload["evidence"]=nlohmann::json::array();
            for(const auto& key:payload.at("evidence_refs"))payload["evidence"].push_back(object(key.get<std::string>()));
        }
        if (!payload.contains("association_revision")) payload["association_revision"] = 0;
        auto snapshot=payload.get<TrajectorySnapshot>();
        auto timed=BuildTimelineIndex(snapshot);cold_cache_=StoredSnapshot{std::move(snapshot),std::move(timed)};
        return *cold_cache_;
    }
    auto it = histories_.find(target_id);
    if (it == histories_.end() || it->second.snapshots.empty()) {
        throw std::out_of_range("target trajectory not found");
    }
    if (version == 0) return it->second.snapshots.back();
    const auto found=std::find_if(it->second.snapshots.begin(),it->second.snapshots.end(),[&](const auto& stored){
        return stored.snapshot.version==version;
    });
    if(found==it->second.snapshots.end())throw std::out_of_range("trajectory version not found");
    return *found;
}

TimelineResult TrajectoryStore::Query(const std::string& target_id,
                                      std::uint64_t version, std::int64_t at_unix_ms,
                                      std::int64_t from_unix_ms,
                                      std::int64_t to_unix_ms) const {
    if (from_unix_ms > at_unix_ms || at_unix_ms > to_unix_ms) {
        throw std::invalid_argument("timeline range must contain at_unix_ms");
    }
    if (version == 0) (void)Get(target_id, 0); // current reads must use current identity evidence
    const auto& stored = Find(target_id, version);
    const auto& snapshot = stored.snapshot;
    TimelineResult result{target_id, snapshot.version, at_unix_ms, NodeStatus::Unknown,
                          {}, {}, std::nullopt, std::nullopt, std::nullopt, {}, {}};
    for (const auto& anchor : snapshot.anchors) {
        if (anchor.time_bounds.first_earliest_unix_ms <= to_unix_ms &&
            anchor.time_bounds.last_latest_unix_ms >= from_unix_ms) result.anchors_in_range.push_back(anchor);
    }
    const auto& timed = stored.timed_anchors;
    const auto error=snapshot.options.max_clock_uncertainty_ms;
    const auto lower=at_unix_ms<std::numeric_limits<std::int64_t>::min()+error
        ?std::numeric_limits<std::int64_t>::min():at_unix_ms-error;
    const auto upper=at_unix_ms>std::numeric_limits<std::int64_t>::max()-error
        ?std::numeric_limits<std::int64_t>::max():at_unix_ms+error;
    auto possible_begin=std::lower_bound(timed.begin(),timed.end(),lower,
        [](const TimedAnchor& item,std::int64_t time){return item.time<time;});
    auto possible_end=std::upper_bound(possible_begin,timed.end(),upper,
        [](std::int64_t time,const TimedAnchor& item){return time<item.time;});
    std::set<std::string> possible;
    for(auto it=possible_begin;it!=possible_end;++it){
        const auto& capture=snapshot.evidence[it->evidence_index];
        const auto distance=it->time<=at_unix_ms
            ?static_cast<std::uint64_t>(at_unix_ms)-static_cast<std::uint64_t>(it->time)
            :static_cast<std::uint64_t>(it->time)-static_cast<std::uint64_t>(at_unix_ms);
        if(distance<=static_cast<std::uint64_t>(capture.clock_uncertainty_ms))
            possible.insert(snapshot.anchors[it->anchor_index].anchor_id);
    }
    for(const auto& anchor:snapshot.anchors)if(possible.count(anchor.anchor_id))
        result.possible_observed_anchor_ids.push_back(anchor.anchor_id);
    const auto after = std::upper_bound(timed.begin(), timed.end(), at_unix_ms,
        [](std::int64_t time, const TimedAnchor& item) { return time < item.time; });
    const auto exact = std::lower_bound(timed.begin(),after,at_unix_ms,
        [](const TimedAnchor& item,std::int64_t time){return item.time<time;});
    for (auto it=exact;it!=after;++it) {
        const auto& id=snapshot.anchors[it->anchor_index].anchor_id;
        if (std::find(result.observed_anchor_ids.begin(),result.observed_anchor_ids.end(),id)==result.observed_anchor_ids.end())
            result.observed_anchor_ids.push_back(id);
    }
    if (after != timed.end()) {
        result.next_anchor_id = snapshot.anchors[after->anchor_index].anchor_id;
        result.next_observation = TimelineObservation{
            *result.next_anchor_id, snapshot.evidence[after->evidence_index]};
    }
    if (after != timed.begin()) {
        const auto previous_time = (after - 1)->time;
        const auto first_at_time = std::lower_bound(timed.begin(), after, previous_time,
            [](const TimedAnchor& item, std::int64_t time) { return item.time < time; });
        result.previous_anchor_id = snapshot.anchors[first_at_time->anchor_index].anchor_id;
        result.previous_observation = TimelineObservation{
            *result.previous_anchor_id, snapshot.evidence[first_at_time->evidence_index]};
    }
    const auto locate = [&](const TrajectorySegment& segment, const PathCandidate& path,
                            std::size_t route_index) {
        TimelinePathLocation location{segment.segment_id, route_index, {}, {}};
        for (std::size_t i = 0; i < path.nodes.size(); ++i) {
            const auto& node = path.nodes[i];
            if (node.earliest_unix_ms <= at_unix_ms && at_unix_ms <= node.latest_unix_ms) {
                location.node_indices.push_back(i);
            }
        }
        for (std::size_t i = 0; i < path.edge_time_bounds.size(); ++i) {
            const auto& bound = path.edge_time_bounds[i];
            if (bound.earliest_unix_ms <= at_unix_ms && at_unix_ms <= bound.latest_unix_ms) {
                location.edge_indices.push_back(i);
            }
        }
        if (!location.node_indices.empty() || !location.edge_indices.empty()) {
            result.path_locations.push_back(std::move(location));
        }
    };
    for (const auto& segment : snapshot.segments) {
        const auto start = std::min(segment.from_unix_ms, segment.to_unix_ms);
        const auto end = std::max(segment.from_unix_ms, segment.to_unix_ms);
        if (start <= to_unix_ms && end >= from_unix_ms) {
            result.segments_in_range.push_back(segment);
        }
        if (start <= at_unix_ms && end >= at_unix_ms) {
            result.active_segment_ids.push_back(segment.segment_id);
            if (segment.primary) locate(segment, *segment.primary, 0);
            for (std::size_t i = 0; i < segment.backups.size(); ++i) {
                locate(segment, segment.backups[i], i + 1);
            }
        }
    }
    if (result.active_segment_ids.size() == 1) {
        result.segment_id = result.active_segment_ids[0];
        const bool primary_located = std::any_of(result.path_locations.begin(), result.path_locations.end(),
            [](const auto& location) { return location.route_index == 0; });
        if (primary_located) result.status = NodeStatus::Inferred;
    }
    if (!result.observed_anchor_ids.empty()) result.status = NodeStatus::Observed;
    else if (!result.possible_observed_anchor_ids.empty()) result.status = NodeStatus::Unknown;
    return result;
}

}  // namespace face_trajectory
