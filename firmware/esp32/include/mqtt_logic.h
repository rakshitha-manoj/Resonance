#pragma once
// Platform-free logic of the MQTT uplink (src/net_main.cpp, -DMQTT_ENABLED). No Arduino, no heap, C++11,
// unit tested on the PC (test/test_mqtt_logic, env mqtt_logic).
//   * topic mapping: a JSON line {"type":"<t>",...} goes to  resonance/<site>/<t>
//       - any type is routed by its own name (future types such as "damage" need no change here);
//       - the leaf names "state" and "status" are reserved (retained decision / online-offline LWT), so a line of
//         that type goes to "<t>_line" (the fusion `status` line therefore lands on .../status_line);
//       - a line without a usable type goes to .../other;
//       - retained  resonance/<site>/state   = the latest decision line (sample / begin / change / status);
//       - retained  resonance/<site>/status  = "online" (published after every connect), LWT "offline";
//       - meta is published retained so late subscribers learn the configuration at once; meta is ALSO
//         re-published after every (re)connect (needed when the retained copy was lost or the broker is a new one).
//   * LineQueue<DEPTH>: fixed-size queue of complete lines, drop-oldest-SAMPLE-first, drop counters.
//   * Backoff / Link: reconnect state machine with exponential back-off and a cap.
//   * Bridge<DEPTH>: queue + meta keeper + publishing against an abstract Io (PubSubClient on the ESP32,
//     a fake in the unit tests).
#include <stdint.h>
#include <stddef.h>
#include <string.h>

namespace mqtt {

constexpr size_t LINE_CAP = 448;   // = fusion::json::JSON_LINE_MAX (static_assert'ed in net_main.cpp)
constexpr size_t SITE_MAX = 32;
constexpr size_t TYPE_MAX = 24;
constexpr size_t TOPIC_MAX = 16 + SITE_MAX + 1 + TYPE_MAX + 8;

// ---------------------------------------------------------------- topics
static inline bool char_ok(char c, bool allow_upper) {
    return (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '-' || c == '_' || (allow_upper && c >= 'A' && c <= 'Z');
}

// Site id: 1..SITE_MAX chars of [a-z0-9_-] (nothing that is special in an MQTT topic: no '/', '+', '#').
static inline bool site_ok(const char *s) {
    if (!s) return false;
    size_t n = strlen(s);
    if (n < 1 || n > SITE_MAX) return false;
    for (size_t i = 0; i < n; i++)
        if (!char_ok(s[i], false)) return false;
    return true;
}

// Extracts the value of the first "type":"..." of a JSON line into out (NUL terminated). False if the line is not a
// JSON object, has no type, or the type is empty / longer than TYPE_MAX / contains anything but [A-Za-z0-9_-].
static inline bool json_type(const char *line, size_t len, char *out, size_t cap) {
    static const char key[] = "\"type\":\"";
    const size_t klen = sizeof(key) - 1;
    if (!line || !out || cap < 2 || len < klen + 3 || line[0] != '{') return false;
    for (size_t i = 1; i + klen < len; i++) {
        if (memcmp(line + i, key, klen) != 0) continue;
        size_t j = i + klen, n = 0;
        while (j < len && line[j] != '"') {
            if (n >= TYPE_MAX || n + 1 >= cap || !char_ok(line[j], true)) return false;
            out[n++] = line[j++];
        }
        if (j >= len || n == 0) return false;
        out[n] = 0;
        return true;
    }
    return false;
}

enum Kind : uint8_t { K_OTHER = 0, K_META, K_RESTART, K_SAMPLE, K_EVENT, K_NODE, K_UNKNOWN_TYPE };

static inline Kind kind_of(const char *type) {
    if (!strcmp(type, "meta")) return K_META;
    if (!strcmp(type, "restart")) return K_RESTART;
    if (!strcmp(type, "sample")) return K_SAMPLE;
    if (!strcmp(type, "begin") || !strcmp(type, "change") || !strcmp(type, "status")) return K_EVENT;
    if (!strcmp(type, "node")) return K_NODE;
    return K_UNKNOWN_TYPE;
}

struct Route {
    char leaf[TYPE_MAX + 8];  // topic leaf name
    Kind kind;
    bool retain;       // the line itself is published retained (meta)
    bool to_state;     // also publish retained to .../state (the decision lines)
};

// Classifies a line. Never fails: unusable lines get leaf "other".
static inline Route route_of(const char *line, size_t len) {
    Route r;
    char type[TYPE_MAX + 1];
    if (!json_type(line, len, type, sizeof type)) {
        strcpy(r.leaf, "other");
        r.kind = K_OTHER;
        r.retain = r.to_state = false;
        return r;
    }
    r.kind = kind_of(type);
    r.retain = r.kind == K_META;
    r.to_state = r.kind == K_SAMPLE || (r.kind == K_EVENT);
    strcpy(r.leaf, type);
    if (!strcmp(type, "state") || !strcmp(type, "status")) strcat(r.leaf, "_line");
    return r;
}

// "resonance/<site>/<leaf>"; returns the length, 0 if the site is invalid or the buffer too small.
static inline size_t make_topic(char *out, size_t cap, const char *site, const char *leaf) {
    if (!out || !leaf || !site_ok(site)) return 0;
    static const char pre[] = "resonance/";
    size_t sl = strlen(site), ll = strlen(leaf), n = (sizeof(pre) - 1) + sl + 1 + ll;
    if (ll == 0 || n + 1 > cap) return 0;
    memcpy(out, pre, sizeof(pre) - 1);
    memcpy(out + sizeof(pre) - 1, site, sl);
    out[sizeof(pre) - 1 + sl] = '/';
    memcpy(out + sizeof(pre) + sl, leaf, ll);
    out[n] = 0;
    return n;
}

// ---------------------------------------------------------------- queue
struct QueueStats {
    uint32_t pushed;           // lines accepted into the queue
    uint32_t popped;           // lines taken out for publishing
    uint32_t dropped_samples;  // sample lines lost (evicted by a newer line, or refused when only non-samples are queued)
    uint32_t dropped_other;    // non-sample lines lost (the queue held nothing but non-samples)
    uint32_t too_long;         // refused: longer than LINE_CAP or empty
};

// Fixed-size FIFO of complete lines (no heap). When full:
//   * the OLDEST queued sample is evicted to make room (samples are a stream, a missing one costs nothing);
//   * if no sample is queued, a new sample is refused (counted), a new non-sample evicts the oldest line.
// Events / node / meta / restart / unknown types therefore survive as long as there is any sample to sacrifice.
template <size_t DEPTH>
class LineQueue {
    static_assert(DEPTH >= 2 && DEPTH <= 255, "queue depth 2..255");

public:
    LineQueue() { clear(); }
    void clear() {
        head_ = count_ = 0;
        memset(&st_, 0, sizeof(st_));
    }
    size_t size() const { return count_; }
    size_t capacity() const { return DEPTH; }
    bool empty() const { return count_ == 0; }
    const QueueStats &stats() const { return st_; }

    bool push(const char *line, size_t len, Kind kind) {
        if (!line || len == 0 || len > LINE_CAP) { st_.too_long++; return false; }
        bool is_sample = kind == K_SAMPLE;
        if (count_ == DEPTH) {
            int victim = oldest_sample();
            if (victim >= 0) {
                remove_at((size_t)victim);
                st_.dropped_samples++;
            } else if (is_sample) {
                st_.dropped_samples++;
                return false;
            } else {
                remove_at(0);
                st_.dropped_other++;
            }
        }
        Slot &s = slot((head_ + count_) % DEPTH);
        memcpy(s.data, line, len);
        s.len = (uint16_t)len;
        s.kind = kind;
        count_++;
        st_.pushed++;
        return true;
    }

    // Oldest line without removing it.
    bool peek(const char **line, size_t *len, Kind *kind) const {
        if (count_ == 0) return false;
        const Slot &s = slots_[head_];
        *line = s.data;
        *len = s.len;
        *kind = s.kind;
        return true;
    }
    void pop() {
        if (count_ == 0) return;
        head_ = (head_ + 1) % DEPTH;
        count_--;
        st_.popped++;
    }
    bool contains(Kind k) const {
        for (size_t i = 0; i < count_; i++)
            if (slots_[(head_ + i) % DEPTH].kind == k) return true;
        return false;
    }

private:
    struct Slot {
        uint16_t len;
        Kind kind;
        char data[LINE_CAP];
    };
    Slot &slot(size_t i) { return slots_[i]; }
    int oldest_sample() const {
        for (size_t i = 0; i < count_; i++)
            if (slots_[(head_ + i) % DEPTH].kind == K_SAMPLE) return (int)i;
        return -1;
    }
    // Removes the i-th oldest entry, keeping the order of the others (shifts the younger ones down).
    void remove_at(size_t i) {
        for (size_t k = i; k + 1 < count_; k++) slots_[(head_ + k) % DEPTH] = slots_[(head_ + k + 1) % DEPTH];
        count_--;
    }
    Slot slots_[DEPTH];
    size_t head_, count_;
    QueueStats st_;
};

// ---------------------------------------------------------------- reconnect back-off
constexpr uint32_t BACKOFF_BASE_MS = 1000;
constexpr uint32_t BACKOFF_CAP_MS = 60000;
constexpr uint32_t WIFI_JOIN_TIMEOUT_MS = 20000;

// Exponential back-off: after n consecutive failures the next attempt is min(cap, base << (n-1)) later
// (1, 2, 4, ... s, then the cap). Optional jitter spreads simultaneous reconnects: pass a random 16-bit
// value, up to +25% is added (never above the cap). success() resets.
class Backoff {
public:
    explicit Backoff(uint32_t base_ms = BACKOFF_BASE_MS, uint32_t cap_ms = BACKOFF_CAP_MS)
        : base_(base_ms ? base_ms : 1), cap_(cap_ms < base_ms ? base_ms : cap_ms), fails_(0), next_(0) {}
    uint32_t delay_for(uint32_t fails) const {
        if (fails == 0) return 0;
        uint32_t d = base_;
        for (uint32_t i = 1; i < fails && d < cap_; i++) d = d > cap_ / 2 ? cap_ : d * 2;
        return d > cap_ ? cap_ : d;
    }
    // Records a failure at now_ms and returns the delay until the next attempt.
    uint32_t failure(uint32_t now_ms, uint16_t jitter16 = 0) {
        if (fails_ < 1000) fails_++;
        uint32_t d = delay_for(fails_);
        d += (uint32_t)(((uint64_t)(d / 4) * jitter16) / 65535u);
        if (d > cap_) d = cap_;
        next_ = now_ms + d;
        return d;
    }
    void success() { fails_ = 0; next_ = 0; }
    bool due(uint32_t now_ms) const { return fails_ == 0 || (int32_t)(now_ms - next_) >= 0; }
    uint32_t fails() const { return fails_; }

private:
    uint32_t base_, cap_, fails_, next_;
};

// Connection supervisor: tells the host what to do next, from the observed Wi-Fi / MQTT state. The host does the
// I/O (WiFi.begin, PubSubClient.connect) and reports the outcome with mqtt_result().
enum Action : uint8_t { ACT_NONE = 0, ACT_WIFI_BEGIN, ACT_MQTT_CONNECT };

class Link {
public:
    Link() : was_up_(false), wifi_waiting_(false), wifi_deadline_(0), connects_(0), drops_(0) {}
    uint32_t connects() const { return connects_; }   // successful MQTT connects
    uint32_t drops() const { return drops_; }          // connections lost after being up
    uint32_t mqtt_fails() const { return mqtt_.fails(); }
    uint32_t wifi_fails() const { return wifi_.fails(); }

    Action poll(uint32_t now_ms, bool wifi_up, bool mqtt_up, uint16_t jitter16 = 0) {
        if (mqtt_up && wifi_up) {
            was_up_ = true;
            wifi_waiting_ = false;
            wifi_.success();
            return ACT_NONE;
        }
        if (was_up_) {  // lost a working connection: first retry is one base delay away
            was_up_ = false;
            drops_++;
            mqtt_.failure(now_ms, jitter16);
        }
        if (!wifi_up) {
            if (wifi_waiting_) {
                if ((int32_t)(now_ms - wifi_deadline_) < 0) return ACT_NONE;  // still joining
                wifi_waiting_ = false;
                wifi_.failure(now_ms, jitter16);                               // join timed out
            }
            if (!wifi_.due(now_ms)) return ACT_NONE;
            wifi_waiting_ = true;
            wifi_deadline_ = now_ms + WIFI_JOIN_TIMEOUT_MS;
            return ACT_WIFI_BEGIN;
        }
        wifi_waiting_ = false;
        wifi_.success();
        if (!mqtt_.due(now_ms)) return ACT_NONE;
        return ACT_MQTT_CONNECT;
    }
    // Outcome of an ACT_MQTT_CONNECT.
    void mqtt_result(uint32_t now_ms, bool ok, uint16_t jitter16 = 0) {
        if (ok) { mqtt_.success(); was_up_ = true; connects_++; }
        else mqtt_.failure(now_ms, jitter16);
    }

private:
    Backoff mqtt_, wifi_;
    bool was_up_, wifi_waiting_;
    uint32_t wifi_deadline_, connects_, drops_;
};

// ---------------------------------------------------------------- publishing
class Io {
public:
    virtual ~Io() {}
    // False = the connection failed (the caller treats the link as down and retries the same line later).
    virtual bool publish(const char *topic, const uint8_t *payload, size_t len, bool retain) = 0;
};

struct BridgeStats {
    uint32_t published;       // lines delivered to the broker (state copies and status not counted)
    uint32_t state_published;
    uint32_t meta_republished;
    uint32_t publish_fail;
    uint32_t bad_site;
};

template <size_t DEPTH>
class Bridge {
public:
    Bridge() : meta_len_(0), have_meta_(false) { site_[0] = 0; memset(&st_, 0, sizeof(st_)); }

    bool set_site(const char *site) {
        if (!site_ok(site)) return false;
        strcpy(site_, site);
        return true;
    }
    const char *site() const { return site_; }
    LineQueue<DEPTH> &queue() { return q_; }
    const LineQueue<DEPTH> &queue() const { return q_; }
    const BridgeStats &stats() const { return st_; }

    // Takes one complete JSON line. A trailing LF / CR LF (the formatter in fusion_json.h terminates every line
    // with one) is not part of the MQTT payload: one message = one line. Meta lines are also remembered for re-publishing.
    bool push(const char *line, size_t len) {
        while (line && len > 0 && (line[len - 1] == 10 || line[len - 1] == 13)) len--;
        Route r = route_of(line, len);
        if (r.kind == K_META && len <= LINE_CAP) {
            memcpy(meta_, line, len);
            meta_len_ = (uint16_t)len;
            have_meta_ = true;
        }
        return q_.push(line, len, r.kind);
    }

    // Call right after (re)connecting: publishes "online" (retained) and, if no meta is waiting in the queue,
    // the last meta again, so a subscriber that joins later (or a fresh broker) learns the configuration.
    bool on_connected(Io &io) {
        char t[TOPIC_MAX];
        if (!make_topic(t, sizeof t, site_, "status")) { st_.bad_site++; return false; }
        static const uint8_t on[] = {'o', 'n', 'l', 'i', 'n', 'e'};
        if (!io.publish(t, on, sizeof on, true)) { st_.publish_fail++; return false; }
        if (have_meta_ && !q_.contains(K_META)) {
            if (!publish_line(io, meta_, meta_len_, route_of(meta_, meta_len_))) return false;
            st_.meta_republished++;
        }
        return true;
    }

    // Publishes queued lines (oldest first), at most max_lines. A failed publish keeps the line queued and returns
    // false (the link is down). Returns true if the queue was drained or max_lines were sent.
    bool pump(Io &io, size_t max_lines = 8) {
        for (size_t n = 0; n < max_lines; n++) {
            const char *line;
            size_t len;
            Kind k;
            if (!q_.peek(&line, &len, &k)) return true;
            Route r = route_of(line, len);
            if (!publish_line(io, line, len, r)) return false;
            q_.pop();
            st_.published++;
        }
        return true;
    }

    // "online"/"offline" payloads are for the status topic; the LWT is configured by the host with these.
    bool status_topic(char *out, size_t cap) const { return make_topic(out, cap, site_, "status") != 0; }

private:
    bool publish_line(Io &io, const char *line, size_t len, const Route &r) {
        char t[TOPIC_MAX];
        if (!make_topic(t, sizeof t, site_, r.leaf)) { st_.bad_site++; return false; }
        if (!io.publish(t, (const uint8_t *)line, len, r.retain)) { st_.publish_fail++; return false; }
        if (r.to_state) {
            char ts[TOPIC_MAX];
            make_topic(ts, sizeof ts, site_, "state");
            if (!io.publish(ts, (const uint8_t *)line, len, true)) { st_.publish_fail++; return false; }
            st_.state_published++;
        }
        return true;
    }

    char site_[SITE_MAX + 1];
    LineQueue<DEPTH> q_;
    char meta_[LINE_CAP];
    uint16_t meta_len_;
    bool have_meta_;
    BridgeStats st_;
};

}  // namespace mqtt
