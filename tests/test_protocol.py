import unittest

from pylabrobot.legacy.heating_shaking import HeaterShaker
from pylabrobot.legacy.liquid_handling import LiquidHandler
from pylabrobot.resources import (
  Coordinate,
  cor_96_wellplate_360uL_Fb,
  hamilton_96_tiprack_1000uL_filter,
  hamilton_plate_carrier_L5_ac,
  hamilton_tip_carrier_L5,
  set_tip_tracking,
  set_volume_tracking,
)
from pylabrobot.resources.hamilton import STARLetDeck

from labsim import SimHeaterShakerBackend, SimSTARBackend, Simulation, SimulationError, Species
from labsim.protocol import ProtocolRecorder


class TestProtocolRecorder(unittest.IsolatedAsyncioTestCase):
  async def asyncSetUp(self):
    set_tip_tracking(True)
    set_volume_tracking(True)
    self.sim = Simulation(seed=0).__enter__()
    self.sim.world.register(Species("dye"))
    deck = STARLetDeck()
    self.lh = LiquidHandler(backend=SimSTARBackend(self.sim), deck=deck)
    tip_carrier = hamilton_tip_carrier_L5(name="tip_carrier")
    tip_carrier[0] = self.tips = hamilton_96_tiprack_1000uL_filter(name="tips")
    deck.assign_child_resource(tip_carrier, track=1)
    plate_carrier = hamilton_plate_carrier_L5_ac(name="plate_carrier")
    plate_carrier[0] = self.plate = cor_96_wellplate_360uL_Fb(name="plate")
    deck.assign_child_resource(plate_carrier, track=9)
    self.hs = HeaterShaker(
      name="hs", size_x=147.0, size_y=104.0, size_z=70.0,
      backend=SimHeaterShakerBackend(self.sim), child_location=Coordinate(9.66, 9.26, 65.0),
    )
    deck.assign_child_resource(self.hs, location=Coordinate(500.0, 150.0, 100.0))
    self.protocol = ProtocolRecorder("test", sim=self.sim)
    self.protocol.add_instrument(self.lh, "liquid_handler", "STARlet", firmware=True)
    self.protocol.add_instrument(self.hs, "heater_shaker", "heater-shaker")
    await self.lh.setup()
    await self.hs.setup()

  async def asyncTearDown(self):
    self.sim.__exit__(None, None, None)

  async def test_records_steps_operations_firmware_and_driver_commands(self):
    self.sim.world.fill(self.plate.get_well("A1"), 100.0, dye=10.0)
    with self.protocol.recording():
      self.protocol.step("Transfer")
      await self.lh.pick_up_tips(self.tips["A1"])
      await self.lh.aspirate(self.plate["A1"], vols=[50.0])
      self.protocol.step("Shake")
      await self.hs.shake(speed=800, duration=60)
    text = self.protocol.render()

    self.assertIn("Step 1. Transfer", text)
    self.assertIn("pick up tips from tips A1", text)
    self.assertIn("aspirate 50 uL x1 from plate A1", text)
    self.assertIn("firmware > C0TP", text)
    self.assertIn("# aspirate", text)
    self.assertIn("Step 2. Shake", text)
    self.assertIn("driver   > start_shaking(speed=800)", text)
    self.assertLess(text.index("C0AS"), text.index("Step 2. Shake"))

  async def test_physics_error_raises_before_any_firmware_is_sent(self):
    set_volume_tracking(False)  # so labsim's check fires, not PLR's own volume tracker
    with self.protocol.recording():
      await self.lh.pick_up_tips(self.tips["A1"])
      with self.assertRaises(SimulationError):
        await self.lh.aspirate(self.plate["A1"], vols=[50.0])  # the well is empty
    commands = [c.text for op in self.protocol.operations for c in op.commands]
    self.assertTrue(any(c.startswith("C0TP") for c in commands))
    self.assertFalse(any(c.startswith("C0AS") for c in commands))

  async def test_repeated_polls_are_collapsed(self):
    with self.protocol.recording():
      await self.hs.set_temperature(37.0)
      await self.hs.wait_for_temperature()
    polls = [
      c for op in self.protocol.operations for c in op.commands
      if c.text == "get_current_temperature()"
    ]
    self.assertGreater(max(c.repeat for c in polls), 100)


if __name__ == "__main__":
  unittest.main()
