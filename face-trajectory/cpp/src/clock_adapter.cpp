#include "face_trajectory/clock_adapter.hpp"
#include "face_trajectory/state.hpp"

#include <algorithm>
#include <cstdint>
#include <limits>
#include <optional>
#include <set>
#include <stdexcept>
#include <vector>

namespace face_trajectory {
using nlohmann::json;
namespace {
using Millis = std::int64_t;
constexpr auto Min = std::numeric_limits<Millis>::min();
constexpr auto Max = std::numeric_limits<Millis>::max();

std::string Required(const json& value, const char* key) {
    const auto& item = value.at(key);
    if (!item.is_string() || item.get_ref<const std::string&>().empty())
        throw std::invalid_argument(std::string(key) + " must be a nonempty string");
    return item.get<std::string>();
}
Millis Integer(const json& value, const char* key) {
    const auto& item = value.at(key);
    if (!item.is_number_integer() ||
        (item.is_number_unsigned() && item.get<std::uint64_t>() > static_cast<std::uint64_t>(Max)))
        throw std::invalid_argument(std::string(key) + " must be a signed 64-bit integer");
    return item.get<Millis>();
}
Millis Add(Millis a, Millis b) {
    if ((b > 0 && a > Max - b) || (b < 0 && a < Min - b))
        throw std::invalid_argument("clock arithmetic overflow");
    return a + b;
}
Millis Subtract(Millis a, Millis b) {
    if ((b > 0 && a < Min + b) || (b < 0 && a > Max + b))
        throw std::invalid_argument("clock arithmetic overflow");
    return a - b;
}
std::string Key(const std::string& camera, const std::string& version) {
    return json::array({camera, version}).dump();
}
Millis FixedOffset(const json& profile) {
    const auto value = Integer(profile, "fixed_utc_offset_minutes");
    if (value < -840 || value > 840) throw std::invalid_argument("fixed UTC offset must be within 14 hours");
    return value;
}

// Proleptic Gregorian civil date to days since 1970-01-01. No host timezone or
// time_t dependency; years before 1970 keep their signed millisecond value.
Millis Days(int year, unsigned month, unsigned day) {
    year -= month <= 2;
    const auto era = year / 400;
    const auto yoe = static_cast<unsigned>(year - era * 400);
    const auto shifted_month = month > 2 ? month - 3 : month + 9;
    const auto doy = (153 * shifted_month + 2) / 5 + day - 1;
    const auto doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    return static_cast<Millis>(era) * 146097 + doe - 719468;
}
struct ParsedTime { Millis utc_ms; Millis resolution_ms; Millis timezone_offset_minutes; std::string timezone_source; };
ParsedTime Parse(const std::string& text, const json& profile) {
    if (text.size() < 19 || text.size() > 32 || text[4] != '-' || text[7] != '-' ||
        (text[10] != 'T' && text[10] != ' ') || text[13] != ':' || text[16] != ':')
        throw std::invalid_argument("expected ISO8601 timestamp with seconds");
    auto digits = [&](std::size_t index, std::size_t count) {
        int result = 0;
        if (index + count > text.size()) throw std::invalid_argument("truncated capture time");
        for (std::size_t i = index; i < index + count; ++i) {
            if (text[i] < '0' || text[i] > '9') throw std::invalid_argument("invalid capture time digit");
            result = result * 10 + text[i] - '0';
        }
        return result;
    };
    const auto year = digits(0, 4), month = digits(5, 2), day = digits(8, 2);
    const auto hour = digits(11, 2), minute = digits(14, 2), second = digits(17, 2);
    const bool leap = year % 4 == 0 && (year % 100 != 0 || year % 400 == 0);
    const int month_days[] = {31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31};
    if (year < 1 || month < 1 || month > 12 || day < 1 || day > month_days[month - 1] ||
        hour > 23 || minute > 59 || second > 59)
        throw std::invalid_argument("invalid Gregorian capture date or time");
    std::size_t cursor = 19;
    Millis fraction = 0, resolution = 1000;
    if (cursor < text.size() && text[cursor] == '.') {
        const auto start = ++cursor;
        while (cursor < text.size() && text[cursor] >= '0' && text[cursor] <= '9') ++cursor;
        const auto count = cursor - start;
        if (count < 1 || count > 3) throw std::invalid_argument("capture fraction must have 1 to 3 digits");
        fraction = digits(start, count);
        resolution = count == 1 ? 100 : count == 2 ? 10 : 1;
        fraction *= resolution;
    }
    Millis offset_minutes = 0;
    std::string timezone_source = "EXPLICIT_TIMESTAMP_OFFSET";
    if (cursor == text.size()) {
        if (!profile.contains("fixed_utc_offset_minutes")) throw std::invalid_argument("naive capture time requires configured fixed UTC offset");
        offset_minutes = FixedOffset(profile);
        timezone_source = "PROFILE_FIXED_OFFSET";
    } else if (text[cursor] == 'Z' && cursor + 1 == text.size()) {
        offset_minutes = 0;
    } else {
        if ((text[cursor] != '+' && text[cursor] != '-') || cursor + 6 != text.size() || text[cursor + 3] != ':')
            throw std::invalid_argument("invalid timestamp UTC offset");
        const auto oh = digits(cursor + 1, 2), om = digits(cursor + 4, 2);
        if (om > 59 || oh > 14 || (oh == 14 && om != 0) ||
            (text[cursor] == '-' && oh == 0 && om == 0))
            throw std::invalid_argument("invalid or unknown timestamp UTC offset");
        offset_minutes = (oh * 60 + om) * (text[cursor] == '-' ? -1 : 1);
    }
    return {Days(year, month, day) * 86400000 + hour * 3600000LL + minute * 60000LL +
                second * 1000LL + fraction - offset_minutes * 60000,
            resolution, offset_minutes, timezone_source};
}
json Hold(const std::string& reason, const json& provenance) {
    return {{"status", "HOLD"}, {"reason_codes", json::array({reason})}, {"provenance", provenance}};
}
}

void ClockAdapter::Put(const json& profile) {
    const auto camera = Required(profile, "camera_id"), version = Required(profile, "profile_version");
    Required(profile, "source_evidence_id");
    if (!profile.at("verified").is_boolean()) throw std::invalid_argument("verified must be explicit boolean");
    const auto semantics = Required(profile, "capture_semantics");
    if (semantics != "DEVICE_CAPTURE" && semantics != "UNKNOWN")
        throw std::invalid_argument("capture_semantics must be DEVICE_CAPTURE or UNKNOWN");
    if (profile.at("verified") == true && semantics != "DEVICE_CAPTURE")
        throw std::invalid_argument("verified profile requires device capture semantics");
    if (profile.contains("fixed_utc_offset_minutes")) FixedOffset(profile);
    const auto& calibrations = profile.at("calibrations");
    if (!calibrations.is_array() || calibrations.size() > 10000 ||
        (profile.at("verified") == true && calibrations.empty()))
        throw std::invalid_argument("calibrations must be a bounded array; verified profile requires calibration");
    std::set<std::string> identifiers;
    std::vector<std::pair<Millis, Millis>> periods;
    for (const auto& calibration : calibrations) {
        if (!identifiers.insert(Required(calibration, "calibration_id")).second)
            throw std::invalid_argument("duplicate calibration_id");
        Required(calibration, "source_evidence_id");
        const auto first = Integer(calibration, "valid_from_utc_ms"), last = Integer(calibration, "valid_until_utc_ms");
        if (first >= last) throw std::invalid_argument("calibration validity must be nonempty [from,until)");
        Integer(calibration, "offset_ms");
        const auto uncertainty = Integer(calibration, "uncertainty_ms"), resolution = Integer(calibration, "time_resolution_ms");
        if (uncertainty < 0 || resolution < 1 || resolution > 86400000)
            throw std::invalid_argument("nonnegative uncertainty and resolution from 1ms to 1 day required");
        Add(uncertainty, std::max<Millis>(resolution, 1000) - 1);
        periods.emplace_back(first, last);
    }
    std::sort(periods.begin(), periods.end());
    for (std::size_t i = 1; i < periods.size(); ++i)
        if (periods[i].first < periods[i - 1].second) throw std::invalid_argument("overlapping calibration validity");
    const auto key = Key(camera, version), encoded = profile.dump();
    std::optional<std::string> old;
    if (state_) old = state_->Get("clock_profile", key);
    else if (const auto it = profiles_.find(key); it != profiles_.end()) old = it->second;
    if (old && *old != encoded) throw std::invalid_argument("clock profile version is immutable");
    if (old) return;
    if (state_) state_->Put("clock_profile", key, encoded, camera);
    else profiles_.emplace(key, encoded);
}

json ClockAdapter::Normalize(const json& request) const {
    const auto camera = Required(request, "camera_id"), version = Required(request, "profile_version");
    const auto raw = Required(request, "raw_time");
    const auto policy = request.value("time_policy", std::string("CORRECTED_UTC"));
    if (policy != "CORRECTED_UTC" && policy != "CAMERA_CAPTURE_60S")
        throw std::invalid_argument("time_policy must be CORRECTED_UTC or CAMERA_CAPTURE_60S");
    json provenance = {{"camera_id", camera}, {"profile_version", version}, {"raw_time", raw},
                       {"offset_convention", "POSITIVE_CAMERA_AHEAD"}, {"time_policy", policy}};
    if (policy == "CAMERA_CAPTURE_60S")
        provenance["max_device_time_error_ms"] = CameraCaptureErrorLimitMs;
    std::optional<std::string> encoded;
    const auto key = Key(camera, version);
    if (state_) encoded = state_->Get("clock_profile", key);
    else if (const auto it = profiles_.find(key); it != profiles_.end()) encoded = it->second;
    if (!encoded) return Hold("UNKNOWN_CLOCK_PROFILE", provenance);
    const auto profile = json::parse(*encoded);
    provenance["profile_source_evidence_id"] = profile.at("source_evidence_id");
    provenance["profile_verified"] = profile.at("verified");
    provenance["capture_semantics"] = profile.at("capture_semantics");
    if (profile.at("verified") != true) return Hold("UNVERIFIED_CLOCK_PROFILE", provenance);
    if (profile.at("capture_semantics") != "DEVICE_CAPTURE") return Hold("UNKNOWN_CAPTURE_SEMANTICS", provenance);
    ParsedTime parsed{};
    try { parsed = Parse(raw, profile); }
    catch (const std::invalid_argument& error) {
        provenance["parse_error"] = error.what();
        return Hold("INVALID_CAPTURE_TIME", provenance);
    }
    provenance["timezone_source"] = parsed.timezone_source;
    provenance["timezone_offset_minutes"] = parsed.timezone_offset_minutes;
    provenance["device_utc_unix_ms"] = parsed.utc_ms;
    provenance["text_resolution_ms"] = parsed.resolution_ms;
    json selected;
    Millis corrected = 0, uncertainty = 0;
    std::size_t nominal_matches = 0;
    bool boundary = false, overflow = false;
    for (const auto& calibration : profile.at("calibrations")) {
        try {
            const auto candidate = Subtract(parsed.utc_ms, Integer(calibration, "offset_ms"));
            const auto first = Integer(calibration, "valid_from_utc_ms"), last = Integer(calibration, "valid_until_utc_ms");
            if (candidate < first || candidate >= last) continue;
            ++nominal_matches;
            const auto error = Add(Integer(calibration, "uncertainty_ms"),
                                   std::max(Integer(calibration, "time_resolution_ms"), parsed.resolution_ms) - 1);
            if (Subtract(candidate, error) < first || Add(candidate, error) >= last) { boundary = true; continue; }
            selected = calibration;
            corrected = candidate;
            uncertainty = error;
        } catch (const std::invalid_argument&) { overflow = true; }
    }
    if (nominal_matches > 1) return Hold("AMBIGUOUS_CLOCK_CALIBRATION", provenance);
    if (selected.is_null()) {
        if (overflow) return Hold("CLOCK_ARITHMETIC_OVERFLOW", provenance);
        return Hold(boundary ? "CALIBRATION_BOUNDARY_UNCERTAIN" : "OUTSIDE_CALIBRATION_VALIDITY", provenance);
    }
    provenance["calibration_id"] = selected.at("calibration_id");
    provenance["calibration_source_evidence_id"] = selected.at("source_evidence_id");
    provenance["offset_ms"] = selected.at("offset_ms");
    provenance["calibration_uncertainty_ms"] = selected.at("uncertainty_ms");
    provenance["configured_time_resolution_ms"] = selected.at("time_resolution_ms");
    provenance["valid_from_utc_ms"] = selected.at("valid_from_utc_ms");
    provenance["valid_until_utc_ms"] = selected.at("valid_until_utc_ms");
    const auto offset = Integer(selected, "offset_ms");
    provenance["applied_offset_ms"] = offset;
    if (policy == "CAMERA_CAPTURE_60S") {
        provenance["applied_offset_ms"] = 0;
        // The calibration selects a verified true-time window, but the emitted
        // nominal time stays on the camera clock. Include the entire offset,
        // measurement/drift uncertainty and quantization in its error bound.
        // Check before abs/add so INT64_MIN and large errors cannot overflow.
        if (offset < -CameraCaptureErrorLimitMs || offset > CameraCaptureErrorLimitMs)
            return Hold("CAMERA_CLOCK_ERROR_EXCEEDS_60S", provenance);
        const auto magnitude = offset < 0 ? -offset : offset;
        if (uncertainty > CameraCaptureErrorLimitMs - magnitude)
            return Hold("CAMERA_CLOCK_ERROR_EXCEEDS_60S", provenance);
        uncertainty += magnitude;
        corrected = parsed.utc_ms;
        provenance["device_time_error_bound_ms"] = uncertainty;
    }
    return {{"status", "NORMALIZED"}, {"reason_codes", json::array()},
            {"captured_at_unix_ms", corrected}, {"clock_uncertainty_ms", uncertainty},
            {"time_source", "UTC_CAPTURE"}, {"clock_domain", "UTC"}, {"provenance", provenance}};
}
}
