#include "face_trajectory/time_constraints.hpp"
#include <algorithm>
#include <limits>
#include <queue>
#include <set>
#include <tuple>
namespace face_trajectory {
ChainFeasibility CheckCaptureChain(std::vector<CaptureInterval> captures,
    const CameraGraph& graph, const std::vector<std::pair<std::string,std::string>>& overlaps,
    bool complete, std::size_t budget) {
    if (captures.empty()) return ChainFeasibility::Feasible;
    std::sort(captures.begin(),captures.end(),[](const auto& a,const auto& b) {
        return std::tie(a.lower,a.upper,a.camera_id)<std::tie(b.lower,b.upper,b.camera_id);
    });
    if (std::all_of(captures.begin(),captures.end(),[&](const auto& c){return c.camera_id==captures[0].camera_id;}))
        return ChainFeasibility::Feasible;
    std::map<std::string,std::vector<CameraEdge>> adjacency;
    std::set<std::string> cameras;
    for (const auto& e:graph.edges) { adjacency[e.from_camera].push_back(e); cameras.insert(e.from_camera); cameras.insert(e.to_camera); }
    for (const auto& pair:overlaps) {
        cameras.insert(pair.first); cameras.insert(pair.second);
        adjacency[pair.first].push_back({pair.first,pair.second,0,0,0,{},{},""});
        adjacency[pair.second].push_back({pair.second,pair.first,0,0,0,{},{},""});
    }
    for (const auto& c:captures) if (!cameras.count(c.camera_id)) return ChainFeasibility::Unknown;
    bool exhausted=false;
    const auto arrive=[&](const std::string& from,const std::string& to,std::int64_t start,std::int64_t end)
        ->std::optional<std::int64_t> {
        if (start>end) return {};
        using Entry=std::pair<std::int64_t,std::string>;
        std::priority_queue<Entry,std::vector<Entry>,std::greater<Entry>> queue;
        std::map<std::string,std::int64_t> earliest{{from,start}}; queue.push({start,from});
        while (!queue.empty()) {
            auto [at,camera]=queue.top(); queue.pop();
            if (earliest.at(camera)!=at) continue;
            if (camera==to) return at;
            for (const auto& e:adjacency[camera]) {
                if (!budget) {exhausted=true;return {}; } --budget;
                auto depart=std::max(at,e.open_from_unix_ms.value_or(std::numeric_limits<std::int64_t>::min()));
                if (depart>e.open_until_unix_ms.value_or(std::numeric_limits<std::int64_t>::max()) ||
                    depart>std::numeric_limits<std::int64_t>::max()-e.min_travel_ms) continue;
                auto next=depart+e.min_travel_ms;
                if (next>end) continue;
                auto it=earliest.find(e.to_camera);
                if (it==earliest.end() || next<it->second) {earliest[e.to_camera]=next;queue.push({next,e.to_camera});}
            }
        }
        return {};
    };
    std::map<std::string,std::int64_t> frontier;
    for (std::size_t begin=0;begin<captures.size();) {
        auto end=begin+1; auto upper=captures[begin].upper;
        while (end<captures.size() && captures[end].lower<=upper) {upper=std::max(upper,captures[end].upper);++end;}
        auto n=end-begin;
        const bool same=std::all_of(captures.begin()+begin,captures.begin()+end,[&](const auto& c){return c.camera_id==captures[begin].camera_id;});
        if (same) {
            std::optional<std::int64_t> at;
            if (frontier.empty()) at=captures[begin].lower;
            for (const auto& [camera,time]:frontier) {
                auto t=arrive(camera,captures[begin].camera_id,time,captures[begin].upper);
                if (t && (!at || *t<*at)) at=t;
            }
            for (auto i=begin;at && i<end;++i) {at=std::max(*at,captures[i].lower);if (*at>captures[i].upper) at.reset();}
            if (exhausted) return ChainFeasibility::Unknown;
            if (!at) return complete?ChainFeasibility::Infeasible:ChainFeasibility::Unknown;
            frontier={{captures[begin].camera_id,*at}};begin=end;continue;
        }
        if (n>12) return ChainFeasibility::Unknown;
        const std::size_t full=(std::size_t{1}<<n)-1;
        std::vector<std::optional<std::int64_t>> times((full+1)*n);
        for (std::size_t i=0;i<n;++i) {
            const auto& c=captures[begin+i]; auto& at=times[(std::size_t{1}<<i)*n+i];
            if (frontier.empty()) at=c.lower;
            for (const auto& [camera,time]:frontier) {
                auto t=arrive(camera,c.camera_id,time,c.upper);
                if (t) {t=std::max(*t,c.lower);if (!at || *t<*at) at=t;}
            }
        }
        for (std::size_t mask=1;mask<=full;++mask) for (std::size_t last=0;last<n;++last) {
            const auto at=times[mask*n+last]; if (!at) continue;
            for (std::size_t next=0;next<n;++next) if (!(mask&(std::size_t{1}<<next))) {
                if (!budget) return ChainFeasibility::Unknown; --budget;
                const auto& c=captures[begin+next];
                auto t=arrive(captures[begin+last].camera_id,c.camera_id,*at,c.upper);
                if (t) {t=std::max(*t,c.lower);auto& cell=times[(mask|(std::size_t{1}<<next))*n+next];if (!cell || *t<*cell) cell=t;}
                if (exhausted) return ChainFeasibility::Unknown;
            }
        }
        frontier.clear();
        for (std::size_t last=0;last<n;++last) if (times[full*n+last]) {
            const auto& camera=captures[begin+last].camera_id; const auto at=*times[full*n+last];
            auto it=frontier.find(camera);if (it==frontier.end() || at<it->second) frontier[camera]=at;
        }
        if (exhausted) return ChainFeasibility::Unknown;
        if (frontier.empty()) return complete?ChainFeasibility::Infeasible:ChainFeasibility::Unknown;
        begin=end;
    }
    return ChainFeasibility::Feasible;
}
}
