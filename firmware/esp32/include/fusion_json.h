#pragma once
// JSON-lines event log, schema 3 (see .claude/STRUCTURE.md "JSON-lines event log"; line types meta, restart, sample, begin/change/status, node, damage). ONE formatter shared by the PC simulator
// (netsim --events-jsonl) and the ESP32 master (net_main.cpp), so both write byte-identical lines.
// Plain C++11, no Arduino, no heap, no stdio, no locale: every number is formatted by hand so the output does not depend on
// the platform's printf (exponent digits, locale decimal point, %g rules).
//
// Number format: unsigned integers in decimal; floats as fixed decimal with 7 significant digits (the precision of a float32;
// 7 minus the integer digits as fractional digits, 7 fractional digits below 1), trailing zeros removed, rounded half up
// (3.0 -> 3, 0.5 -> 0.5, 75.19999695f -> 75.2, 123456.789f -> 123456.8, 0.703380466f -> 0.7033805); never an exponent;
// NaN, +-inf and |v| >= 4e18 -> null; -0 and values that round to zero -> 0. A negative ratio never reaches the writer: r < 0 means "unknown" and callers print null (see ratio()).
// Lines end in "\n". Every *_line() returns the length written (including the newline) or 0 if the buffer is too small
// (nothing usable is left in it then).
#include <stdint.h>
#include <stddef.h>
#include "fusion.h"

namespace fusion {
namespace json {

constexpr int SCHEMA = 3;
constexpr size_t JSON_LINE_MAX = 448;   // a buffer of this size always fits any line below

class Writer {
public:
    Writer(char *buf, size_t cap) : b_(buf), cap_(cap), n_(0), ok_(true) {}
    bool ok() const { return ok_; }
    size_t size() const { return n_; }

    Writer &lit(const char *s) { while (*s) put(*s++); return *this; }
    Writer &ch(char c) { put(c); return *this; }
    Writer &boolean(bool v) { return lit(v ? "true" : "false"); }
    Writer &null() { return lit("null"); }
    Writer &u64(uint64_t v) {
        char t[20];
        int k = 0;
        do { t[k++] = (char)('0' + (int)(v % 10)); v /= 10; } while (v);
        while (k) put(t[--k]);
        return *this;
    }
    Writer &i64(int64_t v) {
        if (v < 0) { put('-'); return u64((uint64_t)0 - (uint64_t)v); }
        return u64((uint64_t)v);
    }
    // Fixed decimal, 7 significant digits, trailing zeros dropped; see the header comment.
    Writer &num(double v) {
        if (!(v == v) || v - v != 0.0 || v >= 4e18 || v <= -4e18) return null();
        bool neg = v < 0.0;
        double a = neg ? -v : v;
        uint64_t ip = (uint64_t)a;
        int idig = 1;
        for (uint64_t t = ip; t >= 10; t /= 10) idig++;
        int dec = ip == 0 ? 7 : (idig >= 7 ? 0 : 7 - idig);
        uint64_t scale = 1;
        for (int k = 0; k < dec; k++) scale *= 10;
        double fr = a - (double)ip;
        uint64_t f = (uint64_t)(fr * (double)scale + 0.5);
        if (f >= scale) { ip++; f -= scale; }
        if (neg && (ip != 0 || f != 0)) put('-');
        u64(ip);
        if (f) {
            put('.');
            char d[8];
            for (int k = dec - 1; k >= 0; k--) { d[k] = (char)('0' + (int)(f % 10)); f /= 10; }
            int last = dec - 1;
            while (last > 0 && d[last] == '0') last--;
            for (int k = 0; k <= last; k++) put(d[k]);
        }
        return *this;
    }
    // Ratio: r < 0 (or NaN) = unknown -> null.
    Writer &ratio(float r) { return (r >= 0.0f) ? num((double)r) : null(); }
    // AE ratio: like ratio() but shown at most AE_SHOW_MAX (a ratio that large is an alarm anyway; it keeps every line short).
    Writer &ae(float r) { return (r >= 0.0f) ? num((double)(r > 1e9f ? 1e9f : r)) : null(); }
    // Optional float (f1 in Hz, drop): NaN / inf = unknown -> null (num() does that).
    Writer &opt(float v) { return num((double)v); }
    // [a,b] of the above
    Writer &ae2(const float *v) { ch('['); ae(v[0]); ch(','); ae(v[1]); return ch(']'); }
    Writer &opt2(const float *v) { ch('['); opt(v[0]); ch(','); opt(v[1]); return ch(']'); }
    // JSON string with escaping: " \ and control characters (< 0x20) as \" \\ \n \r \t \b \f or \u00XX. Bytes >= 0x80 pass through.
    Writer &str(const char *s) {
        put('"');
        for (; *s; s++) {
            unsigned char c = (unsigned char)*s;
            if (c == '"') lit("\\\"");
            else if (c == '\\') lit("\\\\");
            else if (c == '\n') lit("\\n");
            else if (c == '\r') lit("\\r");
            else if (c == '\t') lit("\\t");
            else if (c == '\b') lit("\\b");
            else if (c == '\f') lit("\\f");
            else if (c < 0x20) {
                static const char hex[] = "0123456789abcdef";
                lit("\\u00").ch(hex[c >> 4]).ch(hex[c & 15]);
            } else put((char)c);
        }
        put('"');
        return *this;
    }
    Writer &key(const char *k) { put('"'); lit(k); lit("\":"); return *this; }
    // Terminates with '\n' (and a NUL that is not counted). Returns the line length, or 0 on overflow.
    size_t finish() {
        put('\n');
        if (!ok_ || n_ >= cap_) return 0;
        b_[n_] = '\0';
        return n_;
    }

private:
    void put(char c) {
        if (n_ + 1 < cap_) b_[n_++] = c;   // keep one byte for the NUL
        else ok_ = false;
    }
    char *b_;
    size_t cap_, n_;
    bool ok_;
};

struct Meta {
    const char *source;     // "netsim" | "master" (escaped when written)
    Config cfg;
    int floor1, floor2;     // physical floor of slave 1 / 2
    uint32_t epoch;         // master boot epoch (0 = unknown -> null)
    bool samples;           // this log contains per-superframe "sample" lines
};

static inline size_t meta_line(char *buf, size_t cap, const Meta &m) {
    Writer w(buf, cap);
    char rs[2] = {m.cfg.rule, 0};
    w.lit("{\"type\":\"meta\",\"schema\":").u64(SCHEMA);
    w.lit(",\"source\":").str(m.source ? m.source : "");
    w.lit(",\"rule\":").str(rs);
    // "T" is always the threshold of the std ratio r (what a reader draws on the r chart): rule f: T_b; its AE threshold is "T_ae".
    w.lit(",\"T\":").num((double)(m.cfg.rule == 'f' ? m.cfg.T_b : m.cfg.T));
    if (m.cfg.rule == 'f') w.lit(",\"T_ae\":").num((double)m.cfg.T).lit(",\"gate\":").num((double)m.cfg.gate);
    w.lit(",\"use_raw\":").boolean(m.cfg.use_raw);
    w.lit(",\"debounce\":").u64(m.cfg.debounce);
    w.lit(",\"commission_windows\":").u64(m.cfg.comm_n);
    w.lit(",\"status_every_sf\":").u64(m.cfg.status_every);
    w.lit(",\"dmg_delta\":").num((double)DMG_DELTA).lit(",\"dmg_p\":").u64((uint64_t)DMG_P);
    w.lit(",\"superframe_s\":").num((double)net::SUPERFRAME_US / 1e6);
    w.lit(",\"floors\":[").i64(m.floor1).ch(',').i64(m.floor2).ch(']');
    w.lit(",\"epoch\":");
    if (m.epoch) w.u64(m.epoch); else w.null();
    w.lit(",\"samples\":").boolean(m.samples);
    w.ch('}');
    return w.finish();
}

static inline const char *kind_name(uint8_t k) { return k == EV_BEGIN ? "begin" : k == EV_CHANGE ? "change" : "status"; }

static inline void alive_pair(Writer &w, uint8_t mask) {
    w.lit("\"alive\":[").boolean((mask & 1) != 0).ch(',').boolean((mask & 2) != 0).ch(']');
}

static inline size_t event_line(char *buf, size_t cap, const Event &e) {
    Writer w(buf, cap);
    w.lit("{\"type\":\"").lit(kind_name(e.kind)).lit("\",\"t_us\":").u64(e.t_us);
    w.lit(",\"sf\":").u64(e.sf);
    w.lit(",\"class\":").str(cls_name(e.cls));
    w.lit(",\"slave\":").u64(e.slave);
    w.lit(",\"degraded_alive\":").u64(e.degraded);
    w.lit(",\"r1\":").ratio(e.r[0]);
    w.lit(",\"r2\":").ratio(e.r[1]);
    w.lit(",\"ae\":").ae2(e.ae).lit(",\"f1\":").opt2(e.f1).lit(",\"drop\":").opt2(e.drop).lit(",\"dmg\":").u64(e.dmg ? 1 : 0);
    w.lit(",\"base1\":").num((double)e.base[0]);
    w.lit(",\"base2\":").num((double)e.base[1]);
    w.ch(','); alive_pair(w, e.alive_mask);
    w.lit(",\"closed\":").u64(e.closed);
    w.lit(",\"events\":").u64(e.n_events);
    w.lit(",\"commission_n\":[").u64(e.commission_n[0]).ch(',').u64(e.commission_n[1]).ch(']');
    w.ch('}');
    return w.finish();
}

static inline size_t sample_line(char *buf, size_t cap, const Sample &s) {
    Writer w(buf, cap);
    w.lit("{\"type\":\"sample\",\"t_us\":").u64(s.t_us);
    w.lit(",\"sf\":").u64(s.sf);
    w.lit(",\"class\":").str(cls_name(s.cls));
    w.lit(",\"slave\":").u64(s.slave);
    w.lit(",\"degraded_alive\":").u64(s.degraded);
    w.lit(",\"r1\":").ratio(s.r[0]);
    w.lit(",\"r2\":").ratio(s.r[1]);
    w.lit(",\"ae\":").ae2(s.ae).lit(",\"f1\":").opt2(s.f1).lit(",\"drop\":").opt2(s.drop).lit(",\"dmg\":").u64(s.dmg ? 1 : 0);
    w.ch(','); alive_pair(w, s.alive_mask);
    w.lit(",\"age\":[");
    for (int i = 0; i < 2; i++) {
        if (i) w.ch(',');
        if (s.age_sf[i] >= 0) w.u64((uint64_t)s.age_sf[i]); else w.null();
    }
    w.ch(']');
    w.lit(",\"commission_n\":[").u64(s.commission_n[0]).ch(',').u64(s.commission_n[1]).ch(']');
    w.lit(",\"closed\":").u64(s.closed);
    w.ch('}');
    return w.finish();
}

// prev_epoch 0 = unknown (first boot seen by the host) -> null.
static inline size_t restart_line(char *buf, size_t cap, uint64_t t_us, uint32_t epoch, uint32_t prev_epoch) {
    Writer w(buf, cap);
    w.lit("{\"type\":\"restart\",\"t_us\":").u64(t_us);
    w.lit(",\"epoch\":");
    if (epoch) w.u64(epoch); else w.null();
    w.lit(",\"prev_epoch\":");
    if (prev_epoch) w.u64(prev_epoch); else w.null();
    w.ch('}');
    return w.finish();
}

// Immediate slave health change (schema 2, additive): emitted when the master's DEAD / ALIVE status of a slave changes, without
// waiting for the next periodic status line. `sf` = the master's current superframe at the change. Fusion decisions do not depend on it.
static inline size_t node_line(char *buf, size_t cap, uint64_t t_us, uint32_t sf, uint8_t slave, bool alive) {
    Writer w(buf, cap);
    w.lit("{\"type\":\"node\",\"t_us\":").u64(t_us);
    w.lit(",\"sf\":").u64(sf);
    w.lit(",\"slave\":").u64(slave);
    w.lit(",\"state\":").lit(alive ? "\"ALIVE\"" : "\"DEAD\"");
    w.ch('}');
    return w.finish();
}

// DAMAGE begin / end (schema 3). drop = pooled f1 drop of the triggering superframe, f1 = each slave's estimate in it (null = not
// usable), f1_0 = each slave's commissioned f1 (derived on the master from its last usable report, null = never).
static inline size_t damage_line(char *buf, size_t cap, const DamageEvent &d) {
    Writer w(buf, cap);
    w.lit("{\"type\":\"damage\",\"t_us\":").u64(d.t_us);
    w.lit(",\"sf\":").u64(d.sf);
    w.lit(",\"state\":").lit(d.begin ? "\"BEGIN\"" : "\"END\"");
    w.lit(",\"drop\":").opt(d.drop);
    w.lit(",\"f1\":").opt2(d.f1).lit(",\"f1_0\":").opt2(d.f1_0);
    w.ch('}');
    return w.finish();
}

// Sink that formats every event and (optionally) every sample as a JSON line and hands it to write_line().
// write_line returns false if the line could not be written without blocking: it is then counted as dropped, never retried.
class JsonLineSink : public Sink {
public:
    JsonLineSink() : samples(true), lines(0), dropped(0), sample_lines(0), sample_dropped(0), overflow(0) {}
    bool samples;
    uint32_t lines, dropped, sample_lines, sample_dropped, overflow;   // lines/sample_lines count lines written

    void on_event(const Event &e) override {
        char b[JSON_LINE_MAX];
        size_t n = event_line(b, sizeof b, e);
        emit(b, n, false);
    }
    void on_sample(const Sample &s) override {
        if (!samples) return;
        char b[JSON_LINE_MAX];
        size_t n = sample_line(b, sizeof b, s);
        emit(b, n, true);
    }
    void on_damage(const DamageEvent &d) override {
        char b[JSON_LINE_MAX];
        size_t n = damage_line(b, sizeof b, d);
        emit(b, n, false);
    }
    // Writes one "node" line (slave DEAD / ALIVE); goes through the same counters as events.
    void node(uint64_t t_us, uint32_t sf, uint8_t slave, bool alive) {
        char b[JSON_LINE_MAX];
        emit(b, node_line(b, sizeof b, t_us, sf, slave, alive), false);
    }
    // For meta/restart lines written by the host code through the same path and counters.
    void emit(const char *b, size_t n, bool is_sample) {
        if (n == 0) { overflow++; dropped++; if (is_sample) sample_dropped++; return; }
        if (write_line(b, n, is_sample)) { lines++; if (is_sample) sample_lines++; }
        else { dropped++; if (is_sample) sample_dropped++; }
    }

protected:
    virtual bool write_line(const char *line, size_t len, bool is_sample) = 0;
};

}  // namespace json
}  // namespace fusion
