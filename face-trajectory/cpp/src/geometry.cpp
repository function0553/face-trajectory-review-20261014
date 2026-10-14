#include "face_trajectory/geometry.hpp"
#include "face_trajectory/state.hpp"
#include <cmath>
#include <set>
#include <stdexcept>
namespace face_trajectory {
using nlohmann::json;
namespace {
double Distance(const json& a,const json& b){double s=0;for(int i=0;i<3;++i){double d=a.at(i).get<double>()-b.at(i).get<double>();s+=d*d;}return std::sqrt(s);}
void Required(const json& j,const char* key){if(j.at(key).get<std::string>().empty())throw std::invalid_argument(std::string(key)+" is required");}
}
void GeometryStore::Put(json model) {
    for(auto key:{"geometry_version","graph_version","coordinate_frame","unit"})Required(model,key);
    const auto unit=model.at("unit").get<std::string>();
    if(unit!="m" && unit!="cm" && unit!="mm")throw std::invalid_argument("geometry unit must be m, cm or mm");
    auto tolerance=model.at("join_tolerance").get<double>();
    if(!std::isfinite(tolerance) || tolerance<0)throw std::invalid_argument("explicit finite join tolerance required");
    auto& edges=model.at("edges");if(!edges.is_array())throw std::invalid_argument("geometry edges must be an array");
    std::set<std::string> ids;
    for(const auto& edge:edges){
        for(auto key:{"edge_id","from_camera","to_camera"})Required(edge,key);
        if(!ids.insert(edge.at("edge_id").get<std::string>()).second)throw std::invalid_argument("duplicate geometry edge ID");
        const auto& points=edge.at("points");
        if(!points.is_array() || points.size()<2)throw std::invalid_argument("polyline needs at least two 3D points");
        for(const auto& p:points){if(!p.is_array() || p.size()!=3)throw std::invalid_argument("point must be xyz");
            for(const auto& v:p)if(!v.is_number() || !std::isfinite(v.get<double>()))throw std::invalid_argument("finite coordinates required");}
        double length=0;for(std::size_t i=1;i<points.size();++i)length+=Distance(points[i-1],points[i]);
        if(!std::isfinite(length) || length<=0)throw std::invalid_argument("polyline needs positive finite length");
    }
    std::sort(edges.begin(),edges.end(),[](const auto& a,const auto& b){return a.at("edge_id")<b.at("edge_id");});
    const auto version=model.at("geometry_version").get<std::string>();const auto encoded=model.dump();
    auto old=state_?state_->Get("geometry",version):std::optional<std::string>{};
    if(!state_){auto it=models_.find(version);if(it!=models_.end())old=it->second;}
    if(old && *old!=encoded)throw std::invalid_argument("geometry changed without a new geometry_version");
    if(state_)state_->Put("geometry",version,encoded);else models_[version]=encoded;
}
json GeometryStore::Render(const TrajectorySnapshot& snapshot,const std::string& version) const {
    auto text=state_?state_->Get("geometry",version):std::optional<std::string>{};
    if(!state_){auto it=models_.find(version);if(it!=models_.end())text=it->second;}
    if(!text)throw std::out_of_range("geometry version not found");
    const auto model=json::parse(*text);
    if(model.at("graph_version")!=snapshot.graph.version)throw std::invalid_argument("geometry graph version does not match trajectory");
    std::map<std::string,json> edges;for(const auto& e:model.at("edges"))edges[e.at("edge_id").get<std::string>()]=e;
    json result={{"target_id",snapshot.target_id},{"version",snapshot.version},{"geometry_version",version},
        {"graph_version",snapshot.graph.version},{"coordinate_frame",model.at("coordinate_frame")},{"unit",model.at("unit")},{"segments",json::array()}};
    for(const auto& segment:snapshot.segments){
        json entry={{"segment_id",segment.segment_id},{"search_complete",segment.search_complete},{"unique_path",segment.unique_path},{"routes",json::array()}};
        auto route=[&](const PathCandidate& path,std::size_t index){
            json r={{"route_index",index},{"status","AVAILABLE_INFERRED"},{"conditional_on_route",true},{"polyline",json::array()},{"edges",json::array()},{"missing_edge_ids",json::array()}};
            for(std::size_t i=0;i<path.edge_ids.size();++i){
                const auto found=edges.find(path.edge_ids[i]);
                if(found==edges.end()){r["missing_edge_ids"].push_back(path.edge_ids[i]);r["status"]="MISSING_GEOMETRY";continue;}
                const auto& e=found->second;
                if(e.at("from_camera")!=path.nodes.at(i).camera_id || e.at("to_camera")!=path.nodes.at(i+1).camera_id)
                    throw std::invalid_argument("geometry direction/endpoints disagree with route");
                auto& line=r["polyline"];
                const auto& points=e.at("points");
                if(!line.empty() && Distance(line.back(),points.front())>model.at("join_tolerance").get<double>())r["status"]="DISCONTINUOUS_GEOMETRY";
                for(const auto& p:points)line.push_back(p);
                r["edges"].push_back({{"edge_id",path.edge_ids[i]},{"points",points},
                    {"earliest_unix_ms",path.edge_time_bounds.at(i).earliest_unix_ms},
                    {"latest_unix_ms",path.edge_time_bounds.at(i).latest_unix_ms}});
            }
            if(r["status"]!="AVAILABLE_INFERRED")r["polyline"]=json::array();
            entry["routes"].push_back(std::move(r));
        };
        if(segment.primary)route(*segment.primary,0);
        for(std::size_t i=0;i<segment.backups.size();++i)route(segment.backups[i],i+1);
        entry["status"]=segment.primary?"INFERRED":"UNKNOWN";
        result["segments"].push_back(std::move(entry));
    }
    return result;
}
}
