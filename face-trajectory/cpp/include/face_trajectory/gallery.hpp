#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <map>
#include <shared_mutex>
#include <string>
#include <unordered_map>
#include <vector>

namespace face_trajectory {
class State;

constexpr std::size_t kEmbeddingDim = 512;
using Embedding = std::array<float, kEmbeddingDim>;

struct FeatureSpaceContract {
    std::string recognition_model_sha256;
    std::string detector_model_sha256;  // Empty only for externally aligned faces.
    std::string detector_profile;
    std::string alignment_version;
    std::string preprocessing_version;
    std::string output_version;

    bool operator==(const FeatureSpaceContract& other) const noexcept;
};

struct FaceTemplate {
    std::string template_id;
    std::string person_entity_id;
    FeatureSpaceContract feature_space;
    Embedding embedding;
};

struct PersonHit {
    std::string person_entity_id;
    std::string template_id;
    float similarity;
};

// Exact cosine search, partitioned by feature space. No identity threshold is
// applied here; hits are candidates rather than confirmed identities.
class Gallery {
public:
    explicit Gallery(State* state = nullptr, std::size_t max_templates = 500000);
    static void ValidateQuery(const FeatureSpaceContract& space, const Embedding& query);
    void Add(FaceTemplate value);
    void Remove(const std::string& template_id);
    std::vector<PersonHit> Search(
        const FeatureSpaceContract& feature_space, const Embedding& query, std::size_t top_k) const;
    std::size_t Size() const;
    std::uint64_t Version() const;

private:
    struct SpaceLess {
        bool operator()(const FeatureSpaceContract& a,
                        const FeatureSpaceContract& b) const noexcept;
    };
    mutable std::shared_mutex mutex_;
    struct Bucket {
        struct DenseTemplate {
            std::string template_id;
            std::size_t person_slot;
            Embedding embedding;
        };
        std::vector<DenseTemplate> rows;
        std::unordered_map<std::string, std::size_t> slots;
        struct Person {std::string id;std::size_t count;};
        std::vector<Person> persons;
        std::unordered_map<std::string,std::size_t> person_slots;
        std::vector<std::size_t> free_person_slots;
    };
    std::map<FeatureSpaceContract, Bucket, SpaceLess>
        templates_by_space_;
    std::unordered_map<std::string, FeatureSpaceContract> template_spaces_;
    std::uint64_t version_ = 0;
    State* state_ = nullptr;
    std::size_t max_templates_;
};

}  // namespace face_trajectory
