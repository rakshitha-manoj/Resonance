#pragma once
// Abstract broadcast transport. On hardware: ESP-NOW broadcast. In netsim: the simulated medium.
// Destination filtering is done by the receiver from the id in the packet.
#include <stdint.h>
#include <stddef.h>

namespace net {

class Transport {
public:
    virtual ~Transport() {}
    // Transmit one frame (len <= MAX_PAYLOAD) to everyone in range. now_us is the caller's clock.
    // Returns true if the frame was handed to the radio, false if the send failed (queue full,
    // driver error). It does NOT mean the frame was received. Callers count failures.
    virtual bool broadcast(const uint8_t *buf, size_t len, uint64_t now_us) = 0;
};

}  // namespace net
