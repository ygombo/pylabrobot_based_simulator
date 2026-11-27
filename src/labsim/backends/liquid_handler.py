"""A simulated liquid handler: independent channels, a 96-head, and a plate gripper arm."""

from dataclasses import dataclass
from typing import List, Optional, Union

from pylabrobot.legacy.liquid_handling.backends.backend import LiquidHandlerBackend
from pylabrobot.legacy.liquid_handling.standard import (
  Drop,
  DropTipRack,
  MultiHeadAspirationContainer,
  MultiHeadAspirationPlate,
  MultiHeadDispenseContainer,
  MultiHeadDispensePlate,
  Pickup,
  PickupTipRack,
  ResourceDrop,
  ResourceMove,
  ResourcePickup,
  SingleChannelAspiration,
  SingleChannelDispense,
)
from pylabrobot.resources import Tip

from labsim.simulation import Simulation
from labsim.world import SimulationError

CHANNELS = "channels"
HEAD96 = "head96"


@dataclass
class LiquidHandlerTiming:
  """How long each operation takes, in seconds. Tune these against a real instrument's logs."""

  pick_up_tips: float = 6.0
  drop_tips: float = 5.0
  aspirate_overhead: float = 4.0
  dispense_overhead: float = 3.0
  default_flow_rate: float = 100.0  # uL/s, used when the op does not specify one
  pick_up_resource: float = 8.0
  move_resource: float = 4.0
  drop_resource: float = 6.0


class SimLiquidHandlerBackend(LiquidHandlerBackend):
  """Moves liquid through `sim.world` and spends time on `sim.clock`.

  Catches, independently of PLR's trackers: aspirating more than a container holds, dispensing
  more than a tip holds, overflowing a container, and exceeding a tip's capacity.
  """

  def __init__(
    self,
    sim: Simulation,
    num_channels: int = 8,
    timing: Optional[LiquidHandlerTiming] = None,
    name: str = "liquid_handler",
  ) -> None:
    super().__init__()
    self.sim = sim
    self.timing = timing or LiquidHandlerTiming()
    self.name = name
    self._num_channels = num_channels
    self._num_arms = 1
    self._head96_installed = True

  @property
  def num_channels(self) -> int:
    return self._num_channels

  async def setup(self) -> None:
    await super().setup()
    self.sim.record(self.name, "setup")

  async def stop(self) -> None:
    self.sim.record(self.name, "stop")

  def can_pick_up_tip(self, channel_idx: int, tip: Tip) -> bool:
    return True

  def _flow_time(self, volume: float, flow_rate: Optional[float]) -> float:
    return volume / (flow_rate or self.timing.default_flow_rate)

  # Independent channels.

  async def pick_up_tips(self, ops: List[Pickup], use_channels: List[int], **kwargs) -> None:
    for ch in use_channels:
      self.sim.world.discard_tip(CHANNELS, ch)
    await self.sim.operation(
      self.name, "pick_up_tips", self.timing.pick_up_tips,
      spots=[op.resource.name for op in ops], channels=use_channels,
    )

  async def drop_tips(self, ops: List[Drop], use_channels: List[int], **kwargs) -> None:
    for ch in use_channels:
      self.sim.world.discard_tip(CHANNELS, ch)
    await self.sim.operation(
      self.name, "drop_tips", self.timing.drop_tips,
      targets=[op.resource.name for op in ops], channels=use_channels,
    )

  async def aspirate(
    self, ops: List[SingleChannelAspiration], use_channels: List[int], **kwargs
  ) -> None:
    world = self.sim.world
    for op, ch in zip(ops, use_channels):
      tip = world.tip(CHANNELS, ch)
      if tip.volume + op.volume > op.tip.maximal_volume + 1e-9:
        raise SimulationError(
          f"channel {ch}: {tip.volume + op.volume:.1f} uL exceeds the tip's "
          f"{op.tip.maximal_volume} uL capacity"
        )
      tip.add(world.transfer_out(op.resource, op.volume))
    duration = self.timing.aspirate_overhead + max(
      self._flow_time(op.volume, op.flow_rate) for op in ops
    )
    await self.sim.operation(
      self.name, "aspirate", duration,
      sources=[op.resource.name for op in ops], volumes=[op.volume for op in ops],
    )

  async def dispense(
    self, ops: List[SingleChannelDispense], use_channels: List[int], **kwargs
  ) -> None:
    world = self.sim.world
    for op, ch in zip(ops, use_channels):
      tip = world.tip(CHANNELS, ch)
      if op.volume > tip.volume + 1e-9:
        raise SimulationError(f"channel {ch} holds {tip.volume:.1f} uL, cannot dispense {op.volume}")
      world.transfer_in(op.resource, tip.take(op.volume))
    duration = self.timing.dispense_overhead + max(
      self._flow_time(op.volume, op.flow_rate) for op in ops
    )
    await self.sim.operation(
      self.name, "dispense", duration,
      targets=[op.resource.name for op in ops], volumes=[op.volume for op in ops],
    )

  # 96-head. Head channel i serves the i-th well of the op.

  async def pick_up_tips96(self, pickup: PickupTipRack, **kwargs) -> None:
    for i in range(96):
      self.sim.world.discard_tip(HEAD96, i)
    await self.sim.operation(
      self.name, "pick_up_tips96", self.timing.pick_up_tips, rack=pickup.resource.name
    )

  async def drop_tips96(self, drop: DropTipRack, **kwargs) -> None:
    for i in range(96):
      self.sim.world.discard_tip(HEAD96, i)
    await self.sim.operation(
      self.name, "drop_tips96", self.timing.drop_tips, target=drop.resource.name
    )

  async def aspirate96(
    self, aspiration: Union[MultiHeadAspirationPlate, MultiHeadAspirationContainer], **kwargs
  ) -> None:
    world = self.sim.world
    if isinstance(aspiration, MultiHeadAspirationPlate):
      for i, well in enumerate(aspiration.wells):
        world.tip(HEAD96, i).add(world.transfer_out(well, aspiration.volume))
      source = aspiration.wells[0].parent.name if aspiration.wells[0].parent else "plate"
    else:
      n = len(aspiration.tips)
      bulk = world.transfer_out(aspiration.container, aspiration.volume * n)
      for i in range(n):
        world.tip(HEAD96, i).add(bulk.take(aspiration.volume))
      source = aspiration.container.name
    duration = self.timing.aspirate_overhead + self._flow_time(
      aspiration.volume, aspiration.flow_rate
    )
    await self.sim.operation(
      self.name, "aspirate96", duration, source=source, volume=aspiration.volume
    )

  async def dispense96(
    self, dispense: Union[MultiHeadDispensePlate, MultiHeadDispenseContainer], **kwargs
  ) -> None:
    world = self.sim.world
    if isinstance(dispense, MultiHeadDispensePlate):
      for i, well in enumerate(dispense.wells):
        world.transfer_in(well, world.tip(HEAD96, i).take(dispense.volume))
      target = dispense.wells[0].parent.name if dispense.wells[0].parent else "plate"
    else:
      for i in range(len(dispense.tips)):
        world.transfer_in(dispense.container, world.tip(HEAD96, i).take(dispense.volume))
      target = dispense.container.name
    duration = self.timing.dispense_overhead + self._flow_time(dispense.volume, dispense.flow_rate)
    await self.sim.operation(
      self.name, "dispense96", duration, target=target, volume=dispense.volume
    )

  # Gripper arm. The LiquidHandler front end updates the resource tree; we only spend time.

  async def pick_up_resource(self, pickup: ResourcePickup, **kwargs) -> None:
    await self.sim.operation(
      self.name, "pick_up_resource", self.timing.pick_up_resource, resource=pickup.resource.name
    )

  async def move_picked_up_resource(self, move: ResourceMove, **kwargs) -> None:
    await self.sim.operation(
      self.name, "move_resource", self.timing.move_resource, resource=move.resource.name
    )

  async def drop_resource(self, drop: ResourceDrop, **kwargs) -> None:
    await self.sim.operation(
      self.name, "drop_resource", self.timing.drop_resource, resource=drop.resource.name
    )
