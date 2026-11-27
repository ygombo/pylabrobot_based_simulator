"""A simulated plate reader whose signals come from what is actually in each well."""

import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from pylabrobot.legacy.plate_reading.backend import PlateReaderBackend
from pylabrobot.resources import Plate, Well

from labsim.simulation import Simulation
from labsim.world import Mixture, SimulationError


@dataclass
class PlateReaderModel:
  """Optical and timing parameters. Defaults are typical of a monochromator reader."""

  blank_od: float = 0.04  # plate + buffer background
  od_noise_sd: float = 0.002
  max_od: float = 4.0  # detector saturation
  fluorescence_background: float = 50.0  # RFU
  fluorescence_cv: float = 0.02  # proportional noise
  max_rfu: float = 260_000.0
  luminescence_background: float = 20.0  # RLU
  luminescence_cv: float = 0.05
  read_overhead: float = 5.0  # s per read
  time_per_well: float = 0.25  # s per well
  temperature: float = 25.0


class SimPlateReaderBackend(PlateReaderBackend):
  """Absorbance by Beer-Lambert over the liquid column; fluorescence and luminescence linear in
  concentration. A read needs the drawer closed."""

  def __init__(
    self,
    sim: Simulation,
    model: Optional[PlateReaderModel] = None,
    name: str = "plate_reader",
  ) -> None:
    super().__init__()
    self.sim = sim
    self.model = model or PlateReaderModel()
    self.name = name
    self.drawer_open = False

  async def setup(self) -> None:
    self.sim.record(self.name, "setup")

  async def stop(self) -> None:
    self.sim.record(self.name, "stop")

  async def open(self) -> None:
    self.drawer_open = True
    await self.sim.operation(self.name, "open", 3.0)

  async def close(self, plate: Optional[Plate]) -> None:
    self.drawer_open = False
    await self.sim.operation(self.name, "close", 3.0, plate=plate.name if plate else None)

  @staticmethod
  def path_length_cm(well: Well, volume: float) -> float:
    """Height of the liquid column, in cm."""
    if volume <= 0:
      return 0.0
    try:
      height_mm = well.compute_height_from_volume(volume)
    except NotImplementedError:
      height_mm = volume / (well.get_size_x() * well.get_size_y())  # uL = mm^3
    return height_mm / 10

  def _absorbance(self, well: Well, mixture: Mixture, wavelength: int) -> float:
    path = self.path_length_cm(well, mixture.volume)
    od = self.model.blank_od
    for name in mixture.amounts:
      epsilon = self.sim.world.species[name].absorptivity.get(wavelength, 0.0)
      od += epsilon * mixture.concentration(name) * 1e-6 * path  # uM -> M
    od += self.sim.rng.gauss(0.0, self.model.od_noise_sd)
    return min(od, self.model.max_od)

  def _fluorescence(self, mixture: Mixture, ex: int, em: int) -> float:
    signal = self.model.fluorescence_background
    for name in mixture.amounts:
      k = self.sim.world.species[name].fluorescence.get((ex, em), 0.0)
      signal += k * mixture.concentration(name)
    signal *= 1 + self.sim.rng.gauss(0.0, self.model.fluorescence_cv)
    return min(max(signal, 0.0), self.model.max_rfu)

  def _luminescence(self, mixture: Mixture) -> float:
    signal = self.model.luminescence_background
    for name in mixture.amounts:
      signal += self.sim.world.species[name].luminescence * mixture.concentration(name)
    signal *= 1 + self.sim.rng.gauss(0.0, self.model.luminescence_cv)
    return max(signal, 0.0)

  async def _read(
    self, mode: str, plate: Plate, wells: List[Well], signal: Callable[[Well, Mixture], float]
  ) -> List[List[Optional[float]]]:
    if self.drawer_open:
      raise SimulationError(f"{self.name}: close the drawer before reading")
    data: List[List[Optional[float]]] = [
      [None] * plate.num_items_x for _ in range(plate.num_items_y)
    ]
    for well in wells:
      data[well.get_row()][well.get_column()] = round(
        signal(well, self.sim.world.contents(well)), 4
      )
    duration = self.model.read_overhead + self.model.time_per_well * len(wells)
    await self.sim.operation(self.name, mode, duration, plate=plate.name, wells=len(wells))
    return data

  def _record(self, data: List[List[Optional[float]]], **extra: int) -> Dict:
    # "time" is wall-clock as PLR expects; the simulated time is on the timeline.
    return {"time": time.time(), "temperature": self.model.temperature, "data": data, **extra}

  async def read_absorbance(self, plate: Plate, wells: List[Well], wavelength: int) -> List[Dict]:
    data = await self._read(
      "read_absorbance", plate, wells, lambda w, m: self._absorbance(w, m, wavelength)
    )
    return [self._record(data, wavelength=wavelength)]

  async def read_fluorescence(
    self,
    plate: Plate,
    wells: List[Well],
    excitation_wavelength: int,
    emission_wavelength: int,
    focal_height: float,
  ) -> List[Dict]:
    data = await self._read(
      "read_fluorescence", plate, wells,
      lambda w, m: self._fluorescence(m, excitation_wavelength, emission_wavelength),
    )
    return [
      self._record(data, ex_wavelength=excitation_wavelength, em_wavelength=emission_wavelength)
    ]

  async def read_luminescence(
    self, plate: Plate, wells: List[Well], focal_height: float
  ) -> List[Dict]:
    data = await self._read("read_luminescence", plate, wells, lambda w, m: self._luminescence(m))
    return [self._record(data)]
