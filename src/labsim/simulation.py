"""The shared simulation context every simulated instrument is attached to."""

import contextlib
import logging
import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from labsim.clock import VirtualClock
from labsim.world import World

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Event:
  """One instrument operation on the simulated timeline (times in simulated seconds)."""

  start: float
  end: float
  device: str
  action: str
  details: Dict[str, Any] = field(default_factory=dict)

  @property
  def duration(self) -> float:
    return self.end - self.start


class Simulation:
  """Virtual clock, world model, random source and timeline shared by all simulated instruments.

  Use it as a context manager so `asyncio.sleep` runs on virtual time:

      with Simulation(seed=1) as sim:
        lh = LiquidHandler(backend=SimLiquidHandlerBackend(sim), deck=STARLetDeck())
        ...
  """

  def __init__(self, seed: Optional[int] = 0) -> None:
    self.clock = VirtualClock()
    self.world = World()
    self.rng = random.Random(seed)
    self.timeline: List[Event] = []
    self._patch: Optional[contextlib.AbstractContextManager] = None

  def now(self) -> float:
    return self.clock.now()

  async def operation(self, device: str, action: str, duration: float, **details: Any) -> Event:
    """Spend `duration` simulated seconds on an operation and record it on the timeline."""
    start = self.clock.now()
    await self.clock.sleep(duration)
    event = Event(start, self.clock.now(), device, action, details)
    self.timeline.append(event)
    logger.debug("[t=%.1fs] %s.%s %s", event.end, device, action, details)
    return event

  def record(self, device: str, action: str, **details: Any) -> Event:
    """Record an instantaneous event (a state change, a setpoint)."""
    now = self.clock.now()
    event = Event(now, now, device, action, details)
    self.timeline.append(event)
    return event

  def format_timeline(self) -> str:
    lines = [f"{'start':>8} {'end':>8}  {'device':<16} action"]
    for e in self.timeline:
      lines.append(f"{e.start:8.1f} {e.end:8.1f}  {e.device:<16} {e.action}")
    return "\n".join(lines)

  def __enter__(self) -> "Simulation":
    self._patch = self.clock.patch_asyncio_sleep()
    self._patch.__enter__()
    return self

  def __exit__(self, *exc_info: Any) -> None:
    assert self._patch is not None
    self._patch.__exit__(*exc_info)
    self._patch = None
