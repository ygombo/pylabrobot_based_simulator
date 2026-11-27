"""What is physically in the simulated lab: species, mixtures, and where they are.

PyLabRobot's volume tracker records volume only. This model also records which species are in
each container and tip, which is what instruments like plate readers respond to.

Units: volume in uL, amount in nmol, concentration in uM (nmol/uL * 1000).
"""

from dataclasses import dataclass, field
from typing import Dict, Mapping, Tuple

from pylabrobot.resources import Container


class SimulationError(Exception):
  """The protocol asked a simulated instrument to do something the real one could not."""


@dataclass(frozen=True)
class Species:
  """A chemical species and how it shows up on a plate reader.

  Attributes:
    name: unique name.
    absorptivity: wavelength (nm) -> molar absorptivity (1 / (M * cm)).
    fluorescence: (excitation nm, emission nm) -> signal in RFU per uM.
    luminescence: signal in RLU per uM.
  """

  name: str
  absorptivity: Mapping[int, float] = field(default_factory=dict)
  fluorescence: Mapping[Tuple[int, int], float] = field(default_factory=dict)
  luminescence: float = 0.0


@dataclass
class Mixture:
  """A well-mixed liquid: total volume plus the amount of each species in it."""

  volume: float = 0.0
  amounts: Dict[str, float] = field(default_factory=dict)

  def concentration(self, species: str) -> float:
    """Concentration of `species` in uM; 0 for an empty mixture."""
    if self.volume <= 0:
      return 0.0
    return self.amounts.get(species, 0.0) / self.volume * 1000

  def add(self, other: "Mixture") -> None:
    self.volume += other.volume
    for name, amount in other.amounts.items():
      self.amounts[name] = self.amounts.get(name, 0.0) + amount

  def take(self, volume: float) -> "Mixture":
    """Remove `volume` uL, carrying a proportional share of every species."""
    if volume > self.volume + 1e-9:
      raise SimulationError(f"cannot take {volume} uL from a mixture holding {self.volume:.3f} uL")
    fraction = 0.0 if self.volume == 0 else volume / self.volume
    taken = Mixture(volume, {n: a * fraction for n, a in self.amounts.items()})
    self.volume = max(self.volume - volume, 0.0)
    for name in self.amounts:
      self.amounts[name] -= taken.amounts[name]
    return taken


class World:
  """Contents of every container and every pipetting tip in the simulation."""

  def __init__(self) -> None:
    self.species: Dict[str, Species] = {}
    self._containers: Dict[str, Mixture] = {}
    # Keyed by (head, channel): head is "channels" for the independent channels, "head96" for the
    # 96-head.
    self._tips: Dict[Tuple[str, int], Mixture] = {}

  def register(self, *species: Species) -> None:
    for s in species:
      self.species[s.name] = s

  def contents(self, container: Container) -> Mixture:
    """The mixture in `container`, created empty on first access."""
    return self._containers.setdefault(container.name, Mixture())

  def tip(self, head: str, channel: int) -> Mixture:
    return self._tips.setdefault((head, channel), Mixture())

  def discard_tip(self, head: str, channel: int) -> None:
    self._tips.pop((head, channel), None)

  def fill(self, container: Container, volume: float, **concentrations: float) -> None:
    """Add `volume` uL of liquid with the given species concentrations (uM) to `container`.

    Also updates the container's PyLabRobot volume tracker, so PLR's own checks stay in step.
    Example: `world.fill(well, 200, dye=100)` adds 200 uL of 100 uM dye.
    """
    unknown = set(concentrations) - set(self.species)
    if unknown:
      raise SimulationError(f"unregistered species {sorted(unknown)}: call world.register() first")
    amounts = {name: conc * volume / 1000 for name, conc in concentrations.items()}
    self.contents(container).add(Mixture(volume, amounts))
    container.tracker.set_volume(container.tracker.get_used_volume() + volume)

  def transfer_in(self, container: Container, mixture: Mixture) -> None:
    contents = self.contents(container)
    capacity = container.max_volume
    if contents.volume + mixture.volume > capacity + 1e-9:
      raise SimulationError(
        f"{container.name} would overflow: {contents.volume:.1f} + {mixture.volume:.1f} uL "
        f"exceeds its {capacity} uL capacity"
      )
    contents.add(mixture)

  def transfer_out(self, container: Container, volume: float) -> Mixture:
    contents = self.contents(container)
    if volume > contents.volume + 1e-9:
      raise SimulationError(
        f"{container.name} holds {contents.volume:.1f} uL, cannot aspirate {volume} uL "
        "(fill containers with world.fill() so the simulator knows what is in them)"
      )
    return contents.take(volume)
