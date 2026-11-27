"""A small workcell scheduler: jobs made of tasks, dispatched as their instruments come free.

A job is one sample's (e.g. one plate's) workflow: a sequence of tasks run in order. Jobs run
concurrently. Each task names the instruments it needs:

  uses     instruments busy for the duration of the task
  acquire  instruments the job takes at the start of the task and keeps afterwards, because the
           plate is now sitting on them (e.g. loading a plate onto the heater-shaker)
  release  instruments the job gives back at the end of the task (e.g. unloading that plate)

A task starts only when every instrument it needs, and does not already hold, is free. They are
granted all at once, never one by one, so two jobs cannot deadlock by each holding half of what
they need. Waiting tasks are granted in priority order (lower number first), then request order.

Runs on a `Simulation`'s virtual clock, so the schedule's times are simulated seconds.
"""

import asyncio
import itertools
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Set, Tuple

from labsim.protocol import ProtocolRecorder, _clock, _table
from labsim.simulation import Simulation


@dataclass
class Task:
  name: str
  run: Callable[[], Awaitable[Any]]
  uses: Sequence[str] = ()
  acquire: Sequence[str] = ()
  release: Sequence[str] = ()
  # Filled in by the scheduler.
  id: str = ""
  job: str = ""
  status: str = "pending"  # pending | waiting | running | done | failed | skipped
  queued_at: Optional[float] = None
  start: Optional[float] = None
  end: Optional[float] = None
  error: str = ""

  @property
  def instruments(self) -> List[str]:
    return list(dict.fromkeys([*self.uses, *self.acquire]))

  @property
  def waited(self) -> float:
    if self.queued_at is None or self.start is None:
      return 0.0
    return self.start - self.queued_at


@dataclass
class Job:
  name: str
  tasks: List[Task]
  priority: int = 0
  tag: str = ""  # one character for the Gantt chart
  sample: bool = True  # counts towards throughput (False for housekeeping jobs like pre-heating)
  holds: Dict[str, Tuple[float, Optional[float]]] = field(default_factory=dict)


@dataclass(order=True)
class _Request:
  priority: int
  seq: int
  needed: Set[str] = field(compare=False)
  granted: "asyncio.Future[None]" = field(compare=False)


class Scheduler:
  def __init__(
    self,
    sim: Simulation,
    instruments: Sequence[str],
    recorder: Optional[ProtocolRecorder] = None,
  ) -> None:
    self.sim = sim
    self.instruments = list(instruments)
    self.recorder = recorder
    self.jobs: List[Job] = []
    self._free: Set[str] = set(instruments)
    self._waiting: List[_Request] = []
    self._seq = itertools.count()
    self.start_time = 0.0
    self.end_time = 0.0

  def add_job(
    self,
    name: str,
    tasks: List[Task],
    priority: Optional[int] = None,
    tag: str = "",
    sample: bool = True,
  ) -> Job:
    priority = len(self.jobs) if priority is None else priority
    job = Job(name, tasks, priority, tag or name[-1], sample)
    for n, task in enumerate(tasks, start=1):
      task.id, task.job = f"{name}-T{n}", name
      unknown = set(task.instruments) | set(task.release)
      unknown -= set(self.instruments)
      if unknown:
        raise ValueError(f"{task.id} names unknown instruments {sorted(unknown)}")
    self.jobs.append(job)
    return job

  # Resource management.

  def _dispatch(self) -> None:
    for request in sorted(self._waiting):
      if request.needed <= self._free:
        self._free -= request.needed
        self._waiting.remove(request)
        request.granted.set_result(None)

  async def _obtain(self, needed: Set[str], priority: int) -> None:
    if not needed:
      return
    request = _Request(priority, next(self._seq), needed, asyncio.get_running_loop().create_future())
    self._waiting.append(request)
    self._dispatch()
    await request.granted

  def _give_back(self, instruments: Set[str]) -> None:
    self._free |= instruments
    self._dispatch()

  # Running.

  async def _run_task(self, job: Job, task: Task, held: Set[str]) -> Set[str]:
    now = self.sim.now
    task.status, task.queued_at = "waiting", now()
    needed = (set(task.uses) | set(task.acquire)) - held
    await self._obtain(needed, job.priority)
    held_during = held | needed
    for name in set(task.acquire) - held:
      job.holds[name] = (now(), None)
    task.status, task.start = "running", now()
    try:
      label = f"{task.id} {task.name}  [{', '.join(task.instruments)}]"
      if self.recorder:
        with self.recorder.in_step(label):
          await task.run()
      else:
        await task.run()
      task.status = "done"
    except Exception as e:  # a failed task aborts its job; other jobs carry on
      task.status, task.error = "failed", f"{type(e).__name__}: {e}"
      task.end = now()
      for name in held_during:
        start, _ = job.holds.get(name, (now(), None))
        if name in job.holds:
          job.holds[name] = (start, now())
      self._give_back(held_during)
      raise
    task.end = now()
    keep = (held | set(task.acquire)) - set(task.release)
    for name in held_during - keep:
      if name in job.holds:
        job.holds[name] = (job.holds[name][0], now())
    self._give_back(held_during - keep)
    return keep

  async def _run_job(self, job: Job) -> None:
    held: Set[str] = set()
    for i, task in enumerate(job.tasks):
      try:
        held = await self._run_task(job, task, held)
      except Exception:
        for skipped in job.tasks[i + 1 :]:
          skipped.status = "skipped"
        return
    if held:  # a job must not leave instruments occupied
      self._give_back(held)

  async def run(self) -> None:
    self.start_time = self.sim.now()
    await asyncio.gather(*(self._run_job(job) for job in sorted(self.jobs, key=lambda j: j.priority)))
    self.end_time = self.sim.now()

  # Reporting.

  @property
  def tasks(self) -> List[Task]:
    return [t for job in self.jobs for t in job.tasks]

  @property
  def makespan(self) -> float:
    return self.end_time - self.start_time

  def render_schedule(self) -> str:
    rows = []
    for t in sorted(self.tasks, key=lambda t: (t.start if t.start is not None else float("inf"), t.id)):
      rows.append((
        t.id, t.name, ", ".join(t.instruments),
        _clock(t.start) if t.start is not None else "-",
        _clock(t.end) if t.end is not None else "-",
        f"{t.end - t.start:.1f}" if t.start is not None and t.end is not None else "-",
        f"{t.waited:.1f}", t.status.upper() + (f" ({t.error})" if t.error else ""),
      ))
    head = ["task", "name", "instruments", "start", "end", "dur (s)", "queued (s)", "status"]
    return "\n".join(["SCHEDULE (as executed)"] + _table(head, rows))

  def render_gantt(self, width: int = 72) -> str:
    span = max(self.makespan, 1e-9)
    scale = width / span

    def cols(start: float, end: float) -> range:
      a = int((start - self.start_time) * scale)
      b = max(a + 1, int(round((end - self.start_time) * scale)))
      return range(a, min(b, width))

    lanes: Dict[str, List[str]] = {name: [" "] * width for name in self.instruments}
    for job in self.jobs:  # occupied: the job's plate sits on the instrument
      for name, (start, end) in job.holds.items():
        for c in cols(start, end if end is not None else self.end_time):
          lanes[name][c] = "."
    for job in self.jobs:  # busy: a task is running on the instrument
      for t in job.tasks:
        if t.start is None or t.end is None:
          continue
        for name in t.instruments:
          for c in cols(t.start, t.end):
            lanes[name][c] = job.tag if t.status != "failed" else "X"

    minutes = span / 60
    tick_every = next(s for s in (1, 2, 5, 10, 15, 30, 60, 120) if minutes / s <= 8)
    axis = [" "] * (width + 6)
    for m in range(0, int(minutes) + 1, tick_every):
      pos = int(m * 60 * scale)
      for i, ch in enumerate(f"{m}m"):
        if pos + i < len(axis):
          axis[pos + i] = ch
    pad = max(len(n) for n in self.instruments)
    out = ["GANTT (simulated time; one column = %.0f s)" % (span / width)]
    out.append(" " * (pad + 3) + "".join(axis).rstrip())
    for name in self.instruments:
      out.append(f"{name:<{pad}} |{''.join(lanes[name])}|")
    legend = ", ".join(f"{job.tag} = {job.name}" for job in self.jobs)
    out.append(f"{'':<{pad}}  legend: {legend}; '.' = plate waiting on instrument; X = failed")
    return "\n".join(out)

  def render_utilization(self) -> str:
    span = max(self.makespan, 1e-9)
    rows = []
    for name in self.instruments:
      busy = sum(
        t.end - t.start for t in self.tasks
        if name in t.instruments and t.start is not None and t.end is not None
      )
      occupied = sum(
        (end if end is not None else self.end_time) - start
        for job in self.jobs for n, (start, end) in job.holds.items() if n == name
      )
      rows.append((name, f"{busy:.0f}", f"{100 * busy / span:.0f}%", f"{occupied:.0f}"))
    samples = [job for job in self.jobs if job.sample]
    done = sum(1 for job in samples if all(t.status == "done" for t in job.tasks))
    out = ["UTILIZATION"]
    out += _table(["instrument", "busy (s)", "busy %", "plate on it (s)"], rows)
    out.append("")
    out.append(
      f"makespan {_clock(span)} ({span / 60:.1f} min); samples completed {done}/{len(samples)}; "
      f"throughput {done / (span / 3600):.1f} samples/h"
    )
    return "\n".join(out)

  def render(self) -> str:
    return "\n\n".join([self.render_schedule(), self.render_gantt(), self.render_utilization()])


__all__ = ["Job", "Scheduler", "Task"]
