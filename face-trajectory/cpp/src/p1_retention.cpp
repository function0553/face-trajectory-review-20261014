#include "face_trajectory/p1_retention.hpp"
#include "face_trajectory/activity.hpp"
#include "face_trajectory/clock_adapter.hpp"
#include <sqlite/sqlite3.h>
#include <algorithm>
#include <filesystem>
#include <set>
#include <stdexcept>
#include <map>
#include <memory>
#include <fstream>
#include <chrono>
#include <thread>
#ifdef _WIN32
#define NOMINMAX
#include <windows.h>
#else
#include <sys/file.h>
#include <unistd.h>
#include <fcntl.h>
#endif

namespace face_trajectory {
using nlohmann::json;
namespace {
struct Statement {
    sqlite3* db; sqlite3_stmt* stmt=nullptr;
    Statement(sqlite3* db,const std::string& sql):db(db) {
        if(sqlite3_prepare_v2(db,sql.c_str(),-1,&stmt,nullptr)!=SQLITE_OK)
            throw std::runtime_error(sqlite3_errmsg(db));
    }
    ~Statement(){sqlite3_finalize(stmt);}
    void Bind(int index,const std::string& value) {
        if(sqlite3_bind_text(stmt,index,value.data(),static_cast<int>(value.size()),SQLITE_TRANSIENT)!=SQLITE_OK)
            throw std::runtime_error(sqlite3_errmsg(db));
    }
    int Step(){auto result=sqlite3_step(stmt);if(result!=SQLITE_ROW&&result!=SQLITE_DONE)throw std::runtime_error(sqlite3_errmsg(db));return result;}
    std::string Text(int index) const {
        auto value=sqlite3_column_text(stmt,index);
        return value?std::string(reinterpret_cast<const char*>(value),sqlite3_column_bytes(stmt,index)):std::string{};
    }
};
void Sql(sqlite3* db,const std::string& sql) {
    if(sqlite3_exec(db,sql.c_str(),nullptr,nullptr,nullptr)!=SQLITE_OK)throw std::runtime_error(sqlite3_errmsg(db));
}
struct Database {
    sqlite3* db=nullptr;
#ifdef _WIN32
    std::vector<HANDLE> reader_guards;
    void Guard(const std::filesystem::path& path,bool freeze) {
        auto handle=CreateFileW(path.c_str(),GENERIC_READ,FILE_SHARE_READ|(freeze?0:FILE_SHARE_WRITE),nullptr,OPEN_EXISTING,FILE_ATTRIBUTE_NORMAL,nullptr);
        if(handle==INVALID_HANDLE_VALUE)throw std::runtime_error("DRY_RUN_READER_BUSY: cannot establish non-mutating SQLite reader");
        reader_guards.push_back(handle);
    }
#endif
    static std::string Uri(const std::string& path,const char* options) {
        const char* digits="0123456789ABCDEF";std::string result="file:";
        for(const auto character:path) {const auto c=static_cast<unsigned char>(character);
            if(c=='%'||c=='?'||c=='#'){result+='%';result+=digits[c>>4];result+=digits[c&15];}
            else result+=c=='\\'?'/':character;
        }
        return result+options;
    }
    Database(const std::string& path,bool dry_run) {
        auto filename=path;int flags=SQLITE_OPEN_READWRITE;
        try {
        if(dry_run) {
            const auto main=std::filesystem::u8path(path);auto wal=main;wal+="-wal";auto shm=main;shm+="-shm";
            const auto live=std::filesystem::exists(wal)&&std::filesystem::exists(shm);
#ifdef _WIN32
            // Existing live auxiliaries stay present while SQLite uses a read-
            // only WAL index. Offline immutable mode is safe only after OS
            // handles have denied concurrent writers to main and existing WAL.
            Guard(main,!live);
            if(std::filesystem::exists(wal))Guard(wal,!live);
            if(live)Guard(shm,false);
#else
            if(!live)throw std::runtime_error("DRY_RUN_OFFLINE_FREEZE_UNAVAILABLE: offline immutable inventory requires Windows deny-write handles");
#endif
            filename=Uri(path,live?"?mode=ro&readonly_shm=1":"?mode=ro&immutable=1");
            flags=SQLITE_OPEN_READONLY|SQLITE_OPEN_URI;
        }
        if(sqlite3_open_v2(filename.c_str(),&db,flags,nullptr)!=SQLITE_OK) {
            if(db)sqlite3_close(db);db=nullptr;throw std::runtime_error("cannot open configured P1 SQLite file");
        }
        sqlite3_busy_timeout(db,5000);
        } catch(...) {
#ifdef _WIN32
            for(auto handle:reader_guards)CloseHandle(handle);reader_guards.clear();
#endif
            throw;
        }
    }
    ~Database(){if(db)sqlite3_close(db);
#ifdef _WIN32
        for(auto handle:reader_guards)CloseHandle(handle);
#endif
    }
};
struct Lease {
#ifdef _WIN32
    HANDLE handle=INVALID_HANDLE_VALUE;
    OVERLAPPED overlapped{};
#else
    int descriptor=-1;
#endif
    bool held=false;
    explicit Lease(const std::filesystem::path& path,const char* suffix=".consumer.lock") {
        auto target=path;target+=suffix;
#ifdef _WIN32
        handle=CreateFileW(target.c_str(),GENERIC_READ|GENERIC_WRITE,FILE_SHARE_READ|FILE_SHARE_WRITE,nullptr,OPEN_ALWAYS,FILE_ATTRIBUTE_NORMAL,nullptr);
        if(handle!=INVALID_HANDLE_VALUE)
            held=LockFileEx(handle,LOCKFILE_EXCLUSIVE_LOCK|LOCKFILE_FAIL_IMMEDIATELY,0,1,0,&overlapped)!=0;
#else
        descriptor=open(target.c_str(),O_CREAT|O_RDWR,0600);
        if(descriptor>=0)held=flock(descriptor,LOCK_EX|LOCK_NB)==0;
#endif
    }
    ~Lease(){
#ifdef _WIN32
        if(held)UnlockFileEx(handle,0,1,0,&overlapped);
        if(handle!=INVALID_HANDLE_VALUE)CloseHandle(handle);
#else
        if(descriptor>=0)close(descriptor);
#endif
    }
};
struct PauseRequest {
    std::filesystem::path path;
    bool keep=false;
    bool active=true;
    explicit PauseRequest(const std::filesystem::path& database):path(database) {
        path+=".maintenance.request";
        // A previous committed sweep must not lose its pause barrier if this
        // recovery attempt fails before reaching physical cleanup.
        keep=std::filesystem::exists(path);
        Set("REQUESTED");
    }
#ifdef _WIN32
    template<class Operation>
    static void RetryMarkerOperation(Operation operation,const std::string& context,bool allow_missing=false) {
        const auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(1);
        for(;;) {
            if(operation())return;
            const auto code=GetLastError();
            if(allow_missing&&code==ERROR_FILE_NOT_FOUND)return;
            // CPython's Windows stat/Path.exists briefly opens the marker
            // without delete sharing. Retry the atomic operation itself;
            // deleting the destination first would release P1 prematurely.
            const bool transient=code==ERROR_ACCESS_DENIED||code==ERROR_SHARING_VIOLATION||code==ERROR_LOCK_VIOLATION;
            if(!transient||std::chrono::steady_clock::now()>=deadline)
                throw std::runtime_error(context+"; win32_error="+std::to_string(code)+"; retry_limit_ms=1000");
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
        }
    }
#endif
    void Set(const char* status) {
        auto temporary=path;temporary+=".tmp";
        {std::ofstream stream(temporary,std::ios::binary|std::ios::trunc);
         if(!stream)throw std::runtime_error("cannot publish P1 maintenance request");
         stream<<json({{"protocol",1},{"status",status}}).dump();stream.flush();
         if(!stream)throw std::runtime_error("cannot persist P1 maintenance request");}
#ifdef _WIN32
        RetryMarkerOperation([&]{return MoveFileExW(temporary.c_str(),path.c_str(),MOVEFILE_REPLACE_EXISTING|MOVEFILE_WRITE_THROUGH)!=0;},
                             std::string("cannot replace P1 maintenance request; phase=")+status);
#else
        std::filesystem::rename(temporary,path);
#endif
    }
    void Remove() {
#ifdef _WIN32
        RetryMarkerOperation([&]{return DeleteFileW(path.c_str())!=0;},"cannot clear P1 maintenance request; phase=RESUME",true);
#else
        std::filesystem::remove(path);
#endif
        active=false;
    }
    void Complete(){Remove();}
    ~PauseRequest(){if(active&&!keep){try{Remove();}catch(...) {/* An incomplete run may stay paused for a later recovery. */}}}
};
std::uint64_t Scalar(sqlite3* db,const std::string& sql){Statement s(db,sql);s.Step();return static_cast<std::uint64_t>(sqlite3_column_int64(s.stmt,0));}
std::uint64_t Bytes(const std::filesystem::path& path){std::error_code error;auto result=std::filesystem::file_size(path,error);return error?0:result;}
json Stats(sqlite3* db,const std::filesystem::path& path) {
    const auto page=Scalar(db,"PRAGMA page_size"),count=Scalar(db,"PRAGMA page_count"),free=Scalar(db,"PRAGMA freelist_count");
    auto wal=path;wal+="-wal";auto shm=path;shm+="-shm";
    return {{"page_size",page},{"page_count",count},{"freelist_count",free},{"reusable_bytes",page*free},
        {"main_bytes",Bytes(path)},{"wal_bytes",Bytes(wal)},{"shm_bytes",Bytes(shm)}};
}
std::set<std::string> Columns(sqlite3* db,const std::string& table) {
    std::set<std::string> result;Statement s(db,"PRAGMA table_info("+table+")");
    while(s.Step()==SQLITE_ROW)result.insert(s.Text(1));return result;
}
json Parse(const std::string& text){auto result=json::parse(text,nullptr,false);return result.is_discarded()?json():result;}
struct Row {std::string table,key,hash,route;json data;bool enrolled=false,known=false,expired=true;};
void Qualify(Row& row,const ClockAdapter& clocks,std::int64_t now) {
    if(row.route=="/v1/monitoring/persons") {
        row.enrolled=true;row.expired=false;return;
    }
    if(!row.data.is_object())return;
    const auto& observation=row.data.contains("observation")&&row.data.at("observation").is_object()?row.data.at("observation"):row.data;
    auto record=row.data.value("time_record",json());
    if(record.is_null())record=observation.value("time_record",json());
    if(!record.is_object())return;
    if(observation.contains("camera_id")) {
        if(record.contains("camera_id")&&record.at("camera_id")!=observation.at("camera_id"))return;
        record["camera_id"]=observation.at("camera_id");
    }
    if(record.contains("source_id")&&observation.contains("source_id")&&record.at("source_id")!=observation.at("source_id"))return;
    if(record.contains("time_policy")&&record.at("time_policy")!="CAMERA_CAPTURE_60S")return;
    record["time_policy"]="CAMERA_CAPTURE_60S";
    try {
        auto normalized=clocks.Normalize(record);
        if(normalized.at("status")!="NORMALIZED")return;
        const auto capture=normalized.at("captured_at_unix_ms").get<std::int64_t>();
        // A future assertion must not grant a longer local lifetime.
        if(capture>now&&capture-now>ClockAdapter::CameraCaptureErrorLimitMs)return;
        row.known=true;row.expired=capture<now-ActivityStore::RetentionMs;
    } catch(const std::exception&) { /* Unknown is deleted without a new window. */ }
}
std::vector<Row> Inventory(sqlite3* db,const ClockAdapter& clocks,std::int64_t now) {
    std::vector<Row> rows;
    for(const auto* name:{"captures","deliveries","frames","outbox"}) {
        auto columns=Columns(db,name);if(!columns.count("id"))continue;
        const auto hash=columns.count("hash")?"hash":columns.count("fingerprint")?"fingerprint":columns.count("payload_hash")?"payload_hash":"''";
        const auto body=columns.count("metadata")?"metadata":columns.count("payload")?"payload":"NULL";
        const auto route=columns.count("route")?"route":"''";
        Statement s(db,std::string("SELECT id,")+hash+","+body+","+route+" FROM "+name+" ORDER BY id");
        while(s.Step()==SQLITE_ROW) {
            Row row{name,s.Text(0),s.Text(1),s.Text(3),Parse(s.Text(2))};
            if(row.hash.size()!=64 || !std::all_of(row.hash.begin(),row.hash.end(),[](unsigned char c){return (c>='0'&&c<='9')||(c>='a'&&c<='f');}))
                row.hash=ActivityStore::Digest(row.hash.empty()?s.Text(2):row.hash);
            try {Qualify(row,clocks,now);} catch(const std::exception&) {}
            rows.push_back(std::move(row));
        }
    }
    return rows;
}
std::string LogicalKey(const std::string& activity,const std::string& source,const std::string& event) {
    return "observe:"+json::array({activity,source,event}).dump();
}
std::string ContextString(const json& value,const char* key,const std::string& fallback) {
    return value.contains(key)&&value.at(key).is_string()?value.at(key).get<std::string>():fallback;
}
bool HasExpired(sqlite3* db,const std::set<std::string>& keys,const ExpiredReference& external,
                const std::string& activity,const std::string& source,const std::string& event) {
    if(activity.empty()||source.empty()||event.empty())return false;
    const auto identity=json::array({activity,source,event}).dump();
    const auto has_tombstones=!Columns(db,"retention_tombstone").empty();
    for(const auto* kind:{"observe:","gate:"}) {
        const auto key=std::string(kind)+identity;if(keys.count(key))return true;
        if(has_tombstones) {
            Statement tombstone(db,"SELECT 1 FROM retention_tombstone WHERE key_hash=? AND table_name IN ('deliveries','outbox') LIMIT 1");
            tombstone.Bind(1,ActivityStore::Digest(key));if(tombstone.Step()==SQLITE_ROW)return true;
        }
    }
    return external&&external(activity,source,event);
}
bool ExternalExpired(const std::string& path,const std::string& activity,const std::string& source,
                     const std::string& event,const ClockAdapter& clocks,std::int64_t now) {
    if(!std::filesystem::is_regular_file(std::filesystem::u8path(path)))return false;
    Database reader(path,true);auto db=reader.db;Sql(db,"BEGIN");
    if(HasExpired(db,{}, {},activity,source,event))return true;
    const auto key=LogicalKey(activity,source,event),gate=std::string("gate:")+json::array({activity,source,event}).dump();
    for(const auto* table:{"deliveries","outbox"}) {
        auto columns=Columns(db,table);if(!columns.count("payload"))continue;
        const auto route=columns.count("route")?"route":"''";
        Statement find(db,std::string("SELECT payload,")+route+" FROM "+table+" WHERE id IN (?,?)");find.Bind(1,key);find.Bind(2,gate);
        while(find.Step()==SQLITE_ROW) {
            Row row{table,key,{},find.Text(1),Parse(find.Text(0))};Qualify(row,clocks,now);
            if(row.expired)return true;
        }
    }
    return false;
}
bool ScrubReferences(json& value,const std::string& activity,const std::string& source,
                     const std::function<bool(const std::string&,const std::string&,const std::string&)>& expired) {
    bool changed=false;
    if(value.is_array()) {for(auto& item:value)changed=ScrubReferences(item,activity,source,expired)||changed;return changed;}
    if(!value.is_object())return false;
    const auto own_activity=ContextString(value,"activity_id",activity),own_source=ContextString(value,"source_id",source);
    if(value.contains("binding")&&value.at("binding").is_object()) {
        const auto& binding=value.at("binding");
        if(binding.contains("observation_id")&&binding.at("observation_id").is_string()&&
           expired(ContextString(binding,"activity_id",own_activity),ContextString(binding,"source_id",own_source),binding.at("observation_id").get<std::string>())) {
            value["binding"]=nullptr;changed=true;
        }
    }
    for(auto it=value.begin();it!=value.end();++it) {
        if(it.key()=="reentry_evidence_id"&&it.value().is_string()) {
            auto id=it.value().get<std::string>();auto key=Parse(id);
            const bool scoped=key.is_array()&&key.size()==2&&key[0].is_string()&&key[1].is_string();
            if(expired(own_activity,scoped?key[0].get<std::string>():own_source,scoped?key[1].get<std::string>():id)) {it.value()="";changed=true;}
        } else changed=ScrubReferences(it.value(),own_activity,own_source,expired)||changed;
    }
    return changed;
}
json One(const std::string& configured,std::int64_t now,bool dry_run,const ClockAdapter& clocks,const ExpiredReference& external) {
    const auto path=std::filesystem::weakly_canonical(std::filesystem::absolute(std::filesystem::u8path(configured)));
    json result={{"path",path.u8string()},{"dry_run",dry_run},{"status","ERROR"}};
    if(!std::filesystem::is_regular_file(path)){result["reason"]="CONFIGURED_P1_FILE_MISSING";return result;}
    // Read-only inventory never creates a lease file or a tombstone table.
    std::unique_ptr<Lease> owner,lease;
    std::unique_ptr<PauseRequest> pause;
    if(!dry_run) {
        owner=std::make_unique<Lease>(path,".maintenance.lock");
        if(!owner->held){result["status"]="BLOCKED_OTHER_MAINTENANCE";return result;}
        pause=std::make_unique<PauseRequest>(path);
        const auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(20);
        do {
            lease=std::make_unique<Lease>(path);
            if(lease->held)break;
            lease.reset();std::this_thread::sleep_for(std::chrono::milliseconds(50));
        } while(std::chrono::steady_clock::now()<deadline);
        if(!lease||!lease->held){result["status"]="BLOCKED_P1_QUIESCENCE";result["pause_wait_limit_ms"]=20000;return result;}
        result["pause_handshake"]="QUIESCENT";
        pause->Set("CLEANING");
    }
    Database database(path.u8string(),dry_run);auto db=database.db;
    if(!dry_run)Sql(db,"PRAGMA secure_delete=ON");
    Sql(db,dry_run?"BEGIN":"BEGIN IMMEDIATE");
    try {
        result["before"]=Stats(db,path);
        auto rows=Inventory(db,clocks,now);json tables=json::object();
        std::set<std::string> expired_keys;
        for(const auto& row:rows)if(row.expired&&(row.table=="deliveries"||row.table=="outbox"))expired_keys.insert(row.key);
        std::uint64_t expired=0,known=0,unknown=0,enrolled=0,retained=0;
        for(const auto& row:rows) {
            auto& stats=tables[row.table];if(stats.is_null())stats={{"before_rows",0},{"retained_rows",0},{"expired_known_rows",0},{"expired_unknown_rows",0},{"excluded_enrollment_rows",0}};
            stats["before_rows"]=stats.at("before_rows").get<std::uint64_t>()+1;
            const auto field=row.enrolled?"excluded_enrollment_rows":!row.expired?"retained_rows":row.known?"expired_known_rows":"expired_unknown_rows";
            stats[field]=stats.at(field).get<std::uint64_t>()+1;
            if(row.enrolled)++enrolled;else if(!row.expired)++retained;else {++expired;if(row.known)++known;else ++unknown;}
        }
        result["tables"]=tables;result["expired_rows"]=expired;result["expired_known_rows"]=known;
        result["expired_unknown_rows"]=unknown;result["retained_window_rows"]=retained;result["excluded_enrollment_rows"]=enrolled;
        result["unknown_policy"]="IMMEDIATE_EXPIRY_WITH_HASH_TOMBSTONE_NO_NEW_WINDOW";
        std::uint64_t dependencies=0;
        for(auto& row:rows)if(!row.expired&&!row.enrolled&&row.data.is_object()) {
            auto expired_reference=[&](const std::string& activity,const std::string& source,const std::string& event){return HasExpired(db,expired_keys,external,activity,source,event);};
            if(!ScrubReferences(row.data,ContextString(row.data,"activity_id",{}),ContextString(row.data,"source_id",{}),expired_reference))continue;
            ++dependencies;
            if(!dry_run) {
                const auto column=row.table=="captures"||row.table=="frames"?"metadata":"payload";
                // Preserve the original content hash for identical retry/conflict.
                // A changed pending body cannot be resent under the original ID.
                const auto status=Columns(db,row.table).count("status")?",status=CASE WHEN status='PENDING' THEN 'QUARANTINED' ELSE status END":"";
                Statement update(db,"UPDATE "+row.table+" SET "+column+"=?"+status+" WHERE id=?");
                update.Bind(1,row.data.dump());update.Bind(2,row.key);update.Step();
            }
        }
        result["scrubbed_expired_dependency_rows"]=dependencies;
        if(!dry_run) {
            Sql(db,"CREATE TABLE IF NOT EXISTS retention_tombstone(table_name TEXT NOT NULL,key_hash TEXT NOT NULL,content_hash TEXT NOT NULL,PRIMARY KEY(table_name,key_hash)) WITHOUT ROWID");
            for(const auto& row:rows)if(row.expired) {
                Statement tombstone(db,"INSERT INTO retention_tombstone(table_name,key_hash,content_hash) VALUES(?,?,?) ON CONFLICT(table_name,key_hash) DO NOTHING");
                tombstone.Bind(1,row.table);tombstone.Bind(2,ActivityStore::Digest(row.key));tombstone.Bind(3,row.hash);tombstone.Step();
                Statement erase(db,"DELETE FROM "+row.table+" WHERE id=?");erase.Bind(1,row.key);erase.Step();
            }
            // Receipts are historical transaction snapshots. Canonical current
            // state is queried from P2; retaining an old receipt can retain points
            // from before this delivery's own capture. Enrollment is excluded.
            std::uint64_t scrubbed=0;
            for(const auto* table:{"deliveries","outbox"}) {
                auto columns=Columns(db,table);if(!columns.count("receipt"))continue;
                const auto predicate=columns.count("route")?" AND route<>'/v1/monitoring/persons'":"";
                Sql(db,std::string("UPDATE ")+table+" SET receipt=NULL WHERE receipt IS NOT NULL"+predicate);
                scrubbed+=static_cast<std::uint64_t>(sqlite3_changes(db));
            }
            // A processed capture report may refer to a deleted observation or
            // contain a legacy embedded result. Images and metadata in the valid
            // capture window remain intact; current results come from P2.
            auto columns=Columns(db,"captures");
            if(columns.count("result")) {
                const auto predicate=columns.count("status")?"status='PROCESSED' AND ":"";
                Sql(db,std::string("UPDATE captures SET result=NULL WHERE ")+predicate+"result IS NOT NULL");
                scrubbed+=static_cast<std::uint64_t>(sqlite3_changes(db));
            }
            result["scrubbed_historical_receipts"]=scrubbed;
            result["tombstones"]=Scalar(db,"SELECT count(*) FROM retention_tombstone");
        }
        // Once committing starts, any later error must leave P1 paused until
        // another run verifies both physical cleanup and marker removal.
        if(!dry_run)pause->keep=true;
        Sql(db,"COMMIT");
        if(!dry_run) {
            result["after_logical"]=Stats(db,path);
            // A crash after commit must remain paused until another maintenance
            // run completes the physical sweep; stale pages may still exist.
            pause->Set("PHYSICAL_CLEANUP_PENDING");
            // secure_delete only zeros pages freed by this connection. Earlier
            // image/result updates may have freed pages before it was enabled.
            // VACUUM rebuilds only reachable rows and removes those old bytes.
            bool compacted=true;
            try {Sql(db,"VACUUM");result["vacuum_status"]="COMPLETE";}
            catch(const std::exception& error){compacted=false;result["vacuum_status"]="PENDING_READERS_OR_WRITER";result["vacuum_error"]=error.what();}
            int log=0,checkpointed=0;const auto checkpoint=sqlite3_wal_checkpoint_v2(db,nullptr,SQLITE_CHECKPOINT_TRUNCATE,&log,&checkpointed);
            result["checkpoint_status"]=checkpoint==SQLITE_OK?"COMPLETE":"PENDING_READERS";
            result["checkpoint_log_frames"]=log;result["checkpointed_frames"]=checkpointed;
            result["status"]=checkpoint==SQLITE_OK&&compacted?"CLEANED":"PHYSICAL_CLEANUP_PENDING";
        } else result["status"]="DRY_RUN";
        result["after"]=Stats(db,path);
        if(!dry_run&&result.at("status")=="CLEANED") {
            // Do not report COMPLETE while an ignored DeleteFile failure can
            // still keep the Python runtime paused. Both leases remain held.
            pause->Complete();result["maintenance_request_status"]="CLEARED";
        }
        return result;
    } catch(...) {sqlite3_exec(db,"ROLLBACK",nullptr,nullptr,nullptr);throw;}
}
}
json PruneP1(const std::vector<std::string>& paths,std::int64_t now,bool dry_run,const ClockAdapter& clocks,const ExpiredReference& expired_reference) {
    if(now<ActivityStore::RetentionMs)throw std::invalid_argument("trusted UTC is required for P1 retention");
    json databases=json::array();std::set<std::string> seen;bool complete=true;
    std::vector<std::string> resolved_paths;
    for(const auto& path:paths)resolved_paths.push_back(std::filesystem::weakly_canonical(std::filesystem::absolute(std::filesystem::u8path(path))).u8string());
    for(const auto& resolved:resolved_paths) {
        if(!seen.insert(resolved).second)continue;
        const auto lookup=[&](const std::string& activity,const std::string& source,const std::string& event){
            if(expired_reference&&expired_reference(activity,source,event))return true;
            for(const auto& other:resolved_paths)if(other!=resolved&&ExternalExpired(other,activity,source,event,clocks,now))return true;
            return false;
        };
        try {auto result=One(resolved,now,dry_run,clocks,lookup);complete=complete&&(result.at("status")=="CLEANED"||result.at("status")=="DRY_RUN");databases.push_back(std::move(result));}
        catch(const std::exception& error){complete=false;databases.push_back({{"path",resolved},{"status","ERROR"},{"reason",error.what()}});}
    }
    return {{"status",complete?"COMPLETE":"INCOMPLETE"},{"dry_run",dry_run},{"databases",databases}};
}
}
