#pragma once

// Minimal, deliberately scoped JSON parser: handles exactly ONE flat
// (non-nested) JSON object per call - string/number/bool/number-array
// values only, no nested objects, no \uXXXX escapes. This is not a
// general JSON library; it's sized to tailer.py's OverlayBroadcaster wire
// format (eq_log_suite/tailer.py's OverlayBroadcaster.send, e.g.
// {"kind":"alert","key":"rule_1","text":"...","color":[0.9,0.3,0.2],
// "duration":6,"countdown":true}) and nothing more. Written from scratch
// for this project rather than vendoring a JSON library, since the
// message shape is simple and fixed and this avoids an extra dependency
// fetch for one narrow use.

#include <cctype>
#include <string>
#include <unordered_map>
#include <vector>

struct JsonValue {
    enum class Type { Null, String, Number, Bool, NumberArray } type = Type::Null;
    std::string str;
    double num = 0.0;
    bool boolean = false;
    std::vector<double> numArray;
};
using JsonObject = std::unordered_map<std::string, JsonValue>;

namespace overlay_json_detail {

inline void SkipWs(const std::string& s, size_t& i) {
    while (i < s.size() && std::isspace(static_cast<unsigned char>(s[i]))) i++;
}

inline bool ParseString(const std::string& s, size_t& i, std::string& out) {
    if (i >= s.size() || s[i] != '"') return false;
    i++;
    out.clear();
    while (i < s.size() && s[i] != '"') {
        char c = s[i];
        if (c == '\\' && i + 1 < s.size()) {
            char n = s[i + 1];
            switch (n) {
                case '"': out += '"'; break;
                case '\\': out += '\\'; break;
                case '/': out += '/'; break;
                case 'n': out += '\n'; break;
                case 't': out += '\t'; break;
                default: out += n; break; // good enough for alert text - no \uXXXX support
            }
            i += 2;
        } else {
            out += c;
            i++;
        }
    }
    if (i >= s.size()) return false; // unterminated string
    i++; // closing quote
    return true;
}

inline bool ParseNumber(const std::string& s, size_t& i, double& out) {
    size_t start = i;
    if (i < s.size() && (s[i] == '-' || s[i] == '+')) i++;
    while (i < s.size() && (std::isdigit(static_cast<unsigned char>(s[i])) || s[i] == '.' ||
                             s[i] == 'e' || s[i] == 'E' || s[i] == '-' || s[i] == '+')) {
        i++;
    }
    if (i == start) return false;
    try {
        out = std::stod(s.substr(start, i - start));
    } catch (...) {
        return false;
    }
    return true;
}

inline bool ParseNumberArray(const std::string& s, size_t& i, std::vector<double>& out) {
    if (i >= s.size() || s[i] != '[') return false;
    i++;
    out.clear();
    SkipWs(s, i);
    if (i < s.size() && s[i] == ']') { i++; return true; }
    for (;;) {
        SkipWs(s, i);
        double v;
        if (!ParseNumber(s, i, v)) return false;
        out.push_back(v);
        SkipWs(s, i);
        if (i < s.size() && s[i] == ',') { i++; continue; }
        if (i < s.size() && s[i] == ']') { i++; return true; }
        return false;
    }
}

} // namespace overlay_json_detail

// Parses ONE flat JSON object from `line` into `out`. Returns false (and
// leaves `out` in an unspecified state) on any malformed input - callers
// should just drop the line, matching mangohud_writer.py's
// `except json.JSONDecodeError: pass` behavior for the same wire format.
inline bool ParseFlatJsonObject(const std::string& line, JsonObject& out) {
    using namespace overlay_json_detail;
    out.clear();
    size_t i = 0;
    SkipWs(line, i);
    if (i >= line.size() || line[i] != '{') return false;
    i++;
    SkipWs(line, i);
    if (i < line.size() && line[i] == '}') return true; // empty object

    for (;;) {
        SkipWs(line, i);
        std::string key;
        if (!ParseString(line, i, key)) return false;
        SkipWs(line, i);
        if (i >= line.size() || line[i] != ':') return false;
        i++;
        SkipWs(line, i);

        JsonValue value;
        if (i < line.size() && line[i] == '"') {
            value.type = JsonValue::Type::String;
            if (!ParseString(line, i, value.str)) return false;
        } else if (i < line.size() && line[i] == '[') {
            value.type = JsonValue::Type::NumberArray;
            if (!ParseNumberArray(line, i, value.numArray)) return false;
        } else if (line.compare(i, 4, "true") == 0) {
            value.type = JsonValue::Type::Bool;
            value.boolean = true;
            i += 4;
        } else if (line.compare(i, 5, "false") == 0) {
            value.type = JsonValue::Type::Bool;
            value.boolean = false;
            i += 5;
        } else if (line.compare(i, 4, "null") == 0) {
            value.type = JsonValue::Type::Null;
            i += 4;
        } else {
            value.type = JsonValue::Type::Number;
            if (!ParseNumber(line, i, value.num)) return false;
        }

        out[key] = std::move(value);

        SkipWs(line, i);
        if (i < line.size() && line[i] == ',') { i++; continue; }
        if (i < line.size() && line[i] == '}') { i++; return true; }
        return false;
    }
}
