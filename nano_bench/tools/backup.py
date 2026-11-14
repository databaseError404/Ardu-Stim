"""Explicit read-only Nano flash and EEPROM backup."""
import argparse
import datetime
import json
from pathlib import Path
import sys
from common import ROOT, find_avrdude, avrdude_config, run_logged


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--bootloader-baud", type=int, default=57600)
    parser.add_argument("--avrdude")
    parser.add_argument("--avrdude-config")
    args = parser.parse_args(argv)
    out = Path(args.output_dir).resolve()
    files = (out / "nano_before_flash.hex", out / "nano_before_eeprom.hex")
    if any(p.exists() for p in files): parser.error("backup output exists; refusing to overwrite")
    try:
        exe, conf = find_avrdude(args.avrdude); conf = avrdude_config(args.avrdude_config, conf)
    except FileNotFoundError as exc: parser.error(str(exc))
    out.mkdir(parents=True, exist_ok=True)
    cmd = [exe, "-C", conf, "-p", "m328p", "-c", "arduino", "-P", args.port,
           "-b", str(args.bootloader_baud), "-n", "-v", "-U", f"flash:r:{files[0]}:i",
           "-U", f"eeprom:r:{files[1]}:i"]
    stamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    print("READ-ONLY", cmd, flush=True)
    code, text = run_logged(cmd, out / "backup.log", 100, ROOT)
    print(text, end="")
    if code == 0 and all(p.is_file() and p.stat().st_size for p in files):
        (out / "transport.json").write_text(json.dumps({"port": args.port,
            "bootloader_baud": args.bootloader_baud, "time": stamp}, indent=2) + "\n", encoding="utf-8")
    elif code == 0:
        print("Backup read returned empty/missing flash or EEPROM", file=sys.stderr); return 1
    return code

if __name__ == "__main__": raise SystemExit(main())
