"""Offline safety tests for the publication tool CLIs."""
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import backup
import upload
import common


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.backups = self.root / "backups"
        self.backups.mkdir()
        (self.backups / "nano_before_flash.hex").write_text(":0100000000FF\n")
        (self.backups / "nano_before_eeprom.hex").write_text(":0100000000FF\n")
        (self.backups / "transport.json").write_text(json.dumps({"port": "COM4", "bootloader_baud": 57600}))
        self.build = self.root / "build"
        self.build.mkdir()
        self.hex = self.build / "bench.ino.hex"
        self.hex.write_text(":0100000000FF\n")
        source = ROOT / "firmware" / "bench" / "bench.ino"
        self.settings = {"source": "firmware/bench/bench.ino", "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                         "hex_sha256": hashlib.sha256(self.hex.read_bytes()).hexdigest(), "uart_baud": 500000}
        (self.build / "settings.json").write_text(json.dumps(self.settings))

    @mock.patch.object(upload, "find_avrdude", return_value=("avrdude", "conf"))
    @mock.patch.object(upload, "avrdude_config", return_value="conf")
    @mock.patch.object(upload, "run_logged", return_value=(0, "ok\n"))
    def test_confirm_guard_and_flash_only(self, run, config, finder):
        args = ["--port", "COM4", "--backup-dir", str(self.backups), "--build-dir", str(self.build)]
        with self.assertRaises(SystemExit) as caught: upload.main(args)
        self.assertEqual(caught.exception.code, 2)
        self.assertFalse(run.called)
        self.assertEqual(upload.main(args + ["--confirm"]), 0)
        command = run.call_args.args[0]
        self.assertIn("-D", command)
        self.assertIn("flash:w:" + str(self.hex) + ":i", command)
        self.assertFalse(any("eeprom" in item or "fuse" in item for item in command))
        self.assertEqual(command[command.index("-b") + 1], "57600")

    @mock.patch.object(upload, "find_avrdude", return_value=("avrdude", "conf"))
    @mock.patch.object(upload, "run_logged")
    def test_hash_and_port_guards(self, run, finder):
        args = ["--port", "COM5", "--backup-dir", str(self.backups), "--build-dir", str(self.build), "--confirm"]
        with self.assertRaises(SystemExit): upload.main(args)
        self.assertFalse(run.called)
        args[1] = "COM4"
        self.hex.write_text("modified")
        with self.assertRaises(SystemExit): upload.main(args)
        self.assertFalse(run.called)

    @mock.patch.object(backup, "find_avrdude", return_value=("avrdude", "conf"))
    @mock.patch.object(backup, "avrdude_config", return_value="conf")
    @mock.patch.object(backup, "run_logged")
    def test_backup_read_only_no_overwrite(self, run, config, finder):
        out = self.root / "out"
        def successful_read(command, log, timeout, cwd):
            for item in command:
                if item.startswith("flash:r:") or item.startswith("eeprom:r:"):
                    Path(item.split(":", 2)[2][:-2]).write_text(":0100000000FF\\n")
            return 0, "ok\\n"
        run.side_effect = successful_read
        self.assertEqual(backup.main(["--port", "COM4", "--output-dir", str(out)]), 0)
        command = run.call_args.args[0]
        self.assertIn("-n", command)
        self.assertIn("flash:r:", " ".join(command))
        self.assertIn("eeprom:r:", " ".join(command))
        with self.assertRaises(SystemExit): backup.main(["--port", "COM4", "--output-dir", str(out)])
        self.assertEqual(run.call_count, 1)

    @mock.patch("common.subprocess.run", side_effect=__import__("subprocess").TimeoutExpired("cmd", 1, output=b"partial"))
    def test_timeout_output_preserved(self, runner):
        log = self.root / "timeout.log"
        code, text = common.run_logged(["cmd"], log, 1)
        self.assertEqual(code, 124)
        self.assertIn("partial", text)
        self.assertIn("HOST TIMEOUT", log.read_text())

if __name__ == "__main__": unittest.main()
