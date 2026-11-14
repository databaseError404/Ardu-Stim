"""Real iterator with incoming fake UART bytes; no physical port."""
import struct
import unittest
from host.bench_client import BenchClient,Frame,encode_frame

class IncomingTransport:
    is_open=True
    def __init__(self, data):self.data=bytearray(data)
    def read(self,n=1):
        value=bytes(self.data[:n]);del self.data[:n];return value
    def write(self,_):raise AssertionError('Iterator must not send ADC requests')
    def close(self):self.is_open=False

class StreamIteratorTests(unittest.TestCase):
    def test_count_only_blocks_for_uart_instead_of_nonblocking_spin(self):
        data=b''.join(encode_frame(Frame(3,0,0xa0,struct.pack('<BBIIhB',5,channel,index,1000*index,123,0))) for index,channel in enumerate((6,7),1))
        with BenchClient(transport=IncomingTransport(data),boot_wait=0) as client:
            original=client.get_event
            def checked_wait(timeout=None):
                self.assertIsNotNone(timeout,'Count-only iteration must read UART with a finite wait')
                self.assertGreater(timeout,0)
                return original(timeout)
            client.get_event=checked_wait
            values=list(client.iter_events(count=2))
            self.assertEqual([s.channel for s in values],[6,7])
            self.assertEqual([s.sample_number for s in values],[1,2])

if __name__=='__main__':unittest.main()
