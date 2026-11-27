import unittest
from typing import List, Tuple

from labsim import Simulation
from labsim.scheduler import Scheduler, Task


def overlaps(a: Task, b: Task) -> bool:
  assert a.start is not None and a.end is not None and b.start is not None and b.end is not None
  return a.start < b.end and b.start < a.end


class TestScheduler(unittest.IsolatedAsyncioTestCase):
  async def asyncSetUp(self):
    self.sim = Simulation().__enter__()
    self.log: List[Tuple[str, float]] = []

  async def asyncTearDown(self):
    self.sim.__exit__(None, None, None)

  def work(self, label: str, seconds: float):
    async def run():
      self.log.append((label, self.sim.now()))
      await self.sim.clock.sleep(seconds)

    return run

  async def test_one_task_per_instrument_at_a_time(self):
    s = Scheduler(self.sim, ["arm"])
    s.add_job("A", [Task("a", self.work("a", 10), uses=["arm"])])
    s.add_job("B", [Task("b", self.work("b", 10), uses=["arm"])])
    await s.run()
    a, b = s.tasks
    self.assertFalse(overlaps(a, b))
    self.assertEqual(s.makespan, 20)
    self.assertEqual(b.waited, 10)

  async def test_different_instruments_run_in_parallel(self):
    s = Scheduler(self.sim, ["arm", "reader"])
    s.add_job("A", [Task("a", self.work("a", 10), uses=["arm"])])
    s.add_job("B", [Task("b", self.work("b", 30), uses=["reader"])])
    await s.run()
    self.assertEqual(s.makespan, 30)

  async def test_held_instrument_blocks_other_jobs_until_released(self):
    s = Scheduler(self.sim, ["arm", "shaker"])
    for name in ("A", "B"):
      s.add_job(name, [
        Task("load", self.work(f"{name} load", 5), uses=["arm"], acquire=["shaker"]),
        Task("shake", self.work(f"{name} shake", 100), uses=["shaker"]),
        Task("unload", self.work(f"{name} unload", 5), uses=["arm"], release=["shaker"]),
      ])
    await s.run()
    a_unload = s.jobs[0].tasks[2]
    b_load = s.jobs[1].tasks[0]
    assert a_unload.end is not None and b_load.start is not None
    self.assertGreaterEqual(b_load.start, a_unload.end)  # B's plate never meets A's
    self.assertEqual(s.makespan, 220)

  async def test_priority_decides_who_goes_first(self):
    s = Scheduler(self.sim, ["arm"])
    s.add_job("low", [Task("x", self.work("low", 10), uses=["arm"])], priority=5)
    s.add_job("high", [Task("x", self.work("high", 10), uses=["arm"])], priority=1)
    await s.run()
    self.assertEqual([label for label, _ in self.log], ["high", "low"])

  async def test_all_or_nothing_grant_avoids_deadlock(self):
    # A holds the shaker and needs the arm; B holds the arm briefly and wants the shaker.
    s = Scheduler(self.sim, ["arm", "shaker"])
    s.add_job("A", [
      Task("load", self.work("A load", 5), uses=["arm"], acquire=["shaker"]),
      Task("unload", self.work("A unload", 5), uses=["arm"], release=["shaker"]),
    ])
    s.add_job("B", [
      Task("load", self.work("B load", 5), uses=["arm"], acquire=["shaker"]),
      Task("unload", self.work("B unload", 5), uses=["arm"], release=["shaker"]),
    ])
    await s.run()
    self.assertTrue(all(t.status == "done" for t in s.tasks))

  async def test_failure_aborts_only_its_job(self):
    async def boom():
      raise RuntimeError("jam")

    s = Scheduler(self.sim, ["arm", "shaker"])
    s.add_job("A", [
      Task("load", boom, uses=["arm"], acquire=["shaker"]),
      Task("unload", self.work("A unload", 5), uses=["arm"], release=["shaker"]),
    ])
    s.add_job("B", [Task("work", self.work("B", 5), uses=["arm", "shaker"])])
    await s.run()
    self.assertEqual([t.status for t in s.jobs[0].tasks], ["failed", "skipped"])
    self.assertEqual(s.jobs[1].tasks[0].status, "done")
    self.assertIn("X", s.render_gantt())

  async def test_unknown_instrument_is_rejected(self):
    s = Scheduler(self.sim, ["arm"])
    with self.assertRaises(ValueError):
      s.add_job("A", [Task("x", self.work("x", 1), uses=["robot"])])


if __name__ == "__main__":
  unittest.main()
