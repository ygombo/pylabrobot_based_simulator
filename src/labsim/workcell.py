"""A reference workcell: a STARlet deck with tips, two plates, an on-deck heater-shaker and a
plate reader the arm can reach. Copy and edit this for your own layouts."""

from dataclasses import dataclass

from pylabrobot.legacy.heating_shaking import HeaterShaker
from pylabrobot.legacy.liquid_handling import LiquidHandler
from pylabrobot.legacy.plate_reading import PlateReader
from pylabrobot.resources import (
  Coordinate,
  Plate,
  TipRack,
  cor_96_wellplate_360uL_Fb,
  hamilton_96_tiprack_1000uL_filter,
  hamilton_plate_carrier_L5_ac,
  hamilton_tip_carrier_L5,
)
from pylabrobot.resources.hamilton import STARLetDeck

from labsim.backends import SimHeaterShakerBackend, SimLiquidHandlerBackend, SimPlateReaderBackend
from labsim.simulation import Simulation


@dataclass
class AssayWorkcell:
  lh: LiquidHandler
  reader: PlateReader
  heater_shaker: HeaterShaker
  tips: TipRack
  tips2: TipRack
  source: Plate
  assay: Plate

  async def setup(self) -> None:
    for machine in (self.lh, self.reader, self.heater_shaker):
      await machine.setup()

  async def stop(self) -> None:
    for machine in (self.lh, self.reader, self.heater_shaker):
      await machine.stop()


def build_assay_workcell(sim: Simulation) -> AssayWorkcell:
  deck = STARLetDeck()
  lh = LiquidHandler(backend=SimLiquidHandlerBackend(sim), deck=deck)

  tip_carrier = hamilton_tip_carrier_L5(name="tip_carrier")
  tip_carrier[0] = tips = hamilton_96_tiprack_1000uL_filter(name="tips")
  tip_carrier[1] = tips2 = hamilton_96_tiprack_1000uL_filter(name="tips2")
  deck.assign_child_resource(tip_carrier, track=1)

  plate_carrier = hamilton_plate_carrier_L5_ac(name="plate_carrier")
  plate_carrier[0] = source = cor_96_wellplate_360uL_Fb(name="source")
  plate_carrier[1] = assay = cor_96_wellplate_360uL_Fb(name="assay")
  deck.assign_child_resource(plate_carrier, track=9)

  heater_shaker = HeaterShaker(
    name="heater_shaker", size_x=147.0, size_y=104.0, size_z=70.0,
    backend=SimHeaterShakerBackend(sim), child_location=Coordinate(9.66, 9.26, 65.0),
  )
  deck.assign_child_resource(heater_shaker, location=Coordinate(500.0, 150.0, 100.0))

  reader = PlateReader(
    name="plate_reader", size_x=128.0, size_y=86.0, size_z=40.0,
    backend=SimPlateReaderBackend(sim),
  )
  deck.assign_child_resource(reader, location=Coordinate(700.0, 150.0, 100.0))

  return AssayWorkcell(lh, reader, heater_shaker, tips, tips2, source, assay)
