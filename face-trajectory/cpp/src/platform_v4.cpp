#include "face_trajectory/platform_v4.hpp"
#include "face_trajectory/activity.hpp"
#include "face_trajectory/monitoring.hpp"
#include <algorithm>
#include <limits>
#include <set>
#include <stdexcept>
namespace face_trajectory {
using nlohmann::json;
namespace {
constexpr std::int64_t PersonLifetime=72LL*60*60*1000;
std::string Text(const json& value,const char* field) {
    const auto text=value.at(field).get<std::string>();
    if(text.empty() || text.size()>1024 || text.find_first_of("\r\n\t")!=std::string::npos)
        throw std::invalid_argument(std::string(field)+" must be nonempty text <=1024 bytes");
    return text;
}
json Reply(const json& context,bool duplicate=false) {
    return {{"ok",true},{"code",200},{"message","处理完成"},{"activityId",context.at("activity_id")},
        {"duplicate",duplicate},{"data",{{"enabled",context.at("enabled")}}}};
}
void Bind(json& context,const json& cameras) {
    if(!cameras.is_array() || cameras.size()>10000)throw std::invalid_argument("cameras must be an array of at most 10000 items");
    if(cameras.empty() && !context.value("bindings",json::array()).empty())throw std::invalid_argument("empty cameras cannot implicitly clear existing bindings");
    json bindings=json::array();std::set<std::string> seen;
    for(const auto& camera:cameras) {
        const auto id=Text(camera,"deviceId"),name=Text(camera,"deviceName");
        if(!seen.insert(id).second)throw std::invalid_argument("duplicate deviceId");
        bindings.push_back({{"source_id",id},{"device_id",id},{"camera_name",name},{"kind","CAMERA"},{"route_rule_id","PLATFORM-V4"}});
    }
    // Ordering does not change binding identity or invalidate live producers.
    std::sort(bindings.begin(),bindings.end(),[](const auto& a,const auto& b){return a.at("device_id")<b.at("device_id");});
    if(context.value("bindings",json::array())!=bindings) {
        context["bindings"]=bindings;context["binding_version"]=context.value("binding_version",0ULL)+1;
    }
}
}
json PlatformV4(State& root,const json& request) {
    ActivityStore activities(root);const auto now=activities.TrustedNow(request);
    const auto op=Text(request,"op");
    if(op=="platform_v4_expire")return {{"ok",true},{"expired",ExpirePlatformPersons(root,now)}};
    const auto id=Text(request,"activity_id");
    auto raw=root.Get("activity",id);
    if(op=="platform_v4_sync") {
        const auto& input=request.at("body");
        const auto num=Text(input,"activityNum");Text(input,"activityName");
        const auto start=input.at("start_unix_ms").get<std::int64_t>(),end=input.at("end_unix_ms").get<std::int64_t>();
        if(start<0 || end<=start)throw std::invalid_argument("activityEnd must be after activityStart");
        const auto status=Text(input,"activityState");
        if(status!="0" && status!="1" && status!="2")throw std::invalid_argument("activityState must be 0, 1 or 2");
        if(raw && json::parse(*raw).at("activity_num")!=num)throw std::invalid_argument("SOURCE_CONFLICT: activityNum is immutable");
        if(auto owner=root.Get("activity_num",num);owner && *owner!=id)throw std::invalid_argument("SOURCE_CONFLICT: activityNum already mapped");
        auto context=raw?json::parse(*raw):json{{"activity_id",id},{"activity_num",num},{"venue_id",id},
            {"config_version",1},{"binding_version",0},{"monitoring_version",1},{"enabled",false}};
        const auto old=context;
        context["configuration"]={{"output_mode","PERSON_TRACK_EVENTS"},{"platform_protocol",4},
            {"activity_name",input.at("activityName")},{"activity_state",status},{"start_unix_ms",start},{"end_unix_ms",end}};
        if(input.contains("cameras"))Bind(context,input.at("cameras"));
        if(raw && old.at("configuration")!=context.at("configuration"))context["config_version"]=context.at("config_version").get<std::uint64_t>()+1;
        root.Put("activity",id,context.dump());root.Put("activity_num",num,id);
        return Reply(context,raw && old==context);
    }
    if(!raw)throw std::invalid_argument("UNKNOWN_ACTIVITY: sync the activity first");
    auto context=json::parse(*raw);
    if(context.value("configuration",json::object()).value("platform_protocol",0)!=4)
        throw std::invalid_argument("SOURCE_CONFLICT: activity has not been synchronized with v4");
    if(op=="platform_v4_context")return {{"ok",true},{"context",context}};
    if(op=="platform_v4_bindings" || op=="platform_v4_monitoring") {
        const auto old=context;
        if(op=="platform_v4_bindings")Bind(context,request.at("body").at("cameras"));
        else {
            const bool enabled=request.at("body").at("enabled").get<bool>();
            if(enabled && context.value("bindings",json::array()).empty())throw std::invalid_argument("configure cameras before enabling monitoring");
            if(context.at("enabled")!=enabled) {
                context["enabled"]=enabled;context["monitoring_version"]=context.at("monitoring_version").get<std::uint64_t>()+1;
            }
        }
        root.Put("activity",id,context.dump());return Reply(context,old==context);
    }
    if(op=="platform_v4_person") {
        State scope(root,id);MonitoringStore monitoring(&scope);Gallery gallery(&scope);
        const auto& person=request.at("person");const auto person_id=Text(person,"personId");
        Text(person,"personName");Text(person,"personIdNum");
        MonitoringStore::PhotoUrl(person.at("personPhoto"));
        auto identity=person;identity.erase("templates");
        identity["feature_space"]=person.at("templates").at(0).at("feature_space");
        const auto digest=ActivityStore::Digest(identity.dump());
        const auto old=scope.Get("platform_person_import",person_id);
        if(old && json::parse(*old).at("digest")==digest) {
            const auto record=json::parse(*old);
            bool ready=false;
            scope.Scan("template",[&](const auto& key,const auto& value){auto t=json::parse(value);if(key.rfind("assoc-template:",0)!=0 && t.at("person_entity_id")==person_id && t.at("feature_space")==identity.at("feature_space"))ready=true;});
            if(ready)return {{"ok",true},{"recognition_ready",true},{"duplicate",true},{"expires_at_unix_ms",record.at("expires_at_unix_ms")}};
        }
        if(scope.Get("platform_person_expired",ActivityStore::Digest(person_id+":"+digest)))
            throw std::invalid_argument("PERSON_EXPIRED: the original 72-hour registration has expired");
        const auto previous=monitoring.Person(person_id);const auto version=previous.at("revision").get<std::uint64_t>();
        json registration={{"person_id",person_id},{"person_name",person.at("personName")},{"person_id_num",person.at("personIdNum")},
            {"profile_photo_url",person.at("personPhoto")},{"category","PRIORITY"},{"revision",version+1},{"expected_revision",version},
            {"evidence_id","platform-v4:"+digest},{"templates",person.at("templates")}};
        // The core gallery rejects duplicate template IDs. A metadata change or
        // a new model can legitimately reuse this person's stable photo ID.
        // Remove only that person's colliding entries inside the same transaction.
        for(const auto& incoming:person.at("templates")) {
            const auto key=incoming.at("template_id").get<std::string>();
            if(const auto saved=scope.Get("template",key)) {
                if(json::parse(*saved).at("person_entity_id")!=person_id)throw std::invalid_argument("registration cannot replace another person's template");
                gallery.Remove(key);
            }
        }
        const auto reply=monitoring.PutPerson(registration,gallery,true);
        // Replacing a person's photograph must remove their old recognition templates.
        std::set<std::string> live;for(const auto& t:person.at("templates"))live.insert(t.at("template_id"));
        std::vector<std::string> stale;
        scope.Scan("template",[&](const auto& key,const auto& value){if(json::parse(value).at("person_entity_id")==person_id && !live.count(key))stale.push_back(key);});
        for(const auto& key:stale)gallery.Remove(key);
        const auto expires=old?json::parse(*old).at("expires_at_unix_ms").get<std::int64_t>():now+PersonLifetime;
        scope.Put("platform_person_import",person_id,json{{"digest",digest},{"expires_at_unix_ms",expires}}.dump());
        auto stored=monitoring.Person(person_id);stored["expires_at_unix_ms"]=expires;scope.Put("monitoring_person",person_id,stored.dump());
        const auto next=std::stoll(root.Get("platform_meta","person_next_deadline").value_or(std::to_string(std::numeric_limits<std::int64_t>::max())));
        if(expires<next)root.Put("platform_meta","person_next_deadline",std::to_string(expires));
        return {{"ok",true},{"recognition_ready",reply.at("recognition_ready")},{"duplicate",false},{"expires_at_unix_ms",expires}};
    }
    throw std::invalid_argument("unsupported platform v4 operation");
}
bool ExpirePlatformPersons(State& root,std::int64_t now) {
    const auto deadline=std::stoll(root.Get("platform_meta","person_next_deadline").value_or("0"));
    if(now<deadline)return false;
    bool changed=false;
    auto next=std::numeric_limits<std::int64_t>::max();
    for(const auto& name:root.Namespaces()) {
        const auto ns=json::parse(name,nullptr,false);
        if(!ns.is_array() || ns.size()!=3 || ns[0]!="activity" || ns[2]!="platform_person_import")continue;
        State scope(root,ns[1].get<std::string>());
        std::vector<std::pair<std::string,json>> imports;
        scope.Scan("platform_person_import",[&](const auto& key,const auto& value){imports.emplace_back(key,json::parse(value));});
        for(const auto& [person,record]:imports) {
        const auto expires=record.at("expires_at_unix_ms").get<std::int64_t>();
        if(now<expires) {next=std::min(next,expires);continue;}
        std::vector<std::string> templates,revisions;
        scope.Scan("template",[&](const auto& key,const auto& value){if(json::parse(value).at("person_entity_id")==person)templates.push_back(key);});
        scope.Scan("monitoring_person_revision",[&](const auto& key,const auto& value){if(json::parse(value).at("person_id")==person)revisions.push_back(key);});
        for(const auto& key:templates)scope.Erase("template",key);
        if(!templates.empty())scope.Put("version","gallery",std::to_string(std::stoull(scope.Get("version","gallery").value_or("0"))+templates.size()));
        for(const auto& key:revisions)scope.Erase("monitoring_person_revision",key);
        for(const auto* collection:{"person_track_candidate","person_track_state"}) {
            std::vector<std::pair<std::string,json>> scrub;
            scope.Scan(collection,[&](const auto& key,const auto& raw){auto value=json::parse(raw);if(value.value("person_id",json(nullptr))==person && value.contains("person_name")) {
                value["person_name"]=nullptr;value["person_id_num"]=nullptr;scrub.emplace_back(key,value);
            }});
            for(const auto& [key,value]:scrub) {
                const auto ref=std::string(collection)=="person_track_candidate"?value.value("representative",std::string{}):json::array({value.at("person_id"),value.at("captured_at_unix_ms").get<std::int64_t>()/5000}).dump();
                scope.Put(collection,key,value.dump(),ref);
            }
        }
        // Registration material is removed. Immutable pending outbound events
        // deliberately retain their own identity and capture snapshots.
        scope.Erase("monitoring_person",person);scope.Erase("platform_person_import",person);
        scope.Put("platform_person_expired",ActivityStore::Digest(person+":"+record.at("digest").get<std::string>()),"1");
        changed=true;
        }
    }
    root.Put("platform_meta","person_next_deadline",std::to_string(next));
    return changed;
}
std::string CapturePhotoBase64(const json& value) {
    if(!value.is_string())throw std::invalid_argument("capture_photo_base64 must contain the captured image");
    const auto& encoded=value.get_ref<const std::string&>();
    if(encoded.empty() || encoded.size()%4 || encoded.size()>4*((5*1024*1024+2)/3))
        throw std::invalid_argument("capture image is not bounded pure Base64");
    const std::string alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    std::string decoded;decoded.reserve(encoded.size()/4*3);
    for(std::size_t i=0;i<encoded.size();i+=4) {
        unsigned n=0;int padding=0;
        for(int j=0;j<4;++j) {
            const auto c=encoded[i+j];
            if(c=='=') {if(i+4!=encoded.size() || j<2)throw std::invalid_argument("invalid Base64 padding");++padding;n<<=6;}
            else {const auto digit=alphabet.find(c);if(padding || digit==std::string::npos)throw std::invalid_argument("invalid Base64 image");n=(n<<6)|static_cast<unsigned>(digit);}
        }
        if(padding>2 || (padding==2 && (n&0xffff)) || (padding==1 && (n&0xff)))throw std::invalid_argument("noncanonical Base64 image");
        decoded.push_back(static_cast<char>(n>>16));if(padding<2)decoded.push_back(static_cast<char>(n>>8));if(padding<1)decoded.push_back(static_cast<char>(n));
    }
    const bool png=decoded.size()>=33 && decoded.compare(0,8,"\x89PNG\r\n\x1a\n",8)==0;
    const bool jpeg=decoded.size()>=4 && static_cast<unsigned char>(decoded[0])==255 && static_cast<unsigned char>(decoded[1])==216 &&
        static_cast<unsigned char>(decoded[decoded.size()-2])==255 && static_cast<unsigned char>(decoded.back())==217;
    if(decoded.size()>5*1024*1024 || (!png && !jpeg))throw std::invalid_argument("capture image must be JPEG or PNG <=5MiB");
    return encoded;
}
}
