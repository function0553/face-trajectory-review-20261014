#include "face_trajectory/p1_retention.hpp"
#include "face_trajectory/clock_adapter.hpp"
#include <sqlite/sqlite3.h>
#include <filesystem>
#include <chrono>
#include <stdexcept>
#include <iostream>

int main() {
    using namespace face_trajectory;
    try {
        auto directory=std::filesystem::temp_directory_path()/
            ("pending-frame-retention-"+std::to_string(std::chrono::steady_clock::now().time_since_epoch().count()));
        std::filesystem::create_directory(directory);auto file=directory/"p1.sqlite";sqlite3* db=nullptr;
        if(sqlite3_open(file.string().c_str(),&db)!=SQLITE_OK)throw std::runtime_error("fixture open");
        const auto sql="PRAGMA journal_mode=WAL;CREATE TABLE captures(id TEXT PRIMARY KEY, hash TEXT, image BLOB, metadata TEXT, result TEXT, created REAL, status TEXT);"
            "INSERT INTO captures VALUES('pending','hash',x'010203','{}',NULL,0,'PENDING');"
            "INSERT INTO captures VALUES('held','hash',x'040506','{}',NULL,0,'HELD_ROUTE');"
            "CREATE TABLE deliveries(id TEXT PRIMARY KEY,hash TEXT,route TEXT,payload TEXT,status TEXT);"
            "INSERT INTO deliveries VALUES('queued','hash','/v1/monitoring/observe','{}','PENDING');";
        if(sqlite3_exec(db,sql,nullptr,nullptr,nullptr)!=SQLITE_OK)throw std::runtime_error("fixture schema");
        ClockAdapter clocks;auto report=PruneP1({file.string()},1793240000000LL,true,clocks);
        sqlite3_close(db);
        const auto item=report.at("databases").at(0);
        if(item.at("status")!="DRY_RUN" || item.at("expired_rows")!=0 || item.at("retained_window_rows")!=3)
            throw std::runtime_error("pending evidence retention fixture: "+report.dump());
        std::filesystem::remove(file);std::filesystem::remove(directory);
        std::cout<<"Pending original images and deliveries survive unknown/old time qualification\n";
    } catch(const std::exception& e) {std::cerr<<e.what()<<'\n';return 1;}
}
