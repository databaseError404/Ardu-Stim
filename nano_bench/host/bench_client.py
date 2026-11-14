"""Standard-library Nano v1 UART client. Importing this module never opens hardware."""
from __future__ import annotations

import collections
import enum
import secrets
import struct
import time
from dataclasses import dataclass

VERSION = 1
MAX_PAYLOAD = 48
MAX_RAW = 56
MAX_WIRE = 58
HEADER = struct.Struct("<BBHBB")
ADC_STRUCT = struct.Struct("<BBIIhB")


class FrameType(enum.IntEnum):
    REQUEST = 1
    RESPONSE = 2
    EVENT = 3


class Status(enum.IntEnum):
    OK = 0
    UNKNOWN_COMMAND = 1
    BAD_LENGTH = 2
    BAD_ARGUMENT = 3
    UNKNOWN_DEVICE = 4
    DEVICE_NOT_RESPONDING = 5
    BUS_ERROR = 6
    BUS_TIMEOUT = 7
    BUSY = 8
    NOT_CONFIGURED = 9


class Command(enum.IntEnum):
    HELLO = 1
    GET_STATUS = 2
    LIST_DEVICES = 3
    I2C_SCAN = 4
    GET_DEVICE_CONFIG = 5
    SET_DEVICE_CONFIG = 6
    SET_OUTPUT = 0x10
    GET_OUTPUT = 0x11
    ADC_READ = 0x20
    ADC_STREAM_START = 0x21
    ADC_STREAM_STOP = 0x22
    CRANK_CONFIG = 0x30
    CRANK_SET_RPM = 0x31
    CRANK_STOP = 0x32


@dataclass(frozen=True)
class Frame:
    type: int
    seq: int
    command: int
    payload: bytes


@dataclass(frozen=True)
class Reply:
    command: int
    seq: int
    status: Status
    payload: bytes


@dataclass(frozen=True)
class Hello:
    fw_major: int
    fw_minor: int
    fw_patch: int
    capabilities: int
    max_payload: int
    device_count: int


@dataclass(frozen=True)
class Device:
    id: int
    kind: int
    bus: int
    address: int
    max_code: int
    presence: int


@dataclass(frozen=True)
class DeviceConfig:
    id: int
    config: bytes


@dataclass(frozen=True)
class OutputState:
    id: int
    known: bool
    last_value: int
    last_applied_us: int


@dataclass(frozen=True)
class ADCResult:
    id: int
    channel: int
    sample_number: int
    timestamp_us: int
    value: int
    flags: int


@dataclass(frozen=True)
class BoardStatus:
    uptime_us: int
    crank_running: bool
    rpm: int
    stream_running: bool
    rx_errors: int
    tx_dropped: int
    adc_missed: int
    i2c_errors: int


class ProtocolError(Exception):
    """Malformed or internally inconsistent protocol data."""


class RemoteError(ProtocolError):
    def __init__(self, status: Status):
        self.status = status
        super().__init__(status.name)


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def cobs_encode(data: bytes) -> bytes:
    out = bytearray([0])
    code_position = 0
    code = 1
    for byte in data:
        if byte == 0:
            out[code_position] = code
            code_position = len(out)
            out.append(0)
            code = 1
        else:
            out.append(byte)
            code += 1
            if code == 0xFF:
                out[code_position] = code
                code_position = len(out)
                out.append(0)
                code = 1
    out[code_position] = code
    return bytes(out)


def cobs_decode(data: bytes) -> bytes:
    if not data:
        raise ProtocolError("empty COBS frame")
    out = bytearray()
    index = 0
    while index < len(data):
        code = data[index]
        if code == 0:
            raise ProtocolError("zero COBS code")
        index += 1
        block_end = index + code - 1
        if block_end > len(data):
            raise ProtocolError("truncated COBS block")
        out.extend(data[index:block_end])
        index = block_end
        if code != 0xFF and index < len(data):
            out.append(0)
    return bytes(out)


def encode_frame(frame: Frame) -> bytes:
    payload = bytes(frame.payload)
    if len(payload) > MAX_PAYLOAD or not 0 <= frame.seq <= 0xFFFF:
        raise ValueError("frame range")
    raw = HEADER.pack(VERSION, int(frame.type), frame.seq, int(frame.command), len(payload)) + payload
    raw += struct.pack("<H", crc16(raw))
    return cobs_encode(raw) + b"\0"


def decode_frame(wire: bytes) -> Frame:
    if wire.endswith(b"\0"):
        wire = wire[:-1]
    raw = cobs_decode(wire)
    if len(raw) < 8 or len(raw) > MAX_RAW:
        raise ProtocolError("invalid raw frame size")
    version, frame_type, seq, command, payload_length = HEADER.unpack_from(raw)
    if version != VERSION:
        raise ProtocolError("unsupported protocol version")
    if frame_type not in (FrameType.REQUEST, FrameType.RESPONSE, FrameType.EVENT):
        raise ProtocolError("invalid frame type")
    if payload_length > MAX_PAYLOAD or len(raw) != 6 + payload_length + 2:
        raise ProtocolError("invalid payload length")
    if crc16(raw[:-2]) != struct.unpack_from("<H", raw, len(raw) - 2)[0]:
        raise ProtocolError("CRC mismatch")
    if frame_type == FrameType.EVENT and seq != 0:
        raise ProtocolError("event sequence must be zero")
    if frame_type != FrameType.EVENT and seq == 0:
        raise ProtocolError("request/response sequence must be nonzero")
    return Frame(frame_type, seq, command, raw[6:-2])


def nano_raw_to_volts(raw_value: int, vref: float = 5.0) -> float:
    """Convert a 10-bit Nano ADC code using an assumed reference voltage."""
    import math
    if isinstance(raw_value, bool) or not isinstance(raw_value, int) or not 0 <= raw_value <= 1023:
        raise ValueError("Nano ADC code must be an integer in 0..1023")
    if not isinstance(vref, (int, float)) or not math.isfinite(vref) or vref <= 0:
        raise ValueError("vref must be positive and finite")
    return raw_value * float(vref) / 1024.0


def raw_to_volts(raw_value: int, gain: int) -> float:
    """Convert signed ADS1115 code to volts using PGA code 0..5."""
    full_scales = (6.144, 4.096, 2.048, 1.024, 0.512, 0.256)
    if not -32768 <= raw_value <= 32767 or not 0 <= gain < len(full_scales):
        raise ValueError("raw value or gain outside ADS1115 range")
    return raw_value * full_scales[gain] / 32768.0


class BenchClient:
    def __init__(self, port=None, baud=1000000, *, transport=None, timeout=1.0,
                 scan_timeout=2.0, retries=1, event_limit=128, boot_wait=2.0,
                 reset_on_open=True):
        if event_limit < 1 or retries < 0:
            raise ValueError("event_limit must be positive and retries nonnegative")
        self.port, self.baud, self.transport = port, baud, transport
        self.timeout, self.scan_timeout = timeout, scan_timeout
        self.retries, self.boot_wait = retries, boot_wait
        self.reset_on_open = reset_on_open
        self.seq = secrets.randbelow(65535) + 1
        self.events = collections.deque(maxlen=event_limit)
        self.event_drops = 0
        self._opened = False
        self._rx = bytearray()
        self._discard = False

    def open(self):
        if self._opened:
            return self
        self._rx.clear()
        self._discard = False
        self.events.clear()
        if self.transport is None:
            import serial
            if self.reset_on_open:
                self.transport = serial.Serial(self.port, self.baud, timeout=0.02, write_timeout=1)
            else:
                # Best effort: some drivers briefly assert DTR during open.
                # A hardware-disabled autoreset is needed for a strict guarantee.
                self.transport = serial.Serial(port=None, baudrate=self.baud,
                                               timeout=0.02, write_timeout=1)
                self.transport.dtr = False
                self.transport.rts = False
                self.transport.port = self.port
                self.transport.open()
        else:
            opener = getattr(self.transport, "open", None)
            if opener and not getattr(self.transport, "is_open", True):
                opener()
        self._opened = True
        if self.boot_wait:
            time.sleep(self.boot_wait)
        return self

    def close(self):
        if self._opened:
            try:
                self.transport.close()
            finally:
                self._opened = False
                self._rx.clear()
                self._discard = False
                self.events.clear()

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()

    def _read_frame(self, deadline):
        while time.monotonic() < deadline:
            chunk = self.transport.read(1)
            if not chunk:
                continue
            byte = chunk[0]
            if byte == 0:
                if self._discard:
                    self._discard = False
                    self._rx.clear()
                elif self._rx:
                    encoded = bytes(self._rx)
                    self._rx.clear()
                    try:
                        return decode_frame(encoded)
                    except ProtocolError:
                        pass
            elif not self._discard:
                self._rx.append(byte)
                if len(self._rx) > MAX_WIRE - 1:
                    self._rx.clear()
                    self._discard = True
        return None

    def request(self, command, payload=b"", timeout=None):
        if not self._opened:
            raise RuntimeError("client is not open")
        command = int(command)
        seq = self.seq
        self.seq = self.seq % 65535 + 1
        wire = encode_frame(Frame(FrameType.REQUEST, seq, command, bytes(payload)))
        duration = timeout if timeout is not None else (self.scan_timeout if command == Command.I2C_SCAN else self.timeout)
        for _ in range(self.retries + 1):
            self.transport.write(wire)
            deadline = time.monotonic() + duration
            while time.monotonic() < deadline:
                frame = self._read_frame(deadline)
                if frame is None:
                    break
                if frame.type == FrameType.EVENT:
                    self._queue_event(frame)
                    continue
                if frame.type != FrameType.RESPONSE or frame.seq != seq or frame.command != command:
                    continue
                if not frame.payload:
                    raise ProtocolError("response lacks status")
                try:
                    status = Status(frame.payload[0])
                except ValueError as exc:
                    raise ProtocolError("unknown response status") from exc
                if status != Status.OK:
                    raise RemoteError(status)
                return Reply(command, seq, status, frame.payload[1:])
        raise TimeoutError(f"command {command:#x} timed out after {self.retries + 1} identical transmissions")

    def _request_payload(self, command, payload=b""):
        return self.request(command, payload).payload

    @staticmethod
    def _unpack(fmt, payload, label):
        expected = struct.calcsize(fmt)
        if len(payload) != expected:
            raise ProtocolError(f"{label}: expected {expected} bytes, got {len(payload)}")
        return struct.unpack(fmt, payload)

    def hello(self):
        return Hello(*self._unpack("<BBBIBB", self._request_payload(Command.HELLO), "HELLO"))

    def status(self):
        values = self._unpack("<IBHBIIII", self._request_payload(Command.GET_STATUS), "GET_STATUS")
        return BoardStatus(values[0], bool(values[1]), values[2], bool(values[3]), *values[4:])

    def devices(self):
        payload = self._request_payload(Command.LIST_DEVICES)
        if not payload:
            raise ProtocolError("LIST_DEVICES lacks count")
        count = payload[0]
        if len(payload) != 1 + 7 * count:
            raise ProtocolError("LIST_DEVICES size mismatch")
        return [Device(*struct.unpack_from("<BBBBHB", payload, 1 + 7 * i)) for i in range(count)]

    def scan(self):
        payload = self._request_payload(Command.I2C_SCAN)
        if not payload or payload[0] > 46 or len(payload) != payload[0] + 1:
            raise ProtocolError("I2C_SCAN malformed; at most 46 addresses fit with status/count")
        return list(payload[1:])

    @staticmethod
    def _validate_device_config(device_id, config):
        expected = {1: 0, 2: 4, 3: 1, 4: 1, 5: 1}.get(device_id)
        if expected is None:
            raise ValueError("device id must be 1..5")
        if len(config) != expected:
            raise ProtocolError(f"device {device_id} config must be {expected} bytes")
        if device_id == 5 and config[0] not in (6, 7):
            raise ProtocolError("Nano ADC channel must be 6 (A6) or 7 (A7)")

    def get_config(self, device_id):
        if device_id not in (1, 2, 3, 4, 5): raise ValueError("device id must be 1..5")
        payload = self._request_payload(Command.GET_DEVICE_CONFIG, bytes((device_id,)))
        if len(payload) < 1 or payload[0] != device_id:
            raise ProtocolError("GET_DEVICE_CONFIG id/length mismatch")
        self._validate_device_config(device_id, payload[1:])
        return DeviceConfig(payload[0], payload[1:])

    def set_config(self, device_id, config):
        if device_id not in (1, 2, 3, 4, 5): raise ValueError("device id must be 1..5")
        config = bytes(config)
        self._validate_device_config(device_id, config)
        payload = self._request_payload(Command.SET_DEVICE_CONFIG, bytes((device_id,)) + config)
        if len(payload) < 1 or payload[0] != device_id:
            raise ProtocolError("SET_DEVICE_CONFIG id/length mismatch")
        self._validate_device_config(device_id, payload[1:])
        return DeviceConfig(payload[0], payload[1:])

    def nano_adc_config(self, channel=6):
        if channel not in (6, 7):
            raise ValueError("Nano ADC channel must be 6 (A6) or 7 (A7)")
        return self.set_config(5, bytes((channel,)))

    def ads_config(self, address=0x48, channel=0, gain=0, rate=7):
        if not 0x48 <= address <= 0x4B or not 0 <= channel <= 3 or not 0 <= gain <= 5 or not 0 <= rate <= 7:
            raise ValueError("invalid ADS1115 configuration")
        return self.set_config(2, struct.pack("<BBBB", address, channel, gain, rate))

    def set_output(self, device_id, value):
        payload = self._request_payload(Command.SET_OUTPUT, struct.pack("<BH", device_id, value))
        values = self._unpack("<BHI", payload, "SET_OUTPUT")
        if values[0] != device_id: raise ProtocolError("SET_OUTPUT id mismatch")
        return values

    def get_output(self, device_id):
        values = self._unpack("<BBHI", self._request_payload(Command.GET_OUTPUT, bytes((device_id,))), "GET_OUTPUT")
        if values[0] != device_id:
            raise ProtocolError("GET_OUTPUT id mismatch")
        return OutputState(values[0], bool(values[1]), values[2], values[3])

    def adc_read(self, device_id=2):
        if device_id not in (2, 5):
            raise ValueError("ADC device id must be 2 (ADS1115) or 5 (Nano ADC)")
        values = self._unpack("<BBIIhB", self._request_payload(Command.ADC_READ, bytes((device_id,))), "ADC_READ")
        if values[0] != device_id:
            raise ProtocolError("ADC_READ id mismatch")
        result = ADCResult(*values)
        if device_id == 5:
            if result.channel not in (6, 7) or not 0 <= result.value <= 1023 or result.flags != 0:
                raise ProtocolError("malformed native ADC result")
        return result

    def nano_adc_read(self, channel=None):
        if channel is not None:
            self.nano_adc_config(channel)
        return self.adc_read(5)

    @staticmethod
    def _nano_stream_parameters(period_us, channels):
        if isinstance(period_us, bool) or not isinstance(period_us, int) or not 1000 <= period_us <= 10_000_000:
            raise ValueError("period_us must be an integer in 1000..10000000")
        try:
            channels = tuple(channels)
        except TypeError as exc:
            raise ValueError("channels must be (6,), (7,), or (6, 7)") from exc
        masks = {(6,): 1, (7,): 2, (6, 7): 3}
        if channels not in masks:
            raise ValueError("channels must be (6,), (7,), or (6, 7)")
        return masks[channels]

    def nano_adc_stream_start(self, period_us=10_000, channels=(6, 7)):
        mask = self._nano_stream_parameters(period_us, channels)
        payload = struct.pack("<BBI", 5, mask, period_us)
        values = self._unpack("<B", self._request_payload(Command.ADC_STREAM_START, payload), "ADC_STREAM_START")
        if values[0] != 5:
            raise ProtocolError("ADC_STREAM_START id mismatch")
        return values[0]

    def nano_adc_stream_stop(self):
        values = self._unpack("<B", self._request_payload(Command.ADC_STREAM_STOP, b"\x05"), "ADC_STREAM_STOP")
        if values[0] != 5:
            raise ProtocolError("ADC_STREAM_STOP id mismatch")
        return values[0]

    def adc_stream_start(self, device_id=2):
        if device_id not in (2, 5):
            raise ValueError("ADC stream device id must be 2 or 5")
        values = self._unpack("<B", self._request_payload(Command.ADC_STREAM_START, bytes((device_id,))), "ADC_STREAM_START")
        if values[0] != device_id: raise ProtocolError("ADC_STREAM_START id mismatch")
        return values[0]

    def adc_stream_stop(self, device_id=2):
        if device_id not in (2, 5):
            raise ValueError("ADC stream device id must be 2 or 5")
        values = self._unpack("<B", self._request_payload(Command.ADC_STREAM_STOP, bytes((device_id,))), "ADC_STREAM_STOP")
        if values[0] != device_id: raise ProtocolError("ADC_STREAM_STOP id mismatch")
        return values[0]

    def crank_config(self, pattern_id, invert=False):
        payload = self._request_payload(Command.CRANK_CONFIG, struct.pack("<BB", pattern_id, int(invert)))
        values = self._unpack("<BB", payload, "CRANK_CONFIG")
        if values != (pattern_id, int(invert)): raise ProtocolError("CRANK_CONFIG echo mismatch")
        return values

    def crank_set_rpm(self, rpm):
        value, = self._unpack("<H", self._request_payload(Command.CRANK_SET_RPM, struct.pack("<H", rpm)), "CRANK_SET_RPM")
        if value != rpm: raise ProtocolError("CRANK_SET_RPM echo mismatch")
        return value

    set_rpm = crank_set_rpm

    def crank_stop(self):
        if self._request_payload(Command.CRANK_STOP): raise ProtocolError("CRANK_STOP unexpected payload")

    def _queue_event(self, frame):
        if len(self.events) == self.events.maxlen:
            self.event_drops += 1
        self.events.append(frame)

    def get_event(self, timeout=None):
        if self.events:
            return self.events.popleft()
        if timeout is None:
            return None
        frame = self._read_frame(time.monotonic() + timeout)
        if frame is not None and frame.type == FrameType.EVENT:
            return frame
        return None

    def iter_events(self, *, duration=None, count=None):
        """Yield decoded ADC events until duration/count reached (either may be set)."""
        if duration is None and count is None:
            raise ValueError("duration or count is required")
        if count is not None and count < 0:
            raise ValueError("count must be nonnegative")
        deadline = None if duration is None else time.monotonic() + duration
        emitted = 0
        while count is None or emitted < count:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return
            # get_event(None) is deliberately non-blocking; count-only
            # iteration must still wait for and read new UART frames.
            frame = self.get_event(0.1 if remaining is None else remaining)
            if frame is None:
                if deadline is None: continue
                return
            if frame.command == 0xA0:
                emitted += 1
                yield decode_adc_event(frame)


def decode_adc_event(frame):
    if frame.type != FrameType.EVENT or frame.command != 0xA0 or len(frame.payload) != ADC_STRUCT.size:
        raise ProtocolError("not a valid ADC event")
    result = ADCResult(*ADC_STRUCT.unpack(frame.payload))
    if result.id == 5:
        if result.channel not in (6, 7) or not 0 <= result.value <= 1023 or result.flags not in (0, 2):
            raise ProtocolError("malformed native ADC event")
    elif result.id != 2:
        raise ProtocolError("unknown ADC event device")
    return result
