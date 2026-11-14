"""Offline CLI checks and mathematical Timer1 reference model (not hardware)."""
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock,patch
from host.bench_client import BenchClient,DeviceConfig

ROOT=Path(__file__).resolve().parents[1]

class CliTests(unittest.TestCase):
    def test_json_config_and_device_lists(self):
        # CLI is executable directly; supply its absolute import during this test.
        with patch.dict(sys.modules,{'bench_client':sys.modules['host.bench_client']}):
            spec=importlib.util.spec_from_file_location('bench_cli_test',ROOT/'host/bench_cli.py')
            cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)
        self.assertEqual(json.loads(json.dumps(DeviceConfig(2,b'\x48\0\0\7'),default=cli.json_default)),{'id':2,'config':[72,0,0,7]})

    def test_dtr_preconfigured_no_reset_mode_is_best_effort(self):
        port=Mock();port.is_open=False
        serial_module=types.SimpleNamespace(Serial=Mock(return_value=port))
        with patch.dict(sys.modules,{'serial':serial_module}):
            with BenchClient('NOT-A-REAL-PORT',boot_wait=0,reset_on_open=False):
                self.assertFalse(port.dtr)
                self.assertFalse(port.rts)
                self.assertEqual(port.port,'NOT-A-REAL-PORT')
            port.open.assert_called_once()
            port.close.assert_called_once()
            serial_module.Serial.assert_called_once_with(port=None,baudrate=1000000,timeout=.02,write_timeout=1)

class NanoStreamCliTests(unittest.TestCase):
    def test_nano_stream_cli_one_start_uart_read_then_stop_no_adc_read(self):
        calls=[]
        class Client(BenchClient):
            event_drops=0
            def __init__(self, *args, **kwargs): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def nano_adc_stream_start(self, period, channels): calls.append(("start", period, channels))
            def iter_events(self, **kwargs):
                calls.append(("read", kwargs))
                return iter(())
            def nano_adc_stream_stop(self): calls.append(("stop",))
        with patch.dict(sys.modules,{'bench_client':sys.modules['host.bench_client']}):
            spec=importlib.util.spec_from_file_location('bench_cli_stream_test',ROOT/'host/bench_cli.py')
            cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)
        with patch.object(cli, 'BenchClient', Client), patch.object(sys, 'argv',
                ['bench_cli.py', '--port', 'OFFLINE', 'nano-stream', '--period-us', '2000', '--channels', '7', '--count', '1']):
            self.assertEqual(cli.main(), 0)
        self.assertEqual([entry[0] for entry in calls], ['start', 'read', 'stop'])
        self.assertEqual(calls[0], ('start', 2000, (7,)))

    def test_nano_stream_cli_validates_parameters_before_open(self):
        with patch.dict(sys.modules,{'bench_client':sys.modules['host.bench_client']}):
            spec=importlib.util.spec_from_file_location('bench_cli_invalid_test',ROOT/'host/bench_cli.py')
            cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)
        with patch.object(cli.BenchClient, '__init__', side_effect=AssertionError('must not open')):
            with patch.object(sys, 'argv', ['bench_cli.py', '--port', 'OFFLINE', 'nano-stream', '--period-us', '0', '--seconds', '1']):
                with self.assertRaises(SystemExit): cli.main()


class TimerReferenceModelTests(unittest.TestCase):
    def test_all_rpm_timer_divisors_fit_and_quantization(self):
        # Mirrors the integer timing calculation; it does not execute AVR code.
        for teeth,missing in ((60,2),(36,1),(24,1)):
            for rpm in range(10,20001):
                found=False
                for divisor in (1,8,64,256,1024):
                    denominator=rpm*teeth*divisor
                    ticks=(16_000_000*30+denominator//2)//denominator
                    gap=ticks*(2*missing+1)
                    if 1<=ticks<=65536 and gap<=65536:
                        found=True
                        actual=16_000_000*30/(divisor*ticks*teeth)
                        self.assertLess(abs(actual-rpm)/rpm,.0015)
                        break
                self.assertTrue(found,(teeth,rpm))

    def test_missing_tooth_waveform_revolution_grid(self):
        for teeth,missing in ((60,2),(36,1),(24,1)):
            present=teeth-missing
            # Rising-to-rising periods: normal 2 half-ticks, and a final gap.
            rising_periods=[2]*(present-1)+[2*(missing+1)]
            self.assertEqual(sum(rising_periods),2*teeth)
            # Last falling-to-next-rising is the full gap minus the high half.
            self.assertEqual(rising_periods[-1]-1,2*missing+1)

if __name__=='__main__':unittest.main()
