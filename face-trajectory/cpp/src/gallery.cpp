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
#if defined(__GNUC__) && (defined(__x86_64__) || defined(__i386__))
#include <immintrin.h>
#endif
#include <cstdlib>

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

float DotSse2OrScalar(const Embedding& left, const Embedding& right) {
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

#if defined(__GNUC__) && (defined(__x86_64__) || defined(__i386__))
// 同一套双精度运算的 AVX 版：4 个双精度通道依次对应 SSE2 版的 a0、a1、b0、b1，
// 每个通道的乘、加顺序与 SSE2 版完全相同，收尾也按 (a0+b0)+(a1+b1) 合并，因此结果逐位一致。
// 只开 avx、不开 fma：融合乘加会改变舍入，不能用它换速度。
__attribute__((target("avx"))) float DotAvx(const Embedding& left, const Embedding& right) {
    __m256d acc = _mm256_setzero_pd();
    for (std::size_t i = 0; i < kEmbeddingDim; i += 4) {
        const __m256d l = _mm256_cvtps_pd(_mm_loadu_ps(left.data() + i));
        const __m256d r = _mm256_cvtps_pd(_mm_loadu_ps(right.data() + i));
        acc = _mm256_add_pd(acc, _mm256_mul_pd(l, r));
    }
    double lanes[4];
    _mm256_storeu_pd(lanes, acc);
    return static_cast<float>((lanes[0] + lanes[2]) + (lanes[1] + lanes[3]));
}
const bool kHasAvx = [] { __builtin_cpu_init(); return __builtin_cpu_supports("avx") != 0; }();
#endif

float Dot(const Embedding& left, const Embedding& right) {
#if defined(__GNUC__) && (defined(__x86_64__) || defined(__i386__))
    if (kHasAvx) return DotAvx(left, right);
#endif
    return DotSse2OrScalar(left, right);
}

// 检索线程上限：默认 8（原行为）；与其他业务共用服务器时用 FACE_TRAJECTORY_SEARCH_THREADS 调小（1–64）。
// 线程数只影响耗时，不影响得分和排序结果。
int SearchThreads() {
    static const int value = [] {
        const char* text = std::getenv("FACE_TRAJECTORY_SEARCH_THREADS");
        if (!text || !*text) return 8;
        char* end = nullptr;
        const long parsed = std::strtol(text, &end, 10);
        return (end && *end == '\0' && parsed >= 1 && parsed <= 64) ? static_cast<int>(parsed) : 8;
    }();
    return value;
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
        const auto& persons=bucket->second.persons;
        std::vector<float> scores(rows.size());
#ifdef _OPENMP
        const int threads=std::max(1,std::min(SearchThreads(),omp_get_num_procs()));
#pragma omp parallel for schedule(static) num_threads(threads) if(rows.size()>=10000 && threads>1)
#endif
        for(std::int64_t i=0;i<static_cast<std::int64_t>(rows.size());++i)
            scores[static_cast<std::size_t>(i)]=Dot(normalized_query,rows[static_cast<std::size_t>(i)].embedding);
        std::vector<std::optional<std::size_t>> best(persons.size());
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
        // 先只按 (得分, 人员槽位) 排名，最后只为前 top_k 名复制人员/模板 ID 字符串；
        // 排名规则与原实现相同（得分降序，同分按人员 ID 升序），因此前 top_k 名完全一致。
        std::vector<std::size_t> ranked;
        ranked.reserve(bucket->second.person_slots.size());
        for(std::size_t i=0;i<best.size();++i)if(best[i])ranked.push_back(i);
        const auto better_slot=[&](std::size_t a,std::size_t b){
            const float sa=scores[*best[a]],sb=scores[*best[b]];
            if(sa!=sb)return sa>sb;
            return persons[a].id<persons[b].id;
        };
        if(ranked.size()>top_k){
            std::nth_element(ranked.begin(),ranked.begin()+static_cast<std::ptrdiff_t>(top_k),ranked.end(),better_slot);
            ranked.resize(top_k);
        }
        std::sort(ranked.begin(),ranked.end(),better_slot);
        hits.reserve(ranked.size());
        for(const auto i:ranked)hits.push_back({persons[i].id,rows[*best[i]].template_id,scores[*best[i]]});
    }
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
