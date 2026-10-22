#pragma once

#include "face_trajectory/association.hpp"
#include "face_trajectory/state.hpp"
#include <nlohmann/json.hpp>

namespace face_trajectory {

// Durable intake and monitoring projections. Identity remains owned by the
// association store; ticket events never assert an identity on their own.
class MonitoringStore {
public:
    explicit MonitoringStore(State* state) : state_(state) {}
    void RequireDurable() const;
    nlohmann::json PutPerson(const nlohmann::json& request, Gallery& gallery, bool require_ready=false);
    nlohmann::json Person(const std::string& target) const;
    nlohmann::json PersonSummary(const std::string& target) const;
    static nlohmann::json PhotoUrl(const nlohmann::json& value);
    nlohmann::json CameraTimeline(const nlohmann::json& request,
        const AssociationStore& associations,const nlohmann::json& activity_context) const;
    std::optional<nlohmann::json> ObservationReceipt(const nlohmann::json& request) const;
    void SaveObservation(const nlohmann::json& request, const nlohmann::json& response);
    void CompleteObservationReceipt(const std::string& id, const nlohmann::json& response);
    nlohmann::json PutGate(const nlohmann::json& request, const nlohmann::json& normalization,
                          const AssociationStore& associations);
    nlohmann::json Get(const std::string& target, const AssociationStore& associations,
                       std::optional<std::int64_t> before = {}) const;
    void CheckpointRuntime(std::int64_t cutoff, const AssociationStore& associations);
    nlohmann::json ObserveSummary(const SightingEvidence& sighting,
        std::optional<std::uint64_t> previous_association_revision, const AssociationStore& associations);
    static nlohmann::json Summary(nlohmann::json projection);
private:
    State* state_;
};
}
