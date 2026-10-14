#include "face_trajectory/state.hpp"
#include <sqlite/sqlite3.h>
#include <nlohmann/json.hpp>
#include <filesystem>
#include <stdexcept>
#ifdef _WIN32
#define NOMINMAX
#include <windows.h>
#else
#include <sys/file.h>
#include <unistd.h>
#include <fcntl.h>
#endif
namespace face_trajectory {
namespace {
struct Statement {
    sqlite3* db;
    sqlite3_stmt* value = nullptr;
    Statement(sqlite3* d,const char* sql):db(d) {
        if (sqlite3_prepare_v2(db,sql,-1,&value,nullptr)!=SQLITE_OK) throw std::runtime_error(sqlite3_errmsg(db));
    }
    ~Statement(){sqlite3_finalize(value);}
    void Bind(int index,const std::string& text) {
        if (sqlite3_bind_text(value,index,text.data(),static_cast<int>(text.size()),SQLITE_TRANSIENT)!=SQLITE_OK)
            throw std::runtime_error(sqlite3_errmsg(db));
    }
    int Step() {
        int result=sqlite3_step(value);
        if(result==SQLITE_FULL)throw std::runtime_error("CAPACITY: SQLite state size or disk capacity reached");
        if (result!=SQLITE_ROW && result!=SQLITE_DONE) throw std::runtime_error(sqlite3_errmsg(db));
        return result;
    }
    std::string Text(int col) {
        const auto p=sqlite3_column_text(value,col);
        return p?std::string(reinterpret_cast<const char*>(p),sqlite3_column_bytes(value,col)):std::string{};
    }
};
}
State::State(const std::string& path,std::uint64_t max_bytes) {
    if (path.empty()) throw std::invalid_argument("state file path is required");
    auto resolved=std::filesystem::absolute(std::filesystem::u8path(path));
    path_=resolved.u8string();
    std::filesystem::create_directories(resolved.parent_path());
    auto lockpath=resolved;lockpath+=".lock";
#ifdef _WIN32
    auto handle=CreateFileW(lockpath.c_str(),GENERIC_READ|GENERIC_WRITE,0,nullptr,OPEN_ALWAYS,FILE_ATTRIBUTE_NORMAL,nullptr);
    if (handle==INVALID_HANDLE_VALUE) throw std::runtime_error("state file already owned or lock unavailable");
    lock_=handle;
#else
    int fd=open(lockpath.c_str(),O_CREAT|O_RDWR,0600);
    if (fd<0 || flock(fd,LOCK_EX|LOCK_NB)!=0) {if(fd>=0)close(fd);throw std::runtime_error("state file already owned or lock unavailable");}
    lock_=reinterpret_cast<void*>(static_cast<intptr_t>(fd+1));
#endif
    try {
        if (sqlite3_open_v2(resolved.u8string().c_str(),&db_,SQLITE_OPEN_READWRITE|SQLITE_OPEN_CREATE,nullptr)!=SQLITE_OK)
            throw std::runtime_error("cannot open SQLite state");
        sqlite3_busy_timeout(db_,5000);
        Sql("PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA cache_size=-8192; PRAGMA secure_delete=ON;");
        {Statement version(db_,"PRAGMA user_version");version.Step();
        if (sqlite3_column_int(version.value,0)>1) throw std::runtime_error("unsupported newer state schema");}
        std::uint64_t page_size;
        {Statement size(db_,"PRAGMA page_size");size.Step();page_size=sqlite3_column_int64(size.value,0);}
        if(max_bytes<page_size*16)throw std::invalid_argument("state size limit too small");
        {Statement count(db_,"PRAGMA page_count");count.Step();if(static_cast<std::uint64_t>(sqlite3_column_int64(count.value,0))>max_bytes/page_size)
            throw std::runtime_error("existing state exceeds configured size limit");}
        Sql(("PRAGMA max_page_count="+std::to_string(max_bytes/page_size)).c_str());
        Sql("CREATE TABLE IF NOT EXISTS state(ns TEXT NOT NULL,key TEXT NOT NULL,value TEXT NOT NULL,ref TEXT NOT NULL,track TEXT NOT NULL,meta TEXT NOT NULL,PRIMARY KEY(ns,key)) WITHOUT ROWID;"
            "CREATE INDEX IF NOT EXISTS state_ref ON state(ns,ref); CREATE INDEX IF NOT EXISTS state_track ON state(ns,track); PRAGMA user_version=1;");
    } catch (...) {
        if(db_){sqlite3_close(db_);db_=nullptr;}
#ifdef _WIN32
        CloseHandle(lock_);
#else
        close(static_cast<int>(reinterpret_cast<intptr_t>(lock_))-1);
#endif
        lock_=nullptr;throw;
    }
}
State::State(State& root,const std::string& activity_id) {
    root.RequireOwner();
    if(activity_id.empty())throw std::invalid_argument("activity scope is required");
    // dump validates UTF-8 now, before a view is usable for persistence.
    (void)nlohmann::json::array({"activity",activity_id,""}).dump();
    root_=&root;
    db_=root.db_;
    scope_=activity_id;
}
State::State(MemoryOnly) {
    if(sqlite3_open_v2(":memory:",&db_,SQLITE_OPEN_READWRITE|SQLITE_OPEN_CREATE,nullptr)!=SQLITE_OK)throw std::runtime_error("cannot create retention planning state");
    Sql("CREATE TABLE state(ns TEXT NOT NULL,key TEXT NOT NULL,value TEXT NOT NULL,ref TEXT NOT NULL,track TEXT NOT NULL,meta TEXT NOT NULL,PRIMARY KEY(ns,key)) WITHOUT ROWID; CREATE INDEX state_ref ON state(ns,ref); CREATE INDEX state_track ON state(ns,track);");
}
std::unique_ptr<State> State::PlanningCopy() const {
    RequireOwner();auto copy=std::unique_ptr<State>(new State(MemoryOnly{}));copy->Begin();
    for(const auto& row:AllRows())copy->Put(row.ns,row.key,row.value,row.ref,row.track,row.meta);
    copy->Commit();return copy;
}
State::~State() {
    if(root_)return;
    Rollback();if(db_)sqlite3_close(db_);
#ifdef _WIN32
    if(lock_)CloseHandle(lock_);
#else
    if(lock_)close(static_cast<int>(reinterpret_cast<intptr_t>(lock_))-1);
#endif
}
void State::RequireOwner() const {if(root_)throw std::logic_error("state operation requires the owning connection");}
std::string State::PhysicalNamespace(const std::string& ns) const {
    return root_ ? nlohmann::json::array({"activity",scope_,ns}).dump() : ns;
}
void State::Sql(const char* sql) const {const auto result=sqlite3_exec(db_,sql,nullptr,nullptr,nullptr);if(result==SQLITE_FULL)throw std::runtime_error("CAPACITY: SQLite state size or disk capacity reached");if(result!=SQLITE_OK)throw std::runtime_error(sqlite3_errmsg(db_));}
void State::Begin(){RequireOwner();if(transaction_)throw std::logic_error("nested state transaction");Sql("BEGIN IMMEDIATE");transaction_=true;dirty_=false;}
void State::Commit(){RequireOwner();Sql("COMMIT");transaction_=false;dirty_=false;}
void State::Rollback() noexcept {if(root_)return;if(transaction_){sqlite3_exec(db_,"ROLLBACK",nullptr,nullptr,nullptr);transaction_=false;}dirty_=false;}
void State::Put(const std::string& ns,const std::string& key,const std::string& value,const std::string& ref,const std::string& track,const std::string& meta) {
    Statement s(db_,"INSERT INTO state VALUES(?,?,?,?,?,?) ON CONFLICT(ns,key) DO UPDATE SET value=excluded.value,ref=excluded.ref,track=excluded.track,meta=excluded.meta");
    s.Bind(1,PhysicalNamespace(ns));s.Bind(2,key);s.Bind(3,value);s.Bind(4,ref);s.Bind(5,track);s.Bind(6,meta);s.Step();MarkDirty();
}
void State::Erase(const std::string& ns,const std::string& key){Statement s(db_,"DELETE FROM state WHERE ns=? AND key=?");s.Bind(1,PhysicalNamespace(ns));s.Bind(2,key);s.Step();MarkDirty();}
std::optional<std::string> State::Get(const std::string& ns,const std::string& key) const {
    Statement s(db_,"SELECT value FROM state WHERE ns=? AND key=?");s.Bind(1,PhysicalNamespace(ns));s.Bind(2,key);if(s.Step()==SQLITE_ROW)return s.Text(0);return {};
}
std::string State::Intern(const std::string& value) {
    std::uint64_t hash=14695981039346656037ULL;
    for(unsigned char c:value){hash^=c;hash*=1099511628211ULL;}
    const auto base=std::to_string(hash);
    for(std::size_t collision=0;;++collision){
        const auto key=base+":"+std::to_string(collision);auto old=Get("object",key);
        if(old){if(*old==value)return key;}
        else {Put("object",key,value);return key;}
    }
}
std::optional<std::pair<std::string, std::string>> State::First(const std::string& ns) const {
    Statement s(db_, "SELECT key,value FROM state WHERE ns=? ORDER BY key LIMIT 1");
    s.Bind(1, PhysicalNamespace(ns));
    if (s.Step() == SQLITE_ROW) return std::make_pair(s.Text(0), s.Text(1));
    return {};
}
void State::Scan(const std::string& ns,const std::function<void(const std::string&,const std::string&)>& fn,const std::optional<std::string>& ref,bool metadata,const std::optional<std::string>& track) const {
    std::string sql=std::string("SELECT key,")+(metadata?"meta":"value")+" FROM state";
    // WITHOUT ROWID + large JSON values can cause an unanalysed planner to
    // choose a namespace scan. These are explicit indexed access methods.
    if(ref)sql+=" INDEXED BY state_ref";
    else if(track)sql+=" INDEXED BY state_track";
    sql+=" WHERE ns=?";
    if(ref)sql+=" AND ref=?";if(track)sql+=" AND track=?";
    Statement s(db_,sql.c_str());s.Bind(1,PhysicalNamespace(ns));int i=2;if(ref)s.Bind(i++,*ref);if(track)s.Bind(i++,*track);
    while(s.Step()==SQLITE_ROW)fn(s.Text(0),s.Text(1));
}
std::vector<State::Row> State::AllRows() const {
    RequireOwner();
    std::vector<Row> rows;
    Statement s(db_,"SELECT ns,key,value,ref,track,meta FROM state ORDER BY ns,key");
    while(s.Step()==SQLITE_ROW)rows.push_back({s.Text(0),s.Text(1),s.Text(2),s.Text(3),s.Text(4),s.Text(5)});
    return rows;
}
std::vector<std::string> State::Namespaces() const {
    RequireOwner();
    std::vector<std::string> namespaces;
    Statement s(db_,"SELECT DISTINCT ns FROM state ORDER BY ns");
    while(s.Step()==SQLITE_ROW)namespaces.push_back(s.Text(0));
    return namespaces;
}
void State::ErasePhysical(const std::string& ns,const std::string& key) {
    RequireOwner();
    Erase(ns,key);
}
State::StorageStats State::Stats() const {
    RequireOwner();
    StorageStats stats;
    auto pragma=[&](const char* sql) {
        Statement s(db_,sql);s.Step();
        return static_cast<std::uint64_t>(sqlite3_column_int64(s.value,0));
    };
    stats.page_size=pragma("PRAGMA page_size");
    stats.page_count=pragma("PRAGMA page_count");
    stats.freelist_count=pragma("PRAGMA freelist_count");
    stats.reusable_bytes=stats.page_size*stats.freelist_count;
    auto file_bytes=[](const std::string& path) {
        if(path.empty()||path=="-wal"||path=="-shm")return std::uint64_t{0};
        const auto p=std::filesystem::u8path(path);
        // Sidecars are legitimately absent before/after checkpointing. Other
        // failures must be reported rather than claiming a zero-byte file.
        if(!std::filesystem::exists(p))return std::uint64_t{0};
        return static_cast<std::uint64_t>(std::filesystem::file_size(p));
    };
    stats.main_bytes=file_bytes(path_);
    stats.wal_bytes=file_bytes(path_+"-wal");
    stats.shm_bytes=file_bytes(path_+"-shm");
    Statement s(db_,"SELECT ns,count(*),sum(length(CAST(ns AS BLOB))+length(CAST(key AS BLOB))+length(CAST(value AS BLOB))+length(CAST(ref AS BLOB))+length(CAST(track AS BLOB))+length(CAST(meta AS BLOB))) FROM state GROUP BY ns ORDER BY ns");
    while(s.Step()==SQLITE_ROW) {
        NamespaceStats entry;
        entry.rows=static_cast<std::uint64_t>(sqlite3_column_int64(s.value,1));
        entry.logical_bytes=static_cast<std::uint64_t>(sqlite3_column_int64(s.value,2));
        stats.rows+=entry.rows;
        stats.logical_bytes+=entry.logical_bytes;
        stats.namespaces.emplace(s.Text(0),entry);
    }
    return stats;
}
State::StorageStats State::Maintenance(bool compact) {
    RequireOwner();
    if(transaction_)throw std::logic_error("state maintenance requires a committed transaction");
    auto checkpoint=[&]() {
        int log_frames=0, checkpointed_frames=0;
        const auto result=sqlite3_wal_checkpoint_v2(db_,nullptr,SQLITE_CHECKPOINT_TRUNCATE,
                                                  &log_frames,&checkpointed_frames);
        if(result!=SQLITE_OK)throw std::runtime_error(std::string("state WAL checkpoint failed: ")+sqlite3_errmsg(db_));
    };
    checkpoint();
    if(compact) {
        Sql("VACUUM");
        checkpoint();
    }
    return Stats();
}
}
