#include "face_trajectory/person_track.hpp"
#include "face_trajectory/activity.hpp"
#include "face_trajectory/platform_v4.hpp"
#include <algorithm>
#include <limits>
#include <stdexcept>
namespace face_trajectory {
using nlohmann::json;
namespace {
std::string PersonCamera(const json& event) {
    const auto camera=event.value("device_id",event.value("camera_id",event.value("source_id",std::string{})));
    if(camera.empty())throw std::invalid_argument("person track camera ID is required");
    return json::array({event.at("person_id"),camera}).dump();
}
void EnsureIndex(State& scope) {
    if(scope.Get("person_track_meta","dedup_person_camera_v2"))return;
    std::vector<std::pair<std::string,json>> old;
    scope.Scan("person_track_state",[&](const auto& key,const auto& value){old.emplace_back(key,json::parse(value));});
    for(const auto& [key,event]:old) {
        const auto person=event.at("person_id");
        // Upgrade the lookup only: old event IDs, photos and historical points
        // remain intact and are not replayed to the backend.
        scope.Put("person_track_state",key,event.dump(),person.is_null()?std::string{}:PersonCamera(event));
        auto candidate=event;
        candidate.erase("event_id");candidate.erase("event_type");candidate.erase("revision");
        candidate["representative"]=person.is_null()?std::string{}:key;
        if(!scope.Get("person_track_candidate",key))
            scope.Put("person_track_candidate",key,candidate.dump(),person.is_null()?std::string{}:key);
    }
    scope.Put("person_track_meta","dedup_person_camera_v2","1");
}
// State is activity-scoped. A person's camera reports once for this activity,
// regardless of elapsed time. Different people/cameras have distinct keys.
// Only active representatives are indexed;
// suppressed observations retain independent identity/correction evidence.
std::string Representative(State& scope,const std::string& id,const json& candidate) {
    std::vector<std::pair<std::int64_t,std::string>> matches;
    scope.Scan("person_track_state",[&](const auto& key,const auto& value){
        if(key==id)return;
        const json e=json::parse(value);
        const auto t=e.at("captured_at_unix_ms").get<std::int64_t>();
        if(e.at("person_id")==candidate.at("person_id") && PersonCamera(e)==PersonCamera(candidate))
            matches.emplace_back(t,key);
    },PersonCamera(candidate));
    if(matches.empty())return id;
    return std::min_element(matches.begin(),matches.end())->second;
}
void Emit(State& scope,const std::string& id,const json& candidate,const json& person,std::vector<json>& events) {
    const auto saved=scope.Get("person_track_state",id);
    if(!saved && person.is_null())return;
    json event=saved?json::parse(*saved):candidate;
    if(saved && event.at("person_id")==person)return;
    event.erase("representative");
    const auto revision=event.value("revision",std::uint64_t{0});
    if(revision==std::numeric_limits<std::uint64_t>::max())throw std::runtime_error("person track revision exhausted");
    event["revision"]=revision+1;event["person_id"]=person;event["event_type"]=person.is_null()?"RETRACT":"UPSERT";
    if(event.contains("device_id"))for(const auto* field:{"person_name","person_id_num"})event[field]=person.is_null()?json(nullptr):candidate.at(field);
    event["event_id"]="pt:"+ActivityStore::Digest(json::array({event.at("activity_id"),event.value("device_id",event.value("source_id",std::string{})),event.at("observation_id"),revision+1}).dump());
    const auto ref=person.is_null()?std::string{}:PersonCamera(event);
    scope.Put("person_track_state",id,event.dump(),ref);
    events.push_back(event);
}
void Place(State& scope,const std::string& id,json candidate,std::vector<json>& events) {
    const auto representative=candidate.at("person_id").is_null()?std::string{}:Representative(scope,id,candidate);
    Emit(scope,id,candidate,representative==id?candidate.at("person_id"):json(nullptr),events);
    candidate["representative"]=representative;
    scope.Put("person_track_candidate",id,candidate.dump(),representative);
}
}
std::vector<json> PersonTrackEvents(State& scope,const json& request,const json& context,
    const MonitoringStore& monitoring,const AssociationStore& associations) {
    const auto op=request.at("op").get<std::string>();
    if(op!="monitoring_observe" && op!="association_correct")return {};
    EnsureIndex(scope);
    const auto id=(op=="monitoring_observe"?request.at("observation").at("observation_id"):request.at("observation_id")).get<std::string>();
    const auto result=associations.Result(id);
    if(!result)return {};
    json person=nullptr;
    if(result->sighting && result->target_id && monitoring.Person(*result->target_id).at("category")=="PRIORITY")
        person=*result->target_id;
    const auto saved=scope.Get("person_track_candidate",id);
    json candidate=saved?json::parse(*saved):json::object();
    if(saved && candidate.at("person_id")==person)return {};
    if(!saved) {
        // Preserve any previously delivered record across this software upgrade.
        if(const auto prior=scope.Get("person_track_state",id)) {
            candidate=json::parse(*prior);
            candidate.erase("event_id");candidate.erase("event_type");candidate.erase("revision");
            candidate["representative"]=candidate.at("person_id").is_null()?std::string{}:id;
        } else {
            if(person.is_null())return {};
            const auto raw=scope.Get("monitoring_observation",id);
            if(!raw)return {};
            auto record=json::parse(*raw);
            if(record.contains("record_ref"))record=json::parse(scope.Get("object",record.at("record_ref")).value());
            if(!record.at("response").value("time_admitted",false))return {};
            const auto& sighting=*result->sighting;
            if(record.contains("payload_ref"))record["payload"]=json::parse(scope.Get("object",record.at("payload_ref")).value());
            const auto& observation=record.at("payload").at("observation");
            const auto key=json::parse(id);
            candidate={{"activity_id",context.at("activity_id")},{"source_id",key.at(0)},{"observation_id",key.at(1)},
                {"camera_id",observation.at("camera_id")},{"camera_name",observation.value("camera_name",json(nullptr))},
                {"captured_at_unix_ms",sighting.captured_at_unix_ms},{"clock_uncertainty_ms",sighting.clock_uncertainty_ms},
                {"capture_photo_url",MonitoringStore::PhotoUrl(observation.value("capture_photo_url",json(nullptr)))}};
            if(context.value("configuration",json::object()).value("platform_protocol",0)==4) {
                candidate.erase("source_id");candidate["device_id"]=observation.at("camera_id");
                candidate["capture_photo_url"]=CapturePhotoBase64(observation.value("capture_photo_base64",json(nullptr)));
                for(const auto& binding:context.at("bindings"))if(binding.at("device_id")==observation.at("camera_id"))candidate["camera_name"]=binding.at("camera_name");
            }
        }
    }
    std::vector<std::pair<std::string,json>> orphans;
    if(candidate.value("representative",std::string{})==id)scope.Scan("person_track_candidate",[&](const auto& key,const auto& value){
        if(key!=id)orphans.emplace_back(key,json::parse(value));
    },id);
    candidate["person_id"]=person;
    if(candidate.contains("device_id")) {
        const auto profile=person.is_null()?json::object():monitoring.Person(person.get<std::string>());
        candidate["person_name"]=profile.value("person_name",json(nullptr));
        candidate["person_id_num"]=profile.value("person_id_num",json(nullptr));
    }
    std::vector<json> events;
    Place(scope,id,candidate,events);
    // Withdrawn/corrected representatives do not erase the remaining cameras'
    // evidence of the old person's passage. Promote it in the same transaction.
    std::sort(orphans.begin(),orphans.end(),[](const auto& a,const auto& b){
        return std::make_pair(a.second.at("captured_at_unix_ms").template get<std::int64_t>(),a.first)<
               std::make_pair(b.second.at("captured_at_unix_ms").template get<std::int64_t>(),b.first);
    });
    for(const auto& [key,value]:orphans)Place(scope,key,value,events);
    return events;
}
}
