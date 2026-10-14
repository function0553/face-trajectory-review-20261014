#pragma once
#include <filesystem>
#include <string>
namespace face_trajectory {
inline std::string PathToUtf8(const std::filesystem::path& path) {
    const auto u8 = path.u8string();
    return std::string(u8.begin(), u8.end());
}
}
