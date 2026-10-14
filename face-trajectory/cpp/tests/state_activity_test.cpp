#include "face_trajectory/activity.hpp"
#include "face_trajectory/monitoring.hpp"
#include "face_trajectory/path_utf8.hpp"
#include <filesystem>
#include <iostream>
#include <chrono>
#include <iomanip>
#include <sstream>
using namespace face_trajectory;
using nlohmann::json;
void Check(bool v,const char* name){if(!v)throw std::runtime_error(name);}
std::string Key(std::uint64_t seq){std::ostringstream s;s<<std::setfill('0')<<std::setw(20)<<seq;return s.str();}
int main(){
    const auto path=std::filesystem::temp_directory_path()/std::filesystem::u8path("trajectory-activity-测试-"+std::to_string(std::chrono::steady_clock::now().time_since_epoch().count())+".sqlite");
    try {
        {
            State root(PathToUtf8(path));State a(root,"A"),b(root,"B");root.Begin();
            for(int i=1;i<=2000;++i)a.Put("rows",Key(i),std::to_string(i),i%2?"odd":"even");
            b.Put("rows",Key(1501),"foreign","odd");
            std::vector<std::string> found;
            a.ScanAfter("rows",Key(1500),2,[&](const auto& key,const auto&){found.push_back(key);});
            Check(found==std::vector<std::string>{Key(1501),Key(1502)},"keyset lower bound and limit");
            Check(a.Count("rows","odd",true)==1000&&b.Count("rows")==1,"scoped indexed count");
            found.clear();a.ScanAfter("rows",Key(1500),2,[&](const auto& key,const auto&){found.push_back(key);},"even");
            Check(found==std::vector<std::string>{Key(1502),Key(1504)},"ref keyset uses scoped FIFO");
            const std::int64_t now=1800000000000;
            json stamp={{"verified",true},{"evidence_id","SYNTHETIC:clock"},{"now_unix_ms",now},{"uncertainty_ms",0},{"valid_until_unix_ms",now+10000}};
            ActivityStore activities(root);
            json c={{"activity_id","A"},{"activity_num","NUM-A"},{"venue_id","V"},{"config_version",1},{"binding_version",1},{"monitoring_version",1},{"enabled",true},
                {"bindings",json::array({{{"source_id","S"},{"device_id","C"},{"kind","CAMERA"},{"route_rule_id","R"}}})}};
            root.Put("activity","A",c.dump());
            json request=c;request.erase("bindings");request.erase("enabled");request["op"]="monitoring_observe";request["route_rule_id"]="R";
            request["_trusted_clock"]=stamp;request["_normalization"]={{"status","NORMALIZED"},{"captured_at_unix_ms",now-1000},{"clock_uncertainty_ms",0}};
            request["observation"]={{"source_id","S"},{"camera_id","C"},{"observation_id","event"}};
            const auto original=request;
            Check(!activities.Before(request,a),"new intake admitted");
            activities.Receipt(request,{{"ok",true},{"status","HOLD"}},a);
            auto retry=original;Check(activities.Before(retry,a).value().at("status")=="HOLD","same HOLD returns original receipt");
            auto changed=original;changed["reconsider"]=true;changed["evidence_id"]="new";bool conflict=false;
            try{activities.Before(changed,a);}catch(const std::invalid_argument& e){conflict=std::string(e.what()).find("SOURCE_CONFLICT")==0;}
            Check(conflict,"reconsider does not rewrite held event");
            auto expired=original;expired["_trusted_clock"]["now_unix_ms"]=now+ActivityStore::RetentionMs;
            expired["_trusted_clock"]["valid_until_unix_ms"]=now+ActivityStore::RetentionMs+10000;
            Check(activities.Before(expired,a).value().at("status")=="EXPIRED","capture age expires retry");
            Check(a.Count("retention_tombstone")==1,"expiry writes irreversible tombstone");
            Check(activities.Before(expired,a).value().at("status")=="EXPIRED","tombstone prevents resurrection");
            // Result pagination must skip expired rows without losing later live ones.
            const auto current=now+ActivityStore::RetentionMs;
            for(int i=1;i<=120;++i)a.Put("activity_result",Key(i),json{{"sequence",i},{"retention_deadline_unix_ms",i<110?current-1:current}}.dump());
            a.Put("activity_result_meta","sequence","120");
            json query={{"op","activity_results"},{"activity_id","A"},{"config_version",1},{"binding_version",1},{"monitoring_version",1},{"after_sequence",100},{"limit",2},{"_trusted_clock",expired.at("_trusted_clock")}};
            const auto result=activities.Control(query);
            Check(result.at("events").size()==2&&result.at("events")[0].at("sequence")==110,"expired cursor rows do not hide live results");
            root.Commit();
        }
        for(const auto* suffix:{"","-wal","-shm",".lock"})std::filesystem::remove(std::filesystem::u8path(PathToUtf8(path)+suffix));
        std::cout<<"Passed state keyset, scope, HOLD idempotency and expiry checks\n";
    }catch(const std::exception& e){std::cerr<<e.what()<<'\n';return 1;}
}
