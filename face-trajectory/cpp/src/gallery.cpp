#include "face_trajectory/gallery.hpp"
#include "face_trajectory/state.hpp"
#include "face_trajectory/storage_json.hpp"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <mutex>
#include <shared_mutex>
#include <stdexcept>
#include <tuple>
#include <utility>
#ifdef _OPENMP
#include <omp.h>
#endif
#if defined(_M_X64) || defined(__SSE2__)
#include <emmintrin.h>
#endif

namespace face_trajectory {
namespace {

bool IsSha256(const std::string& value) {
    return value.size() == 64 && std::all_of(value.begin(), value.end(), [](unsigned char c) {
        return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F');
    });
}

void ValidateSpace(const FeatureSpaceContract& value) {
    if (!IsSha256(value.recognition_model_sha256) ||
        (!value.detector_model_sha256.empty() && !IsSha256(value.detector_model_sha256)) ||
        (value.detector_model_sha256.empty() != value.detector_profile.empty()) ||
        value.alignment_version.empty() || value.preprocessing_version.empty() ||
        value.output_version.empty()) {
        throw std::invalid_argument("incomplete or invalid feature space contract");
    }
}

FeatureSpaceContract CanonicalizeSpace(FeatureSpaceContract value) {
    ValidateSpace(value);
    const auto lower = [](unsigned char c) { return static_cast<char>(std::tolower(c)); };
    std::transform(value.recognition_model_sha256.begin(), value.recognition_model_sha256.end(),
                   value.recognition_model_sha256.begin(), lower);
    std::transform(value.detector_model_sha256.begin(), value.detector_model_sha256.end(),
                   value.detector_model_sha256.begin(), lower);
    return value;
}

void Normalize(Embedding& value) {
    double squared_norm = 0;
    for (float component : value) {
        if (!std::isfinite(component)) {
            throw std::invalid_argument("embedding must contain finite values");
        }
        squared_norm += static_cast<double>(component) * component;
    }
    if (!(squared_norm > 0) || !std::isfinite(squared_norm)) {
        throw std::invalid_argument("embedding must have nonzero finite norm");
    }
    const double inverse_norm = 1.0 / std::sqrt(squared_norm);
    for (float& component : value) {
        component = static_cast<float>(component * inverse_norm);
    }
}

float Dot(const Embedding& left, const Embedding& right) {
#if defined(_M_X64) || defined(__SSE2__)
    // Float inputs, double products/accumulation. Do not lower precision near
    // identity thresholds to obtain a faster benchmark.
    __m128d a=_mm_setzero_pd(),b=a;
    for(std::size_t i=0;i<kEmbeddingDim;i+=4){
        const auto l=_mm_loadu_ps(left.data()+i),r=_mm_loadu_ps(right.data()+i);
        a=_mm_add_pd(a,_mm_mul_pd(_mm_cvtps_pd(l),_mm_cvtps_pd(r)));
        b=_mm_add_pd(b,_mm_mul_pd(_mm_cvtps_pd(_mm_movehl_ps(l,l)),_mm_cvtps_pd(_mm_movehl_ps(r,r))));
    }
    double lanes[2];_mm_storeu_pd(lanes,_mm_add_pd(a,b));
    return static_cast<float>(lanes[0]+lanes[1]);
#else
    double sum = 0;
    for (std::size_t i = 0; i < kEmbeddingDim; ++i) {
        sum += static_cast<double>(left[i]) * right[i];
    }
    return static_cast<float>(sum);
#endif
}

}  // namespace

bool FeatureSpaceContract::operator==(const FeatureSpaceContract& other) const noexcept {
    return recognition_model_sha256 == other.recognition_model_sha256 &&
           detector_model_sha256 == other.detector_model_sha256 &&
           detector_profile == other.detector_profile && alignment_version == other.alignment_version &&
           preprocessing_version == other.preprocessing_version && output_version == other.output_version;
}

bool Gallery::SpaceLess::operator()(const FeatureSpaceContract& a,
                                    const FeatureSpaceContract& b) const noexcept {
    return std::tie(a.recognition_model_sha256, a.detector_model_sha256,
                    a.detector_profile, a.alignment_version, a.preprocessing_version,
                    a.output_version) <
           std::tie(b.recognition_model_sha256, b.detector_model_sha256,
                    b.detector_profile, b.alignment_version, b.preprocessing_version,
                    b.output_version);
}

Gallery::Gallery(State* state,std::size_t max_templates): state_(state),max_templates_(max_templates) {
    if(!max_templates_)throw std::invalid_argument("gallery capacity must be positive");
    if (!state_) return;
    auto persisted=state_;
    state_=nullptr;
    persisted->Scan("template",[&](const std::string&,const std::string& text){Add(nlohmann::json::parse(text).get<FaceTemplate>());});
    version_=std::stoull(persisted->Get("version","gallery").value_or("0"));
    state_=persisted;
}
void Gallery::ValidateQuery(const FeatureSpaceContract& space,const Embedding& query) {
    CanonicalizeSpace(space);auto copy=query;Normalize(copy);
}

void Gallery::Add(FaceTemplate value) {
    if (value.template_id.empty() || value.person_entity_id.empty()) {
        throw std::invalid_argument("template and person IDs are required");
    }
    value.feature_space = CanonicalizeSpace(std::move(value.feature_space));
    Normalize(value.embedding);
    std::unique_lock lock(mutex_);
    if (template_spaces_.find(value.template_id) != template_spaces_.end()) {
        throw std::invalid_argument("duplicate template_id");
    }
    if(template_spaces_.size()>=max_templates_)throw std::runtime_error("CAPACITY: gallery template limit reached");
    const auto id = value.template_id;
    const auto space = value.feature_space;
    if (state_) state_->Put("template",id,nlohmann::json(value).dump());
    template_spaces_.emplace(id, space);
    try {
        auto& bucket=templates_by_space_[space];
        auto person=bucket.person_slots.find(value.person_entity_id);
        std::size_t slot;
        if(person!=bucket.person_slots.end())slot=person->second;
        else {
            if(bucket.free_person_slots.empty()){slot=bucket.persons.size();bucket.persons.push_back({value.person_entity_id,0});}
            else {slot=bucket.free_person_slots.back();bucket.free_person_slots.pop_back();bucket.persons[slot]={value.person_entity_id,0};}
            bucket.person_slots.emplace(value.person_entity_id,slot);
        }
        bucket.rows.push_back({value.template_id,slot,value.embedding});
        ++bucket.persons[slot].count;
        bucket.slots.emplace(id,bucket.rows.size()-1);
    } catch (...) {
        template_spaces_.erase(id);
        throw;
    }
    ++version_;
    if(state_)state_->Put("version","gallery",std::to_string(version_));
}

void Gallery::Remove(const std::string& template_id) {
    std::unique_lock lock(mutex_);
    const auto index = template_spaces_.find(template_id);
    if (index == template_spaces_.end()) {
        throw std::invalid_argument("unknown template_id");
    }
    auto bucket = templates_by_space_.find(index->second);
    auto& rows=bucket->second.rows;
    const auto slot=bucket->second.slots.at(template_id);
    auto& person=bucket->second.persons[rows[slot].person_slot];
    if(--person.count==0){bucket->second.person_slots.erase(person.id);person.id.clear();bucket->second.free_person_slots.push_back(rows[slot].person_slot);}
    if(slot+1!=rows.size()) { rows[slot]=std::move(rows.back());bucket->second.slots.at(rows[slot].template_id)=slot; }
    rows.pop_back();bucket->second.slots.erase(template_id);
    if (rows.empty()) templates_by_space_.erase(bucket);
    template_spaces_.erase(index);
    if(state_)state_->Erase("template",template_id);
    ++version_;
    if(state_)state_->Put("version","gallery",std::to_string(version_));
}

std::vector<PersonHit> Gallery::Search(
    const FeatureSpaceContract& feature_space, const Embedding& query, std::size_t top_k) const {
    const auto normalized_space = CanonicalizeSpace(feature_space);
    if (top_k == 0) throw std::invalid_argument("positive top_k is required");
    Embedding normalized_query = query;
    Normalize(normalized_query);
    std::shared_lock lock(mutex_);
    std::vector<PersonHit> hits;
    const auto bucket = templates_by_space_.find(normalized_space);
    if (bucket != templates_by_space_.end()) {
        const auto& rows=bucket->second.rows;
        std::vector<float> scores(rows.size());
#ifdef _OPENMP
        const int threads=std::max(1,std::min(8,omp_get_num_procs()));
#pragma omp parallel for schedule(static) num_threads(threads) if(rows.size()>=10000)
#endif
        for(std::int64_t i=0;i<static_cast<std::int64_t>(rows.size());++i)
            scores[static_cast<std::size_t>(i)]=Dot(normalized_query,rows[static_cast<std::size_t>(i)].embedding);
        std::vector<std::optional<std::size_t>> best(bucket->second.persons.size());
        for (std::size_t slot=0;slot<rows.size();++slot) {
            const auto& value=rows[slot];
            const auto& id=value.template_id;
            const float score = scores[slot];
            auto& winner=best[value.person_slot];
            if (!winner || score > scores[*winner] ||
                (score == scores[*winner] && id < rows[*winner].template_id)) {
                winner=slot;
            }
        }
        hits.reserve(bucket->second.person_slots.size());
        for(std::size_t i=0;i<best.size();++i)if(best[i])
            hits.push_back({bucket->second.persons[i].id,rows[*best[i]].template_id,scores[*best[i]]});
    }
    const auto better = [](const PersonHit& a, const PersonHit& b) {
        if (a.similarity != b.similarity) return a.similarity > b.similarity;
        return a.person_entity_id < b.person_entity_id;
    };
    if (hits.size() > top_k) {
        std::nth_element(hits.begin(), hits.begin() + top_k, hits.end(), better);
        hits.resize(top_k);
    }
    std::sort(hits.begin(), hits.end(), better);
    return hits;
}

std::size_t Gallery::Size() const {
    std::shared_lock lock(mutex_);
    return template_spaces_.size();
}

std::uint64_t Gallery::Version() const {
    std::shared_lock lock(mutex_);
    return version_;
}

}  // namespace face_trajectory
