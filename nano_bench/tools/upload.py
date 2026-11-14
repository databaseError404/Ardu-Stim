"""Flash a validated bench HEX only, after explicit confirmation and backup."""
import argparse
import datetime
import hashlib
import json
from pathlib import Path
import sys
from common import ROOT, find_avrdude, avrdude_config, run_logged


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True)
    parser.add_argument("--backup-dir", required=True)
    parser.add_argument("--confirm", action="store_true", help="explicitly authorize flash write")
    parser.add_argument("--bootloader-baud", type=int)
    parser.add_argument("--build-dir", default=str(ROOT / ".build"))
    parser.add_argument("--hex")
    parser.add_argument("--avrdude")
    parser.add_argument("--avrdude-config")
    args = parser.parse_args(argv)
    if not args.confirm: parser.error("--confirm is required before any flash write")
    bdir, build = Path(args.backup_dir).resolve(), Path(args.build_dir).resolve()
    backup_files = (bdir / "nano_before_flash.hex", bdir / "nano_before_eeprom.hex")
    try:
        if any(not p.is_file() or p.stat().st_size == 0 for p in backup_files):
            raise ValueError("both nonempty flash and EEPROM backups are required")
        metadata = json.loads((bdir / "transport.json").read_text(encoding="utf-8"))
        if not metadata.get("port") or not metadata.get("bootloader_baud"):
            raise ValueError("invalid backup transport metadata")
        if metadata["port"].casefold() != args.port.casefold():
            raise ValueError("upload port does not match backup port")
        baud = args.bootloader_baud if args.bootloader_baud is not None else metadata["bootloader_baud"]
        hexfile = Path(args.hex).resolve() if args.hex else build / "bench.ino.hex"
        settings = json.loads((build / "settings.json").read_text(encoding="utf-8"))
        source = ROOT / "firmware" / "bench" / "bench.ino"
        if settings["source_sha256"] != hashlib.sha256(source.read_bytes()).hexdigest():
            raise ValueError("source hash differs from build settings")
        if settings["hex_sha256"] != hashlib.sha256(hexfile.read_bytes()).hexdigest():
            raise ValueError("HEX hash differs from build settings")
        if settings.get("source") and settings["source"].replace("\\", "/") != source.relative_to(ROOT).as_posix():
            raise ValueError("settings source does not identify current sketch")
        exe, conf = find_avrdude(args.avrdude); conf = avrdude_config(args.avrdude_config, conf)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    logdir = build / stamp
    logdir.mkdir(parents=True, exist_ok=True)
    cmd = [exe, "-C", conf, "-p", "m328p", "-c", "arduino", "-P", args.port,
           "-b", str(baud), "-D", "-U", f"flash:w:{hexfile}:i"]
    print("FLASH ONLY; avrdude default readback verification enabled", cmd, flush=True)
    code, text = run_logged(cmd, logdir / "upload.log", 90, ROOT)
    print(text, end="")
    if code == 0:
        record = {"utc": stamp, "hex_sha256": settings["hex_sha256"], "port": args.port,
                  "bootloader_baud": baud, "flash_only": True, "verified": True,
                  "application_baud": settings["uart_baud"], "source_sha256": settings["source_sha256"]}
        (logdir / "upload.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return code

if __name__ == "__main__": raise SystemExit(main())
