"""A simulated Hamilton STAR: PyLabRobot's STAR firmware generator plus labsim physics."""

from typing import List, Optional, Union

from pylabrobot.legacy.liquid_handling.backends.hamilton.STAR_chatterbox import (
  STARChatterboxBackend,
)
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

from labsim.backends.liquid_handler import LiquidHandlerTiming, SimLiquidHandlerBackend
from labsim.simulation import Simulation


class SimSTARBackend(STARChatterboxBackend):
  """Every operation first runs labsim's physics (liquid, species, timing, error checks), then
  PyLabRobot's STAR backend, which builds the firmware commands a real STAR would receive and
  logs them at PLR's IO log level. A protocol error raises before any firmware is generated."""

  def __init__(
    self,
    sim: Simulation,
    timing: Optional[LiquidHandlerTiming] = None,
    name: str = "liquid_handler",
    **star_kwargs,
  ) -> None:
    super().__init__(**star_kwargs)
    self.physics = SimLiquidHandlerBackend(sim, timing=timing, name=name)

  async def pick_up_tips(self, ops: List[Pickup], use_channels: List[int], **kwargs):
    await self.physics.pick_up_tips(ops, use_channels)
    return await super().pick_up_tips(ops, use_channels, **kwargs)

  async def drop_tips(self, ops: List[Drop], use_channels: List[int], **kwargs):
    await self.physics.drop_tips(ops, use_channels)
    return await super().drop_tips(ops, use_channels, **kwargs)

  async def aspirate(self, ops: List[SingleChannelAspiration], use_channels: List[int], **kwargs):
    await self.physics.aspirate(ops, use_channels)
    return await super().aspirate(ops, use_channels, **kwargs)

  async def dispense(self, ops: List[SingleChannelDispense], use_channels: List[int], **kwargs):
    await self.physics.dispense(ops, use_channels)
    return await super().dispense(ops, use_channels, **kwargs)

  async def pick_up_tips96(self, pickup: PickupTipRack, **kwargs):
    await self.physics.pick_up_tips96(pickup)
    return await super().pick_up_tips96(pickup, **kwargs)

  async def drop_tips96(self, drop: DropTipRack, **kwargs):
    await self.physics.drop_tips96(drop)
    return await super().drop_tips96(drop, **kwargs)

  async def aspirate96(
    self, aspiration: Union[MultiHeadAspirationPlate, MultiHeadAspirationContainer], **kwargs
  ):
    await self.physics.aspirate96(aspiration)
    return await super().aspirate96(aspiration, **kwargs)

  async def dispense96(
    self, dispense: Union[MultiHeadDispensePlate, MultiHeadDispenseContainer], **kwargs
  ):
    await self.physics.dispense96(dispense)
    return await super().dispense96(dispense, **kwargs)

  async def pick_up_resource(self, pickup: ResourcePickup, **kwargs):
    await self.physics.pick_up_resource(pickup)
    return await super().pick_up_resource(pickup, **kwargs)

  async def move_picked_up_resource(self, move: ResourceMove, **kwargs):
    await self.physics.move_picked_up_resource(move)
    return await super().move_picked_up_resource(move, **kwargs)

  async def drop_resource(self, drop: ResourceDrop, **kwargs):
    await self.physics.drop_resource(drop)
    return await super().drop_resource(drop, **kwargs)
