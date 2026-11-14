"""Shared, import-safe helpers for the Nano bench tools (stdlib only)."""
from pathlib import Path
import os
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
FQBN = "arduino:avr:nano:cpu=atmega328old"


def find_arduino_cli(explicit=None):
    if explicit:
        if not Path(explicit).is_file():
            raise FileNotFoundError("arduino-cli not found at --cli path")
        return str(Path(explicit))
    candidates = [os.environ.get("ARDUINO_CLI"), shutil.which("arduino-cli")]
    candidates += [r"C:\Program Files\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe",
                   r"C:\Program Files (x86)\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe"]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate))
    raise FileNotFoundError("arduino-cli not found (use --cli, ARDUINO_CLI, or PATH)")


def find_avrdude(explicit=None):
    if explicit:
        p = Path(explicit)
        if p.is_dir():
            exe = p / "bin" / ("avrdude.exe" if os.name == "nt" else "avrdude")
            conf = p / "etc" / "avrdude.conf"
        else:
            exe, conf = p, p.parent.parent / "etc" / "avrdude.conf"
        if exe.is_file():
            return str(exe), str(conf) if conf.is_file() else None
        raise FileNotFoundError("avrdude not found at --avrdude path")
    names = ("avrdude.exe", "avrdude")
    found = shutil.which("avrdude") or shutil.which("avrdude.exe")
    candidates = [Path(found)] if found else []
    roots = [Path(os.environ.get("LOCALAPPDATA", "")) / "Arduino15" / "packages" / "arduino" / "tools" / "avrdude",
             Path.home() / ".arduino15" / "packages" / "arduino" / "tools" / "avrdude"]
    for root in roots:
        if root.is_dir():
            for version in sorted(root.iterdir(), reverse=True):
                for name in names:
                    candidate = version / "bin" / name
                    if candidate.is_file(): candidates.append(candidate)
    for exe in candidates:
        confs = [exe.parent.parent / "etc" / "avrdude.conf"]
        if exe.parent.parent.parent.name == "tools":
            confs += list(exe.parent.parent.parent.parent.glob("*/tools/avrdude/*/etc/avrdude.conf"))
        conf = next((p for p in confs if p.is_file()), None)
        return str(exe), str(conf) if conf else None
    raise FileNotFoundError("avrdude not found (use --avrdude or PATH)")


def run_logged(command, log_path, timeout, cwd=None, env=None):
    """Run process, preserving complete merged output including timeout partial output."""
    try:
        result = subprocess.run(command, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=timeout)
        output, code = result.stdout or b"", result.returncode
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or b""
        if isinstance(output, str): output = output.encode("utf-8", errors="replace")
        output += b"\nHOST TIMEOUT\n"
        code = 124
    except OSError as exc:
        output, code = str(exc).encode("utf-8"), 127
    try:
        text = output.decode("utf-8")
    except UnicodeDecodeError:
        text = output.decode("cp1251" if os.name == "nt" else "utf-8", errors="replace")
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    Path(log_path).write_text(text, encoding="utf-8")
    return code, text


def avrdude_config(explicit, discovered):
    if explicit:
        if not Path(explicit).is_file(): raise FileNotFoundError("avrdude config not found")
        return str(Path(explicit))
    if discovered and Path(discovered).is_file(): return discovered
    raise FileNotFoundError("avrdude.conf not found (use --avrdude-config)")
