#include "face_trajectory/monitoring.hpp"
#include "face_trajectory/activity.hpp"
#include "face_trajectory/storage_json.hpp"
#include <algorithm>
#include <cctype>
#include <limits>
#include <set>
#include <stdexcept>
#include <tuple>

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
// Reverse index target -> receipts that may be shown as HOLD review bubbles.
// Rows are written for every stored revision whose candidate list names the
// target, so the index is a superset; HeldCandidates re-applies the complete
// filter to the current receipt. Without it each projection parsed every
// receipt and payload of the activity (O(all observations) per observe).
constexpr const char* kHeldIndex="monitoring_held_candidate";
constexpr const char* kHeldIndexReady="held_candidate_index_v1";
bool HoldReceipt(const json& response) {
    if(!response.is_object() || response.value("status","")=="ACCEPTED" ||
       !response.contains("association") || !response.at("association").is_object()) return false;
    const auto& association=response.at("association");
    return !(association.contains("sighting") && !association.at("sighting").is_null());
}
void IndexHeldCandidates(State* state,const std::string& id,const json& response) {
    if(!HoldReceipt(response)) return;
    const auto& candidates=response.at("association").value("candidates",json::array());
    if(!candidates.is_array()) return;
    for(const auto& candidate:candidates)
        if(candidate.is_object() && candidate.contains("target_id") && candidate.at("target_id").is_string()) {
            const auto& target=candidate.at("target_id").get_ref<const std::string&>();
            if(!target.empty()) state->Put(kHeldIndex,json::array({target,id}).dump(),json(id).dump(),target,id);
        }
}
void StoreObservationRecord(State* state,const std::string& id,json record) {
    IndexHeldCandidates(state,id,record.at("response"));
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

// HOLD observations are intentionally kept separate from confirmed sightings.
// Their durable monitoring receipt still contains the candidate list and the
// original non-biometric capture metadata, which is enough for a command UI to
// show a review bubble and later call association_correct.  Never expose the
// embedding or the complete candidate list from this query.
json HeldCandidates(State* state, const std::string& target, const AssociationStore& associations,
                    std::optional<std::int64_t> before) {
    auto result = json::array();
    auto consider = [&](const std::string& id, const json& record) {
        const auto& response = record.at("response");
        if (response.value("status", "") == "ACCEPTED" ||
            !response.contains("association") || response.at("association").is_null()) return;
        const auto& association = response.at("association");
        if (association.contains("sighting") && !association.at("sighting").is_null()) return;
        const auto& candidates = association.value("candidates", json::array());
        bool target_candidate = false;
        for (const auto& candidate : candidates) {
            if (candidate.value("target_id", "") == target) {
                target_candidate = true;
                break;
            }
        }
        if (!target_candidate) return;

        const auto payload = record.value("payload", json::object());
        const auto observation = payload.value("observation", json::object());
        const auto observation_id = observation.value("observation_id", id);
        // Corrections/reconsiderations update the authoritative association
        // row; do not leave a stale review bubble after it becomes confirmed.
        if (const auto current = associations.Result(observation_id);
            current && (current->sighting || current->target_id)) return;
        const auto normalization = response.value("normalization", json::object());
        if (before && normalization.contains("captured_at_unix_ms") &&
            normalization.at("captured_at_unix_ms").is_number_integer() &&
            normalization.at("captured_at_unix_ms").template get<std::int64_t>() >= *before) return;
        const auto policy = payload.value("policy", json::object());
        bool quality_passed = false;
        if (observation.contains("detection_score") && observation.contains("face_size_px") &&
            policy.contains("min_detection_score") && policy.contains("min_face_size_px")) {
            quality_passed = observation.at("detection_score").is_number() &&
                observation.at("face_size_px").is_number() &&
                observation.at("detection_score").template get<double>() >= policy.at("min_detection_score").template get<double>() &&
                observation.at("face_size_px").template get<double>() >= policy.at("min_face_size_px").template get<double>();
        } else {
            quality_passed = association.value("decision", "") != "UNASSIGNED";
        }
        json top1 = nullptr;
        if (!candidates.empty() && candidates.front().contains("similarity"))
            top1 = candidates.front().at("similarity");
        auto reasons = response.value("reason_codes", json::array());
        std::string hold_reason;
        if (reasons.is_array() && !reasons.empty() && reasons.front().is_string())
            hold_reason = reasons.front().template get<std::string>();
        if (hold_reason.empty()) hold_reason = association.value("reason", "HOLD");
        const auto captured = normalization.contains("captured_at_unix_ms")
            ? normalization.at("captured_at_unix_ms") : json(nullptr);
        result.push_back({
            {"observation_id", observation_id},
            {"source_id", observation.value("source_id", "")},
            {"camera_id", observation.value("camera_id", "")},
            {"captured_at_unix_ms", captured},
            {"hold_reason", hold_reason},
            {"status_reason", association.value("reason", hold_reason)},
            {"top1_similarity", top1},
            {"quality_passed", quality_passed},
            {"status", "HOLD"},
            {"included_in_confirmed_trajectory", false}
        });
    };
    if (state->Get("monitoring_meta", kHeldIndexReady)) {
        std::set<std::string> ids;
        state->Scan(kHeldIndex, [&](const std::string&, const std::string& id) { ids.insert(json::parse(id).get<std::string>()); }, target);
        for (const auto& id : ids)
            if (const auto text = state->Get("monitoring_observation", id)) consider(id, ObservationRecord(state, *text, true));
    } else {
        // One-time upgrade of receipts written before the index existed:
        // read everything first (no writes during Scan), then index them.
        std::vector<std::pair<std::string, std::string>> rows;
        state->Scan("monitoring_observation", [&](const auto& id, const auto& text) { rows.emplace_back(id, text); });
        for (const auto& [id, text] : rows) {
            const auto record = ObservationRecord(state, text, true);
            consider(id, record);
            IndexHeldCandidates(state, id, record.at("response"));
        }
        state->Put("monitoring_meta", kHeldIndexReady, "1");
    }
    std::sort(result.begin(), result.end(), [](const auto& a, const auto& b) {
        const auto at = a.at("captured_at_unix_ms").is_number_integer()
            ? a.at("captured_at_unix_ms").template get<std::int64_t>() : std::numeric_limits<std::int64_t>::max();
        const auto bt = b.at("captured_at_unix_ms").is_number_integer()
            ? b.at("captured_at_unix_ms").template get<std::int64_t>() : std::numeric_limits<std::int64_t>::max();
        return std::tie(at, a.at("observation_id").template get_ref<const std::string&>()) <
            std::tie(bt, b.at("observation_id").template get_ref<const std::string&>());
    });
    return result;
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

json MonitoringStore::PhotoUrl(const json& value) {
    if(value.is_null())return nullptr;
    if(!value.is_string())throw std::invalid_argument("photo URL must be an absolute HTTP(S) URL or null");
    const auto url=value.get<std::string>();
    const auto separator=url.find("://");
    auto scheme=url.substr(0,separator);
    std::transform(scheme.begin(),scheme.end(),scheme.begin(),[](unsigned char c){return static_cast<char>(std::tolower(c));});
    if(url.size()>4096 || separator==std::string::npos || (scheme!="https" && scheme!="http") ||
       std::any_of(url.begin(),url.end(),[](unsigned char c){return c<=32 || c==127 || c=='\\';}))
        throw std::invalid_argument("photo URL must be an absolute HTTP(S) URL without whitespace, limited to 4096 bytes");
    const auto start=separator+3,end=url.find_first_of("/?#",start);
    const auto authority=url.substr(start,end==std::string::npos?end:end-start);
    if(authority.empty() || authority.find('@')!=std::string::npos)
        throw std::invalid_argument("photo URL requires a host and must not contain user credentials");
    std::string host,port;
    if(authority.front()=='[') {
        const auto close=authority.find(']');
        if(close==std::string::npos || close<=1 || authority.substr(1,close-1).find(':')==std::string::npos)
            throw std::invalid_argument("photo URL has an invalid bracketed host");
        host=authority.substr(0,close+1);
        if(close+1<authority.size()) {
            if(authority[close+1]!=':')throw std::invalid_argument("photo URL has an invalid host/port");
            port=authority.substr(close+2);
            if(port.empty())throw std::invalid_argument("photo URL port must be numeric");
        }
    } else {
        const auto colon=authority.find(':');
        host=authority.substr(0,colon);
        if(colon!=std::string::npos) {
            port=authority.substr(colon+1);
            if(port.empty())throw std::invalid_argument("photo URL port must be numeric");
        }
        if(host.find_first_of("[]")!=std::string::npos)
            throw std::invalid_argument("photo URL has an invalid host");
    }
    if(host.empty() || (!port.empty() && (port.size()>5 ||
       !std::all_of(port.begin(),port.end(),[](unsigned char c){return c>='0' && c<='9';}) || std::stoul(port)>65535)))
        throw std::invalid_argument("photo URL requires a nonempty host and a valid port");
    return value;
}

json MonitoringStore::PersonSummary(const std::string& target) const {
    const auto person=Person(target);
    return {{"person_id",target},{"person_name",person.value("person_name",json(nullptr))},
        {"profile_photo_url",person.value("profile_photo_url",json(nullptr))},{"category",person.at("category")},
        {"is_priority",person.at("category")=="PRIORITY"},{"person_revision",person.at("revision")}};
}

json MonitoringStore::CameraTimeline(const json& request,const AssociationStore& associations,const json& context) const {
    RequireDurable();
    if(!ActivityStore::CameraEventsMode(context))
        throw std::invalid_argument("SOURCE_CONFLICT: named camera timeline requires UE_CAMERA_EVENTS");
    const auto timestamp=[](const json& object,const char* field)->std::int64_t {
        const auto& v=object.at(field);
        if(!v.is_number_integer() || (v.is_number_unsigned() && v.get<std::uint64_t>()>static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max())) ||
           (!v.is_number_unsigned() && v.get<std::int64_t>()<0))
            throw std::invalid_argument(std::string(field)+" must be nonnegative UTC milliseconds within int64");
        return v.get<std::int64_t>();
    };
    const auto target=Required(request,"person_id");
    const auto from=timestamp(request,"from_unix_ms"),to=timestamp(request,"to_unix_ms");
    if(from>=to)throw std::invalid_argument("from_unix_ms must precede to_unix_ms (end exclusive)");
    const auto limit=request.contains("limit")?Revision(request,"limit"):500;
    if(limit<1 || limit>1000)throw std::invalid_argument("timeline limit must be within 1..1000");
    const auto revision=associations.TargetRevision(target);
    if(request.contains("expected_association_revision") && Revision(request,"expected_association_revision")!=revision)
        throw std::invalid_argument("VERSION_CONFLICT: appearance timeline changed; restart pagination");
    const auto now=timestamp(request.at("_trusted_clock"),"now_unix_ms");
    const auto effective_from=std::max(from,now-ActivityStore::RetentionMs);
    using Order=std::tuple<std::int64_t,std::string,std::string>;
    const auto identity=[](const SightingEvidence& s) {
        auto source=s.source_id,id=s.observation_id;
        const auto parsed=json::parse(id,nullptr,false);
        if(parsed.is_array() && parsed.size()==2 && parsed[0].is_string() && parsed[1].is_string()) {
            source=parsed[0].get<std::string>();id=parsed[1].get<std::string>();
        }
        return std::make_pair(source,id);
    };
    const auto order=[&](const SightingEvidence& s) {
        const auto key=identity(s);return Order{s.captured_at_unix_ms,key.first,key.second};
    };
    std::optional<Order> after;
    if(request.contains("after")) {
        const auto& c=request.at("after");
        if(!c.is_object())throw std::invalid_argument("after must be a timeline cursor object");
        if(!request.contains("expected_association_revision"))
            throw std::invalid_argument("expected_association_revision is required with after");
        after=Order{timestamp(c,"captured_at_unix_ms"),Required(c,"source_id"),Required(c,"observation_id")};
    }
    std::vector<SightingEvidence> sightings;
    if(revision) {
        try{sightings=associations.Sightings(target);}catch(const std::out_of_range&){}
    }
    sightings.erase(std::remove_if(sightings.begin(),sightings.end(),[&](const auto& s) {
        return s.captured_at_unix_ms<effective_from || s.captured_at_unix_ms>=to ||
            s.link_status!=TargetLinkStatus::Confirmed || s.time_source!=TimeSource::UtcCapture || s.clock_domain!="UTC" ||
            s.clock_uncertainty_ms<0 || s.clock_uncertainty_ms>60000 || (after && order(s)<=*after);
    }),sightings.end());
    std::sort(sightings.begin(),sightings.end(),[&](const auto& a,const auto& b){return order(a)<order(b);});
    const bool more=sightings.size()>limit;
    if(more)sightings.resize(static_cast<std::size_t>(limit));
    auto timeline=json::array();
    for(const auto& s:sightings) {
        const auto key=identity(s);
        json name=nullptr,photo=nullptr;std::string name_source="UNAVAILABLE";
        if(const auto saved=state_->Get("monitoring_observation",s.observation_id)) {
            const auto record=ObservationRecord(state_,*saved,false);
            const auto event=record.at("response").value("camera_event",json(nullptr));
            if(event.is_object() && event.value("camera_id",std::string{})==s.camera_id &&
               event.value("captured_at_unix_ms",std::int64_t{-1})==s.captured_at_unix_ms && event.contains("camera_name")) {
                name=event.at("camera_name");name_source="CAPTURE_EVENT";
                photo=event.value("capture_photo_url",json(nullptr));
            }
            // A HOLD receipt never acquires a camera_event when identity is
            // later corrected. The immutable observation still owns its photo.
            if(photo.is_null() || (ActivityStore::PersonTrackMode(context) && name.is_null())) {
                const auto full=ObservationRecord(state_,*saved,true);
                const auto observation=full.value("payload",json::object()).value("observation",json::object());
                if(observation.value("camera_id",std::string{})==s.camera_id) {
                    if(ActivityStore::PersonTrackMode(context) && name.is_null()) {
                        name=observation.value("camera_name",json(nullptr));
                        name_source=name.is_null()?"UNAVAILABLE":"CAPTURE_EVENT";
                    }
                    try{photo=PhotoUrl(observation.value("capture_photo_url",json(nullptr)));}
                    catch(const std::invalid_argument&){} // Legacy unvalidated metadata is unavailable.
                }
            }
        }
        if(name.is_null() && !ActivityStore::PersonTrackMode(context))for(const auto& b:context.value("bindings",json::array())) {
            if(b.at("kind")=="CAMERA" && b.at("source_id")==key.first && b.at("device_id")==s.camera_id && b.contains("camera_name")) {
                name=b.at("camera_name");name_source="CURRENT_ACTIVITY_BINDING";break;
            }
        }
        timeline.push_back({{"camera_id",s.camera_id},{"camera_name",name},{"camera_name_source",name_source},
            {"capture_photo_url",photo},
            {"captured_at_unix_ms",s.captured_at_unix_ms},{"clock_uncertainty_ms",s.clock_uncertainty_ms},
            {"source_id",key.first},{"observation_id",s.observation_id}});
    }
    json next=nullptr;
    if(more && !timeline.empty()) {
        const auto& last=timeline.back();next={{"captured_at_unix_ms",last.at("captured_at_unix_ms")},
            {"source_id",last.at("source_id")},{"observation_id",last.at("observation_id")}};
    }
    const auto person=Person(target);
    return {{"ok",true},{"person_id",target},{"category",person.at("category")},{"is_priority",person.at("category")=="PRIORITY"},
        {"person",PersonSummary(target)},{"person_revision",person.at("revision")},{"association_revision",revision},{"from_unix_ms",from},{"to_unix_ms",to},
        {"effective_from_unix_ms",effective_from},{"history_pruned",effective_from>from},{"order_basis","CAPTURE_TIME_NOMINAL"},
        {"timeline",timeline},{"count",timeline.size()},{"has_more",more},{"next_after",next}};
}

json MonitoringStore::PutPerson(const json& request, Gallery& gallery, bool require_ready) {
    RequireDurable();
    const auto target = Required(request,"person_id");
    const auto category = Required(request,"category");
    if (category != "PRIORITY" && category != "NORMAL" && category != "UNREGISTERED")
        throw std::invalid_argument("category must be PRIORITY, NORMAL or UNREGISTERED");
    const auto revision = Revision(request,"revision"), expected = Revision(request,"expected_revision");
    if (!revision || revision <= expected) throw std::invalid_argument("revision must exceed expected_revision");
    const auto evidence = Required(request,"evidence_id");
    json content = {{"person_id",target},{"category",category},{"revision",revision},{"evidence_id",evidence}};
    if(request.contains("person_name")) {
        const auto& name=request.at("person_name");
        if(!name.is_null() && (!name.is_string() || name.get_ref<const std::string&>().empty() ||
           name.get_ref<const std::string&>().size()>1024 ||
           std::any_of(name.get_ref<const std::string&>().begin(),name.get_ref<const std::string&>().end(),[](unsigned char c){return c<32 || c==127;})))
            throw std::invalid_argument("person_name must be nonempty text limited to 1024 bytes, or null");
        content["person_name"]=name;
    }
    if(request.contains("profile_photo_url"))content["profile_photo_url"]=PhotoUrl(request.at("profile_photo_url"));
    if(request.contains("person_id_num")) {
        const auto& number=request.at("person_id_num");
        if(!number.is_string() || number.get_ref<const std::string&>().empty() || number.get_ref<const std::string&>().size()>128)
            throw std::invalid_argument("person_id_num must be a nonempty string limited to 128 bytes");
        content["person_id_num"]=number;
    }
    if (request.contains("templates")) content["templates"] = request.at("templates");
    bool ready=false;
    state_->Scan("template",[&](const std::string& id,const std::string& text){
        if(id.rfind("assoc-template:",0)!=0 && json::parse(text).at("person_entity_id")==target)ready=true;
    });
    const auto history = state_->Get("monitoring_person_revision", Key(target,revision));
    if (history) {
        if (json::parse(*history) != content) throw std::invalid_argument("SOURCE_CONFLICT: person revision changed");
        if(require_ready && !ready)throw std::invalid_argument("registration template is no longer available; submit a new revision with templates");
        return {{"ok",true},{"person",Person(target)},{"submitted_revision",revision},{"duplicate",true},{"gallery_version",gallery.Version()},{"recognition_ready",ready}};
    }
    if(request.contains("templates") && request.at("templates").is_array() && !request.at("templates").empty())ready=true;
    if(require_ready && !ready)throw std::invalid_argument("registration requires a valid recognition template; profile photo URL alone is insufficient");
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
    const auto previous=Person(target);
    for(const auto* field:{"person_name","person_id_num","profile_photo_url"})
        if(!content.contains(field) && previous.contains(field))content[field]=previous.at(field);
    state_->Put("monitoring_person",target,content.dump());
    return {{"ok",true},{"person",content},{"submitted_revision",revision},{"duplicate",false},{"gallery_version",gallery.Version()},{"recognition_ready",ready}};
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
        {"held_candidates",HeldCandidates(state_,target,associations,before)},
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
