// Minimal, dependency-free HX711 driver for the Arduino UNO Q (Zephyr core).
//
// The HX711 is a 24-bit ADC built for load cells. It is read by clocking
// 24 bits out of DOUT on the rising edges of SCK, then 1-3 extra pulses that
// select the gain/channel for the NEXT conversion (1 pulse = channel A, gain 128).
//
// Timing note: if SCK stays HIGH for more than ~60 us the chip powers down,
// so each bit is clocked with interrupts briefly disabled.

#pragma once
#include <Arduino.h>

class HX711Lite {
public:
  HX711Lite(uint8_t doutPin, uint8_t sckPin) : _dout(doutPin), _sck(sckPin) {}

  void begin() {
    pinMode(_sck, OUTPUT);
    pinMode(_dout, INPUT_PULLUP);   // pulled up so a missing module reads "not ready"
    digitalWrite(_sck, LOW);        // LOW keeps the chip awake
  }

  // DOUT goes LOW when a fresh conversion is ready (10 or 80 samples/s
  // depending on the module's RATE pin).
  bool isReady() const { return digitalRead(_dout) == LOW; }

  // Reads one signed 24-bit sample. Call only when isReady() is true.
  int32_t read() {
    uint32_t value = 0;
    for (uint8_t i = 0; i < 24; i++) {
      value = (value << 1) | clockBit();
    }
    clockBit();  // 25th pulse: channel A, gain 128 for the next conversion

    // Sign-extend the 24-bit two's-complement result to 32 bits.
    if (value & 0x800000UL) {
      value |= 0xFF000000UL;
    }
    return static_cast<int32_t>(value);
  }

  void powerDown() {
    digitalWrite(_sck, LOW);
    digitalWrite(_sck, HIGH);
    delayMicroseconds(70);
  }

  void powerUp() { digitalWrite(_sck, LOW); }

private:
  uint8_t clockBit() {
    noInterrupts();
    digitalWrite(_sck, HIGH);
    delayMicroseconds(1);
    uint8_t bit = digitalRead(_dout) == HIGH ? 1 : 0;
    digitalWrite(_sck, LOW);
    interrupts();
    delayMicroseconds(1);
    return bit;
  }

  uint8_t _dout;
  uint8_t _sck;
};
