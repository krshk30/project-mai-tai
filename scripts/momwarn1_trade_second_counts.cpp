// Offline, bounded all-print cardinalities; no provider or trading policy.
#include <algorithm>
#include <array>
#include <charconv>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <string_view>
#include <unordered_map>
#include <vector>
#include <unistd.h>
#include <zlib.h>

constexpr uint64_t NS = 1000000000;
constexpr size_t MAX_SYMBOLS = 100000, MAX_LINE = 4096, CHUNK = 1024 * 1024;

uint64_t number(std::string_view text) {
    uint64_t value = 0;
    auto result = std::from_chars(text.data(), text.data() + text.size(), value);
    if (result.ec != std::errc{} || result.ptr != text.data() + text.size())
        throw std::runtime_error("invalid integer SIP timestamp/bound");
    return value;
}

std::string quoted(std::string_view value) {
    std::string result = "\"";
    for (unsigned char ch : value) {
        if (ch < 32) throw std::runtime_error("control byte in source symbol");
        if (ch == '"' || ch == '\\') result += '\\';
        result += ch;
    }
    return result + '"';
}

size_t fields(std::string_view line, std::array<std::string_view, 32>& parts) {
    bool quote = false;
    size_t begin = 0, count = 0;
    for (size_t i = 0; i <= line.size(); ++i) {
        if (i < line.size() && line[i] == '"') {
            if (quote && i + 1 < line.size() && line[i + 1] == '"') ++i;
            else quote = !quote;
        } else if (i == line.size() || (line[i] == ',' && !quote)) {
            if (count == parts.size()) throw std::runtime_error("CSV field bound");
            parts[count++] = line.substr(begin, i - begin);
            begin = i + 1;
        }
    }
    if (quote) throw std::runtime_error("unterminated CSV quote");
    return count;
}

struct Symbol { std::vector<uint64_t> seen; };

int main(int argc, char** argv) {
    gzFile input = nullptr;
    try {
        if (argc != 4 && argc != 5) throw std::runtime_error("usage: scanner FILE START_NS END_NS_EXCLUSIVE [PAUSE_FILE]");
        const uint64_t start = number(argv[2]), end = number(argv[3]);
        if (start % NS || end % NS || end <= start || (end - start) / NS > 19800)
            throw std::runtime_error("whole-second PM window bound");
        const size_t seconds = (end - start) / NS, words = (seconds + 63) / 64;
        std::vector<uint32_t> counts(seconds);
        std::unordered_map<std::string, Symbol> symbols;
        std::string previous, carry;
        Symbol* cached = nullptr;
        std::array<std::string_view, 32> parts;
        size_t width = 0, ticker = 0, sip = 0, price = 0, size = 0;
        bool header = false, have_template = false;
        uint64_t rows = 0, pm_rows = 0, minimum = UINT64_MAX, maximum = 0;
        uint64_t pm_minimum = UINT64_MAX, pm_maximum = 0, template_ns = 0;
        std::string template_symbol, template_price, template_size;
        auto consume = [&](std::string_view line) {
            if (!line.empty() && line.back() == '\r') line.remove_suffix(1);
            if (line.size() > MAX_LINE || line.empty()) throw std::runtime_error("CSV line bound/empty row");
            const size_t count = fields(line, parts);
            if (!header) {
                width = count;
                std::unordered_map<std::string, size_t> names;
                for (size_t i = 0; i < count; ++i)
                    if (!names.emplace(parts[i], i).second) throw std::runtime_error("duplicate CSV header");
                for (auto name : {"ticker", "sip_timestamp", "price", "size", "conditions"})
                    if (!names.count(name)) throw std::runtime_error("missing required CSV column");
                ticker = names.at("ticker"); sip = names.at("sip_timestamp");
                price = names.at("price"); size = names.at("size");
                header = true;
                return;
            }
            if (count != width) throw std::runtime_error("CSV width changed");
            auto name = parts[ticker];
            if (name.size() >= 2 && name.front() == '"' && name.back() == '"') {
                name.remove_prefix(1); name.remove_suffix(1);
            }
            if (name.empty() || name.size() > 128 || name.find('"') != name.npos)
                throw std::runtime_error("invalid source symbol/bound");
            const uint64_t stamp = number(parts[sip]);
            if (!stamp) throw std::runtime_error("zero SIP stamp");
            ++rows;
            minimum = std::min(minimum, stamp); maximum = std::max(maximum, stamp);
            if (name != previous || !cached) {
                previous = std::string(name);
                auto found = symbols.find(previous);
                if (found == symbols.end()) {
                    if (symbols.size() == MAX_SYMBOLS) throw std::runtime_error("symbol memory bound");
                    found = symbols.emplace(previous, Symbol{}).first;
                }
                cached = &found->second;
            }
            if (rows % 10000000 == 0) std::cerr << "rows=" << rows << " pm_rows=" << pm_rows << '\n';
            if (stamp < start || stamp >= end) return;
            ++pm_rows;
            pm_minimum = std::min(pm_minimum, stamp); pm_maximum = std::max(pm_maximum, stamp);
            if (cached->seen.empty()) cached->seen.resize(words);
            const size_t second = (stamp - start) / NS;
            const uint64_t mask = uint64_t{1} << (second % 64);
            auto& word = cached->seen[second / 64];
            if (!(word & mask)) { word |= mask; ++counts[second]; }
            if (!have_template) {
                std::string value(parts[price]);
                char* last = nullptr;
                const double numeric = std::strtod(value.c_str(), &last);
                if (last == value.c_str() + value.size() && std::isfinite(numeric) && numeric > 0) {
                    have_template = true; template_ns = stamp;
                    template_symbol = previous; template_price = value; template_size = std::string(parts[size]);
                }
            }
        };
        input = gzopen(argv[1], "rb");
        if (!input) throw std::runtime_error("cannot open gzip");
        gzbuffer(input, 128 * 1024);
        std::vector<char> buffer(CHUNK);
        int read;
        while (true) {
            if (argc == 5 && access(argv[4], F_OK) == 0) {
                std::cerr << "PAUSED scanner\n";
                while (access(argv[4], F_OK) == 0) usleep(100000);
                std::cerr << "RESUMED scanner\n";
            }
            read = gzread(input, buffer.data(), buffer.size());
            if (read <= 0) break;
            size_t begin = 0;
            while (begin < static_cast<size_t>(read)) {
                auto* newline = static_cast<char*>(std::memchr(buffer.data() + begin, '\n', read - begin));
                if (!newline) {
                    carry.append(buffer.data() + begin, read - begin);
                    if (carry.size() > MAX_LINE) throw std::runtime_error("partial line bound");
                    break;
                }
                const size_t stop = newline - buffer.data();
                if (carry.empty()) consume(std::string_view(buffer.data() + begin, stop - begin));
                else { carry.append(buffer.data() + begin, stop - begin); consume(carry); carry.clear(); }
                begin = stop + 1;
            }
        }
        if (read < 0 || !gzeof(input)) throw std::runtime_error("gzip EOF/CRC failure");
        if (!carry.empty()) consume(carry);
        if (!header) throw std::runtime_error("empty gzip source");
        if (gzclose(input) != Z_OK) throw std::runtime_error("gzip close/CRC failure");
        input = nullptr;
        size_t pm_symbols = 0;
        std::vector<std::string> names;
        for (const auto& [name, state] : symbols) {
            names.push_back(name);
            pm_symbols += !state.seen.empty();
        }
        std::sort(names.begin(), names.end());
        std::cout << "{\"source_rows_all_day\":" << rows << ",\"window_raw_prints\":" << pm_rows
                  << ",\"symbols_downloaded\":" << names.size() << ",\"window_printed_symbols\":" << pm_symbols
                  << ",\"first_sip_ns\":" << (rows ? minimum : 0) << ",\"last_sip_ns\":" << maximum
                  << ",\"first_window_sip_ns\":" << (pm_rows ? pm_minimum : 0)
                  << ",\"last_window_sip_ns\":" << pm_maximum << ",\"gzip_eof_crc_verified\":true"
                  << ",\"counts_per_second\":[";
        for (size_t i = 0; i < counts.size(); ++i) std::cout << (i ? "," : "") << counts[i];
        std::cout << "],\"downloaded_symbols\":[";
        for (size_t i = 0; i < names.size(); ++i) std::cout << (i ? "," : "") << quoted(names[i]);
        std::cout << "],\"template_actual_print\":";
        if (!have_template) std::cout << "null";
        else std::cout << "{\"symbol\":" << quoted(template_symbol) << ",\"sip_ns\":" << template_ns
                       << ",\"price\":" << quoted(template_price) << ",\"size\":" << quoted(template_size) << "}";
        std::cout << "}\n";
        return 0;
    } catch (const std::exception& error) {
        if (input) gzclose(input);
        std::cerr << error.what() << '\n';
        return 2;
    }
}
