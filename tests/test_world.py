import unittest

from pylabrobot.resources import cor_96_wellplate_360uL_Fb

from labsim import Mixture, SimulationError, Species, World


class TestMixture(unittest.TestCase):
  def test_take_is_proportional(self):
    m = Mixture(200.0, {"dye": 20.0})  # 100 uM
    taken = m.take(50.0)
    self.assertAlmostEqual(taken.amounts["dye"], 5.0)
    self.assertAlmostEqual(m.concentration("dye"), 100.0)
    self.assertAlmostEqual(taken.concentration("dye"), 100.0)

  def test_take_more_than_present_raises(self):
    with self.assertRaises(SimulationError):
      Mixture(10.0).take(11.0)


class TestWorld(unittest.TestCase):
  def setUp(self):
    self.world = World()
    self.world.register(Species("dye"))
    self.plate = cor_96_wellplate_360uL_Fb(name="plate")
    self.well = self.plate.get_well("A1")

  def test_fill_sets_concentration_and_plr_tracker(self):
    self.world.fill(self.well, 200.0, dye=50.0)
    self.assertAlmostEqual(self.world.contents(self.well).concentration("dye"), 50.0)
    self.assertAlmostEqual(self.well.tracker.get_used_volume(), 200.0)

  def test_fill_with_unregistered_species_raises(self):
    with self.assertRaises(SimulationError):
      self.world.fill(self.well, 10.0, mystery=1.0)

  def test_overflow_raises(self):
    self.world.fill(self.well, 300.0)
    with self.assertRaises(SimulationError):
      self.world.transfer_in(self.well, Mixture(100.0))

  def test_aspirate_from_empty_container_raises(self):
    with self.assertRaises(SimulationError):
      self.world.transfer_out(self.well, 1.0)


if __name__ == "__main__":
  unittest.main()
