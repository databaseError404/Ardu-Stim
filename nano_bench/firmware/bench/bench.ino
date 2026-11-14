// SPDX-License-Identifier: GPL-3.0-or-later
// Standalone bench adaptation inspired by Ardu-Stim (David J. Andruczyk
// and Speeduino contributors). Reference patterns are not a Voge calibration.
#include <Arduino.h>
#include <SPI.h>
#include <Wire.h>
#include <avr/interrupt.h>
#include <util/atomic.h>
#include <util/crc16.h>

#ifndef BENCH_UART_BAUD
#define BENCH_UART_BAUD 1000000UL
#endif
#if BENCH_UART_BAUD != 1000000UL && BENCH_UART_BAUD != 500000UL
#error "Use exact Nano baud 1000000 or 500000; 921600 has 8.5 percent error"
#endif

// Nano bench protocol v1. No EEPROM, boot-time device writes, or automatic
// crank start.
static const uint8_t VERSION = 1, MAX_PAYLOAD = 48, RX_CAP = 58, TX_CAP = 58;
enum {
  OK = 0,
  UNKNOWN_COMMAND = 1,
  BAD_LENGTH = 2,
  BAD_ARGUMENT = 3,
  UNKNOWN_DEVICE = 4,
  DEVICE_NOT_RESPONDING = 5,
  BUS_ERROR = 6,
  BUS_TIMEOUT = 7,
  BUSY = 8,
  NOT_CONFIGURED = 9
};
static uint8_t rx[RX_CAP], raw[56], tx[TX_CAP];
static uint8_t rxLen = 0, txLen = 0;
static uint8_t adcEventQueue[4][23], adcEventLen[4], adcEventHead = 0, adcEventTail = 0, adcEventCount = 0;
static bool rxDiscard = false, responsePending = false, scanActive = false;
static uint32_t rxLastUs = 0, rxErrors = 0, txDropped = 0, i2cErrors = 0;
static volatile uint32_t adcMissed = 0;
static uint8_t lastReq[56], lastReqLen = 0, lastReply[56], lastReplyLen = 0;
static bool cacheValid = false, spiInitialized = false;

static uint8_t adsAddr = 0x48, adsChannel = 0, adsGain = 0, adsRate = 7;
static uint8_t dacAddr[2] = {0x60, 0x61};
static uint16_t outputValue[3] = {0, 0, 0};
static bool outputKnown[3] = {false, false, false};
static uint32_t outputTime[3] = {0, 0, 0};
static uint8_t presence[4] = {0, 0, 3, 3};
static bool scanFinished = false;
static uint8_t scanAddresses[46], scanCount = 0, scanProbe = 0x08,
                                  scanRequest[56], scanRequestLen = 0;
static uint16_t scanSeq = 0;
static uint8_t scanCmd = 0, scanResultStatus = OK;
static bool adcReadActive = false, adcStream = false, adsConfigured = false,
            adcReadFinished = false, adcStartActive = false;
static uint8_t adcPendingMode = 0; // 0=single read, 1=stream start
static uint8_t adcReadStatus = OK;
static uint32_t adcStarted = 0, adcReadyTime = 0, adcCounter = 0;
static uint8_t adcRequest[56], adcRequestLen = 0;
static uint16_t adcSeq = 0;
static uint8_t adcCmd = 0;
static uint32_t adcStartStarted = 0, adcStartPollAt = 0;
// Built-in ADC supports one-shot and autonomous A6/A7 scheduling.
// AVCC, 125-kHz ADC clock; no ADC interrupt/free-running.
static uint8_t nativeChannel = 6, nativeHardwareChannel = 6, nativeLastChannel = 0xff;
static bool nativeActive = false, nativeFinished = false, nativeDiscard = false;
static bool nativeStream = false, nativeStreamConversion = false, nativeStreamLost = false;
static bool nativeCycleInProgress = false;
static uint8_t nativeStreamMask = 3, nativeStreamPhase = 0;
static uint32_t nativeStreamPeriod = 10000UL, nativeNextCycle = 0;
static uint8_t nativeStatus = OK;
static uint16_t nativeValue = 0;
static uint32_t nativeStarted = 0, nativeReadyTime = 0, nativeCounter = 0;
static int16_t adcValue = 0;
static uint8_t adcFlags = 0;
static volatile bool alertFlag = false, eventMissedPending = false;
static volatile uint8_t alertCoalesced = 0;
static volatile uint32_t alertTime = 0;
static bool alertAttached = false;
static uint32_t streamLastReady = 0;
static volatile bool crankRun = false, crankInvert = false;
static volatile uint16_t crankRpm = 0, crankBaseOcr = 0, crankGapOcr = 0;
static volatile uint8_t crankPendingClock = 0;
static volatile bool crankClockPending = false;
static volatile uint8_t crankPattern = 0, crankPhase = 0, crankHalfCount = 0;
static volatile bool crankOut = false;
static uint32_t adcPeriodUs();
static void queueAdcEvent(uint8_t device, uint8_t channel, uint32_t counter,
                          uint32_t ts, int16_t value, uint8_t flags);

static uint16_t crc16(const uint8_t *p, uint8_t n) {
  // avr-libc's optimized non-reflected 0x1021 update; init FFFF makes
  // this CRC-16/CCITT-FALSE, exactly the existing wire algorithm.
  uint16_t c = 0xffff;
  while (n--)
    c = _crc_xmodem_update(c, *p++);
  return c;
}
static uint8_t cobsEncode(const uint8_t *in, uint8_t n, uint8_t *out) {
  uint8_t codeAt = 0, op = 1, code = 1;
  out[0] = 0;
  while (op <= n) {
    uint8_t b = in[op - 1];
    if (!b) {
      out[codeAt] = code;
      codeAt = op;
      out[op] = 0;
      code = 1;
      op++;
    } else {
      out[op++] = b;
      code++;
      if (code == 0xff) {
        out[codeAt] = code;
        codeAt = op;
        out[op] = 0;
        code = 1;
      }
    }
  }
  out[codeAt] = code;
  out[op++] = 0;
  return op;
}
static int8_t cobsDecode(const uint8_t *in, uint8_t n, uint8_t *out) {
  if (!n)
    return -1;
  uint8_t ip = 0, op = 0;
  while (ip < n) {
    uint8_t code = in[ip++];
    if (!code || ip + (uint8_t)(code - 1) > n)
      return -1;
    uint8_t copy = (uint8_t)(code - 1);
    if ((uint16_t)op + copy + (code != 0xff && ip + copy < n ? 1 : 0) > 56)
      return -1;
    for (uint8_t i = 0; i < copy; i++)
      out[op++] = in[ip++];
    if (code != 0xff && ip < n)
      out[op++] = 0;
  }
  return op;
}
static void put16(uint8_t *p, uint16_t v) {
  p[0] = v;
  p[1] = v >> 8;
}
static void put32(uint8_t *p, uint32_t v) {
  p[0] = v;
  p[1] = v >> 8;
  p[2] = v >> 16;
  p[3] = v >> 24;
}
static uint16_t get16(const uint8_t *p) {
  return (uint16_t)p[0] | ((uint16_t)p[1] << 8);
}
static void putSigned16(uint8_t *p, int16_t v) { put16(p, (uint16_t)v); }
static void setResponse(uint16_t seq, uint8_t cmd, uint8_t status,
                        const uint8_t *data, uint8_t n) {
  if (n > MAX_PAYLOAD - 1)
    n = 0;
  raw[0] = VERSION;
  raw[1] = 2;
  put16(raw + 2, seq);
  raw[4] = cmd;
  raw[5] = n + 1;
  raw[6] = status;
  if (n)
    memcpy(raw + 7, data, n);
  uint16_t c = crc16(raw, 7 + n);
  put16(raw + 7 + n, c);
  txLen = cobsEncode(raw, 9 + n, tx);
  responsePending = true;
}
static void marker() { PIND = _BV(PD2); }
static uint8_t i2cWrite(uint8_t addr, const uint8_t *p, uint8_t n) {
  Wire.beginTransmission(addr);
  bool queued = true;
  for (uint8_t i = 0; i < n; i++)
    if (Wire.write(p[i]) != 1)
      queued = false;
  uint8_t e = Wire.endTransmission();
  if (!queued) {
    i2cErrors++;
    return BUS_ERROR;
  }
  if (Wire.getWireTimeoutFlag()) {
    Wire.clearWireTimeoutFlag();
    i2cErrors++;
    return BUS_TIMEOUT;
  }
  if (e == 2 || e == 3) {
    i2cErrors++;
    return DEVICE_NOT_RESPONDING;
  }
  if (e) {
    i2cErrors++;
    return BUS_ERROR;
  }
  return OK;
}
static uint8_t i2cRead(uint8_t addr, uint8_t reg, uint8_t *p, uint8_t n) {
  uint8_t status = i2cWrite(addr, &reg, 1);
  if (status != OK)
    return status;
  uint8_t got = Wire.requestFrom(addr, n);
  if (Wire.getWireTimeoutFlag()) {
    Wire.clearWireTimeoutFlag();
    i2cErrors++;
    while (Wire.available())
      Wire.read();
    return BUS_TIMEOUT;
  }
  if (got != n) {
    while (Wire.available())
      Wire.read();
    i2cErrors++;
    return DEVICE_NOT_RESPONDING;
  }
  for (uint8_t i = 0; i < n; i++) {
    if (!Wire.available()) {
      i2cErrors++;
      return BUS_ERROR;
    }
    p[i] = Wire.read();
  }
  return OK;
}
static void wireRecover() {
  Wire.end();
  Wire.begin();
  Wire.setClock(400000);
  Wire.setWireTimeout(3000, true);
}
static uint8_t adsWriteConfig(uint16_t cfg) {
  uint8_t b[3] = {1, (uint8_t)(cfg >> 8), (uint8_t)cfg};
  return i2cWrite(adsAddr, b, 3);
}
static void alertISR();
static uint16_t adsBaseConfig(bool single, bool readyAlert) {
  uint16_t mux = (uint16_t)(4 + adsChannel) << 12;
  return (uint16_t)((single ? 0x8100 : 0) | mux | ((uint16_t)adsGain << 9) |
                    ((uint16_t)adsRate << 5) | (readyAlert ? 0x0000 : 0x0003));
}
static uint8_t configureAdcReadyAlert() {
  uint8_t threshold[3] = {2, 0x00, 0x00}; // ADS1115 LO_TH register.
  uint8_t status = i2cWrite(adsAddr, threshold, sizeof(threshold));
  if (status != OK)
    return status;
  threshold[0] = 3; // HI_TH register.
  threshold[1] = 0x80;
  threshold[2] = 0x00;
  return i2cWrite(adsAddr, threshold, sizeof(threshold));
}

static void detachAdcAlert() {
  if (alertAttached) {
    detachInterrupt(digitalPinToInterrupt(3));
    alertAttached = false;
  }
  ATOMIC_BLOCK(ATOMIC_RESTORESTATE) { alertFlag = false; }
}

// First force the requested MUX/PGA/rate into single-shot power-down, then
// wait for OS=1 before starting the actual conversion or continuous stream.
static uint8_t beginAdcOperation(uint8_t mode) {
  detachAdcAlert();
  uint16_t idleConfig = (uint16_t)(adsBaseConfig(true, false) & 0x7fff);
  uint8_t status = adsWriteConfig(idleConfig); // MODE=1, OS=0.
  if (status != OK) {
    presence[1] = 0;
    wireRecover();
    return status;
  }
  adcPendingMode = mode;
  adcStartStarted = micros();
  // OS=1 is the ADS1115 hardware indication of idle. An old conversion
  // may take up to its previous configured period, but an already idle ADC
  // requires no fixed 130-ms delay. Wait asynchronously for OS below.
  adcStartPollAt = adcStartStarted;
  adcStartActive = true;
  adcReadFinished = false;
  cacheValid = false;
  return OK;
}

static void finishAdcStart(uint8_t status) {
  adcStartActive = false;
  adcReadStatus = status;
  adcReadFinished = true;
  if (status != OK) {
    detachAdcAlert();
    adcStream = false;
    presence[1] = 0;
    wireRecover();
  }
}

static void pollAdcStart() {
  if (!adcStartActive || (int32_t)(micros() - adcStartPollAt) < 0)
    return;
  adcStartPollAt = micros() + 100UL;

  uint8_t configBytes[2];
  uint8_t status = i2cRead(adsAddr, 1, configBytes, sizeof(configBytes));
  if (status != OK) {
    finishAdcStart(status);
    return;
  }
  uint32_t now = micros();
  if (!(configBytes[0] & 0x80)) {
    if ((uint32_t)(now - adcStartStarted) > 1000000UL)
      finishAdcStart(BUS_TIMEOUT);
    return;
  }

  status = configureAdcReadyAlert();
  if (status != OK) {
    finishAdcStart(status);
    return;
  }

  // Clear any edge from the previous conversion/configuration immediately
  // before arming the new conversion-ready interrupt.
  pinMode(3, INPUT_PULLUP);
  ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
    alertFlag = false;
    alertTime = 0;
    alertCoalesced = 0;
    eventMissedPending = false;
  }
  EIFR = _BV(INTF1); // Discard a latched edge from the previous operation.
  attachInterrupt(digitalPinToInterrupt(3), alertISR, FALLING);
  alertAttached = true;

  bool singleShot = (adcPendingMode == 0);
  status = adsWriteConfig(adsBaseConfig(singleShot, true));
  if (status != OK) {
    finishAdcStart(status);
    return;
  }

  presence[1] = 1;
  adsConfigured = true;
  if (singleShot) {
    adcStarted = micros();
    adcReadActive = true;
    adcFlags = 0;
    adcStartActive = false;
    return;
  }

  adcStream = true;
  streamLastReady = micros();
  adcStartActive = false;
  adcReadStatus = OK;
  adcReadFinished =
      true; // ADC_STREAM_START reply is deferred until config ACK.
}

static uint8_t adsReadFinish() {
  uint8_t b[2];
  uint8_t status = i2cRead(adsAddr, 0, b, 2);
  if (status != OK) {
    wireRecover();
    return status;
  }
  adcValue = (int16_t)((uint16_t)b[0] << 8 | b[1]);
  return OK;
}
static void alertISR() {
  if (alertFlag) {
    if (alertCoalesced < 255)
      alertCoalesced++;
    eventMissedPending = true;
  }
  alertTime = micros();
  alertFlag = true;
}
static uint8_t stopStream() {
  detachAdcAlert();
  adcStream = false;

  // Always command MODE=1, OS=0, including after a Nano reset when the ADS
  // may still be converting continuously. Never perform this write in setup.
  uint16_t idleConfig = (uint16_t)(adsBaseConfig(true, false) & 0x7fff);
  uint8_t status = adsWriteConfig(idleConfig);
  if (status != OK) {
    presence[1] = 0; // ADS physical configuration is unknown.
    wireRecover();
  }
  return status;
}

// Timer1 CTC: one interrupt per nominal half-tooth. The ISR is bounded.
ISR(TIMER1_COMPA_vect) {
  if (crankClockPending) {
    TCCR1B = _BV(WGM12) | crankPendingClock;
    crankClockPending = false;
  }
  OCR1A = crankBaseOcr;
  crankOut = !crankOut;
  if (crankOut)
    PORTB |= _BV(PB0);
  else
    PORTB &= ~_BV(PB0);
  if (crankInvert) {
    if (crankOut)
      PORTB &= ~_BV(PB1);
    else
      PORTB |= _BV(PB1);
  } else
    PORTB &= ~_BV(PB1);
  if (++crankHalfCount == 2) {
    crankHalfCount = 0;
    uint8_t teeth = (crankPattern == 0 ? 60 : (crankPattern == 1 ? 36 : 24));
    uint8_t missing = (crankPattern == 0 ? 2 : 1);
    if (++crankPhase >= teeth - missing)
      crankPhase = 0;
    if (crankPhase == 0)
      OCR1A = crankGapOcr;
  }
}
static bool crankTiming(uint16_t rpm, uint8_t *clockSelect, uint16_t *ocr,
                        uint16_t *gapOcr) {
  if (!rpm)
    return false;
  const uint16_t prescalers[5] = {1, 8, 64, 256, 1024};
  uint8_t teeth = (crankPattern == 0 ? 60 : (crankPattern == 1 ? 36 : 24));
  uint8_t missing = (crankPattern == 0 ? 2 : 1);
  for (uint8_t i = 0; i < 5; i++) {
    uint64_t numerator = (uint64_t)F_CPU * 30ULL;
    uint32_t denominator = (uint32_t)rpm * teeth * prescalers[i];
    uint32_t ticks = (uint32_t)((numerator + denominator / 2) / denominator);
    if (ticks < 1 || ticks > 65536UL)
      continue;
    uint32_t gapTicks = ticks * ((uint16_t)missing * 2U + 1U);
    if (gapTicks > 65536UL)
      continue;
    *clockSelect = (uint8_t)(i + 1);
    *ocr = (uint16_t)(ticks - 1);
    *gapOcr = (uint16_t)(gapTicks - 1);
    return true;
  }
  return false;
}
static void stopCrank() {
  uint8_t s = SREG;
  cli();
  TIMSK1 &= ~_BV(OCIE1A);
  crankClockPending = false;
  TCCR1A = 0;
  TCCR1B = 0;
  crankRun = false;
  crankRpm = 0;
  PORTB &= ~(_BV(PB0) | _BV(PB1));
  SREG = s;
}
static bool startCrank(uint16_t rpm) {
  if (!rpm) {
    stopCrank();
    return true;
  }
  if (rpm < 10 || rpm > 20000)
    return false;
  uint8_t cs;
  uint16_t base, gap;
  if (!crankTiming(rpm, &cs, &base, &gap))
    return false;
  uint8_t s = SREG;
  cli();
  if (!crankRun) {
    TCCR1A = 0;
    TCCR1B = 0;
    TCNT1 = 0;
    crankPhase = 0;
    crankHalfCount = 0;
    crankOut = false;
    PORTB &= ~(_BV(PB0) | _BV(PB1));
    TIFR1 = _BV(OCF1A);
    TIMSK1 |= _BV(OCIE1A);
    OCR1A = base;
  }
  crankBaseOcr = base;
  crankGapOcr = gap;
  if (crankRun) {
    // Timer prescaler changes only at the next compare boundary, preserving
    // the current tooth phase and avoiding a mid-interval timing discontinuity.
    crankPendingClock = cs;
    crankClockPending = true;
  } else {
    TCCR1B = _BV(WGM12) | cs;
    crankClockPending = false;
  }
  crankRun = true;
  crankRpm = rpm;
  SREG = s;
  return true;
}

static void beginNativeRead(const uint8_t *request, uint8_t size) {
  nativeStreamConversion = false;
  nativeHardwareChannel = nativeChannel;
  memcpy(adcRequest, request, size);
  adcRequestLen = size;
  adcSeq = get16(request + 2);
  adcCmd = request[4];
  cacheValid = false;
  nativeStatus = OK;
  nativeFinished = false;
  nativeDiscard = !(ADCSRA & _BV(ADEN)) || nativeLastChannel != nativeHardwareChannel;
  // ADC6/ADC7 are analog-only pins. Never touch GPIO, I2C or AREF externally.
  ADCSRB = 0;
  ADMUX = _BV(REFS0) | nativeHardwareChannel; // AVCC reference, right-adjusted.
  ADCSRA = _BV(ADEN) | _BV(ADIF) | _BV(ADPS2) | _BV(ADPS1) | _BV(ADPS0);
  nativeStarted = micros();
  nativeActive = true;
  ADCSRA |= _BV(ADSC);
}

static void startNativeConversion(uint8_t channel, bool streamConversion) {
  nativeHardwareChannel = channel;
  nativeStreamConversion = streamConversion;
  nativeDiscard = !(ADCSRA & _BV(ADEN)) || nativeLastChannel != channel;
  ADCSRB = 0;
  ADMUX = _BV(REFS0) | channel;
  ADCSRA = _BV(ADEN) | _BV(ADIF) | _BV(ADPS2) | _BV(ADPS1) | _BV(ADPS0);
  nativeStarted = micros();
  nativeActive = true;
  ADCSRA |= _BV(ADSC);
}

static void pollNativeRead() {
  if (!nativeActive)
    return;
  if (!(ADCSRA & _BV(ADSC))) {
    uint32_t ready = micros(); // Observed ready time, not ADC aperture/IRQ.
    uint8_t low = ADCL;
    uint16_t result = (uint16_t)low | ((uint16_t)ADCH << 8);
    if (nativeDiscard) {
      nativeDiscard = false;
      ADCSRA |= _BV(ADIF) | _BV(ADSC);
      return;
    }
    nativeValue = result;
    nativeReadyTime = ready;
    nativeStatus = result <= 1023 ? OK : BUS_ERROR;
    if (nativeStatus == OK) {
      nativeCounter++;
      nativeLastChannel = nativeHardwareChannel;
    }
    nativeActive = false;
    nativeFinished = true;
    if (nativeStreamConversion) {
      if (nativeStatus == OK) {
        uint8_t flags = nativeStreamLost ? 2 : 0;
        nativeStreamLost = false;
        queueAdcEvent(5, nativeHardwareChannel, nativeCounter, nativeReadyTime, (int16_t)nativeValue, flags);
      } else {
        adcMissed++;
        nativeStreamLost = true;
      }
      nativeStreamConversion = false;
      nativeFinished = false;
      nativeStreamPhase++;
      if (nativeStreamPhase >= ((nativeStreamMask == 3) ? 2 : 1)) { nativeStreamPhase = 0; nativeCycleInProgress = false; }
    }
  } else if ((uint32_t)(micros() - nativeStarted) > 10000UL) {
    ADCSRA &= ~_BV(ADEN);
    nativeStatus = BUS_TIMEOUT;
    nativeLastChannel = 0xff;
    nativeActive = false;
    nativeFinished = true;
    if (nativeStreamConversion) {
      adcMissed++;
      nativeStreamLost = true;
      nativeStreamConversion = false;
      nativeFinished = false;
      nativeStreamPhase++;
      if (nativeStreamPhase >= ((nativeStreamMask == 3) ? 2 : 1)) { nativeStreamPhase = 0; nativeCycleInProgress = false; }
    }
  }
}

static void serviceNativeStream() {
  if (!nativeStream) return;
  uint32_t now = micros();
  if ((int32_t)(now - nativeNextCycle) >= 0) {
    uint32_t late = now - nativeNextCycle;
    uint32_t cycles = late / nativeStreamPeriod + 1UL;
    uint8_t channels = nativeStreamMask == 3 ? 2 : 1;
    // Keep the phase, discard missed cycles, and never generate a catch-up burst.
    uint32_t skipped = nativeCycleInProgress ? cycles : cycles - 1UL;
    if (skipped) {
      adcMissed += skipped * channels;
      nativeStreamLost = true;
    }
    nativeNextCycle += cycles * nativeStreamPeriod;
    if (!nativeCycleInProgress) {
      nativeCycleInProgress = true;
      nativeStreamPhase = 0;
    }
  }
  if (nativeCycleInProgress && !nativeActive && !nativeStreamConversion) {
    uint8_t channel = nativeStreamMask == 1 ? 6 : nativeStreamMask == 2 ? 7 : (nativeStreamPhase == 0 ? 6 : 7);
    startNativeConversion(channel, true);
  }
}

static void finishNativeRead() {
  if (!nativeFinished || responsePending)
    return;
  uint8_t data[13], size = 0;
  if (nativeStatus == OK) {
    data[0] = 5;
    data[1] = nativeChannel;
    put32(data + 2, nativeCounter);
    put32(data + 6, nativeReadyTime);
    put16(data + 10, nativeValue);
    data[12] = 0; // Polling: no per-conversion ADC interrupt.
    size = 13;
  }
  setResponse(adcSeq, adcCmd, nativeStatus, size ? data : 0, size);
  memcpy(lastReq, adcRequest, adcRequestLen);
  lastReqLen = adcRequestLen;
  memcpy(lastReply, raw, 9 + size);
  lastReplyLen = 9 + size;
  cacheValid = true;
  nativeFinished = false;
}

static uint8_t deviceKind(uint8_t id) {
  return id == 1 ? 1 : id == 2 ? 2 : (id == 3 || id == 4) ? 3 : id == 5 ? 4 : 0;
}
static uint8_t devicePresence(uint8_t id) {
  if (id == 5)
    return 4; // Integrated MCU peripheral; no bus discovery.
  if (id == 1)
    return 0;
  if (id >= 2 && id <= 4)
    return presence[id - 1];
  return 0;
}
static uint8_t deviceConfig(uint8_t id, uint8_t *out) {
  if (id == 5) {
    out[0] = nativeChannel;
    return 1;
  }
  if (id == 1)
    return 0;
  if (id == 2) {
    out[0] = adsAddr;
    out[1] = adsChannel;
    out[2] = adsGain;
    out[3] = adsRate;
    return 4;
  }
  if (id == 3 || id == 4) {
    out[0] = dacAddr[id - 3];
    return 1;
  }
  return 255;
}
static void handleRequest(uint8_t *p, uint8_t n) {
  uint8_t req[56];
  memcpy(req, p, n);
  p = req;
  uint8_t type = p[1], cmd = p[4], len = p[5];
  uint16_t seq = get16(p + 2);
  uint8_t *a = p + 6;
  uint8_t out[48], on = 0, st = OK;
  if (type != 1 || !seq) {
    rxErrors++;
    return;
  }
  if (cacheValid && seq == get16(lastReq + 2)) {
    if (n == lastReqLen && !memcmp(p, lastReq, n)) {
      memcpy(raw, lastReply, lastReplyLen);
      txLen = cobsEncode(raw, lastReplyLen, tx);
      responsePending = true;
      return;
    }
    setResponse(seq, cmd, BAD_ARGUMENT, 0, 0);
    return;
  }
  if (scanActive || adcStartActive || adcReadActive || scanFinished ||
      adcReadFinished || nativeFinished || (nativeActive && !nativeStreamConversion)) {
    uint8_t *pending = scanActive || scanFinished ? scanRequest : adcRequest;
    uint8_t pendingLen = scanActive || scanFinished ? scanRequestLen : adcRequestLen;
    if (n == pendingLen && !memcmp(p, pending, n)) return;
    if (seq == get16(pending + 2)) setResponse(seq, cmd, BAD_ARGUMENT, 0, 0);
    else setResponse(seq, cmd, BUSY, 0, 0);
    return;
  }
  switch (cmd) {
  case 0x01:
    if (len)
      st = BAD_LENGTH;
    else {
      out[0] = 0;
      out[1] = 3;
      out[2] = 0;
      put32(out + 3, 0xff);
      out[7] = 48;
      out[8] = 5;
      on = 9;
    }
    break;
  case 0x02:
    if (len)
      st = BAD_LENGTH;
    else {
      uint16_t rpm;
      uint32_t missed;
      bool running;
      ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
        rpm = crankRpm;
        running = crankRun;
        missed = adcMissed;
      }
      put32(out, micros());
      out[4] = running;
      put16(out + 5, rpm);
      out[7] = (adcStream || nativeStream);
      put32(out + 8, rxErrors);
      put32(out + 12, txDropped);
      put32(out + 16, missed);
      put32(out + 20, i2cErrors);
      on = 24;
    }
    break;
  case 0x03:
    if (len)
      st = BAD_LENGTH;
    else {
      out[0] = 5;
      uint8_t k = 1;
      for (uint8_t id = 1; id <= 5; id++) {
        out[k++] = id;
        out[k++] = deviceKind(id);
        out[k++] = id == 5 ? 0 : (id == 1 ? 1 : 2);
        out[k++] = (id == 1 || id == 5) ? 0 : (id == 2 ? adsAddr : dacAddr[id - 3]);
        put16(out + k, id == 1 ? 255 : (id == 2 ? 32767 : (id == 5 ? 1023 : 4095)));
        k += 2;
        out[k++] = devicePresence(id);
      }
      on = k;
    }
    break;
  case 0x04:
    if (len)
      st = BAD_LENGTH;
    else if (adcStream || scanActive)
      st = BUSY;
    else {
      scanActive = true;
      scanFinished = false;
      scanProbe = 8;
      scanCount = 0;
      presence[1] = (adsConfigured ? 2 : 0);
      presence[2] = 2;
      presence[3] = 2;
    }
    break;
  case 0x05:
    if (len != 1)
      st = BAD_LENGTH;
    else if (!deviceKind(a[0]))
      st = UNKNOWN_DEVICE;
    else {
      out[0] = a[0];
      on = 1;
      on += deviceConfig(a[0], out + on);
    }
    break;
  case 0x06: {
    if (len < 1) {
      st = BAD_LENGTH;
      break;
    }
    uint8_t id = a[0];
    if (!deviceKind(id)) {
      st = UNKNOWN_DEVICE;
      break;
    }
    if (id == 5) {
      if (len != 2) {
        st = BAD_LENGTH;
        break;
      }
      if (nativeStream || nativeActive) { st = BUSY; break; }
      if (a[1] != 6 && a[1] != 7) {
        st = BAD_ARGUMENT;
        break;
      }
      nativeChannel = a[1];
      out[0] = 5;
      out[1] = nativeChannel;
      on = 2;
      break;
    }
    if (adcStream || adcReadActive || scanActive) {
      st = BUSY;
      break;
    }
    if (id == 1) {
      if (len != 1)
        st = BAD_LENGTH;
    } else if (id == 2) {
      if (len != 5) {
        st = BAD_LENGTH;
        break;
      }
      if (a[1] < 0x48 || a[1] > 0x4b || a[2] > 3 || a[3] > 5 || a[4] > 7) {
        st = BAD_ARGUMENT;
        break;
      }
      for (uint8_t j = 3; j <= 4; j++)
        if (a[1] == dacAddr[j - 3]) {
          st = BAD_ARGUMENT;
          break;
        }
      if (st != OK)
        break;
      adsAddr = a[1];
      adsChannel = a[2];
      adsGain = a[3];
      adsRate = a[4];
      presence[1] = 0;
    } else {
      if (len != 2) {
        st = BAD_LENGTH;
        break;
      }
      uint8_t ix = id - 3, v = a[1];
      if (v < 0x60 || v > 0x67 || v == dacAddr[1 - ix] || v == adsAddr) {
        st = BAD_ARGUMENT;
        break;
      }
      dacAddr[ix] = v;
      presence[id - 1] = 3;
    }
    if (st == OK) {
      out[0] = id;
      on = 1 + deviceConfig(id, out + 1);
    }
  } break;
  case 0x10:
    if (len != 3)
      st = BAD_LENGTH;
    else {
      uint8_t id = a[0];
      uint16_t v = get16(a + 1);
      if (id != 1 && id != 3 && id != 4)
        st = (id == 2 ? BAD_ARGUMENT : UNKNOWN_DEVICE);
      else if ((id == 1 && v > 255) || (id != 1 && v > 4095))
        st = BAD_ARGUMENT;
      else {
        uint8_t status = OK;
        if (id == 1) {
          if (!spiInitialized) {
            digitalWrite(10, HIGH);
            pinMode(10, OUTPUT);
            SPI.begin();
            spiInitialized = true;
          }
          // Nano maximum SCLK=F_CPU/2; below TPL0501-100's 25-MHz limit.
          SPI.beginTransaction(SPISettings(8000000, MSBFIRST, SPI_MODE0));
          digitalWrite(10, LOW);
          SPI.transfer((uint8_t)v);
          digitalWrite(10, HIGH);
          SPI.endTransaction();
        } else {
          uint8_t b[2] = {(uint8_t)(v >> 8), (uint8_t)v};
          status = i2cWrite(dacAddr[id - 3], b, 2);
          if (status != OK)
            wireRecover();
        }
        if (status != OK)
          st = status;
        else {
          uint8_t ix = (id == 1 ? 0 : id - 2);
          outputValue[ix] = v;
          outputKnown[ix] = true;
          outputTime[ix] = micros();
          out[0] = id;
          put16(out + 1, v);
          put32(out + 3, outputTime[ix]);
          on = 7;
          marker();
        }
      }
    }
    break;
  case 0x11:
    if (len != 1)
      st = BAD_LENGTH;
    else if (a[0] != 1 && a[0] != 3 && a[0] != 4)
      st = (a[0] == 2 ? BAD_ARGUMENT : UNKNOWN_DEVICE);
    else {
      uint8_t i = a[0] == 1 ? 0 : a[0] - 2;
      out[0] = a[0];
      out[1] = outputKnown[i];
      put16(out + 2, outputValue[i]);
      put32(out + 4, outputTime[i]);
      on = 8;
    }
    break;
  case 0x20:
    if (len != 1)
      st = BAD_LENGTH;
    else if (a[0] == 5) {
      if (nativeStream) st = BUSY;
      else { beginNativeRead(p, n); return; }
    } else if (a[0] != 2)
      st = UNKNOWN_DEVICE;
    else if (adcStream || adcStartActive || adcReadActive || scanActive)
      st = BUSY;
    else {
      memcpy(adcRequest, p, n);
      adcRequestLen = n;
      adcSeq = seq;
      adcCmd = cmd;
      adcPendingMode = 0;
      st = beginAdcOperation(adcPendingMode);
      if (st == OK)
        return;
    }
    break;
  case 0x21:
    if (len != 1 && len != 6) st = BAD_LENGTH;
    else if (a[0] == 5) {
      uint8_t mask = len == 1 ? nativeStreamMask : a[1];
      uint32_t period = len == 1 ? nativeStreamPeriod : ((uint32_t)a[2] | ((uint32_t)a[3] << 8) | ((uint32_t)a[4] << 16) | ((uint32_t)a[5] << 24));
      if (len == 6 && (mask < 1 || mask > 3 || period < 1000UL || period > 10000000UL)) st = BAD_ARGUMENT;
      else if (nativeStream || nativeActive || nativeFinished || adcReadActive || adcReadFinished) st = BUSY;
      else {
        nativeStreamMask = mask; nativeStreamPeriod = period; nativeStream = true;
        // Keep any unreported loss across STOP/START until the next event.
        nativeStreamPhase = 0; nativeCycleInProgress = false; nativeNextCycle = micros();
        out[0] = 5; on = 1;
      }
    } else if (a[0] != 2)
      st = UNKNOWN_DEVICE;
    else if (len != 1)
      st = BAD_LENGTH;
    else if (scanActive || adcStartActive || adcReadActive || adcStream)
      st = BUSY;
    else {
      memcpy(adcRequest, p, n);
      adcRequestLen = n;
      adcSeq = seq;
      adcCmd = cmd;
      adcPendingMode = 1;
      st = beginAdcOperation(adcPendingMode);
      if (st == OK)
        return;
    }
    break;
  case 0x22:
    if (len != 1) st = BAD_LENGTH;
    else if (a[0] == 5) {
      nativeStream = false;
      nativeCycleInProgress = false;
      if (nativeActive && nativeStreamConversion) {
        ADCSRA &= ~_BV(ADEN);
        nativeActive = false; nativeFinished = false; nativeStreamConversion = false;
        nativeLastChannel = 0xff;
      }
      out[0] = 5; on = 1;
    } else if (a[0] != 2) st = UNKNOWN_DEVICE;
    else { st = stopStream(); if (st == OK) { out[0] = 2; on = 1; } }
    break;
  case 0x30:
    if (len != 2)
      st = BAD_LENGTH;
    else if (a[0] > 2 || a[1] > 1)
      st = BAD_ARGUMENT;
    else if (crankRun)
      st = BUSY;
    else {
      crankPattern = a[0];
      crankInvert = a[1];
      out[0] = a[0];
      out[1] = a[1];
      on = 2;
    }
    break;
  case 0x31:
    if (len != 2)
      st = BAD_LENGTH;
    else {
      uint16_t rpm = get16(a);
      if (rpm && (rpm < 10 || rpm > 20000))
        st = BAD_ARGUMENT;
      else if (!startCrank(rpm))
        st = BAD_ARGUMENT;
      else {
        put16(out, rpm);
        on = 2;
      }
    }
    break;
  case 0x32:
    if (len)
      st = BAD_LENGTH;
    else
      stopCrank();
    break;
  default:
    st = UNKNOWN_COMMAND;
    break;
  }
  if (cmd == 0x04 && scanActive) {
    memcpy(scanRequest, p, n);
    scanRequestLen = n;
    cacheValid = false;
    scanSeq = seq;
    scanCmd = cmd;
    return;
  }
  setResponse(seq, cmd, st, st == OK ? out : 0, st == OK ? on : 0);
  memcpy(lastReq, p, n);
  lastReqLen = n;
  memcpy(lastReply, raw, 9 + (st == OK ? on : 0));
  lastReplyLen = 9 + (st == OK ? on : 0);
  cacheValid = true;
}
static void queueAdcEvent(uint8_t device, uint8_t channel, uint32_t counter,
                          uint32_t ts, int16_t value, uint8_t flags) {
  if (adcEventCount >= 4) {
    txDropped++; adcMissed++;
    if (device == 5) nativeStreamLost = true; else eventMissedPending = true;
    return;
  }
  raw[0] = VERSION; raw[1] = 3; put16(raw + 2, 0); raw[4] = 0xa0; raw[5] = 13;
  raw[6] = device; raw[7] = channel; put32(raw + 8, counter); put32(raw + 12, ts);
  putSigned16(raw + 16, value); raw[18] = flags;
  put16(raw + 19, crc16(raw, 19));
  adcEventLen[adcEventHead] = cobsEncode(raw, 21, adcEventQueue[adcEventHead]);
  adcEventHead = (adcEventHead + 1) & 3; adcEventCount++;
}
static void emitAdcEvent(uint32_t ts, int16_t value, uint8_t flags) {
  bool missed = false;
  ATOMIC_BLOCK(ATOMIC_RESTORESTATE) { missed = eventMissedPending; eventMissedPending = false; }
  if (missed) flags |= 2;
  queueAdcEvent(2, adsChannel, adcCounter, ts, value, flags);
}
static void processScan() {
  if (!scanActive)
    return;
  if (scanProbe > 0x77) {
    scanActive = false;
    scanFinished = true;
    scanResultStatus = OK;
    return;
  }
  uint8_t address = scanProbe++;
  Wire.beginTransmission(address);
  uint8_t e = Wire.endTransmission();
  if (Wire.getWireTimeoutFlag()) {
    Wire.clearWireTimeoutFlag();
    i2cErrors++;
    wireRecover();
    scanResultStatus = BUS_TIMEOUT;
    scanActive = false;
    scanFinished = true;
    return;
  }
  if (e == 0) {
    if (address == adsAddr)
      presence[1] = 1;
    for (uint8_t i = 0; i < 2; i++)
      if (address == dacAddr[i])
        presence[2 + i] = 1;
    if (scanCount >= 46) {
      scanResultStatus = BAD_ARGUMENT;
      scanActive = false;
      scanFinished = true;
      return;
    }
    scanAddresses[scanCount++] = address;
  }
}
static void finishScan() {
  if (!scanFinished || responsePending)
    return;
  uint8_t data[48], n = 0;
  uint8_t status = scanResultStatus;
  if (status == OK) {
    data[0] = scanCount;
    memcpy(data + 1, scanAddresses, scanCount);
    n = scanCount + 1;
  }
  setResponse(scanSeq, scanCmd, status, status == OK ? data : 0, n);
  memcpy(lastReq, scanRequest, scanRequestLen);
  lastReqLen = scanRequestLen;
  memcpy(lastReply, raw, 9 + n);
  lastReplyLen = 9 + n;
  cacheValid = true;
  scanFinished = false;
}
static uint32_t adcPeriodUs() {
  static const uint32_t periods[8] = {125000UL, 62500UL, 31250UL, 15625UL,
                                      7813UL,   4000UL,  2106UL,  1163UL};
  return periods[adsRate];
}
static void finishAdcRead() {
  if (!adcReadFinished || responsePending)
    return;
  uint8_t data[13], n = 0, status = adcReadStatus;
  if (alertAttached && (adcPendingMode == 0 || status != OK))
    detachAdcAlert();
  if (status == OK && adcPendingMode == 1) {
    data[0] = 2;
    n = 1;
  } else if (status == OK) {
    data[0] = 2;
    data[1] = adsChannel;
    put32(data + 2, adcCounter);
    put32(data + 6, adcReadyTime);
    putSigned16(data + 10, adcValue);
    data[12] = adcFlags;
    n = 13;
  }
  setResponse(adcSeq, adcCmd, status, status == OK ? data : 0, n);
  memcpy(lastReq, adcRequest, adcRequestLen);
  lastReqLen = adcRequestLen;
  memcpy(lastReply, raw, 9 + n);
  lastReplyLen = 9 + n;
  cacheValid = true;
  adcReadFinished = false;
  adcPendingMode = 0;
}
static void pollAdcRead() {
  if (!adcReadActive)
    return;
  uint8_t cfg[2];
  uint8_t status = i2cRead(adsAddr, 1, cfg, 2);
  if (status != OK) {
    adcReadActive = false;
    adcReadFinished = true;
    adcReadStatus = status;
    presence[1] = 0;
    wireRecover();
    return;
  }
  if (cfg[0] & 0x80) {
    bool fromAlert = false;
    uint32_t ts = 0;
    ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
      if (alertFlag) {
        fromAlert = true;
        ts = alertTime;
        alertFlag = false;
      }
    }
    if (!fromAlert)
      ts = micros();
    adcReadyTime = ts;
    adcFlags = fromAlert ? 1 : 0;
    status = adsReadFinish();
    adcReadActive = false;
    adcReadFinished = true;
    adcReadStatus = status;
    if (status == OK) {
      adcCounter++;
      presence[1] = 1;
    } else {
      presence[1] = 0;
      wireRecover();
    }
  } else if ((uint32_t)(micros() - adcStarted) > 1000000UL) {
    adcReadActive = false;
    adcReadFinished = true;
    adcReadStatus = BUS_TIMEOUT;
    presence[1] = 0;
  }
}

void setup() {
  pinMode(8, OUTPUT);
  digitalWrite(8, LOW);
  pinMode(9, OUTPUT);
  digitalWrite(9, LOW);
  digitalWrite(10, HIGH);
  pinMode(10, OUTPUT);
  pinMode(2, OUTPUT);
  digitalWrite(2, LOW);
  pinMode(3, INPUT_PULLUP);
  DDRB |= _BV(PB0) | _BV(PB1);
  PORTB &= ~(_BV(PB0) | _BV(PB1));
  Serial.begin(BENCH_UART_BAUD);
  Wire.begin();
  Wire.setClock(400000);
  Wire.setWireTimeout(3000, true);
}
void loop() {
  uint32_t now = micros();
  uint8_t coalesced;
  ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
    coalesced = alertCoalesced;
    alertCoalesced = 0;
  }
  adcMissed += coalesced;
  if (rxLen && (uint32_t)(now - rxLastUs) > 20000UL) {
    rxLen = 0;
    rxDiscard = true;
    rxErrors++;
  }
  // Bounded byte budget: sustained host traffic cannot starve ADC/crank
  // service.
  for (uint8_t budget = 0; budget < 16 && Serial.available(); budget++) {
    uint8_t b = Serial.read();
    rxLastUs = micros();
    if (rxDiscard) {
      if (!b)
        rxDiscard = false;
      continue;
    }
    if (!b) {
      if (rxLen) {
        int8_t decoded = cobsDecode(rx, rxLen, raw);
        rxLen = 0;
        if (decoded < 8 || raw[0] != VERSION || raw[1] != 1 ||
            raw[5] > MAX_PAYLOAD || decoded != 8 + raw[5] ||
            get16(raw + decoded - 2) != crc16(raw, decoded - 2))
          rxErrors++;
        else if (responsePending)
          rxErrors++;
        else
          handleRequest(raw, (uint8_t)decoded);
      }
      continue;
    }
    if (rxLen >= RX_CAP) {
      rxLen = 0;
      rxDiscard = true;
      rxErrors++;
    } else
      rx[rxLen++] = b;
  }
  pollNativeRead();
  serviceNativeStream();
  pollAdcStart();
  pollAdcRead();
  if (adcStream) {
    uint32_t t = micros();
    bool ready = false;
    uint32_t sampleTime = 0;
    ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
      if (alertFlag) {
        ready = true;
        sampleTime = alertTime;
        alertFlag = false;
      }
    }
    uint32_t period = adcPeriodUs();
    // Continuous-mode OS cannot identify a new conversion. Require D3 ALERT;
    // on watchdog expiry stop once and report the lost samples as missed.
    if (!ready && (uint32_t)(t - streamLastReady) > period * 4UL + 100000UL) {
      adcStream = false;
      detachAdcAlert();
      presence[1] = 0;
      adcMissed++;
      stopStream();
    } else if (ready) {
      uint8_t value[2];
      uint8_t status = i2cRead(adsAddr, 0, value, 2);
      if (status == OK) {
        adcValue = (int16_t)((uint16_t)value[0] << 8 | value[1]);
        adcCounter++;
        presence[1] = 1;
        streamLastReady = sampleTime;
        emitAdcEvent(streamLastReady, adcValue, 1);
      } else {
        presence[1] = (status == DEVICE_NOT_RESPONDING ? 2 : presence[1]);
        wireRecover();
        adcStream = false;
        detachAdcAlert();
        presence[1] = 0;
        adcMissed++;
        stopStream();
      }
    }
  }
  if (scanActive)
    processScan();
  // Deliver response frames atomically when they fit. Responses always precede
  // events.
  if (responsePending && txLen && Serial.availableForWrite() >= txLen) {
    Serial.write(tx, txLen);
    txLen = 0;
    responsePending = false;
  }
  if (!responsePending) {
    finishScan();
    finishAdcRead();
    finishNativeRead();
  }
  if (!responsePending && adcEventCount) {
    uint8_t n = adcEventLen[adcEventTail];
    if (Serial.availableForWrite() >= n) {
      Serial.write(adcEventQueue[adcEventTail], n);
      adcEventTail = (adcEventTail + 1) & 3; adcEventCount--;
    }
  }
}
