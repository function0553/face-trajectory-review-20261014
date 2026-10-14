#pragma once

#include <map>
#include <string>
#include <nlohmann/json.hpp>

namespace face_trajectory {
class State;

// Published model annotations give camera and landmark context, not person
// localization, camera FOV, identity evidence, or a verified navigation graph.
class SiteContextStore {
public:
    explicit SiteContextStore(State* state = nullptr) : state_(state) {}
    void Put(nlohmann::json site);
    nlohmann::json Get(const std::string& version) const;
    nlohmann::json CameraContext(const std::string& version, const std::string& camera_ref,
                                 const std::string& id_namespace, double radius_m) const;
private:
    State* state_;
    std::map<std::string, std::string> sites_;
};
}
