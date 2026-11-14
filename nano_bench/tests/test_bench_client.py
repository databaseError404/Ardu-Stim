import random
import struct
import unittest

from host.bench_client import (
    ADCResult, BenchClient, Command, Frame, FrameType, ProtocolError, RemoteError,
    Status, cobs_decode, cobs_encode, crc16, decode_adc_event, decode_frame, encode_frame,
    raw_to_volts, nano_raw_to_volts,
)


class FakeTransport:
    def __init__(self):
        self.buf = bytearray()
        self.writes = []
        self.is_open = True
        self.drop_writes = 0
        self.response_fn = None
    def write(self, wire):
        self.writes.append(bytes(wire))
        if self.drop_writes:
            self.drop_writes -= 1
            return len(wire)
        request = decode_frame(wire)
        if self.response_fn:
            self.buf.extend(self.response_fn(request))
        else:
            self.buf.extend(encode_frame(Frame(FrameType.RESPONSE, request.seq, request.command, b'\0')))
        return len(wire)
    def read(self, count=1):
        if not self.buf:
            return b""
        result = bytes(self.buf[:min(count, 3)])
        del self.buf[:len(result)]
        return result
    def close(self): self.is_open = False
    def open(self): self.is_open = True


class CodecTests(unittest.TestCase):
    def test_crc_reference(self):
        self.assertEqual(crc16(b"123456789"), 0x29B1)

    def test_cobs_boundaries_and_deterministic_random_roundtrips(self):
        rng = random.Random(0xC0B5)
        cases = [b"", b"\0", b"\0" * 500, bytes(range(256)), b"x" * 254,
                 b"x" * 255, b"x" * 510]
        cases.extend(bytes(rng.randrange(256) for _ in range(n)) for n in range(513))
        for value in cases:
            with self.subTest(length=len(value)):
                self.assertEqual(cobs_decode(cobs_encode(value)), value)

    def test_cobs_truncated_and_invalid_codewords(self):
        for value in (b"", b"\0", b"\x02", b"\x03a", b"\xff" + b"a" * 10):
            with self.subTest(value=value), self.assertRaises(ProtocolError):
                cobs_decode(value)

    def test_frame_random_payloads_all_sizes_and_max_wire(self):
        rng = random.Random(557)
        for length in range(49):
            for _ in range(8):
                payload = bytes(rng.randrange(256) for _ in range(length))
                frame = Frame(FrameType.REQUEST, 65535, 0x55, payload)
                wire = encode_frame(frame)
                self.assertLessEqual(len(wire), 58)
                self.assertEqual(decode_frame(wire), frame)
        maximum = encode_frame(Frame(FrameType.REQUEST, 1, 0x55, b"\0" * 48))
        self.assertEqual(len(maximum), 58)
        self.assertEqual(decode_frame(maximum).payload, b"\0" * 48)

    def test_frame_header_length_crc_type_version_and_sequences(self):
        baseline = bytearray(cobs_decode(encode_frame(Frame(1, 1, 1, b""))[:-1]))
        cases = []
        for position, value in ((0, 2), (1, 4), (5, 49), (5, 1)):
            raw = bytearray(baseline); raw[position] = value
            raw[-2:] = struct.pack("<H", crc16(raw[:-2]))
            cases.append(cobs_encode(raw))
        raw = bytearray(baseline); raw[2:4] = b"\\0\\0"; raw[-2:] = struct.pack("<H", crc16(raw[:-2])); cases.append(cobs_encode(raw))
        raw = bytearray(baseline); raw[-1] ^= 1; cases.append(cobs_encode(raw))
        for wire in cases:
            with self.subTest(wire=wire), self.assertRaises(ProtocolError): decode_frame(wire)
        for frame in (Frame(FrameType.EVENT, 1, 1, b""), Frame(FrameType.RESPONSE, 0, 1, b"")):
            with self.assertRaises(ProtocolError): decode_frame(encode_frame(frame))
        with self.assertRaises(ValueError): encode_frame(Frame(1, 1, 1, b"x" * 49))


class ClientTests(unittest.TestCase):
    def client(self, transport=None, **kwargs):
        return BenchClient(transport=transport or FakeTransport(), boot_wait=0, timeout=0.015, **kwargs)

    def test_retry_is_identical_and_remote_error_not_retried(self):
        transport = FakeTransport(); transport.drop_writes = 1
        with self.client(transport) as client:
            client.request(7)
        self.assertEqual(transport.writes[0], transport.writes[1])
        transport = FakeTransport()
        transport.response_fn = lambda req: encode_frame(Frame(2, req.seq, req.command, bytes([Status.BUSY])))
        with self.client(transport) as client:
            with self.assertRaises(RemoteError): client.request(7)
        self.assertEqual(len(transport.writes), 1)

    def test_stale_response_telemetry_and_response_demultiplex(self):
        transport = FakeTransport()
        def responses(req):
            event = Frame(3, 0, 0xA0, struct.pack("<BBIIhB", 2, 1, 9, 12, -17, 1))
            stale = Frame(2, req.seq - 1 or 65535, req.command, b"\0")
            good = Frame(2, req.seq, req.command, b"\0")
            return encode_frame(event) + encode_frame(stale) + encode_frame(good)
        transport.response_fn = responses
        with self.client(transport) as client:
            self.assertEqual(client.request(3).status, Status.OK)
            self.assertEqual(decode_adc_event(client.get_event()), ADCResult(2, 1, 9, 12, -17, 1))

    def test_overflow_recovers_at_delimiter(self):
        transport = FakeTransport()
        good = encode_frame(Frame(2, 123, 9, b"\0"))
        transport.buf.extend(b"x" * 80 + b"\0" + good)
        with self.client(transport) as client:
            # _read_frame drains malformed/oversized data and returns the following frame.
            frame = client._read_frame(__import__('time').monotonic() + .1)
        self.assertEqual(frame, Frame(2, 123, 9, b"\0"))

    def test_method_exact_payloads_and_decoded_validation(self):
        t = FakeTransport()
        def response(req):
            payloads = {
                1: struct.pack("<BBBIBB", 0, 1, 0, 63, 48, 4),
                2: struct.pack("<IBHBIIII", 100, 1, 300, 0, 1, 2, 3, 4),
                3: bytes([1]) + struct.pack("<BBBBHB", 2, 2, 2, 0x48, 32767, 1),
                4: bytes([46]) + bytes(range(46)),
                5: bytes([2, 0x48, 0, 0, 7]),
                6: req.payload,
                0x10: struct.pack("<BHI", 1, 42, 999),
                0x11: struct.pack("<BBHI", 1, 1, 42, 999),
                0x20: struct.pack("<BBIIhB", 2, 0, 7, 9, -5, 2),
                0x21: b"\x02", 0x22: b"\x02",
                0x30: req.payload, 0x31: req.payload, 0x32: b"",
            }
            return encode_frame(Frame(2, req.seq, req.command, b"\0" + payloads[req.command]))
        t.response_fn = response
        with self.client(t) as c:
            self.assertEqual(c.hello().fw_minor, 1)
            self.assertTrue(c.status().crank_running)
            self.assertEqual(c.devices()[0].address, 0x48)
            self.assertEqual(len(c.scan()), 46)
            self.assertEqual(c.get_config(2).config, bytes([0x48, 0, 0, 7]))
            self.assertEqual(c.ads_config(0x49, 1, 2, 6).config, bytes([0x49, 1, 2, 6]))
            self.assertEqual(c.set_output(1, 42), (1, 42, 999))
            self.assertTrue(c.get_output(1).known)
            self.assertEqual(c.adc_read().value, -5)
            self.assertEqual(c.adc_stream_start(), 2)
            self.assertEqual(c.adc_stream_stop(), 2)
            self.assertEqual(c.crank_config(1, True), (1, 1))
            self.assertEqual(c.set_rpm(300), 300)
            c.crank_stop()
        decoded = [decode_frame(w) for w in t.writes]
        self.assertEqual([f.payload for f in decoded], [b"", b"", b"", b"", b"\x02", b"\x02\x49\x01\x02\x06", b"\x01\x2a\x00", b"\x01", b"\x02", b"\x02", b"\x02", b"\x01\x01", b"\x2c\x01", b""])

    def test_native_adc_config_and_read_and_ads_events(self):
        t = FakeTransport()
        def response(req):
            if req.command == Command.SET_DEVICE_CONFIG:
                return encode_frame(Frame(2, req.seq, req.command, b"\0" + req.payload))
            if req.command == Command.GET_DEVICE_CONFIG:
                return encode_frame(Frame(2, req.seq, req.command, b"\0\x05\x06"))
            event = Frame(3, 0, 0xA0, struct.pack("<BBIIhB", 2, 1, 4, 55, 123, 0))
            reply = Frame(2, req.seq, req.command,
                          b"\0" + struct.pack("<BBIIhB", 5, 6, 8, 77, 900, 0))
            return encode_frame(event) + encode_frame(reply)
        t.response_fn = response
        with self.client(t) as c:
            self.assertEqual(c.get_config(5).config, b"\x06")
            self.assertEqual(c.nano_adc_config(7).config, b"\x07")
            result = c.nano_adc_read(6)
            self.assertEqual(result, ADCResult(5, 6, 8, 77, 900, 0))
            self.assertEqual(decode_adc_event(c.get_event()), ADCResult(2, 1, 4, 55, 123, 0))
            self.assertEqual(c.adc_read(5).channel, 6)
        requests = [decode_frame(w) for w in t.writes]
        self.assertEqual([request.payload for request in requests],
                         [b"\x05", b"\x05\x07", b"\x05\x06", b"\x05", b"\x05"])

    def test_native_periodic_stream_payload_validation_and_no_adc_read(self):
        t = FakeTransport()
        t.response_fn = lambda req: encode_frame(Frame(2, req.seq, req.command, b"\0\x05"))
        with self.client(t) as c:
            for channels, mask in (((6,), 1), ((7,), 2), ((6, 7), 3)):
                self.assertEqual(c.nano_adc_stream_start(1000, channels), 5)
                request = decode_frame(t.writes[-1])
                self.assertEqual(request.payload, struct.pack("<BBI", 5, mask, 1000))
            for period, channels in ((999, (6,)), (10_000_001, (7,)), (0, (6, 7)), (True, (6,)),
                                     (1000, ()), (1000, (7, 6)), (1000, (6, 6)), (1000, (8,))):
                with self.subTest(period=period, channels=channels), self.assertRaises(ValueError):
                    c.nano_adc_stream_start(period, channels)
            self.assertEqual(c.nano_adc_stream_start(10_000_000, (7,)), 5)
            self.assertEqual(decode_frame(t.writes[-1]).payload, struct.pack("<BBI", 5, 2, 10_000_000))
            self.assertEqual(c.nano_adc_stream_stop(), 5)
        commands = [decode_frame(w).command for w in t.writes]
        self.assertEqual(commands, [Command.ADC_STREAM_START] * 4 + [Command.ADC_STREAM_STOP])
        self.assertNotIn(Command.ADC_READ, commands)
        self.assertEqual(decode_frame(t.writes[-1]).payload, b"\x05")

    def test_native_adc_rejects_bad_channels_shapes_codes_and_flags(self):
        t = FakeTransport()
        with self.client(t) as c:
            for method in (lambda: c.nano_adc_config(5), lambda: c.set_config(5, b"\x08")):
                with self.assertRaises((ValueError, ProtocolError)):
                    method()
            for payload in (b"\x05", b"\x05\x06", b"\x05\x08" + b"\0" * 9):
                t.response_fn = lambda req, payload=payload: encode_frame(Frame(2, req.seq, req.command, b"\0" + payload))
                with self.assertRaises(ProtocolError):
                    c.adc_read(5)
            t.response_fn = lambda req: encode_frame(Frame(2, req.seq, req.command,
                b"\0" + struct.pack("<BBIIhB", 5, 7, 1, 2, 1024, 0)))
            with self.assertRaises(ProtocolError): c.adc_read(5)
            t.response_fn = lambda req: encode_frame(Frame(2, req.seq, req.command,
                b"\0" + struct.pack("<BBIIhB", 5, 7, 1, 2, 10, 1)))
            with self.assertRaises(ProtocolError): c.adc_read(5)

    def test_native_and_ads_events_mixed_demux_and_validation(self):
        t = FakeTransport()
        native = Frame(3, 0, 0xA0, struct.pack("<BBIIhB", 5, 6, 17, 1234, 1023, 2))
        ads = Frame(3, 0, 0xA0, struct.pack("<BBIIhB", 2, 1, 4, 55, -123, 1))
        t.response_fn = lambda req: encode_frame(native) + encode_frame(ads) + encode_frame(Frame(2, req.seq, req.command, b"\0"))
        with self.client(t) as c:
            c.request(0x44)
            self.assertEqual(decode_adc_event(c.get_event()), ADCResult(5, 6, 17, 1234, 1023, 2))
            self.assertEqual(decode_adc_event(c.get_event()), ADCResult(2, 1, 4, 55, -123, 1))
        for device, channel, value, flags in ((5, 8, 0, 0), (5, 7, 1024, 0), (5, 7, 1, 1), (9, 6, 1, 0)):
            frame = Frame(3, 0, 0xA0, struct.pack("<BBIIhB", device, channel, 1, 2, value, flags))
            with self.subTest(frame=frame), self.assertRaises(ProtocolError):
                decode_adc_event(frame)

    def test_native_volts_conversion_validation(self):
        self.assertEqual(nano_raw_to_volts(1023), 1023 * 5.0 / 1024)
        for code in (-1, 1024, 1.0, True):
            with self.assertRaises(ValueError): nano_raw_to_volts(code)
        for vref in (0, -1, float("inf"), float("nan")):
            with self.assertRaises(ValueError): nano_raw_to_volts(1, vref)

    def test_reply_shape_and_echo_errors_are_protocol_errors(self):
        t = FakeTransport()
        t.response_fn = lambda req: encode_frame(Frame(2, req.seq, req.command, b"\0\x01"))
        with self.client(t) as c:
            with self.assertRaises(ProtocolError): c.hello()
            with self.assertRaises(ProtocolError): c.get_output(2)

    def test_scan_limit_and_raw_volts(self):
        self.assertAlmostEqual(raw_to_volts(32767, 0), 6.144 * 32767 / 32768)
        self.assertAlmostEqual(raw_to_volts(-32768, 5), -0.256)
        for gain in range(6): self.assertAlmostEqual(raw_to_volts(16384, gain), (6.144,4.096,2.048,1.024,.512,.256)[gain]/2)
        with self.assertRaises(ValueError): raw_to_volts(1, 6)
        t = FakeTransport(); t.response_fn = lambda req: encode_frame(Frame(2, req.seq, req.command, b"\0\x2e" + b"x" * 45))
        with self.client(t) as c:
            with self.assertRaises(ProtocolError): c.scan()

    def test_parser_reset_open_close_and_sequence_nonzero(self):
        t = FakeTransport(); c = self.client(t)
        self.assertGreater(c.seq, 0)
        c._rx.extend(b"garbage"); c._discard = True
        with c:
            self.assertFalse(c._rx); self.assertFalse(c._discard)
        self.assertFalse(c._rx); self.assertFalse(c._discard)


if __name__ == "__main__":
    unittest.main()
