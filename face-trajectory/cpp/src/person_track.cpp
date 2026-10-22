#include "face_trajectory/person_track.hpp"
#include "face_trajectory/activity.hpp"
#include <limits>
#include <stdexcept>
namespace face_trajectory {
using nlohmann::json;
std::optional<json> PersonTrackEvent(State& scope,const json& request,const json& context,
    const MonitoringStore& monitoring,const AssociationStore& associations) {
    const auto op=request.at("op").get<std::string>();
    if(op!="monitoring_observe" && op!="association_correct")return {};
    const auto id=(op=="monitoring_observe"?request.at("observation").at("observation_id"):request.at("observation_id")).get<std::string>();
    const auto result=associations.Result(id);
    if(!result)return {};
    json person=nullptr;
    if(result->sighting && result->target_id && monitoring.Person(*result->target_id).at("category")=="PRIORITY")
        person=*result->target_id;
    const auto saved=scope.Get("person_track_state",id);
    json event=saved?json::parse(*saved):json::object();
    if(!saved && person.is_null())return {};
    if(saved && event.at("person_id")==person)return {};
    if(!saved) {
        const auto raw=scope.Get("monitoring_observation",id);
        if(!raw)return {};
        auto record=json::parse(*raw);
        if(record.contains("record_ref"))record=json::parse(scope.Get("object",record.at("record_ref")).value());
        const auto& response=record.at("response");
        if(!response.value("time_admitted",false))return {};
        // The authoritative sighting retains qualified capture time even when
        // retention compacts an old receipt and drops its normalization detail.
        const auto& sighting=*result->sighting;
        if(record.contains("payload_ref"))record["payload"]=json::parse(scope.Get("object",record.at("payload_ref")).value());
        const auto& observation=record.at("payload").at("observation");
        const auto key=json::parse(id);
        event={{"activity_id",context.at("activity_id")},{"source_id",key.at(0)},{"observation_id",key.at(1)},
            {"camera_id",observation.at("camera_id")},{"camera_name",nullptr},
            {"captured_at_unix_ms",sighting.captured_at_unix_ms},{"clock_uncertainty_ms",sighting.clock_uncertainty_ms},
            {"capture_photo_url",MonitoringStore::PhotoUrl(observation.value("capture_photo_url",json(nullptr)))},{"revision",0}};
        const auto camera_event=response.value("camera_event",json(nullptr));
        if(camera_event.is_object())event["camera_name"]=camera_event.value("camera_name",json(nullptr));
        else for(const auto& binding:context.at("bindings"))
            if(binding.at("kind")=="CAMERA" && binding.at("source_id")==key.at(0) && binding.at("device_id")==observation.at("camera_id"))
                event["camera_name"]=binding.value("camera_name",json(nullptr));
    }
    const auto revision=event.at("revision").get<std::uint64_t>();
    if(revision==std::numeric_limits<std::uint64_t>::max())throw std::runtime_error("person track revision exhausted");
    event["revision"]=revision+1;event["person_id"]=person;event["event_type"]=person.is_null()?"RETRACT":"UPSERT";
    event["event_id"]="pt:"+ActivityStore::Digest(json::array({event.at("activity_id"),event.at("source_id"),event.at("observation_id"),revision+1}).dump());
    scope.Put("person_track_state",id,event.dump());
    return event;
}
}
