import asyncio
import time
import unittest

from labsim import VirtualClock


class TestVirtualClock(unittest.IsolatedAsyncioTestCase):
  async def test_sleep_advances_virtual_time_not_wall_time(self):
    clock = VirtualClock()
    start = time.monotonic()
    await clock.sleep(3600)
    self.assertEqual(clock.now(), 3600)
    self.assertLess(time.monotonic() - start, 1.0)

  async def test_parallel_sleeps_take_the_longest_not_the_sum(self):
    clock = VirtualClock()
    await asyncio.gather(clock.sleep(10), clock.sleep(25), clock.sleep(5))
    self.assertEqual(clock.now(), 25)

  async def test_sequential_work_in_parallel_branches(self):
    clock = VirtualClock()
    order = []

    async def branch(name, steps):
      for step in steps:
        await clock.sleep(step)
        order.append((name, clock.now()))

    await asyncio.gather(branch("a", [10, 10, 10]), branch("b", [15, 15]))
    self.assertEqual(clock.now(), 30)
    self.assertEqual([t for _, t in order], sorted(t for _, t in order))

  async def test_patch_routes_asyncio_sleep_and_restores_it(self):
    clock = VirtualClock()
    original = asyncio.sleep
    with clock.patch_asyncio_sleep():
      await asyncio.sleep(120)
    self.assertEqual(clock.now(), 120)
    self.assertIs(asyncio.sleep, original)


if __name__ == "__main__":
  unittest.main()
