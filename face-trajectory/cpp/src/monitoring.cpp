#include "face_trajectory/monitoring.hpp"
#include "face_trajectory/activity.hpp"
#include "face_trajectory/storage_json.hpp"
#include <algorithm>
#include <limits>
#include <set>
#include <stdexcept>

namespace face_trajectory {
namespace {
using nlohmann::json;
std::string Required(const json& value, const char* name) {
    const auto result = value.at(name).get<std::string>();
    if (result.empty() || result.size() > 1024) throw std::invalid_argument(std::string(name) + " is required and limited to 1024 bytes");
    return result;
}
std::uint64_t Revision(const json& value, const char* name) {
    const auto& item = value.at(name);
    if (!item.is_number_integer() || (item.is_number_integer() && !item.is_number_unsigned() && item.get<std::int64_t>() < 0))
        throw std::invalid_argument(std::string(name) + " must be a nonnegative integer");
    return item.get<std::uint64_t>();
}
json Payload(json request) {
    for(const auto* k:{"op","_trusted_clock","_client_context","_source_event_key","_normalization","_content_digest"})request.erase(k);
    return request;
}
std::string Key(const std::string& id, std::uint64_t revision) { return json::array({id,revision}).dump(); }
json ObservationRecord(State* state,const std::string& text,bool payload=true) {
    auto record=json::parse(text);
    if(record.contains("record_ref")) {
        const auto object=state->Get("object",record.at("record_ref"));
        if(!object) throw std::runtime_error("missing immutable monitoring record");
        record=json::parse(*object);
    }
    if(payload && record.contains("payload_ref")) {
        const auto object=state->Get("object",record.at("payload_ref"));
        if(!object) throw std::runtime_error("missing immutable monitoring input");
        record["payload"]=json::parse(*object);
    }
    return record;
}
void StoreObservationRecord(State* state,const std::string& id,json record) {
    if(record.contains("payload")) { record["payload_ref"]=state->Intern(record.at("payload").dump()); record.erase("payload"); }
    const json ref={{"revision",record.at("revision")},{"record_ref",state->Intern(record.dump())},
        {"time_admitted",record.at("response").value("time_admitted",false)}};
    state->Put("monitoring_observation_revision",Key(id,record.at("revision")),ref.dump(),id);
    state->Put("monitoring_observation",id,ref.dump());
}
struct Event { std::string id, kind; std::int64_t lower, upper; json evidence; };
std::pair<std::int64_t,std::int64_t> Bounds(std::int64_t at, std::int64_t error) {
    if (error < 0 || at < std::numeric_limits<std::int64_t>::min() + error ||
        at > std::numeric_limits<std::int64_t>::max() - error) throw std::invalid_argument("monitoring time bound overflow");
    return {at-error,at+error};
}
}

void MonitoringStore::RequireDurable() const {
    if (!state_) throw std::invalid_argument("monitoring intake requires durable state-file");
}

json MonitoringStore::Person(const std::string& target) const {
    RequireDurable();
    const auto stored = state_->Get("monitoring_person", target);
    if (stored) return json::parse(*stored);
    return {{"person_id",target},{"category","UNREGISTERED"},{"revision",0},{"evidence_id",nullptr}};
}

json MonitoringStore::PutPerson(const json& request, Gallery& gallery) {
    RequireDurable();
    const auto target = Required(request,"person_id");
    const auto category = Required(request,"category");
    if (category != "PRIORITY" && category != "NORMAL" && category != "UNREGISTERED")
        throw std::invalid_argument("category must be PRIORITY, NORMAL or UNREGISTERED");
    const auto revision = Revision(request,"revision"), expected = Revision(request,"expected_revision");
    if (!revision || revision <= expected) throw std::invalid_argument("revision must exceed expected_revision");
    const auto evidence = Required(request,"evidence_id");
    json content = {{"person_id",target},{"category",category},{"revision",revision},{"evidence_id",evidence}};
    if (request.contains("templates")) content["templates"] = request.at("templates");
    const auto history = state_->Get("monitoring_person_revision", Key(target,revision));
    if (history) {
        if (json::parse(*history) != content) throw std::invalid_argument("SOURCE_CONFLICT: person revision changed");
        return {{"ok",true},{"person",Person(target)},{"submitted_revision",revision},{"duplicate",true},{"gallery_version",gallery.Version()}};
    }
    if (Person(target).at("revision").get<std::uint64_t>() != expected)
        throw std::runtime_error("VERSION_CONFLICT: person expected_revision is stale");
    std::vector<FaceTemplate> templates;
    std::set<std::string> ids;
    if (request.contains("templates")) {
        if (!request.at("templates").is_array() || request.at("templates").size() > 64)
            throw std::invalid_argument("templates must be an array of at most 64 items");
        for (const auto& item : request.at("templates")) {
            auto t = item;
            const auto id = Required(t,"template_id");
            if (!ids.insert(id).second || id.rfind("assoc-template:",0)==0 || target.rfind("auto:",0)==0)
                throw std::invalid_argument("duplicate or reserved registration template/target ID");
            if (t.contains("person_entity_id") && t.at("person_entity_id") != target)
                throw std::invalid_argument("registration template belongs to another person");
            if (!t.at("embedding").is_array() || t.at("embedding").size()!=kEmbeddingDim)
                throw std::invalid_argument("embedding must contain exactly 512 numbers");
            t["person_entity_id"] = target;
            for(const auto* optional:{"detector_model_sha256","detector_profile"})
                if(!t.at("feature_space").contains(optional)) t["feature_space"][optional]="";
            const auto value = t.get<FaceTemplate>();
            Gallery::ValidateQuery(value.feature_space,value.embedding);
            const auto owner = state_->Get("template",id);
            if (owner && json::parse(*owner).at("person_entity_id") != target)
                throw std::invalid_argument("registration cannot replace another person's template");
            templates.push_back(value);
        }
    }
    for (const auto& value : templates) gallery.Add(value);
    state_->Put("monitoring_person_revision",Key(target,revision),content.dump(),target);
    content.erase("templates");
    state_->Put("monitoring_person",target,content.dump());
    return {{"ok",true},{"person",content},{"submitted_revision",revision},{"duplicate",false},{"gallery_version",gallery.Version()}};
}

std::optional<json> MonitoringStore::ObservationReceipt(const json& request) const {
    RequireDurable();
    const auto id = Required(request.at("observation"),"observation_id");
    const auto stored = state_->Get("monitoring_observation",id);
    if (!stored) return {};
    const auto previous = ObservationRecord(state_,*stored);
    const auto payload = Payload(request);
    if (previous.at("payload") == payload)
        return std::optional<json>{previous.at("response")};
    throw std::invalid_argument("SOURCE_CONFLICT: immutable intake changed; use associations correct/rebuild");
}

void MonitoringStore::SaveObservation(const json& request, const json& response) {
    RequireDurable();
    const auto id = Required(request.at("observation"),"observation_id");
    const auto old = state_->Get("monitoring_observation",id);
    const auto revision = old ? json::parse(*old).at("revision").get<std::uint64_t>()+1 : 1;
    const json record = {{"revision",revision},{"payload",Payload(request)},{"response",response}};
    StoreObservationRecord(state_,id,record);
}

void MonitoringStore::CompleteObservationReceipt(const std::string& id, const json& response) {
    const auto stored=state_->Get("monitoring_observation",id);
    if(!stored) throw std::logic_error("missing intake receipt");
    auto record=ObservationRecord(state_,*stored,false); record["response"]=response;
    StoreObservationRecord(state_,id,record);
}

json MonitoringStore::PutGate(const json& request, const json& normalization, const AssociationStore& associations) {
    RequireDurable();
    const auto id=Required(request,"event_id");
    Required(request,"source_id"); Required(request,"gate_id");
    const auto direction=Required(request,"direction");
    if(direction!="ENTRY" && direction!="EXIT") throw std::invalid_argument("gate direction must be ENTRY or EXIT");
    if(request.contains("target_id") || request.contains("person_id"))
        throw std::invalid_argument("ticket event cannot assert a person; bind confirmed face observation with evidence");
    auto payload=Payload(request); payload.erase("binding");
    const auto oldtext=state_->Get("monitoring_gate",id);
    json old, binding=request.value("binding",json(nullptr));
    std::uint64_t revision=1;
    if(oldtext) {
        old=json::parse(*oldtext); revision=old.at("revision").get<std::uint64_t>();
        if(old.at("payload")!=payload)
            throw std::invalid_argument("SOURCE_CONFLICT: immutable gate intake changed");
        if(binding.is_null()) binding=old.at("binding");
        if(!old.at("binding").is_null() && old.at("binding")!=binding)
            throw std::invalid_argument("SOURCE_CONFLICT: gate binding is immutable; correct referenced face attribution instead");
    }
    std::string observation;
    if(!binding.is_null()) { observation=Required(binding,"observation_id"); Required(binding,"evidence_id"); }
    json record={{"event_id",id},{"revision",revision},{"payload",payload},{"binding",binding},{"normalization",normalization}};
    if(oldtext && old.at("payload")==payload && old.at("binding")==binding && old.at("normalization")==normalization) record=old;
    else {
        if(oldtext) record["revision"]=++revision;
        state_->Put("monitoring_gate_revision",Key(id,revision),record.dump(),id);
        state_->Put("monitoring_gate",id,record.dump(),"",observation);
        const auto count=std::stoull(state_->Get("monitoring_meta","gate_revision").value_or("0"));
        state_->Put("monitoring_meta","gate_revision",std::to_string(count+1));
    }
    std::string status="UNBOUND"; json target=nullptr;
    if(normalization.value("status","")!="NORMALIZED") status="HOLD";
    else if(!observation.empty()) {
        const auto managed=state_->Get("monitoring_observation",observation);
        const auto result=associations.Result(observation);
        if(managed && ObservationRecord(state_,*managed,false).at("response").value("time_admitted",false) && result && result->sighting) {
            status="BOUND"; target=*result->target_id;
        }
        else status="BINDING_PENDING";
    }
    json answer={{"ok",true},{"status",status},{"event_id",id},{"revision",record.at("revision")},
        {"normalization",normalization},{"target_id",target},{"identity_source","CONFIRMED_FACE_OBSERVATION_ONLY"}};
    if(!target.is_null()) answer["monitoring"]=Summary(Get(target.get<std::string>(),associations));
    if(status=="BOUND") {
        record["bound_target_id"]=target;
        record["binding_evidence_hash"]=ActivityStore::Digest(binding.at("evidence_id").get<std::string>());
        state_->Put("monitoring_gate",id,record.dump(),"",observation);
        state_->Put("monitoring_gate_revision",Key(id,record.at("revision")),record.dump(),id);
    }
    return answer;
}

json MonitoringStore::Summary(json projection) {
    const auto& sessions=projection.at("sessions");
    projection["current_session_id"]=sessions.empty()?json(nullptr):sessions.back().at("session_id");
    projection["session_record_count"]=sessions.size();
    projection["event_group_count"]=projection.at("event_groups").size();
    projection.erase("sessions"); projection.erase("event_groups");
    projection.erase("ignored_unqualified_gate_events");
    projection["detail_query"]="POST /v1/monitoring/get";
    return projection;
}

json MonitoringStore::ObserveSummary(const SightingEvidence& sighting,
    std::optional<std::uint64_t> previous_association_revision,const AssociationStore& associations) {
    RequireDurable();
    if(state_->Get("monitoring_runtime",sighting.target_id))return Summary(Get(sighting.target_id,associations));
    const auto& target=sighting.target_id;
    const auto gate_revision=std::stoull(state_->Get("monitoring_meta","gate_revision").value_or("0"));
    const auto stored=state_->Get("monitoring_projection",target);
    const auto bounds=Bounds(sighting.captured_at_unix_ms,sighting.clock_uncertainty_ms);
    const auto event_id="face:"+sighting.observation_id;
    bool bound_gate=false;
    state_->Scan("monitoring_gate",[&](const auto&,const auto& text) {
        bound_gate|=json::parse(text).at("normalization").value("status","")=="NORMALIZED";
    },{},false,sighting.observation_id);
    if(stored && previous_association_revision && !bound_gate) {
        auto cache=json::parse(*stored); auto& summary=cache["summary"];
        const auto new_key=std::make_tuple(bounds.first,bounds.second,event_id);
        const auto old_key=std::make_tuple(cache.at("last_lower").get<std::int64_t>(),
            cache.at("last_upper").get<std::int64_t>(),cache.at("last_id").get<std::string>());
        if(summary.at("association_revision")==*previous_association_revision &&
           summary.at("gate_revision")==gate_revision && summary.at("status")=="ACTIVE" && new_key>=old_key) {
            const auto group_upper=cache.at("group_upper").get<std::int64_t>();
            if(bounds.first>group_upper) summary["event_group_count"]=summary.at("event_group_count").get<std::size_t>()+1;
            cache["group_upper"]=std::max(group_upper,bounds.second);
            cache["last_lower"]=bounds.first; cache["last_upper"]=bounds.second; cache["last_id"]=event_id;
            const auto person=Person(target);
            summary["category"]=person.at("category"); summary["person_revision"]=person.at("revision");
            summary["association_revision"]=associations.TargetRevision(target);
            state_->Put("monitoring_projection",target,cache.dump());
            return summary;
        }
    }
    const auto projection=Get(target,associations);
    auto summary=Summary(projection);
    const auto& groups=projection.at("event_groups");
    if(!groups.empty()) {
        const auto& group=groups.back(); const auto& event=group.at("events").back();
        const json cache={{"summary",summary},{"group_upper",group.at("latest_unix_ms")},
            {"last_lower",event.at("earliest_unix_ms")},{"last_upper",event.at("latest_unix_ms")},{"last_id",event.at("event_id")}};
        state_->Put("monitoring_projection",target,cache.dump());
    } else state_->Erase("monitoring_projection",target);
    return summary;
}

json MonitoringStore::Get(const std::string& target, const AssociationStore& associations,std::optional<std::int64_t> before) const {
    RequireDurable();
    if(target.empty()) throw std::invalid_argument("target_id is required");
    std::vector<Event> events; std::set<std::string> seen_gates;
    auto ignored=json::array();
    const auto sightings=associations.TargetRevision(target)>0 ? associations.Sightings(target) : std::vector<SightingEvidence>{};
    for(const auto& sighting:sightings) {
        const auto managed=state_->Get("monitoring_observation",sighting.observation_id);
        if(!managed) continue; // Legacy bare UTC assertions cannot activate this clock-qualified intake.
        const auto rec=ObservationRecord(state_,*managed,false);
        if(!rec.at("response").value("time_admitted",false)) continue;
        const auto face_bounds=Bounds(sighting.captured_at_unix_ms,sighting.clock_uncertainty_ms);
        bool represented_by_gate=false;
        state_->Scan("monitoring_gate",[&](const auto& id,const auto& text) {
            const auto gate=json::parse(text);
            if(gate.at("normalization").value("status","")!="NORMALIZED") { ignored.push_back(id); return; }
            if(!seen_gates.insert(id).second) return;
            const auto& n=gate.at("normalization");
            const auto bounds=Bounds(n.at("captured_at_unix_ms"),n.at("clock_uncertainty_ms"));
            events.push_back({"gate:"+id,gate.at("payload").at("direction"),bounds.first,bounds.second,
                {{"event_id",id},{"observation_id",sighting.observation_id},{"binding_evidence_id",gate.at("binding").at("evidence_id")}}});
            // An explicit identity link does not erase an independently timed
            // face sighting. Only overlapping intervals can represent the same
            // passage evidence without fabricating an extra inside observation.
            represented_by_gate |= bounds.first<=face_bounds.second && face_bounds.first<=bounds.second;
        },{},false,sighting.observation_id);
        if(!represented_by_gate) {
            events.push_back({"face:"+sighting.observation_id,"SIGHTING",face_bounds.first,face_bounds.second,
                {{"observation_id",sighting.observation_id},{"camera_id",sighting.camera_id}}});
        }
    }
    // Identity already established for this gate passage remains evidence of
    // its own still-valid event. An expired face reference is not replayed.
    state_->Scan("monitoring_gate",[&](const auto& id,const auto& text){
        const auto gate=json::parse(text);
        if(!gate.value("binding_history_pruned",false)||!gate.at("binding").is_null()||seen_gates.count(id)||gate.value("bound_target_id",std::string{})!=target||gate.at("normalization").value("status","")!="NORMALIZED")return;
        const auto& n=gate.at("normalization");const auto bounds=Bounds(n.at("captured_at_unix_ms"),n.at("clock_uncertainty_ms"));
        events.push_back({"gate:"+id,gate.at("payload").at("direction"),bounds.first,bounds.second,{{"event_id",id},{"binding_evidence_hash",gate.value("binding_evidence_hash",std::string{})}}});
    });
    if(before)events.erase(std::remove_if(events.begin(),events.end(),[&](const auto& e){return e.lower+(e.upper-e.lower)/2>=*before;}),events.end());
    std::sort(events.begin(),events.end(),[](const auto& a,const auto& b){return std::tie(a.lower,a.upper,a.id)<std::tie(b.lower,b.upper,b.id);});
    auto groups=json::array(), sessions=json::array();
    std::string status="NOT_STARTED";
    auto boundary_dependencies=json::array();
    if(const auto seed=state_->Get("monitoring_runtime",target)) {
        auto runtime=json::parse(*seed);status=runtime.at("status");
        boundary_dependencies=runtime.value("boundary_dependencies",json::array());
        if(runtime.contains("session_id") && !runtime.at("session_id").is_null())sessions.push_back({{"session_id",runtime.at("session_id")},{"entry_status",runtime.at("entry_status")},{"status",status},{"boundary_ambiguous",runtime.value("boundary_ambiguous",false)},{"event_ids",json::array()},{"history_pruned",true}});
    }
    for(std::size_t i=0;i<events.size();) {
        std::size_t end=i+1; auto upper=events[i].upper;
        while(end<events.size() && events[end].lower<=upper) { upper=std::max(upper,events[end].upper); ++end; }
        bool positive=false, exits=false, entry=false;
        auto ids=json::array(), members=json::array();
        for(auto j=i;j<end;++j) {
            positive|=events[j].kind!="EXIT"; exits|=events[j].kind=="EXIT"; entry|=events[j].kind=="ENTRY";
            ids.push_back(events[j].id);
            members.push_back({{"event_id",events[j].id},{"kind",events[j].kind},{"earliest_unix_ms",events[j].lower},
                {"latest_unix_ms",events[j].upper},{"evidence",events[j].evidence}});
        }
        bool pruned_positive=false,pruned_exit=false;
        for(auto j=i;j<end;++j)for(const auto& dependency:boundary_dependencies)
            if(dependency.at("event_hash")==ActivityStore::Digest(events[j].id)){
                pruned_positive|=dependency.at("pruned_positive").get<bool>();
                pruned_exit|=dependency.at("pruned_exit").get<bool>();
            }
        positive|=pruned_positive;exits|=pruned_exit;
        const bool ambiguous=positive && exits;
        groups.push_back({{"event_ids",ids},{"events",members},{"earliest_unix_ms",events[i].lower},
            {"latest_unix_ms",upper},{"order_ambiguous",ambiguous},
            {"pruned_positive",pruned_positive},{"pruned_exit",pruned_exit}});
        if(sessions.empty() || (status=="CLOSED" && positive)) {
            sessions.push_back({{"session_id","monitor:"+ActivityStore::Digest(json::array({state_->Scope(),target,events[i].id}).dump())},{"entry_status",entry&&!ambiguous?"CONFIRMED":"UNKNOWN"},
                {"status","ACTIVE"},{"boundary_ambiguous",ambiguous},{"event_ids",json::array()},
                {"first_earliest_unix_ms",events[i].lower},{"last_latest_unix_ms",upper}});
        }
        auto& session=sessions.back();
        for(const auto& id:ids) session["event_ids"].push_back(id);
        session["last_latest_unix_ms"]=upper;
        if(ambiguous) session["boundary_ambiguous"]=true;
        status=ambiguous?"AMBIGUOUS":(positive?"ACTIVE":"CLOSED");
        session["status"]=status;
        i=end;
    }
    const auto person=Person(target);
    return {{"target_id",target},{"category",person.at("category")},{"person_revision",person.at("revision")},
        {"entry_status",!sessions.empty() && sessions.back().at("entry_status")=="CONFIRMED"?"KNOWN":"UNKNOWN"},
        {"association_revision",associations.TargetRevision(target)},
        {"gate_revision",std::stoull(state_->Get("monitoring_meta","gate_revision").value_or("0"))},
        {"status",status},{"continue_monitoring",status=="ACTIVE" || status=="AMBIGUOUS"},
        {"possible_statuses",status=="AMBIGUOUS"?json::array({"ACTIVE","CLOSED"}):json::array({status})},
        {"sessions",sessions},{"event_groups",groups},{"ignored_unqualified_gate_events",ignored},
        {"session_scope","CONSERVATIVE_REPLAY_OF_QUALIFIED_OBSERVATIONS_AND_EXPLICIT_GATE_BINDINGS"}};
}
void MonitoringStore::CheckpointRuntime(std::int64_t cutoff,const AssociationStore& associations) {
    std::set<std::string> targets;
    state_->Scan("association_target",[&](const auto& id,const auto&){targets.insert(id);});
    state_->Scan("monitoring_runtime",[&](const auto& id,const auto&){targets.insert(id);});
    for(const auto& target:targets) {
        const auto complete=Get(target,associations);
        auto p=Get(target,associations,cutoff);
        auto dependencies=json::array();
        for(const auto& group:complete.at("event_groups")) {
            bool old_positive=group.value("pruned_positive",false),old_exit=group.value("pruned_exit",false);
            for(const auto& event:group.at("events")) {
                const auto lower=event.at("earliest_unix_ms").get<std::int64_t>(),upper=event.at("latest_unix_ms").get<std::int64_t>();
                if(lower+(upper-lower)/2<cutoff){old_exit|=event.at("kind")=="EXIT";old_positive|=event.at("kind")!="EXIT";}
            }
            if(!old_positive&&!old_exit)continue;
            for(const auto& event:group.at("events")) {
                const auto lower=event.at("earliest_unix_ms").get<std::int64_t>(),upper=event.at("latest_unix_ms").get<std::int64_t>();
                if(lower+(upper-lower)/2>=cutoff)dependencies.push_back({{"event_hash",ActivityStore::Digest(event.at("event_id").get<std::string>())},{"pruned_positive",old_positive},{"pruned_exit",old_exit}});
            }
        }
        if(p.at("status")=="NOT_STARTED"&&dependencies.empty())continue;
        json seed={{"status",p.at("status")},{"entry_status","UNKNOWN"},{"session_id",nullptr},{"boundary_ambiguous",false}};
        seed["boundary_dependencies"]=dependencies;
        if(!p.at("sessions").empty()){const auto& s=p.at("sessions").back();seed["session_id"]=s.at("session_id");seed["entry_status"]=s.at("entry_status");seed["boundary_ambiguous"]=s.at("boundary_ambiguous");}
        // No historical time, camera, event key or passage sequence survives.
        state_->Put("monitoring_runtime",target,seed.dump());
    }
}
}
