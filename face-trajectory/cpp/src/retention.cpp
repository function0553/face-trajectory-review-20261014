#include "face_trajectory/retention.hpp"
#include "face_trajectory/activity.hpp"
#include "face_trajectory/association.hpp"
#include "face_trajectory/monitoring.hpp"
#include "face_trajectory/trajectory.hpp"
#include <algorithm>
#include <set>
#include <map>
#include <memory>
#include <limits>
#include <functional>
namespace face_trajectory {
using nlohmann::json;
namespace {
json Parse(const std::string& raw){return json::parse(raw,nullptr,false);}
std::pair<std::string,std::string> Scope(const std::string& ns){auto v=Parse(ns);if(v.is_array()&&v.size()==3&&v[0]=="activity")return {v[1],v[2]};return {"",ns};}
bool Qualified(const json& v){
    if(!v.is_object()||!v.contains("time_source"))return false;
    const auto& source=v.at("time_source");
    const bool utc=source.is_number_integer()?source.get<int>()==static_cast<int>(TimeSource::UtcCapture):source.is_string()&&source=="UTC_CAPTURE";
    return utc&&v.value("clock_domain","")=="UTC"&&v.value("clock_uncertainty_ms",-1LL)>=0&&v.value("clock_uncertainty_ms",-1LL)<=60000;
}
std::int64_t Deadline(const json& rec){
    auto d=rec.value("first_quarantined_unix_ms",0LL)+ActivityStore::RetentionMs;
    if(rec.value("capture_trusted",false)){const auto capture=rec.at("captured_at_unix_ms").get<std::int64_t>()+ActivityStore::RetentionMs;d=rec.value("had_untrusted_capture",false)?std::min<std::int64_t>(d,capture):capture;}
    return d;
}
void Refs(const json& v,const std::function<void(const std::string&)>& mark){
    if(v.is_array()){for(const auto& x:v)Refs(x,mark);return;}
    if(!v.is_object())return;
    for(auto it=v.begin();it!=v.end();++it){
        if((it.key()=="record_ref"||it.key()=="payload_ref"||it.key()=="graph_ref"||it.key()=="$avl"||it.key()=="value_ref"||((it.key()=="l"||it.key()=="r")&&v.contains("h")&&v.contains("k")))&&it.value().is_string()){auto id=it.value().get<std::string>();if(!id.empty())mark(id);}
        else if(it.key()=="evidence_refs"&&it.value().is_array()){for(const auto& id:it.value())mark(id.get<std::string>());}
        else Refs(it.value(),mark);
    }
}
void ScrubReentry(json& v,const std::set<std::string>& expired){
    if(v.is_array()){for(auto& x:v)ScrubReentry(x,expired);return;}if(!v.is_object())return;
    for(auto it=v.begin();it!=v.end();++it){if(it.key()=="reentry_evidence_id"&&it.value().is_string()&&expired.count(it.value().get<std::string>()))it.value()="";else ScrubReentry(it.value(),expired);}
}
json Compact(const json& response){
    json r=json::object();for(const auto* k:{"ok","status","sequence","operation","activity_id","activity_num","venue_id","config_version","binding_version","monitoring_version","route_rule_id","retention_deadline_unix_ms","reason_codes","time_admitted"})if(response.contains(k))r[k]=response.at(k);
    if(response.contains("association")&&response.at("association").is_object()){
        r["association"]=json::object();for(const auto* k:{"target_id","decision","reason","version"})if(response.at("association").contains(k))r["association"][k]=response.at("association").at(k);
    }
    r["detail_query_required"]=true;return r;
}
}
json StorageReport(const State::StorageStats& s){
    json ns=json::object();for(const auto& [k,v]:s.namespaces)ns[k]={{"rows",v.rows},{"logical_bytes",v.logical_bytes}};
    return {{"rows",s.rows},{"logical_bytes",s.logical_bytes},{"page_size",s.page_size},{"page_count",s.page_count},{"freelist_count",s.freelist_count},{"reusable_bytes",s.reusable_bytes},{"main_bytes",s.main_bytes},{"wal_bytes",s.wal_bytes},{"shm_bytes",s.shm_bytes},{"namespaces",ns}};
}
json Retain(State& root,std::int64_t now,bool dry_run){
    if(dry_run){
        auto copy=root.PlanningCopy();copy->Begin();auto plan=Retain(*copy,now,false);copy->Rollback();
        plan["dry_run"]=true;plan["before"]=StorageReport(root.Stats());plan["after_logical"]=plan.at("before");
        plan["count_semantics"]="EXACT_IN_MEMORY_PLAN_NO_SOURCE_MUTATION";
        for(auto& activity:plan["activities"]){activity["shared_object_count_is_pre_rewrite"]=false;activity["snapshots"]["dry_run"]=true;}
        return plan;
    }
    const auto cutoff=now-ActivityStore::RetentionMs;const auto initial=root.AllRows();
    std::map<std::string,std::vector<State::Row>> scopes;for(const auto& row:initial){const auto [id,ns]=Scope(row.ns);if(ns!="push_outbox")scopes[id].push_back(row);}
    json report={{"ok",true},{"dry_run",dry_run},{"trusted_now_unix_ms",now},{"cutoff_unix_ms",cutoff},{"before",StorageReport(root.Stats())},{"activities",json::array()}};
    std::set<std::string> changed_activities;std::uint64_t total_deleted=0;auto next=std::numeric_limits<std::int64_t>::max();
    for(const auto& [id,rows]:scopes){
        std::unique_ptr<State> view;State* state=&root;if(!id.empty()){view=std::make_unique<State>(root,id);state=view.get();}
        std::set<std::pair<std::string,std::string>> deletions;std::set<std::string> expired,dead_templates;std::map<std::string,json> intake;
        auto remove=[&](const std::string& ns,const std::string& key){deletions.emplace(ns,key);};
        std::uint64_t before=0,after=0;
        for(const auto& row:rows){const auto ns=Scope(row.ns).second;const auto v=Parse(row.value);
            if(ns=="activity_intake"){
                intake[row.key]=v;const auto deadline=Deadline(v);
                if(v.value("expired",false)||deadline<now){expired.insert(row.key);remove(ns,row.key);if(!dry_run){auto hashes=v.value("fingerprints",json::array());if(hashes.empty()&&v.contains("fingerprint"))hashes.push_back(v.at("fingerprint"));state->Put("retention_tombstone",ActivityStore::Digest(row.key),json{{"fingerprints",hashes}}.dump());}}
                else next=std::min<std::int64_t>(next,deadline);
            }
        }
        for(const auto& row:rows){const auto ns=Scope(row.ns).second;const auto v=Parse(row.value);
            if(ns!="observation")continue;++before;
            const auto& o=v.at("observation");bool old=expired.count(row.key)||!Qualified(o)||o.at("captured_at_unix_ms").get<std::int64_t>()<cutoff;
            if(old){expired.insert(row.key);remove(ns,row.key);remove("association_frame",row.key);}
            else {++after;next=std::min(next,o.at("captured_at_unix_ms").get<std::int64_t>()+ActivityStore::RetentionMs);}
        }
        for(const auto& row:rows){const auto ns=Scope(row.ns).second;auto v=Parse(row.value);
            if(ns!="monitoring_observation"&&ns!="monitoring_observation_revision")continue;
            const auto event=ns=="monitoring_observation"?row.key:row.ref;
            if(intake.count(event)||expired.count(event))continue;
            if(v.contains("record_ref"))v=Parse(state->Get("object",v.at("record_ref")).value());
            const auto response=v.value("response",json::object());const auto norm=response.value("normalization",json::object());
            const bool known=norm.value("status","")=="NORMALIZED";
            const auto deadline=known?norm.at("captured_at_unix_ms").get<std::int64_t>()+ActivityStore::RetentionMs:0;
            // Missing trusted first quarantine provenance never obtains a fresh
            // migration window. Qualified legacy receipt uses capture expiry.
            if(deadline<now){expired.insert(event);remove(ns,row.key);}else next=std::min<std::int64_t>(next,deadline);
        }
        const bool changed=!expired.empty();
        Gallery gallery(state);AssociationStore associations(gallery,state);MonitoringStore monitoring(state);TrajectoryStore trajectories(state);
        if(changed&&!dry_run)monitoring.CheckpointRuntime(cutoff,associations);
        auto snapshots=trajectories.Prune(cutoff,dry_run,std::vector<std::string>(expired.begin(),expired.end()));
        const auto earliest=snapshots.at("earliest_retained_capture_unix_ms").get<std::int64_t>();
        if(earliest<std::numeric_limits<std::int64_t>::max()-ActivityStore::RetentionMs)next=std::min(next,earliest+ActivityStore::RetentionMs);
        for(const auto& row:rows){const auto ns=Scope(row.ns).second;auto v=Parse(row.value);
            if(ns=="owned_template"){
                auto live=json::array();for(const auto& t:v){if(expired.count(t.at("observation_id").get<std::string>())){dead_templates.insert(t.at("template_id"));remove("template",t.at("template_id"));}else live.push_back(t);}
                if(live.empty())remove(ns,row.key);else if(!dry_run&&live!=v)state->Put(ns,row.key,live.dump());
            }
            if(ns=="monitoring_observation"&&expired.count(row.key))remove(ns,row.key);
            if(ns=="monitoring_observation_revision"&&expired.count(row.ref))remove(ns,row.key);
            if(ns=="monitoring_gate"||ns=="monitoring_gate_revision"){
                const auto event=ns=="monitoring_gate"?row.key:row.ref;
                const auto& norm=v.at("normalization");const bool known=norm.value("status","")=="NORMALIZED";
                const auto deadline=known?norm.at("captured_at_unix_ms").get<std::int64_t>()+ActivityStore::RetentionMs:(intake.count(event)?Deadline(intake.at(event)):0);
                const bool lost_face=!v.at("binding").is_null()&&expired.count(v.at("binding").at("observation_id").get<std::string>());
                if(expired.count(event)||deadline<now)remove(ns,row.key);
                else {
                    next=std::min<std::int64_t>(next,deadline);
                    if(lost_face&&!dry_run){
                        // Freeze current confirmed attribution, including any
                        // correction made while the face evidence was live.
                        // A client supplied identity is never accepted here.
                        const auto observation=v.at("binding").at("observation_id").get<std::string>();
                        const auto managed=state->Get("monitoring_observation",observation);
                        const auto result=associations.Result(observation);
                        v.erase("bound_target_id");
                        if(managed&&result&&result->sighting&&result->target_id){
                            auto record=Parse(*managed);
                            if(record.contains("record_ref"))record=Parse(state->Get("object",record.at("record_ref")).value());
                            if(record.at("response").value("time_admitted",false))v["bound_target_id"]=*result->target_id;
                        }
                        v["binding_evidence_hash"]=ActivityStore::Digest(v.at("binding").at("evidence_id").get<std::string>());
                        v["binding"]=nullptr;v["binding_history_pruned"]=true;
                        state->Put(ns,row.key,v.dump(),row.ref,"",row.meta);
                    }
                }
            }
            if(changed&&(ns=="association_track_targets"||ns=="association_target_summary"||ns=="monitoring_projection"))remove(ns,row.key);
            if(ns=="activity_result"){
                if(v.value("retention_deadline_unix_ms",std::int64_t{0})<now)remove(ns,row.key);
                else {next=std::min(next,v.at("retention_deadline_unix_ms").get<std::int64_t>());if(changed&&!dry_run)state->Put(ns,row.key,Compact(v).dump());}
            }
            if(changed&&ns=="activity_intake"&&!expired.count(row.key)&&v.contains("response")&&!dry_run){
                v["response"]=Compact(v.at("response"));ScrubReentry(v,expired);
                if(v.at("content").contains("binding")&&v.at("content").at("binding").is_object()){
                    const auto& b=v.at("content").at("binding");const auto key=json::array({b.value("source_id",std::string{}),b.value("observation_id",std::string{})}).dump();
                    if(expired.count(key)||expired.count(b.value("observation_id",std::string{})))v["content"]["binding"]=nullptr;
                }
                state->Put(ns,row.key,v.dump());
            }
            if(changed&&(ns=="monitoring_observation"||ns=="monitoring_observation_revision")&&!deletions.count({ns,row.key})&&!dry_run&&v.contains("record_ref")){
                auto record=json::parse(state->Get("object",v.at("record_ref")).value());record["response"]=Compact(record.at("response"));
                if(record.contains("payload_ref")){auto payload=json::parse(state->Get("object",record.at("payload_ref")).value());ScrubReentry(payload,expired);record["payload_ref"]=state->Intern(payload.dump());}
                v["record_ref"]=state->Intern(record.dump());state->Put(ns,row.key,v.dump(),row.ref,row.track,row.meta);
            }
        }
        if(changed&&!dry_run)for(const auto& row:rows)if(Scope(row.ns).second=="observation"&&!expired.count(row.key)){
            auto v=Parse(row.value);ScrubReentry(v,expired);auto& candidates=v["result"]["candidates"];
            candidates.erase(std::remove_if(candidates.begin(),candidates.end(),[&](const auto& candidate){return dead_templates.count(candidate.at("hit").at("template_id").template get<std::string>())!=0;}),candidates.end());
            auto meta=Parse(row.meta);ScrubReentry(meta,expired);state->Put("observation",row.key,v.dump(),row.ref,row.track,meta.dump());
        }
        // Automatic templates not linked from any live observation are not
        // registration. Legacy orphan templates must not remain an identity source.
        std::set<std::string> owned;
        for(const auto& row:rows)if(Scope(row.ns).second=="owned_template")for(const auto& t:Parse(row.value))if(!expired.count(t.at("observation_id")))owned.insert(t.at("template_id"));
        for(const auto& row:rows)if(Scope(row.ns).second=="template"&&row.key.rfind("assoc-template:",0)==0&&!owned.count(row.key))remove("template",row.key);
        if(!dry_run)for(const auto& [ns,key]:deletions)state->Erase(ns,key);
        // Mark all surviving roots together. Never sweep shared references by
        // one snapshot or receipt in isolation.
        std::map<std::string,std::string> objects;std::vector<json> roots;
        if(!dry_run){for(const auto& row:root.AllRows()){auto [scope,ns]=Scope(row.ns);if(scope!=id)continue;if(ns=="object")objects[row.key]=row.value;else if(ns=="trajectory"||ns=="monitoring_observation"||ns=="monitoring_observation_revision")roots.push_back(Parse(row.value));}}
        else for(const auto& row:rows){auto ns=Scope(row.ns).second;if(ns=="object")objects[row.key]=row.value;else if(!deletions.count({ns,row.key})&&(ns=="trajectory"||ns=="monitoring_observation"||ns=="monitoring_observation_revision"))roots.push_back(Parse(row.value));}
        std::set<std::string> live;std::function<void(const std::string&)> mark=[&](const auto& key){if(!live.insert(key).second)return;auto found=objects.find(key);if(found==objects.end())throw std::runtime_error("retention shared root missing object");Refs(Parse(found->second),mark);};
        for(const auto& value:roots)Refs(value,mark);
        std::size_t collected=0;for(const auto& [key,value]:objects)if(!live.count(key)){++collected;if(!dry_run)state->Erase("object",key);}
        if(changed||snapshots.at("versions_rewritten").get<std::uint64_t>()>0)changed_activities.insert(id);
        std::uint64_t existing_deletions=0;for(const auto& row:rows)if(deletions.count({Scope(row.ns).second,row.key}))++existing_deletions;
        total_deleted+=existing_deletions+collected;
        report["activities"].push_back({{"activity_id",id.empty()?json(nullptr):json(id)},{"observations_before",before},{"observations_after",after},{"expired_event_count",expired.size()},{"direct_existing_rows_deleted",existing_deletions},{"rows_deleted",existing_deletions},{"shared_objects_collected",collected},{"shared_object_count_is_pre_rewrite",dry_run},{"snapshots",snapshots}});
    }
    std::size_t pending=0,push_removed=0;
    for(const auto& row:initial)if(row.ns=="push_outbox"){
        const auto v=Parse(row.value);const auto deadline=v.value("retention_deadline_unix_ms",std::int64_t{0});
        if(deadline<now||!v.contains("activity_id")){++push_removed;if(!dry_run)root.Erase("push_outbox",row.key);}else{++pending;next=std::min<std::int64_t>(next,deadline);if(!dry_run&&changed_activities.count(v.value("activity_id",std::string{}))){auto compact=v;compact["result"]=Compact(v.at("result"));root.Put("push_outbox",row.key,compact.dump(),v.at("activity_id"),v.at("event_id"));}}
    }
    if(!dry_run){root.Put("push_meta","pending",std::to_string(pending));root.Put("retention_meta","next_deadline",std::to_string(next));root.Put("retention_meta","last_run_unix_ms",std::to_string(now));}
    std::set<std::pair<std::string,std::string>> surviving;for(const auto& row:root.AllRows())surviving.emplace(row.ns,row.key);
    std::uint64_t removed_keys=0;for(const auto& row:initial)if(!surviving.count({row.ns,row.key}))++removed_keys;
    report["count_semantics"]="EXACT_INITIAL_ROW_KEYS_REMOVED; REWRITTEN_VALUES_REPORTED_SEPARATELY";
    report["push_rows_removed"]=push_removed;report["rows_deleted"]=removed_keys;report["erase_operations"]=total_deleted+push_removed;report["next_deadline_unix_ms"]=next;
    if(!dry_run)root.Put("retention_meta","last_report",report.dump());
    report["after_logical"]=StorageReport(root.Stats());return report;
}
}
