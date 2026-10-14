#pragma once
#include "face_trajectory/trajectory.hpp"
#include <nlohmann/json.hpp>
namespace face_trajectory {
class GeometryStore {
public:
    explicit GeometryStore(State* state=nullptr):state_(state){}
    void Put(nlohmann::json model);
    nlohmann::json Render(const TrajectorySnapshot& snapshot,const std::string& version) const;
private:
    State* state_;
    std::map<std::string,std::string> models_;
};
}
