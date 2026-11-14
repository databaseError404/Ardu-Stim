"""Mathematical scheduler reference checks, not AVR execution or hardware tests."""
import unittest

MASK=0xffffffff

def step(now, deadline, period, busy):
    delta=(now-deadline)&MASK
    if delta&0x80000000:
        return deadline,0,False
    cycles=delta//period+1
    skipped=cycles if busy else cycles-1
    return (deadline+cycles*period)&MASK,skipped,not busy

class NativeScheduleModelTests(unittest.TestCase):
    def test_late_idle_starts_one_cycle_but_busy_keeps_old(self):
        self.assertEqual(step(1000,1000,1000,False),(2000,0,True))
        self.assertEqual(step(3500,1000,1000,False),(4000,2,True))
        self.assertEqual(step(3500,1000,1000,True),(4000,3,False))
        self.assertEqual(step(999,1000,1000,False),(1000,0,False))

    def test_deadline_phase_across_micros_rollover(self):
        deadline=0xfffffff0
        self.assertEqual(step(0x20,deadline,1000,False),((deadline+1000)&MASK,0,True))
        self.assertEqual(step(0xffffffe0,deadline,1000,False),(deadline,0,False))
        next_due,skipped,start=step(0x1000,deadline,1000,False)
        self.assertEqual(skipped,4)
        self.assertTrue(start)
        self.assertEqual(next_due,(deadline+5000)&MASK)

if __name__=='__main__':unittest.main()
