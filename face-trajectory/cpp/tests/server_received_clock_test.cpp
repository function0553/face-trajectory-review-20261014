#include "face_trajectory/clock_adapter.hpp"
#include <stdexcept>

void check(bool condition) {
    if (!condition) throw std::runtime_error("server reception clock contract failed");
}

int main() {
    using nlohmann::json;
    face_trajectory::ClockAdapter clocks;
    json calibration={{"calibration_id","server-clock"},{"source_evidence_id","ntp-server"},
        {"valid_from_utc_ms",0LL},{"valid_until_utc_ms",4102444800000LL},
        {"offset_ms",0},{"uncertainty_ms",1000},{"time_resolution_ms",1}};
    json profile={{"camera_id","camera"},{"profile_version","server-v1"},
        {"source_evidence_id","ntp-server"},{"verified",true},
        {"capture_semantics","SERVER_RECEIVED_AT"},{"calibrations",json::array({calibration})}};
    clocks.Put(profile);
    json request={{"camera_id","camera"},{"profile_version","server-v1"},
        {"raw_time","2020-01-02T03:04:05.123Z"},{"time_policy","CAMERA_CAPTURE_60S"},
        {"source_kind","SERVER_FRAME_RECEIVED"},{"source_evidence_id","original-frame-receive"}};
    auto reply=clocks.Normalize(request);
    check(reply.at("status")=="NORMALIZED");
    check(reply.at("captured_at_unix_ms")==1577934245123LL);
    check(reply.at("provenance").at("time_basis")=="SERVER_RECEIVED_AT");
    check(reply.at("provenance").at("camera_capture_time_verified")==false);
    check(reply.at("provenance").at("network_delay_bounded")==false);
    auto missing=request;missing.erase("source_evidence_id");
    check(clocks.Normalize(missing).at("status")=="HOLD");
    auto camera=request;camera["source_kind"]="CAMERA_OSD_SAME_IMAGE_OCR";
    check(clocks.Normalize(camera).at("status")=="HOLD");
    auto bad=profile;bad["profile_version"]="invalid-offset";bad["calibrations"][0]["offset_ms"]=1;
    bool refused=false;
    try { clocks.Put(bad); } catch(const std::invalid_argument&) { refused=true; }
    check(refused);
    auto legacy=profile;legacy["profile_version"]="camera-v1";legacy["capture_semantics"]="DEVICE_CAPTURE";
    clocks.Put(legacy);camera["profile_version"]="camera-v1";
    check(clocks.Normalize(camera).at("status")=="NORMALIZED");
}
