import math
import unittest

from pylabrobot.resources import set_tip_tracking, set_volume_tracking

from labsim import HeaterShakerModel, Simulation, SimulationError, Species
from labsim.workcell import build_assay_workcell

DYE = Species("dye", absorptivity={450: 20_000.0}, fluorescence={(485, 520): 100.0})


class WorkcellTestCase(unittest.IsolatedAsyncioTestCase):
  async def asyncSetUp(self):
    set_tip_tracking(False)  # keep tests independent of PLR's global tracker state
    set_volume_tracking(False)
    self.sim = Simulation(seed=0).__enter__()
    self.sim.world.register(DYE)
    self.wc = build_assay_workcell(self.sim)
    await self.wc.setup()

  async def asyncTearDown(self):
    self.sim.__exit__(None, None, None)


class TestLiquidHandler(WorkcellTestCase):
  async def test_transfer_moves_species(self):
    world, wc = self.sim.world, self.wc
    world.fill(wc.source.get_well("A1"), 200.0, dye=100.0)
    world.fill(wc.assay.get_well("A1"), 100.0)
    await wc.lh.pick_up_tips(wc.tips["A1"])
    await wc.lh.aspirate(wc.source["A1"], vols=[100.0])
    await wc.lh.dispense(wc.assay["A1"], vols=[100.0])
    self.assertAlmostEqual(world.contents(wc.assay.get_well("A1")).concentration("dye"), 50.0)
    self.assertAlmostEqual(world.contents(wc.source.get_well("A1")).volume, 100.0)

  async def test_96_head_stamp(self):
    world, wc = self.sim.world, self.wc
    for well in wc.source.get_all_items():
      world.fill(well, 100.0, dye=10.0)
    await wc.lh.pick_up_tips96(wc.tips)
    await wc.lh.aspirate96(wc.source, volume=40.0)
    await wc.lh.dispense96(wc.assay, volume=40.0)
    for well in wc.assay.get_all_items():
      self.assertAlmostEqual(world.contents(well).concentration("dye"), 10.0)
      self.assertAlmostEqual(world.contents(well).volume, 40.0)

  async def test_dispensing_more_than_the_tip_holds_raises(self):
    wc = self.wc
    self.sim.world.fill(wc.source.get_well("A1"), 200.0)
    await wc.lh.pick_up_tips(wc.tips["A1"])
    await wc.lh.aspirate(wc.source["A1"], vols=[50.0])
    with self.assertRaises(SimulationError):
      await wc.lh.dispense(wc.assay["A1"], vols=[60.0])

  async def test_operations_take_simulated_time(self):
    await self.wc.lh.pick_up_tips(self.wc.tips["A1"])
    self.assertEqual(self.sim.now(), self.wc.lh.backend.timing.pick_up_tips)
    self.assertEqual(self.sim.timeline[-1].action, "pick_up_tips")


class TestPlateReader(WorkcellTestCase):
  async def _read_a450(self, well_name: str) -> float:
    plate = self.wc.assay
    plate.unassign()
    self.wc.reader.assign_child_resource(plate)
    reads = await self.wc.reader.read_absorbance(
      450, wells=[plate.get_well(well_name)], use_new_return_type=True
    )
    well = plate.get_well(well_name)
    return reads[0]["data"][well.get_row()][well.get_column()]

  async def test_absorbance_follows_beer_lambert(self):
    world, wc = self.sim.world, self.wc
    world.fill(wc.assay.get_well("A1"), 100.0, dye=50.0)
    well = wc.assay.get_well("A1")
    path_cm = wc.reader.backend.path_length_cm(well, 100.0)
    expected = 20_000.0 * 50e-6 * path_cm + wc.reader.backend.model.blank_od
    self.assertAlmostEqual(await self._read_a450("A1"), expected, delta=0.01)

  async def test_empty_well_reads_blank(self):
    self.assertAlmostEqual(await self._read_a450("B1"), 0.04, delta=0.01)

  async def test_read_with_open_drawer_raises(self):
    await self.wc.reader.open()
    with self.assertRaises(SimulationError):
      await self._read_a450("A1")

  async def test_same_seed_same_data(self):
    self.sim.world.fill(self.wc.assay.get_well("A1"), 100.0, dye=10.0)
    self.sim.rng.seed(42)
    first = await self._read_a450("A1")
    self.sim.rng.seed(42)
    self.assertEqual(await self._read_a450("A1"), first)


class TestHeaterShaker(WorkcellTestCase):
  async def test_wait_for_temperature_follows_first_order_model(self):
    hs = self.wc.heater_shaker
    model: HeaterShakerModel = hs.backend.model
    await hs.set_temperature(37.0)
    await hs.wait_for_temperature(tolerance=0.5)
    # Within 0.5 C of a 15 C step. The reading is rounded to 0.01 C and must be strictly inside the
    # tolerance (so effectively 0.495 C), and the front end polls once per second.
    expected = math.ceil(model.time_constant * math.log((37.0 - model.ambient) / 0.495))
    self.assertEqual(self.sim.now(), expected)

  async def test_shake_for_duration_spends_virtual_time(self):
    await self.wc.heater_shaker.shake(speed=800, duration=3600)
    self.assertGreaterEqual(self.sim.now(), 3600)
    self.assertEqual(self.wc.heater_shaker.backend.speed, 0.0)

  async def test_shaking_an_unlocked_plate_raises(self):
    with self.assertRaises(SimulationError):
      await self.wc.heater_shaker.backend.start_shaking(800)

  async def test_out_of_range_speed_raises(self):
    with self.assertRaises(SimulationError):
      await self.wc.heater_shaker.shake(speed=5000, duration=10)


if __name__ == "__main__":
  unittest.main()
