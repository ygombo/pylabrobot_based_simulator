"""Deterministic virtual time for asyncio code.

PyLabRobot front ends wait with `asyncio.sleep` (shaking for a duration, holding a temperature,
polling `wait_for_temperature`). While a `VirtualClock` is patched in, those sleeps advance
simulated time instead of wall time, so an hour-long incubation finishes instantly and every run
produces the same timeline.

Concurrent sleepers wake in order of their wake time, so `asyncio.gather` of two instruments
working in parallel takes as long as the slower one, not the sum.

Limitation: only `asyncio.sleep` takes time. Any other await (real I/O, a lock) counts as instant.
"""

import asyncio
import contextlib
import itertools
from asyncio.tasks import sleep as _real_sleep  # the original, unaffected by patching
from typing import Any, Dict, Iterator
from unittest import mock

# A sleeper must see itself as the earliest waker this many times in a row before time moves.
# This lets other ready tasks run (and register their own sleeps) first.
_SETTLE_PASSES = 3


class VirtualClock:
  """Simulated seconds since the start of the simulation."""

  def __init__(self, start: float = 0.0) -> None:
    self._now = start
    self._sleepers: Dict[int, float] = {}
    self._tokens = itertools.count()

  def now(self) -> float:
    return self._now

  async def sleep(self, delay: float, result: Any = None) -> Any:
    """Wait `delay` simulated seconds. Same signature as `asyncio.sleep`."""
    if delay <= 0:
      await _real_sleep(0)
      return result
    token = next(self._tokens)
    wake = self._now + delay
    self._sleepers[token] = wake
    try:
      passes = 0
      while passes < _SETTLE_PASSES:
        await _real_sleep(0)
        passes = passes + 1 if wake <= min(self._sleepers.values()) else 0
      self._now = max(self._now, wake)
    finally:
      del self._sleepers[token]
    return result

  @contextlib.contextmanager
  def patch_asyncio_sleep(self) -> Iterator["VirtualClock"]:
    """Route every `asyncio.sleep` call through this clock while the context is open."""
    with mock.patch.object(asyncio, "sleep", self.sleep):
      yield self
