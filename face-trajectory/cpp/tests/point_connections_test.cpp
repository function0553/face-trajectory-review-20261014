#include "face_trajectory/point_trajectory.hpp"
#include "face_trajectory/activity.hpp"
#include <iostream>
#include <limits>
#include <stdexcept>

using namespace face_trajectory;
using nlohmann::json;

namespace {
int checks = 0;
void Check(bool value, const char* name) {
    if (!value) throw std::runtime_error(name);
    ++checks;
}
json Site() {
    json site = {{"scene_version", "SYNTHETIC:scene"}, {"model_id", "SYNTHETIC:model"},
        {"scene_url", "SYNTHETIC:unused"}, {"tileset_id", "SYNTHETIC:unused"},
        {"calibration_profile", "SYNTHETIC:calibration"}, {"coordinate_frame", "SYNTHETIC:local"},
        {"world_coordinate_frame", "ECEF_WGS84"}, {"unit", "m"},
        {"source_evidence_id", "SYNTHETIC:independent-points"}, {"duplicate_tolerance_m", 0.1},
        {"to_world_matrix", {1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,0,1}},
        {"cameras", json::array()}, {"annotations", json::array()}};
    int i = 0;
    for (const auto* camera : {"A", "B", "C", "MISSING", "CONFLICT"}) {
        site["cameras"].push_back({{"camera_id", camera}, {"vcl_channel_id", nullptr}});
        if (std::string(camera) == "MISSING") continue;
        site["annotations"].push_back({{"annotation_id", std::string("point:") + camera},
            {"source_scene_id", "SYNTHETIC:source"}, {"name", camera}, {"kind_id", "SYNTHETIC:camera"},
            {"role", "CAMERA"}, {"shape", "point"}, {"visible", true}, {"camera_id", camera},
            {"points", {{i++ * 10, 0, i * 5}}}});
    }
    auto conflicting = site["annotations"].back();
    conflicting["annotation_id"] = "point:conflict-other";
    conflicting["points"] = {{100, 0, 100}};
    site["annotations"].push_back(conflicting);
    return site;
}
SightingEvidence Event(const std::string& id, const std::string& camera, std::int64_t t,
    std::int64_t error = 0, TargetLinkStatus link = TargetLinkStatus::Confirmed) {
    return {id, "PERSON", camera, "SYNTHETIC:face", t, error, link,
        TimeSource::UtcCapture, "SYNTHETIC:source", "UTC", ""};
}
json Trace(const std::vector<SightingEvidence>& sightings, SiteContextStore& sites,
    std::int64_t session_gap = 3600000) {
    TrajectoryOptions options;
    options.max_clock_uncertainty_ms = 60000;
    options.session_gap_ms = session_gap;
    TrajectoryStore trajectories;
    const auto revision = trajectories.Rebuild("PERSON", sightings, {"SYNTHETIC:graph", {}},
        options, "SYNTHETIC:independent-events", 0);
    return CameraPointTrace(trajectories.Get("PERSON", revision.version),
        {{"graph_version", "SYNTHETIC:graph"}, {"scene_version", "SYNTHETIC:scene"},
         {"coordinate_frame", "SYNTHETIC:local"}, {"topology_verified", false},
         {"topology_is_complete", false}}, sites);
}
}

int main() {
    try {
        SiteContextStore sites;
        sites.Put(Site());
        Check(Trace({}, sites)["connections"].empty(), "empty trace has no lines");
        Check(Trace({Event("a", "A", 100)}, sites)["connections"].empty(), "one point has no lines");
        const auto trace = Trace({Event("c", "C", 500), Event("b2", "B", 400),
            Event("a1", "A", 100), Event("b1", "B", 300), Event("a2", "A", 200)}, sites);
        const auto& lines = trace["connections"];
        Check(trace["observations"].size() == 5, "all evidence retained");
        Check(lines.size() == 2, "repeated cameras have no self loops");
        Check(lines[0]["from_observation_id"] == "a2" && lines[0]["to_observation_id"] == "b1",
            "connection uses actual boundary observations");
        Check(lines[1]["from_observation_id"] == "b2" && lines[1]["to_observation_id"] == "c",
            "late input sorted deterministically");
        Check(lines[0]["drawing_status"] == "DRAWABLE" && lines[0]["coordinates"].size() == 2,
            "exactly two supplied endpoints, no route points");
        Check(lines[0]["coordinates"][0] == json({0,0,5}) && lines[0]["coordinates"][1] == json({10,0,10}),
            "coordinates from bound markers");
        Check(lines[0]["order_status"] == "ORDERED" && !lines[0]["physical_path_observed"].get<bool>(),
            "ordered line retains display-only semantics");
        Check(Trace({Event("a", "A", 100), Event("b", "B", 200), Event("a2", "A", 300)}, sites)
            ["connections"].size() == 2, "return to previous camera preserved");
        for (const auto* camera : {"MISSING", "CONFLICT", "UNKNOWN"}) {
            const auto unresolved = Trace({Event("a", "A", 100), Event("b", camera, 200)}, sites);
            Check(unresolved["connections"][0]["drawing_status"] == "UNRESOLVED_POSITION" &&
                unresolved["connections"][0]["coordinates"].empty(), "no invented position");
        }
        Check(Trace({Event("a", "A", 100), Event("b", "B", 200, 100)}, sites)
            ["connections"][0]["order_status"] == "UNCERTAIN_CLOCK_ORDER", "touching intervals ambiguous");
        const auto spanning = Trace({Event("a", "A", 100, 500), Event("b", "B", 200), Event("c", "C", 300)}, sites);
        Check(spanning["connections"][1]["order_status"] == "UNCERTAIN_CLOCK_ORDER",
            "third spanning interval does not manufacture unique adjacency");
        const auto split = Trace({Event("a", "A", 100), Event("b", "B", 10000)}, sites, 100);
        Check(split["connections"][0]["drawing_status"] == "SESSION_BREAK" &&
            split["connections"][0]["coordinates"].empty(), "separate sessions not bridged");
        auto foreign = Event("foreign", "B", 150); foreign.target_id = "OTHER_PERSON";
        bool rejected_foreign = false;
        try { Trace({Event("a", "A", 100), foreign}, sites); }
        catch (const std::invalid_argument&) { rejected_foreign = true; }
        Check(rejected_foreign, "foreign target rejected by authoritative rebuild");
        const auto admitted = Trace({Event("a", "A", 100),
            Event("held", "C", 175, 0, TargetLinkStatus::Provisional), Event("b", "B", 200)}, sites);
        Check(admitted["observations"].size() == 2 && admitted["connections"].size() == 1,
            "foreign and provisional observations not connected");
        const auto max = std::numeric_limits<std::int64_t>::max();
        const auto extreme = Trace({Event("a", "A", max-1), Event("b", "B", max)}, sites);
        Check(extreme["connections"][0]["order_status"] == "ORDERED", "int64 millisecond precision retained");
        const auto saturated = Trace({Event("a", "A", max-1, 500), Event("b", "B", max, 500)}, sites);
        Check(saturated["connections"][0]["order_status"] == "UNCERTAIN_CLOCK_ORDER", "interval bounds do not overflow");
        Check(Trace({Event("b", "B", 200), Event("a", "A", 100)}, sites) ==
            Trace({Event("a", "A", 100), Event("b", "B", 200)}, sites), "input permutation stable");
        const auto scoped = json::array({"S1", "same"}).dump();
        json exported = {{"meaning", "DIRECT_CAMERA_POINT_CONNECTION"}, {"from_observation_id", scoped}};
        ActivityStore::ExternalIds(exported);
        Check(exported["from_observation_id"] == "same" && exported["from_source_id"] == "S1",
            "connection exports original ID with source namespace");
        json legacy = {{"meaning", "OBSERVATION_SEQUENCE_ONLY"}, {"from_observation_id", scoped}};
        ActivityStore::ExternalIds(legacy);
        Check(legacy["from_observation_id"] == scoped, "legacy observation order identifiers remain compatible");
        std::cout << "Passed " << checks << " point connection checks\n";
    } catch (const std::exception& e) {
        std::cerr << e.what() << '\n';
        return 1;
    }
}
