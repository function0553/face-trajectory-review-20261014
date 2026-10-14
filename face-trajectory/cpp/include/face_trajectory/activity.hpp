#pragma once
#include "face_trajectory/state.hpp"
#include <nlohmann/json.hpp>

namespace face_trajectory {
// Activity identity and lifecycle are independent of viewer sessions. All
// methods participate in the owning State transaction.
class ActivityStore {
public:
    static constexpr std::int64_t RetentionMs = 604800000;
    explicit ActivityStore(State& root) : root_(root) {}
    nlohmann::json Control(const nlohmann::json& request);
    nlohmann::json Context(const std::string& id) const;
    std::int64_t TrustedNow(const nlohmann::json& request, bool advance=true);
    std::optional<nlohmann::json> Before(nlohmann::json& request, State& scope);
    void Receipt(const nlohmann::json& request, const nlohmann::json& response, State& scope);
    void Notify(const nlohmann::json& request, const nlohmann::json& response, State& scope);
    static nlohmann::json Content(nlohmann::json request);
    static std::string EventKey(const nlohmann::json& request);
    static std::string Digest(const std::string& text);
    static void ExternalIds(nlohmann::json& value);
private:
    State& root_;
};
}
