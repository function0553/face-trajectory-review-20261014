#include "face_trajectory/p1_retention.hpp"
#include "face_trajectory/clock_adapter.hpp"
#include <iostream>
#include <string>
int main() {
    try {
        std::string line;std::getline(std::cin,line);auto request=nlohmann::json::parse(line);
        face_trajectory::ClockAdapter clocks;
        for(const auto& profile:request.at("profiles"))clocks.Put(profile);
        const auto expired=request.value("expired_references",nlohmann::json::array());
        const auto lookup=[&](const std::string& activity,const std::string& source,const std::string& event){
            const auto key=nlohmann::json::array({activity,source,event});
            return std::find(expired.begin(),expired.end(),key)!=expired.end();
        };
        auto result=face_trajectory::PruneP1(request.at("paths").get<std::vector<std::string>>(),
            request.at("now_unix_ms").get<std::int64_t>(),request.at("dry_run").get<bool>(),clocks,lookup);
        std::cout<<result.dump()<<std::endl;return 0;
    } catch(const std::exception& error){std::cerr<<error.what()<<std::endl;return 1;}
}
