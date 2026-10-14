#include "face_trajectory/activity.hpp"
#include <algorithm>
#include <array>
#include <iomanip>
#include <limits>
#include <set>
#include <sstream>
#include <stdexcept>

namespace face_trajectory {
using nlohmann::json;
namespace {
std::string Text(const json& v,const char* k) {
    const auto s=v.at(k).get<std::string>();
    if(s.empty() || s.size()>1024) throw std::invalid_argument(std::string(k)+" must be nonempty and <=1024 bytes");
    return s;
}
std::uint64_t Version(const json& v,const char* k) {
    const auto& n=v.at(k);
    if(!n.is_number_integer() || (!n.is_number_unsigned() && n.get<std::int64_t>()<0))
        throw std::invalid_argument(std::string(k)+" must be a nonnegative integer");
    return n.get<std::uint64_t>();
}
bool Event(const json& r) {return r.at("op")=="monitoring_observe" || r.at("op")=="monitoring_gate_event";}
json Stamp(const json& r) {
    auto v=r.at("_trusted_clock");
    if(v.at("verified")!=true || Text(v,"evidence_id").empty() ||
       !v.at("now_unix_ms").is_number_integer() || v.at("now_unix_ms").get<std::int64_t>()<ActivityStore::RetentionMs ||
       !v.at("uncertainty_ms").is_number_integer() || v.at("uncertainty_ms").get<std::int64_t>()<0 ||
       v.at("uncertainty_ms").get<std::int64_t>()>1000 ||
       v.at("valid_until_unix_ms").get<std::int64_t>()<=v.at("now_unix_ms").get<std::int64_t>())
        throw std::invalid_argument("CLOCK_UNTRUSTED: verified fresh UTC authority reference required");
    return v;
}
json WithContext(json r,const json& c) {
    for(const auto* k:{"activity_id","activity_num","venue_id","config_version","binding_version","monitoring_version","enabled"})r[k]=c.at(k);
    return r;
}
}

// SHA-256 is a content fingerprint, not identity or authorization. This small
// portable implementation keeps tombstones free of embeddings/times/locations.
std::string ActivityStore::Digest(const std::string& text) {
    static constexpr std::uint32_t k[64]={0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2};
    std::array<std::uint32_t,8> h={0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19};
    std::vector<unsigned char> b(text.begin(),text.end()); const auto bits=static_cast<std::uint64_t>(b.size())*8;
    b.push_back(0x80);while(b.size()%64!=56)b.push_back(0);for(int i=7;i>=0;--i)b.push_back(static_cast<unsigned char>(bits>>(i*8)));
    const auto rr=[](std::uint32_t n,int s){return(n>>s)|(n<<(32-s));};
    for(std::size_t p=0;p<b.size();p+=64) {
        std::uint32_t w[64];for(int i=0;i<16;++i)w[i]=(std::uint32_t(b[p+i*4])<<24)|(std::uint32_t(b[p+i*4+1])<<16)|(std::uint32_t(b[p+i*4+2])<<8)|b[p+i*4+3];
        for(int i=16;i<64;++i){auto x=w[i-15],y=w[i-2];w[i]=w[i-16]+(rr(x,7)^rr(x,18)^(x>>3))+w[i-7]+(rr(y,17)^rr(y,19)^(y>>10));}
        auto a=h[0],c=h[2],d=h[3],e=h[4],f=h[5],g=h[6],z=h[7],bb=h[1];
        for(int i=0;i<64;++i){auto t=z+(rr(e,6)^rr(e,11)^rr(e,25))+((e&f)^(~e&g))+k[i]+w[i];auto u=(rr(a,2)^rr(a,13)^rr(a,22))+((a&bb)^(a&c)^(bb&c));z=g;g=f;f=e;e=d+t;d=c;c=bb;bb=a;a=t+u;}
        h[0]+=a;h[1]+=bb;h[2]+=c;h[3]+=d;h[4]+=e;h[5]+=f;h[6]+=g;h[7]+=z;
    }
    std::ostringstream s;s<<std::hex<<std::setfill('0');for(auto n:h)s<<std::setw(8)<<n;return s.str();
}
json ActivityStore::Content(json r) {
    for(const auto* k:{"op","_trusted_clock","_client_context","_source_event_key","_normalization","_content_digest"})r.erase(k);
    return r;
}
std::string ActivityStore::EventKey(const json& r) {
    if(r.at("op")=="monitoring_observe")return json::array({Text(r.at("observation"),"source_id"),Text(r.at("observation"),"observation_id")}).dump();
    return json::array({Text(r,"source_id"),Text(r,"event_id")}).dump();
}
json ActivityStore::Context(const std::string& id) const {
    const auto raw=root_.Get("activity",id);
    if(!raw)throw std::invalid_argument("UNKNOWN_ACTIVITY: no authoritative activity mapping");
    return json::parse(*raw);
}
std::int64_t ActivityStore::TrustedNow(const json& r,bool advance) {
    const auto s=Stamp(r);const auto now=s.at("now_unix_ms").get<std::int64_t>();
    const auto old=std::stoll(root_.Get("retention_meta","utc_watermark").value_or("0"));
    if(now<old)throw std::invalid_argument("CLOCK_UNTRUSTED: authority UTC regressed");
    if(advance && now>old)root_.Put("retention_meta","utc_watermark",std::to_string(now));
    return now;
}
json ActivityStore::Control(const json& r) {
    TrustedNow(r);
    const auto id=Text(r,"activity_id"),op=Text(r,"op");
    if(op=="activity_get"){
        const auto c=Context(id);
        if(r.contains("activity_num")&&r.at("activity_num")!=c.at("activity_num"))throw std::invalid_argument("SOURCE_CONFLICT: activity_num disagrees with authoritative mapping");
        return WithContext({{"ok",true}},c);
    }
    if(op=="activity_results") {
        const auto c=Context(id);
        if(r.contains("activity_num")&&r.at("activity_num")!=c.at("activity_num"))throw std::invalid_argument("SOURCE_CONFLICT: activity_num disagrees with authoritative mapping");
        for(const auto* k:{"config_version","binding_version","monitoring_version"})
            if(Version(r,k)!=c.at(k))throw std::invalid_argument("VERSION_CONFLICT: subscription activity revision changed");
        State scope(root_,id); const auto after=Version(r,"after_sequence");
        const auto latest=std::stoull(scope.Get("activity_result_meta","sequence").value_or("0"));
        const auto limit=std::min<std::uint64_t>(r.value("limit",100ULL),100);
        auto events=json::array(); const auto now=TrustedNow(r);
        const auto number=std::to_string(after);
        auto cursor=std::string(20-number.size(),'0')+number;
        while(events.size()<limit) {
            bool advanced=false;
            scope.ScanAfter("activity_result",cursor,limit-events.size(),[&](const std::string& key,const std::string& raw){
                cursor=key;advanced=true;const auto e=json::parse(raw);
                if(e.at("retention_deadline_unix_ms").get<std::int64_t>()>=now)events.push_back(e);
            });
            if(!advanced)break;
        }
        return WithContext({{"ok",true},{"events",events},{"latest_sequence",latest}},c);
    }
    if(op=="activity_sync") {
        const auto num=Text(r,"activity_num"),venue=Text(r,"venue_id");
        const auto version=Version(r,"config_version"),expected=Version(r,"expected_config_version");
        if(version<=expected)throw std::invalid_argument("config_version must exceed expected");
        const auto old=root_.Get("activity",id);auto c=old?json::parse(*old):json{{"activity_id",id},{"activity_num",num},{"venue_id",venue},{"config_version",0},{"binding_version",0},{"monitoring_version",0},{"enabled",false}};
        const auto key=json::array({id,version}).dump(),content=Content(r).dump();
        if(auto history=root_.Get("activity_revision",key)) {
            if(*history!=content)throw std::invalid_argument("SOURCE_CONFLICT: activity revision content changed");
            return WithContext({{"ok",true},{"duplicate",true}},c);
        }
        if(c.at("config_version")!=expected)throw std::invalid_argument("VERSION_CONFLICT: activity config CAS");
        if(c.at("activity_num")!=num || c.at("venue_id")!=venue)throw std::invalid_argument("SOURCE_CONFLICT: activity identity mapping is immutable");
        if(auto owner=root_.Get("activity_num",num);owner && *owner!=id)throw std::invalid_argument("SOURCE_CONFLICT: activity_num already mapped");
        c["config_version"]=version;c["configuration"]=r.value("configuration",json::object());
        root_.Put("activity_revision",key,content,id);root_.Put("activity",id,c.dump());root_.Put("activity_num",num,id);
        return WithContext({{"ok",true},{"duplicate",false}},c);
    }
    auto c=Context(id);
    if(Version(r,"config_version")!=c.at("config_version"))throw std::invalid_argument("VERSION_CONFLICT: config version stale");
    if(op=="activity_bindings") {
        const auto v=Version(r,"binding_version"),e=Version(r,"expected_binding_version");
        if(v<=e)throw std::invalid_argument("binding_version must exceed expected");
        const auto key=json::array({id,v}).dump(),content=Content(r).dump();
        if(auto old=root_.Get("activity_binding_revision",key)) {
            if(*old!=content)throw std::invalid_argument("SOURCE_CONFLICT: binding revision content changed");
            return WithContext({{"ok",true},{"duplicate",true}},c);
        }
        if(c.at("binding_version")!=e)throw std::invalid_argument("VERSION_CONFLICT: binding CAS");
        const auto bindings=r.at("bindings");if(!bindings.is_array() || bindings.size()>10000)throw std::invalid_argument("bounded bindings array required");
        std::set<std::string> unique;
        for(const auto& b:bindings){auto kind=Text(b,"kind");if(kind!="CAMERA" && kind!="GATE")throw std::invalid_argument("binding kind CAMERA or GATE required");Text(b,"route_rule_id");const auto k=json::array({Text(b,"source_id"),Text(b,"device_id"),kind}).dump();if(!unique.insert(k).second)throw std::invalid_argument("SOURCE_CONFLICT: ambiguous duplicate device binding");}
        c["bindings"]=bindings;c["binding_version"]=v;
        root_.Put("activity_binding_revision",key,content,id);root_.Put("activity",id,c.dump());
        return WithContext({{"ok",true},{"duplicate",false}},c);
    }
    if(op=="activity_monitoring") {
        const auto v=Version(r,"monitoring_version"),e=Version(r,"expected_monitoring_version");
        if(v<=e || !r.at("enabled").is_boolean())throw std::invalid_argument("monitoring version CAS and explicit enabled required");
        if(Version(r,"binding_version")!=c.at("binding_version"))throw std::invalid_argument("VERSION_CONFLICT: binding stale");
        const auto key=json::array({id,v}).dump(),content=Content(r).dump();
        if(auto old=root_.Get("activity_monitor_revision",key)) {
            if(*old!=content)throw std::invalid_argument("SOURCE_CONFLICT: monitoring revision content changed");
            return WithContext({{"ok",true},{"duplicate",true}},c);
        }
        if(c.at("monitoring_version")!=e)throw std::invalid_argument("VERSION_CONFLICT: monitoring CAS");
        if(r.at("enabled")==true && (!c.contains("bindings") || c.at("bindings").empty()))throw std::invalid_argument("SOURCE_CONFLICT: configure enabled device scope before starting");
        c["enabled"]=r.at("enabled");c["monitoring_version"]=v;
        root_.Put("activity_monitor_revision",key,content,id);root_.Put("activity",id,c.dump());
        return WithContext({{"ok",true},{"duplicate",false}},c);
    }
    throw std::invalid_argument("unsupported activity operation");
}

std::optional<json> ActivityStore::Before(json& r,State& scope) {
    const auto now=TrustedNow(r);const auto c=Context(Text(r,"activity_id"));
    if(!Event(r)) {
        if(r.contains("activity_num")&&r.at("activity_num")!=c.at("activity_num"))throw std::invalid_argument("SOURCE_CONFLICT: activity_num disagrees with authoritative mapping");
        for(const auto* k:{"config_version","binding_version","monitoring_version"})
            if(Version(r,k)!=c.at(k))throw std::invalid_argument(std::string("VERSION_CONFLICT: stale ")+k);
        if(r.at("op")=="association_correct")r["observation_id"]=json::array({Text(r,"source_id"),Text(r,"observation_id")}).dump();
        return {};
    }
    const auto key=EventKey(r);
    const auto content=Content(r);
    const auto fingerprint=Digest(content.dump());
    const bool known=r.contains("_normalization") && r.at("_normalization").value("status","")=="NORMALIZED";
    r["_source_event_key"]=key;r["_content_digest"]=fingerprint;
    const auto old=scope.Get("activity_intake",key);
    if(const auto tombstone=scope.Get("retention_tombstone",Digest(key))) {
        const auto hashes=json::parse(*tombstone).at("fingerprints");
        if(std::find(hashes.begin(),hashes.end(),fingerprint)==hashes.end())throw std::invalid_argument("SOURCE_CONFLICT: expired idempotency key content changed");
        return WithContext({{"ok",true},{"status","EXPIRED"},{"reason_codes",json::array({"RETENTION_EXPIRED"})},{"duplicate",true}},c);
    }
    json rec=old?json::parse(*old):json{{"first_quarantined_unix_ms",now},{"had_untrusted_capture",!known},{"fingerprint",fingerprint},{"fingerprints",json::array({fingerprint})},{"expired",false}};
    if(!rec.contains("had_untrusted_capture"))rec["had_untrusted_capture"]=!rec.value("capture_trusted",known);
    const bool changed=old && rec.at("fingerprint")!=fingerprint;
    if(changed) {
        throw std::invalid_argument("SOURCE_CONFLICT: same activity/source/event key has different content");
    }
    const auto captured=known?r.at("_normalization").at("captured_at_unix_ms").get<std::int64_t>():0;
    const auto cutoff=now-RetentionMs;
    const bool fixed_known=rec.value("capture_trusted",known);
    const auto fixed_capture=fixed_known&&rec.contains("captured_at_unix_ms")?rec.at("captured_at_unix_ms").get<std::int64_t>():captured;
    const bool expired=rec.value("expired",false) || (fixed_known && fixed_capture<cutoff) ||
        ((!fixed_known||rec.at("had_untrusted_capture")==true)&&rec.at("first_quarantined_unix_ms").get<std::int64_t>()<cutoff);
    if(expired) {
        scope.Put("retention_tombstone",Digest(key),json{{"fingerprints",rec.at("fingerprints")}}.dump());
        scope.Erase("activity_intake",key);
        return WithContext({{"ok",true},{"status","EXPIRED"},{"reason_codes",json::array({"RETENTION_EXPIRED"})},{"duplicate",bool(old)}},c);
    }
    if(old && !changed && rec.contains("response")) {
        auto response=rec.at("response");response["duplicate"]=true;return response;
    }
    rec["content"]=content;rec["capture_trusted"]=known;
    if(known){rec["captured_at_unix_ms"]=captured;rec["clock_uncertainty_ms"]=r.at("_normalization").at("clock_uncertainty_ms");}
    scope.Put("activity_intake",key,rec.dump());
    auto deadline=rec.at("first_quarantined_unix_ms").get<std::int64_t>()+RetentionMs;
    if(known)deadline=rec.at("had_untrusted_capture")==true?std::min(deadline,captured+RetentionMs):captured+RetentionMs;
    const auto previous=std::stoll(root_.Get("retention_meta","next_deadline").value_or(std::to_string(std::numeric_limits<std::int64_t>::max())));
    if(deadline<previous)root_.Put("retention_meta","next_deadline",std::to_string(deadline));
    auto reasons=json::array();
    for(const auto* k:{"config_version","binding_version","monitoring_version"})if(Version(r,k)!=c.at(k))reasons.push_back(std::string("STALE_")+k);
    if(r.contains("activity_num") && r.at("activity_num")!=c.at("activity_num"))reasons.push_back("ACTIVITY_MAPPING_CONFLICT");
    if(c.at("enabled")!=true)reasons.push_back("ACTIVITY_STOPPED");
    const bool face=r.at("op")=="monitoring_observe";
    if(face && r.at("observation").contains("activity_id") && r.at("observation").at("activity_id")!=c.at("activity_id"))reasons.push_back("OBSERVATION_ACTIVITY_CONFLICT");
    if(!face && r.contains("binding") && r.at("binding").is_object() && r.at("binding").contains("activity_id") && r.at("binding").at("activity_id")!=c.at("activity_id"))reasons.push_back("BINDING_ACTIVITY_CONFLICT");
    const auto source=face?Text(r.at("observation"),"source_id"):Text(r,"source_id");
    const auto device=face?Text(r.at("observation"),"camera_id"):Text(r,"gate_id");
    bool bound=false;
    if(r.contains("route_rule_id"))for(const auto& b:c.value("bindings",json::array()))if(b.at("source_id")==source && b.at("device_id")==device && b.at("kind")== (face?"CAMERA":"GATE") && b.at("route_rule_id")==r.at("route_rule_id"))bound=true;
    if(!bound)reasons.push_back("DEVICE_ACTIVITY_BINDING_UNRESOLVED");
    if(!reasons.empty()) {
        auto response=WithContext({{"ok",true},{"status","HOLD"},{"time_admitted",known},{"reason_codes",reasons},{"association",nullptr},{"monitoring",nullptr},{"trajectories",json::array()}},c);
        Receipt(r,response,scope);return response;
    }
    // Composite event identifiers isolate sources within the activity. Person
    // identifiers remain unchanged. Frame/track/session keys include source.
    if(face){auto& o=r["observation"];o["observation_id"]=key;for(const auto* f:{"frame_id","track_session_id"})if(o.contains(f) && o.at(f)!="")o[f]=json::array({source,o.at(f)}).dump();}
    else {r["event_id"]=key;if(r.contains("binding") && !r.at("binding").is_null()){auto& b=r["binding"];b["observation_id"]=json::array({Text(b,"source_id"),Text(b,"observation_id")}).dump();}}
    return {};
}
void ActivityStore::Receipt(const json& r,const json& response,State& scope) {
    if(!r.contains("_source_event_key"))return;
    const auto key=r.at("_source_event_key").get<std::string>();auto rec=json::parse(scope.Get("activity_intake",key).value());
    rec["response"]=response;rec["accepted"]=response.value("status","")=="ACCEPTED" || response.value("status","")=="BOUND";
    scope.Put("activity_intake",key,rec.dump());
}
void ActivityStore::Notify(const json& r,const json& response,State& scope) {
    if(!Event(r) || response.value("duplicate",false) || response.value("status","")=="EXPIRED")return;
    auto seq=std::stoull(scope.Get("activity_result_meta","sequence").value_or("0"))+1;
    // Notification is a compact committed algorithm result, independent of
    // backend delivery acknowledgements. Full trajectory history stays queried.
    json e={{"sequence",seq},{"operation",r.at("op")},{"status",response.value("status","")},
        {"retention_deadline_unix_ms",response.at("retention_deadline_unix_ms")}};
    for(const auto* k:{"activity_id","activity_num","venue_id","config_version","binding_version","monitoring_version","route_rule_id","association","monitoring","reason_codes"})
        if(response.contains(k))e[k]=response.at(k);
    // Nested monitoring may contain session history; retention removes results
    // whenever their activity's dependent evidence is pruned.
    std::ostringstream key;key<<std::setfill('0')<<std::setw(20)<<seq;
    scope.Put("activity_result",key.str(),e.dump());
    scope.Put("activity_result_meta","sequence",std::to_string(seq));
}
void ActivityStore::ExternalIds(json& v) {
    if(v.is_array()){for(auto& e:v)ExternalIds(e);return;}
    if(!v.is_object())return;
    const bool direct_connection = v.contains("meaning") && v.at("meaning") == "DIRECT_CAMERA_POINT_CONNECTION";
    for(auto it=v.begin();it!=v.end();++it){
        if((it.key()=="observation_id" || it.key()=="event_id" || it.key()=="frame_id" || it.key()=="track_session_id" ||
            (direct_connection && (it.key()=="from_observation_id" || it.key()=="to_observation_id"))) && it.value().is_string()) {
            try{auto p=json::parse(it.value().get<std::string>());if(p.is_array() && p.size()==2 && p[0].is_string() && p[1].is_string()){
                if(it.key()=="observation_id" || it.key()=="event_id")v["source_id"]=p[0];
                if(it.key()=="from_observation_id")v["from_source_id"]=p[0];
                if(it.key()=="to_observation_id")v["to_source_id"]=p[0];
                it.value()=p[1];}}catch(const json::exception&){}
        } else ExternalIds(it.value());
    }
}
}
