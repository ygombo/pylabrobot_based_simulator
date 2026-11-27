"""A simulated heater-shaker with a first-order thermal response."""

import math
from dataclasses import dataclass
from typing import Optional

from pylabrobot.legacy.heating_shaking.backend import HeaterShakerBackend

from labsim.simulation import Simulation
from labsim.world import SimulationError


@dataclass
class HeaterShakerModel:
  """Physical limits and thermal behaviour.

  Temperature follows T(t) = T_set + (T_0 - T_set) * exp(-t / time_constant). Without active
  cooling, a setpoint below the current temperature relaxes towards ambient instead.
  """

  ambient: float = 22.0
  time_constant: float = 180.0  # s
  min_temperature: float = 4.0
  max_temperature: float = 105.0
  min_speed: float = 200.0  # rpm
  max_speed: float = 3000.0  # rpm
  active_cooling: bool = False
  has_lock: bool = True


class SimHeaterShakerBackend(HeaterShakerBackend):
  """Shaking needs a locked plate (when the device has a lock); speeds and temperatures are
  checked against the model's limits."""

  def __init__(
    self,
    sim: Simulation,
    model: Optional[HeaterShakerModel] = None,
    name: str = "heater_shaker",
  ) -> None:
    super().__init__()
    self.sim = sim
    self.model = model or HeaterShakerModel()
    self.name = name
    self.locked = False
    self.speed = 0.0
    self._t0 = 0.0
    self._temp0 = self.model.ambient
    self._setpoint = self.model.ambient

  @property
  def supports_active_cooling(self) -> bool:
    return self.model.active_cooling

  @property
  def supports_locking(self) -> bool:
    return self.model.has_lock

  async def setup(self) -> None:
    self._t0 = self.sim.now()
    self.sim.record(self.name, "setup")

  async def stop(self) -> None:
    self.speed = 0.0
    self.sim.record(self.name, "stop")

  # Temperature.

  def _temperature_at(self, t: float) -> float:
    return self._setpoint + (self._temp0 - self._setpoint) * math.exp(
      -(t - self._t0) / self.model.time_constant
    )

  def _retarget(self, setpoint: float) -> None:
    now = self.sim.now()
    self._temp0 = self._temperature_at(now)
    self._t0 = now
    self._setpoint = setpoint

  async def set_temperature(self, temperature: float) -> None:
    if not self.model.min_temperature <= temperature <= self.model.max_temperature:
      raise SimulationError(
        f"{self.name}: {temperature} C is outside "
        f"{self.model.min_temperature}-{self.model.max_temperature} C"
      )
    current = self._temperature_at(self.sim.now())
    target = temperature
    if temperature < current and not self.model.active_cooling:
      target = max(temperature, self.model.ambient)  # can only coast down towards ambient
    self._retarget(target)
    self.sim.record(self.name, "set_temperature", setpoint=temperature)

  async def get_current_temperature(self) -> float:
    return round(self._temperature_at(self.sim.now()), 2)

  async def deactivate(self) -> None:
    self._retarget(self.model.ambient)
    self.sim.record(self.name, "deactivate")

  # Shaking.

  async def lock_plate(self) -> None:
    self.locked = True
    await self.sim.operation(self.name, "lock_plate", 1.0)

  async def unlock_plate(self) -> None:
    if self.speed > 0:
      raise SimulationError(f"{self.name}: cannot unlock while shaking")
    self.locked = False
    await self.sim.operation(self.name, "unlock_plate", 1.0)

  async def start_shaking(self, speed: float) -> None:
    if not self.model.min_speed <= speed <= self.model.max_speed:
      raise SimulationError(
        f"{self.name}: {speed} rpm is outside {self.model.min_speed}-{self.model.max_speed} rpm"
      )
    if self.model.has_lock and not self.locked:
      raise SimulationError(f"{self.name}: lock the plate before shaking")
    self.speed = speed
    self.sim.record(self.name, "start_shaking", speed=speed)

  async def stop_shaking(self) -> None:
    self.speed = 0.0
    self.sim.record(self.name, "stop_shaking")
