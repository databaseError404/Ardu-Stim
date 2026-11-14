#!/usr/bin/env python3
"""JSON CLI; does not discover ports or touch outputs unless explicitly requested."""
import argparse
import dataclasses
import json
import time

from bench_client import BenchClient


def json_default(value):
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    if isinstance(value, (bytes, bytearray)):
        return list(value)
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", required=True)
    parser.add_argument("--baud", type=int, default=1000000)
    parser.add_argument("--boot-wait", type=float, default=2.0,
                        help="wait after serial open/DTR (use 0 if no reset expected)")
    parser.add_argument("--no-reset", action="store_true",
                        help="deassert DTR/RTS before opening; best effort, driver dependent")
    commands = parser.add_subparsers(dest="action", required=True)
    for name in ("hello", "status", "devices", "scan", "adc-read", "adc-stream", "adc-stop", "stop"):
        sub = commands.add_parser(name)
        if name == "adc-stream":
            sub.add_argument("--seconds", type=float, help="stop stream after this many seconds")
            sub.add_argument("--count", type=int, help="stop after this many samples")
    nano_stream = commands.add_parser("nano-stream")
    nano_stream.add_argument("--period-us", type=int, default=10_000)
    nano_stream.add_argument("--channels", type=int, nargs="+", choices=(6, 7), default=(6, 7))
    nano_stream.add_argument("--seconds", type=float)
    nano_stream.add_argument("--count", type=int)
    nano_read = commands.add_parser("nano-read")
    nano_read.add_argument("--channel", type=int, choices=(6, 7))
    config = commands.add_parser("config")
    config.add_argument("id", type=int)
    config.add_argument("--set", nargs="+", type=lambda value: int(value, 0))
    output = commands.add_parser("set-output")
    output.add_argument("id", type=int)
    output.add_argument("value", type=int)
    output = commands.add_parser("get-output")
    output.add_argument("id", type=int)
    crank = commands.add_parser("crank-config")
    crank.add_argument("pattern", type=int)
    crank.add_argument("--invert", action="store_true")
    rpm = commands.add_parser("rpm")
    rpm.add_argument("rpm", type=int)
    args = parser.parse_args()
    if args.action == "nano-stream":
        if args.seconds is None and args.count is None:
            parser.error("nano-stream requires --seconds and/or --count")
        if args.seconds is not None and args.seconds < 0 or args.count is not None and args.count < 0:
            parser.error("stream duration/count must be nonnegative")
        try:
            BenchClient._nano_stream_parameters(args.period_us, args.channels)
        except ValueError as exc:
            parser.error(str(exc))

    with BenchClient(args.port, args.baud, boot_wait=args.boot_wait,
                     reset_on_open=not args.no_reset) as client:
        action = args.action
        if action == "hello": result = client.hello()
        elif action == "status": result = client.status()
        elif action == "devices": result = client.devices()
        elif action == "scan": result = {"addresses": [hex(value) for value in client.scan()]}
        elif action == "config": result = client.set_config(args.id, bytes(args.set)) if args.set is not None else client.get_config(args.id)
        elif action == "nano-read": result = client.nano_adc_read(args.channel)
        elif action == "set-output": result = client.set_output(args.id, args.value)
        elif action == "get-output": result = client.get_output(args.id)
        elif action == "adc-read": result = client.adc_read()
        elif action == "adc-stop": result = {"stopped_device": client.adc_stream_stop()}
        elif action == "crank-config": result = client.crank_config(args.pattern, args.invert)
        elif action == "rpm": result = client.set_rpm(args.rpm)
        elif action == "stop": client.crank_stop(); result = {"stopped": True}
        elif action == "nano-stream":
            client.nano_adc_stream_start(args.period_us, tuple(args.channels))
            try:
                for sample in client.iter_events(duration=args.seconds, count=args.count):
                    print(json.dumps({"host_monotonic": time.monotonic(),
                                      "sample": dataclasses.asdict(sample),
                                      "host_event_drops": client.event_drops}), flush=True)
            finally:
                client.nano_adc_stream_stop()
            return 0
        elif action == "adc-stream":
            if args.seconds is None and args.count is None:
                parser.error("adc-stream requires --seconds and/or --count")
            client.adc_stream_start()
            try:
                for sample in client.iter_events(duration=args.seconds, count=args.count):
                    print(json.dumps({"host_monotonic": time.monotonic(),
                                      "sample": dataclasses.asdict(sample),
                                      "host_event_drops": client.event_drops}), flush=True)
            finally:
                client.adc_stream_stop()
            return 0
        print(json.dumps(dataclasses.asdict(result) if dataclasses.is_dataclass(result) else result,
                         default=json_default, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
