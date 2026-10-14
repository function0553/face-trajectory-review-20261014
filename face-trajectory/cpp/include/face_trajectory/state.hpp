#pragma once
#include <functional>
#include <cstdint>
#include <optional>
#include <map>
#include <string>
#include <utility>
#include <vector>
#include <memory>
struct sqlite3;
namespace face_trajectory {
// Single owner process. Cold observations and immutable snapshots live on disk.
class State {
public:
    explicit State(const std::string& path, std::uint64_t max_bytes = 8ULL*1024*1024*1024);
    // The owner must outlive the view. Views share its connection and atomic
    // transaction but expose only their activity's physical namespaces.
    State(State& root, const std::string& activity_id);
    ~State();
    State(const State&) = delete;
    State& operator=(const State&) = delete;
    void Begin();
    void Commit();
    void Rollback() noexcept;
    bool Dirty() const {return root_ ? root_->dirty_ : dirty_;}
    const std::string& Scope() const {return scope_;}
    std::string PhysicalNamespace(const std::string& ns) const;
    void Put(const std::string& ns, const std::string& key, const std::string& value,
             const std::string& ref = "", const std::string& track = "", const std::string& meta = "");
    void Erase(const std::string& ns, const std::string& key);
    std::optional<std::string> Get(const std::string& ns, const std::string& key) const;
    std::optional<std::pair<std::string, std::string>> First(const std::string& ns) const;
    // Content deduplication with collision comparison; not a security digest.
    std::string Intern(const std::string& value);
    void Scan(const std::string& ns, const std::function<void(const std::string&,const std::string&)>& fn,
              const std::optional<std::string>& ref = {}, bool metadata = false,
              const std::optional<std::string>& track = {}) const;
    struct Row {
        std::string ns, key, value, ref, track, meta;
    };
    struct NamespaceStats {
        std::uint64_t rows = 0;
        // UTF-8 bytes in persisted columns, separate from SQLite page usage.
        std::uint64_t logical_bytes = 0;
    };
    struct StorageStats {
        std::uint64_t page_size = 0, page_count = 0, freelist_count = 0;
        std::uint64_t reusable_bytes = 0;
        std::uint64_t main_bytes = 0, wal_bytes = 0, shm_bytes = 0;
        std::uint64_t rows = 0, logical_bytes = 0;
        std::map<std::string, NamespaceStats> namespaces;
    };
    // Administrative inspection/deletion accepts physical namespaces and is
    // available only on the owner; an activity view cannot escape its scope.
    std::vector<Row> AllRows() const;
    std::vector<std::string> Namespaces() const;
    void ErasePhysical(const std::string& ns, const std::string& key);
    StorageStats Stats() const;
    // Owner-only, outside a transaction. Deleted SQLite cells are cleared at
    // write time; checkpoint removes obsolete WAL pages. Optional compaction
    // also purges preexisting freelist contents and is explicitly requested.
    StorageStats Maintenance(bool compact = false);
    std::unique_ptr<State> PlanningCopy() const;
private:
    struct MemoryOnly {};
    explicit State(MemoryOnly);
    void Sql(const char* sql) const;
    void RequireOwner() const;
    void MarkDirty() {if (root_) root_->dirty_ = true; else dirty_ = true;}
    sqlite3* db_ = nullptr;
    State* root_ = nullptr;
    std::string scope_;
    std::string path_;
    bool transaction_ = false;
    bool dirty_ = false;
    void* lock_ = nullptr;
};
}
