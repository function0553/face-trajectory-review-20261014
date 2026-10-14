#pragma once
#include "face_trajectory/trajectory.hpp"
#include "face_trajectory/site_context.hpp"
#include <map>
#include <nlohmann/json.hpp>
namespace face_trajectory {
class State;
class PointGraphStore {
public:
    explicit PointGraphStore(State* state = nullptr) : state_(state) {}
    void Put(nlohmann::json packet, const SiteContextStore& sites);
    nlohmann::json Get(const std::string& version) const;
    CameraGraph Graph(const std::string& version) const;
private:
    State* state_;
    std::map<std::string, std::string> packets_;
};
nlohmann::json CameraPointTrace(const TrajectorySnapshot& snapshot,
    const nlohmann::json& packet, const SiteContextStore& sites);
}
