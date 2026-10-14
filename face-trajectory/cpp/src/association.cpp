#include "face_trajectory/association.hpp"
#include "face_trajectory/activity.hpp"
#include "face_trajectory/time_constraints.hpp"
#include "face_trajectory/state.hpp"
#include "face_trajectory/storage_json.hpp"

#include <algorithm>
#include <cmath>
#include <limits>
#include <queue>
#include <stdexcept>

namespace face_trajectory {
namespace {

template<class T> std::int64_t Lower(const T& o) {
    return o.captured_at_unix_ms < std::numeric_limits<std::int64_t>::min() + o.clock_uncertainty_ms
        ? std::numeric_limits<std::int64_t>::min() : o.captured_at_unix_ms - o.clock_uncertainty_ms;
}

template<class T> std::int64_t Upper(const T& o) {
    return o.captured_at_unix_ms > std::numeric_limits<std::int64_t>::max() - o.clock_uncertainty_ms
        ? std::numeric_limits<std::int64_t>::max() : o.captured_at_unix_ms + o.clock_uncertainty_ms;
}

auto PolicyKey(const AssociationPolicy& p) {
    return std::tie(p.match_threshold, p.new_target_threshold, p.min_margin,
                    p.template_update_threshold, p.max_template_similarity, p.min_detection_score, p.min_face_size_px,
                    p.max_clock_uncertainty_ms, p.max_templates_per_target,
                    p.require_topology, p.topology_is_complete, p.overlapping_cameras);
}

auto GraphKey(const CameraEdge& e) {
    return std::tie(e.from_camera, e.to_camera, e.min_travel_ms, e.max_travel_ms, e.cost,
                    e.open_from_unix_ms, e.open_until_unix_ms, e.edge_id);
}

bool SameGraph(const CameraGraph& a, const CameraGraph& b) {
    if (a.edges.size() != b.edges.size()) return false;
    for (std::size_t i = 0; i < a.edges.size(); ++i) if (GraphKey(a.edges[i]) != GraphKey(b.edges[i])) return false;
    return true;
}

template<class T> auto Track(const T& o) {
    return std::make_tuple(o.source_id, o.camera_id, o.track_session_id, o.local_track_id);
}

template<class T> std::string TrackIndexKey(const T& o) {
    return nlohmann::json::array({o.source_id,o.camera_id,o.track_session_id,o.local_track_id}).dump();
}

template<class T> std::string FrameIndexKey(const std::string& target, const T& o) {
    return nlohmann::json::array({target,o.source_id,o.camera_id,o.frame_id}).dump();
}

bool SameObservation(const FaceObservation& a, const FaceObservation& b) {
    return std::tie(a.observation_id, a.source_id, a.camera_id, a.frame_id,
                    a.captured_at_unix_ms, a.clock_uncertainty_ms, a.time_source, a.clock_domain,
                    a.detection_score, a.face_size_px, a.local_track_id, a.track_session_id,
                    a.reentry_evidence_id) ==
           std::tie(b.observation_id, b.source_id, b.camera_id, b.frame_id,
                    b.captured_at_unix_ms, b.clock_uncertainty_ms, b.time_source, b.clock_domain,
                    b.detection_score, b.face_size_px, b.local_track_id, b.track_session_id,
                    b.reentry_evidence_id) && a.feature_space == b.feature_space && a.embedding == b.embedding;
}

AssociationPolicy CheckedPolicy(AssociationPolicy p) {
    const auto unit = [](double v) { return std::isfinite(v) && v >= 0 && v <= 1; };
    if (p.policy_id.empty() || !unit(p.new_target_threshold) || !unit(p.match_threshold) ||
        p.new_target_threshold >= p.match_threshold || !unit(p.template_update_threshold) ||
        p.template_update_threshold < p.match_threshold || !unit(p.max_template_similarity) ||
        p.max_template_similarity < p.template_update_threshold || !std::isfinite(p.min_margin) ||
        p.min_margin < 0 || p.min_margin > 2 || !unit(p.min_detection_score) ||
        !std::isfinite(p.min_face_size_px) || p.min_face_size_px <= 0 ||
        p.max_clock_uncertainty_ms < 0 || p.max_templates_per_target == 0) {
        throw std::invalid_argument("invalid association policy");
    }
    for (auto& pair : p.overlapping_cameras) {
        if (pair.first.empty() || pair.second.empty() || pair.first == pair.second)
            throw std::invalid_argument("overlapping cameras must name two different cameras");
        if (pair.first > pair.second) std::swap(pair.first, pair.second);
    }
    std::sort(p.overlapping_cameras.begin(), p.overlapping_cameras.end());
    p.overlapping_cameras.erase(std::unique(p.overlapping_cameras.begin(), p.overlapping_cameras.end()),
                                p.overlapping_cameras.end());
    return p;
}

bool UsableTime(const FaceObservation& o, const AssociationPolicy& p) {
    return o.time_source == TimeSource::UtcCapture && o.clock_domain == "UTC" &&
           o.clock_uncertainty_ms >= 0 && o.clock_uncertainty_ms <= p.max_clock_uncertainty_ms;
}

bool UsableFace(const FaceObservation& o, const AssociationPolicy& p) {
    return o.detection_score >= p.min_detection_score && o.face_size_px >= p.min_face_size_px;
}

}  // namespace

AssociationStore::AssociationStore(Gallery& gallery,State* state):gallery_(gallery),state_(state) {
    if(!state_)return;
    version_=std::stoull(state_->Get("version","association").value_or("0"));
    state_->Scan("policy",[&](const std::string& id,const std::string& value){policies_[id]=nlohmann::json::parse(value).get<AssociationPolicy>();});
    state_->Scan("association_graph",[&](const std::string& id,const std::string& value){graphs_[id]=nlohmann::json::parse(value).get<CameraGraph>();});
    state_->Scan("owned_template",[&](const std::string& id,const std::string& value){
        for(const auto& v:nlohmann::json::parse(value))templates_[id].push_back({v.at("observation_id"),v.at("template_id"),v.at("quality")});
    });
}
std::optional<AssociationStore::Record> AssociationStore::LoadRecord(const std::string& id) const {
    if(state_){auto text=state_->Get("observation",id);if(!text)return {};auto j=nlohmann::json::parse(*text);
        return Record{j.at("observation").get<FaceObservation>(),j.at("result").get<AssociationResult>()};}
    auto it=records_.find(id);if(it==records_.end())return {};return it->second;
}
std::optional<AssociationResult> AssociationStore::Result(const std::string& id) const {
    const auto record = LoadRecord(id);
    return record ? std::optional<AssociationResult>(record->result) : std::nullopt;
}
void AssociationStore::SaveTemplates(const std::string& target) {
    const auto found=templates_.find(target);
    if(found==templates_.end() || found->second.empty()) {
        if(state_)state_->Erase("owned_template",target);
        templates_.erase(target);return;
    }
    if(!state_)return;
    auto j=nlohmann::json::array();for(const auto& t:found->second)j.push_back({{"observation_id",t.observation_id},{"template_id",t.template_id},{"quality",t.quality}});
    state_->Put("owned_template",target,j.dump());
}
void AssociationStore::SaveRecord(const Record& record) {
    // Derive changes before overwriting the observation or its revision. This
    // upgrades legacy indices lazily within the same transaction.
    const auto previous = state_ ? LoadRecord(record.observation.observation_id) : std::optional<Record>{};
    std::map<std::string, TargetSummary> summaries;
    if (state_) {
        for (const auto& target : record.result.affected_target_ids)
            summaries.emplace(target, EnsureTargetIndex(target));
        if (previous && previous->result.target_id && !summaries.count(*previous->result.target_id))
            summaries.emplace(*previous->result.target_id, EnsureTargetIndex(*previous->result.target_id));
        const auto old_target = previous ? previous->result.target_id : std::optional<std::string>{};
        const auto& new_target = record.result.target_id;
        if (old_target != new_target) {
            if (old_target) {
                auto& counts = summaries.at(*old_target).camera_counts;
                auto found = counts.find(previous->observation.camera_id);
                if (found == counts.end() || !found->second)
                    throw std::logic_error("association camera index is inconsistent");
                if (--found->second == 0) counts.erase(found);
            }
            if (new_target) ++summaries.at(*new_target).camera_counts[record.observation.camera_id];
            if (!record.observation.local_track_id.empty()) {
                const auto track = TrackIndexKey(record.observation);
                auto counts = TrackTargetCounts(track);
                if (old_target) {
                    auto found = counts.find(*old_target);
                    if (found == counts.end() || !found->second)
                        throw std::logic_error("association track index is inconsistent");
                    if (--found->second == 0) counts.erase(found);
                }
                if (new_target) ++counts[*new_target];
                state_->Put("association_track_targets",track,
                    nlohmann::json({{"schema_version",1},{"counts",counts}}).dump());
            }
        }
        if (new_target) {
            const auto& o = record.observation;
            state_->Put("association_frame",o.observation_id,
                nlohmann::json::array({o.track_session_id,o.local_track_id}).dump(),
                *new_target,FrameIndexKey(*new_target,o));
        } else if (old_target) state_->Erase("association_frame",record.observation.observation_id);
    }
    for (const auto& target : record.result.affected_target_ids) {
        if (state_) state_->Put("association_target_revision", target, std::to_string(record.result.version));
        else target_revisions_[target] = record.result.version;
    }
    if(!state_){records_.insert_or_assign(record.observation.observation_id,record);return;}
    const auto& o=record.observation;
    const auto track=nlohmann::json::array({o.source_id,o.camera_id,o.track_session_id,o.local_track_id}).dump();
    auto meta=nlohmann::json(o);meta.erase("embedding");meta.erase("feature_space");
    // Metadata includes the sighting but never the 512D vector.
    meta["sighting"]=record.result.sighting;meta["target_id"]=record.result.target_id;
    state_->Put("observation",o.observation_id,nlohmann::json({{"observation",o},{"result",record.result}}).dump(),
        record.result.target_id.value_or(""),track,meta.dump());
    for (auto& [target, summary] : summaries) {
        summary.revision = TargetRevision(target);
        SaveTargetSummary(target,summary);
    }
    state_->Put("version","association",std::to_string(version_));
}

void AssociationStore::SaveTargetSummary(const std::string& target, const TargetSummary& summary) const {
    state_->Put("association_target_summary",target,
        nlohmann::json({{"schema_version",1},{"revision",summary.revision},
                        {"camera_counts",summary.camera_counts}}).dump());
}

AssociationStore::TargetSummary AssociationStore::EnsureTargetIndex(const std::string& target) const {
    const auto revision = TargetRevision(target);
    if (const auto stored = state_->Get("association_target_summary",target)) {
        const auto value = nlohmann::json::parse(*stored);
        if (value.value("schema_version",0)==1 && value.at("revision").get<std::uint64_t>()==revision)
            return {revision,value.at("camera_counts").get<std::map<std::string,std::uint64_t>>()};
    }
    TargetSummary summary{revision,{}};
    // Remove stale derived rows, never authoritative observation history.
    std::vector<std::string> stale;
    state_->Scan("association_frame",[&](const auto& id,const auto&){stale.push_back(id);},target);
    for (const auto& id : stale) state_->Erase("association_frame",id);
    for (const auto& o : TargetMetadata(target)) {
        ++summary.camera_counts[o.camera_id];
        state_->Put("association_frame",o.observation_id,
            nlohmann::json::array({o.track_session_id,o.local_track_id}).dump(),target,FrameIndexKey(target,o));
    }
    SaveTargetSummary(target,summary);
    return summary;
}

std::map<std::string,std::uint64_t> AssociationStore::TrackTargetCounts(const std::string& track) const {
    if (const auto stored = state_->Get("association_track_targets",track)) {
        const auto value=nlohmann::json::parse(*stored);
        if (value.value("schema_version",0)==1)
            return value.at("counts").get<std::map<std::string,std::uint64_t>>();
    }
    std::map<std::string,std::uint64_t> counts;
    state_->Scan("observation",[&](const std::string&,const std::string& text){
        const auto value=nlohmann::json::parse(text);
        if (!value.at("target_id").is_null()) ++counts[value.at("target_id").get<std::string>()];
    },{},true,track);
    state_->Put("association_track_targets",track,
        nlohmann::json({{"schema_version",1},{"counts",counts}}).dump());
    return counts;
}
std::vector<AssociationStore::CaptureMetadata> AssociationStore::TargetMetadata(const std::string& target) const {
    std::vector<CaptureMetadata> result;
    if(state_)state_->Scan("observation",[&](const std::string&,const std::string& value){
        const auto j=nlohmann::json::parse(value);CaptureMetadata o{};
        o.observation_id=j.at("observation_id");o.source_id=j.at("source_id");o.camera_id=j.at("camera_id");o.frame_id=j.at("frame_id");
        o.captured_at_unix_ms=j.at("captured_at_unix_ms");o.clock_uncertainty_ms=j.at("clock_uncertainty_ms");
        o.local_track_id=j.at("local_track_id");o.track_session_id=j.at("track_session_id");result.push_back(std::move(o));
    },target,true);
    else {auto it=target_observations_.find(target);if(it!=target_observations_.end())for(const auto& id:it->second){const auto& o=records_.at(id).observation;
        result.push_back({o.observation_id,o.source_id,o.camera_id,o.frame_id,o.local_track_id,o.track_session_id,o.captured_at_unix_ms,o.clock_uncertainty_ms});}}
    return result;
}

CameraCompatibility AssociationStore::Compatible(const FaceObservation& o, const std::string& target,
                                                  const AssociationPolicy& policy, const CameraGraph& graph) const {
    if (state_) {
        const auto summary = EnsureTargetIndex(target);
        if (summary.camera_counts.empty()) return CameraCompatibility::Compatible;
        if (summary.camera_counts.size()==1 && summary.camera_counts.begin()->first==o.camera_id) {
            bool conflict=false;
            state_->Scan("association_frame",[&](const std::string&,const std::string& text){
                const auto track=nlohmann::json::parse(text);
                if (o.local_track_id.empty() || track.at(1).get<std::string>().empty() ||
                    track.at(0)!=o.track_session_id || track.at(1)!=o.local_track_id) conflict=true;
            },{},false,FrameIndexKey(target,o));
            // CheckCaptureChain makes exactly this same-camera conclusion,
            // after applying the original same-source/frame collision rule.
            return conflict ? CameraCompatibility::Conflict : CameraCompatibility::Compatible;
        }
    }
    const auto history = TargetMetadata(target);
    if (history.empty()) return CameraCompatibility::Compatible;
    std::vector<CaptureInterval> captures{{o.camera_id,Lower(o),Upper(o)}};
    for (const auto& old : history) {
        if (o.camera_id == old.camera_id) {
            // Two different face observations in one source frame cannot be one person.
            if (o.source_id == old.source_id && o.frame_id == old.frame_id &&
                (o.local_track_id.empty() || old.local_track_id.empty() || Track(o) != Track(old))) {
                return CameraCompatibility::Conflict;
            }
        }
        captures.push_back({old.camera_id,Lower(old),Upper(old)});
    }
    switch (CheckCaptureChain(std::move(captures),graph,policy.overlapping_cameras,policy.topology_is_complete)) {
        case ChainFeasibility::Feasible: return CameraCompatibility::Compatible;
        case ChainFeasibility::Infeasible: return CameraCompatibility::Conflict;
        default:return CameraCompatibility::Unknown;
    }
}

void AssociationStore::ForgetTemplate(const std::string& observation_id, const std::string& target) {
    auto owner=templates_.find(target);if(owner==templates_.end())return;
    auto& entries = owner->second;
    const auto found = std::find_if(entries.begin(), entries.end(), [&](const auto& t) {
        return t.observation_id == observation_id;
    });
    if (found != entries.end()) {
        gallery_.Remove(found->template_id);
        entries.erase(found);
        SaveTemplates(target);
    }
}

void AssociationStore::Assign(Record& record, const std::string& target, const std::string& evidence,
                              const AssociationPolicy& policy, bool update_template) {
    auto& result = record.result;
    const auto& o = record.observation;
    const auto owner=templates_.find(target);
    auto entries=owner==templates_.end()?std::vector<OwnedTemplate>{}:owner->second;
    const auto by_quality = [](const auto& a, const auto& b) { return a.quality < b.quality; };
    while (entries.size() > policy.max_templates_per_target) {
        const auto worst = std::min_element(entries.begin(), entries.end(), by_quality);
        gallery_.Remove(worst->template_id);
        entries.erase(worst);
    }
    if (update_template) {
        const auto quality = o.detection_score * o.face_size_px;
        const auto worst = std::min_element(entries.begin(), entries.end(), by_quality);
        if (entries.size() < policy.max_templates_per_target || quality > worst->quality) {
            const auto template_id = "assoc-template:" + o.observation_id;
            if (entries.size() >= policy.max_templates_per_target) {
                gallery_.Remove(worst->template_id);
                entries.erase(worst);
            }
            gallery_.Add({template_id, target, o.feature_space, o.embedding});
            entries.push_back({o.observation_id, template_id, quality});
            result.template_added = true;
        }
    }
    if (result.target_id && *result.target_id != target) {
        const auto old = *result.target_id;
        if(!state_)target_observations_[old].erase(o.observation_id);
        ForgetTemplate(o.observation_id, old);
        result.affected_target_ids.push_back(old);
    }
    result.target_id = target;
    result.sighting = SightingEvidence{o.observation_id, target, o.camera_id, evidence,
        o.captured_at_unix_ms, o.clock_uncertainty_ms, TargetLinkStatus::Confirmed,
        o.time_source, o.source_id, o.clock_domain, o.reentry_evidence_id};
    if(!state_) {
        target_observations_[target].insert(o.observation_id);
        if (!o.local_track_id.empty()) track_observations_[Track(o)].insert(o.observation_id);
    }
    templates_.insert_or_assign(target,std::move(entries));
    SaveTemplates(target);
    if(state_)state_->Put("association_target",target,"1");
    result.affected_target_ids.push_back(target);
    // Read track identity from accepted records so corrections cannot leave stale bindings.
}

AssociationResult AssociationStore::Observe(FaceObservation observation, const AssociationPolicy& input_policy,
                                            const CameraGraph& input_graph, bool reconsider) {
    last_append_.reset();
    const auto policy = CheckedPolicy(input_policy);
    const auto graph = NormalizeCameraGraph(input_graph);
    const auto& o = observation;
    if (o.observation_id.empty() || o.source_id.empty() || o.camera_id.empty() || o.frame_id.empty() ||
        o.clock_domain.empty() || (o.local_track_id.empty() != o.track_session_id.empty()) ||
        !std::isfinite(o.detection_score) || o.detection_score < 0 || o.detection_score > 1 ||
        !std::isfinite(o.face_size_px) || o.face_size_px <= 0) {
        throw std::invalid_argument("invalid face observation metadata");
    }
    const auto known_policy = policies_.find(policy.policy_id);
    if (known_policy != policies_.end() && PolicyKey(known_policy->second) != PolicyKey(policy))
        throw std::invalid_argument("association policy changed without a new policy_id");
    const auto known_graph = graphs_.find(graph.version);
    if (known_graph != graphs_.end() && !SameGraph(known_graph->second, graph))
        throw std::invalid_argument("camera graph changed without a new version");
    const auto previous = LoadRecord(o.observation_id);
    if (previous) {
        if (!SameObservation(previous->observation, o))
            throw std::invalid_argument("conflicting duplicate observation_id");
        if (!reconsider || previous->result.target_id) return previous->result;
    }
    std::optional<std::uint64_t> append_previous_target_revision;
    // Search all persons in the feature space; a TopK cutoff can hide the runner-up
    // after camera filtering. The gallery also validates the contract and vector.
    Gallery::ValidateQuery(o.feature_space,o.embedding);
    const auto hits = UsableTime(o,policy) && UsableFace(o,policy)
        ? gallery_.Search(o.feature_space, o.embedding, std::numeric_limits<std::size_t>::max())
        : std::vector<PersonHit>{};
    Record next{std::move(observation), {}};
    auto& result = next.result;
    result.observation_id = next.observation.observation_id;
    result.policy_id = policy.policy_id;
    result.graph_version = graph.version;
    const auto& current = next.observation;
    if (!UsableTime(current, policy)) {
        result.decision = "UNASSIGNED";
        result.reason = "capture time is not qualified UTC evidence";
    } else if (!UsableFace(current, policy)) {
        result.decision = "UNASSIGNED";
        result.reason = "face does not pass configured detection and size gates";
    } else {
        std::vector<AssociationCandidate> eligible;
        for (const auto& hit : hits) {
            // Sorted lower scores cannot alter a decision once both feasible
            // leaders are known and the visual Top8 has been classified.
            if (result.candidates.size()>=8 && eligible.size()>=2) break;
            if (result.candidates.size()>=8 && eligible.empty() && hit.similarity<policy.new_target_threshold) break;
            if(result.candidates.size()>=8 && eligible.size()==1 &&
                (eligible[0].hit.similarity<policy.match_threshold ||
                 eligible[0].hit.similarity-hit.similarity>=policy.min_margin)) {
                result.remaining_similarity_upper_bound=hit.similarity;
                break;
            }
            const auto compatibility = Compatible(current, hit.person_entity_id, policy, graph);
            AssociationCandidate candidate{hit, compatibility};
            if (result.candidates.size() < 8) result.candidates.push_back(candidate);
            if (compatibility != CameraCompatibility::Conflict)
                eligible.push_back(candidate);
        }
        // Keep the visual Top8 and the decision's feasible winner/runner-up.
        for (std::size_t i = 0; i < std::min<std::size_t>(2, eligible.size()); ++i) {
            if (std::none_of(result.candidates.begin(), result.candidates.end(), [&](const auto& c) {
                    return c.hit.person_entity_id == eligible[i].hit.person_entity_id;
                })) result.candidates.push_back(eligible[i]);
        }
        std::set<std::string> track_targets;
        if (!current.local_track_id.empty()) {
            if(state_) {
                for (const auto& [target,count] : TrackTargetCounts(TrackIndexKey(current)))
                    if (count) track_targets.insert(target);
            } else {
            const auto track = track_observations_.find(Track(current));
            if (track != track_observations_.end()) {
                for (const auto& id : track->second) track_targets.insert(*records_.at(id).result.target_id);
            }
            }
        }
        if (!eligible.empty() && eligible[0].hit.similarity >= policy.match_threshold &&
            (eligible.size() == 1 || eligible[0].hit.similarity - eligible[1].hit.similarity >= policy.min_margin)) {
            const auto& best = eligible[0];
            if ((!track_targets.empty() && (track_targets.size() != 1 || !track_targets.count(best.hit.person_entity_id)))) {
                result.decision = "CONFLICT";
                result.reason = "local track identity disagrees with face evidence";
            } else if ((policy.require_topology || policy.topology_is_complete) && best.compatibility == CameraCompatibility::Unknown) {
                result.decision = "PROVISIONAL";
                result.reason = "joint capture feasibility could not be established from topology or within solve budget";
            } else {
                result.decision = "MATCHED";
                result.reason = "face margin and available camera constraints passed";
                if (!previous) append_previous_target_revision=TargetRevision(best.hit.person_entity_id);
                Assign(next, best.hit.person_entity_id, "association:" + current.observation_id, policy,
                       best.hit.similarity >= policy.template_update_threshold &&
                       best.hit.similarity < policy.max_template_similarity);
            }
        } else if (hits.empty() || hits[0].similarity < policy.new_target_threshold) {
            if (!track_targets.empty()) {
                result.decision = "CONFLICT";
                result.reason = "local track face no longer supports its accepted identity";
            } else {
                result.decision = "CREATED";
                result.reason = "no similar identity in the compatible feature space";
                const auto target = "auto:" + ActivityStore::Digest(nlohmann::json::array({
                    state_?state_->Scope():std::string{},current.source_id,current.observation_id}).dump());
                if (!previous) append_previous_target_revision=TargetRevision(target);
                Assign(next, target, "association:" + current.observation_id, policy, true);
            }
        } else {
            const bool conflict = eligible.empty() || eligible[0].hit.similarity < policy.new_target_threshold;
            result.decision = conflict ? "CONFLICT" : "PROVISIONAL";
            result.reason = conflict ? "similar faces conflict with camera evidence"
                                             : "face score or person margin is inconclusive";
        }
    }
    result.version = ++version_;
    const bool new_policy=policies_.emplace(policy.policy_id, policy).second;
    const bool new_graph=graphs_.emplace(graph.version, graph).second;
    if(state_){if(new_policy)state_->Put("policy",policy.policy_id,nlohmann::json(policy).dump());if(new_graph)state_->Put("association_graph",graph.version,nlohmann::json(graph).dump());}
    const auto id = current.observation_id;
    const bool append = !previous && append_previous_target_revision && result.sighting && result.target_id &&
        result.affected_target_ids.size()==1 && result.affected_target_ids.front()==*result.target_id;
    SaveRecord(next);
    if (append) last_append_=AssociationAppend{*result.sighting,*append_previous_target_revision,TargetRevision(*result.target_id)};
    return next.result;
}

AssociationResult AssociationStore::Correct(const std::string& observation_id, const std::string& target,
                                            const std::string& evidence) {
    last_append_.reset();
    if (target.empty() || evidence.empty()) throw std::invalid_argument("target and correction evidence are required");
    auto loaded=LoadRecord(observation_id);
    if(!loaded)throw std::out_of_range("observation not found");
    auto& record=*loaded;
    const auto& policy = policies_.at(record.result.policy_id);
    if (!UsableTime(record.observation, policy) || !UsableFace(record.observation, policy))
        throw std::invalid_argument("correction cannot manufacture qualified face/capture evidence");
    if (record.result.sighting && record.result.target_id == target &&
        record.result.sighting->attribution_evidence_id == evidence) return record.result;
    record.result.affected_target_ids.clear();
    record.result.template_added = false;
    if (record.result.target_id) ForgetTemplate(observation_id, *record.result.target_id);
    Assign(record, target, evidence, policy, true);
    record.result.decision = "CORRECTED";
    record.result.reason = "external verified attribution correction";
    record.result.version = ++version_;
    SaveRecord(record);
    return record.result;
}

std::vector<SightingEvidence> AssociationStore::Sightings(const std::string& target) const {
    std::vector<SightingEvidence> result;
    if(state_){state_->Scan("observation",[&](const std::string&,const std::string& value){auto j=nlohmann::json::parse(value);if(!j.at("sighting").is_null())result.push_back(j.at("sighting").get<SightingEvidence>());},target,true);
        if(result.empty() && !state_->Get("association_target",target))throw std::out_of_range("association target not found");return result;}
    const auto history = target_observations_.find(target);
    if (history == target_observations_.end()) throw std::out_of_range("association target not found");
    for (const auto& id : history->second) result.push_back(*records_.at(id).result.sighting);
    return result;
}

std::uint64_t AssociationStore::TargetRevision(const std::string& target) const {
    if (state_) {
        const auto revision = state_->Get("association_target_revision", target);
        if (revision) return std::stoull(*revision);
        // Old databases have targets but no per-target binding. A stable legacy
        // generation requires one rebuild, without invalidating unrelated targets.
        return state_->Get("association_target", target) ? 1 : 0;
    }
    const auto found = target_revisions_.find(target);
    return found == target_revisions_.end() ? 0 : found->second;
}

}  // namespace face_trajectory
