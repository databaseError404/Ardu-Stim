"""Compile the Nano bench sketch; core installation is never attempted."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
from common import ROOT, FQBN, find_arduino_cli, run_logged


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli")
    parser.add_argument("--config-file")
    parser.add_argument("--build-dir", default=str(ROOT / ".build"))
    parser.add_argument("--cache-dir", default=str(ROOT / ".cache"))
    parser.add_argument("--baud", type=int, choices=(1000000, 500000), default=1000000)
    args = parser.parse_args(argv)
    try: cli = find_arduino_cli(args.cli)
    except FileNotFoundError as exc: parser.error(str(exc))
    build, cache = Path(args.build_dir).resolve(), Path(args.cache_dir).resolve()
    tmp = build / ".tmp"
    for path in (build, cache, tmp): path.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy(); env["TMP"] = env["TEMP"] = env["TMPDIR"] = str(tmp)
    command = [cli]
    if args.config_file: command += ["--config-file", args.config_file]
    command += ["compile", "--fqbn", FQBN, "--build-path", str(build),
                "--build-cache-path", str(cache), "--build-property",
                f"compiler.cpp.extra_flags=-DBENCH_UART_BAUD={args.baud}UL",
                "--warnings", "all", str(ROOT / "firmware" / "bench")]
    code, text = run_logged(command, build / "build.log", 300, ROOT, env)
    print(text, end="")
    if code == 0:
        source = ROOT / "firmware" / "bench" / "bench.ino"
        hexfile = build / "bench.ino.hex"
        if not hexfile.is_file(): print("Build succeeded but HEX missing", file=sys.stderr); return 1
        settings = {"source": str(source.relative_to(ROOT)), "hex": hexfile.name,
                    "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                    "hex_sha256": hashlib.sha256(hexfile.read_bytes()).hexdigest(), "uart_baud": args.baud}
        (build / "settings.json").write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return code

if __name__ == "__main__": raise SystemExit(main())
