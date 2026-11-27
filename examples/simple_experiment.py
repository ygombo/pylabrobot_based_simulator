"""A simple automated experiment, run by a scheduler: does absorbance scale with dye amount?

Workcell (Hamilton STARlet deck):
  - Hamilton STARlet liquid handler, 8 channels + iSWAP plate gripper
  - heater-shaker (on deck)
  - absorbance plate reader (reachable by the gripper)

Each assay plate is one job; the scheduler interleaves the jobs on the shared instruments:
  T1 add buffer           liquid_handler           100/80/60/20 uL into columns 1-4
  T2 add dye              liquid_handler           0/20/40/80 uL of 50 uM dye (8 replicates)
  T3 load heater-shaker   liquid_handler, heater_shaker (held from here)
  T4 incubate             heater_shaker            37 C, 800 rpm, 5 min
  T5 move to reader       liquid_handler, plate_reader (held from here); frees heater_shaker
  T6 read A450            plate_reader
  T7 return plate         liquid_handler; frees plate_reader
A system job pre-heats the heater-shaker at t=0, in parallel with the first pipetting.

Output: the instruments and labware, the schedule as executed (with queue times), a Gantt chart
per instrument, utilization and throughput, then the execution log of every task down to the
low-level commands (Hamilton STAR firmware; driver commands for the heater-shaker and reader),
then the results. Everything is also saved under runs/.

Backends:
  --backends labsim  This project's simulators: STAR firmware + physics, realistic absorbances.
  --backends plr     PyLabRobot's built-in simulators only (the reader returns zeros).

Run: python examples/simple_experiment.py [--plates 1-4] [--backends labsim|plr] [--full]
"""

import argparse
import asyncio
import pathlib
from dataclasses import dataclass
from statistics import mean, stdev
from typing import Callable, Dict, List

from pylabrobot.legacy.heating_shaking import HeaterShaker, HeaterShakerChatterboxBackend
from pylabrobot.legacy.liquid_handling import LiquidHandler
from pylabrobot.legacy.liquid_handling.backends.hamilton.STAR_chatterbox import (
  STARChatterboxBackend,
)
from pylabrobot.legacy.plate_reading import PlateReader, PlateReaderChatterboxBackend
from pylabrobot.resources import (
  Coordinate,
  Plate,
  TipRack,
  Well,
  cor_96_wellplate_360uL_Fb,
  hamilton_96_tiprack_1000uL_filter,
  hamilton_plate_carrier_L5_ac,
  hamilton_tip_carrier_L5,
  set_tip_tracking,
  set_volume_tracking,
)
from pylabrobot.resources.hamilton import STARLetDeck

from labsim import (
  SimHeaterShakerBackend,
  SimPlateReaderBackend,
  SimSTARBackend,
  Simulation,
  Species,
)
from labsim.protocol import ProtocolRecorder
from labsim.scheduler import Scheduler, Task

DYE_STOCK_UM = 50.0
WELL_VOLUME = 100.0
DYE_VOLUMES = [0.0, 20.0, 40.0, 80.0]  # one condition per assay column 1-4
INCUBATION_C, INCUBATION_RPM, INCUBATION_S = 37.0, 800, 5 * 60
MAX_PLATES = 4  # plate carrier: 1 reagent plate + 4 assay plates
RUNS_DIR = pathlib.Path(__file__).resolve().parent.parent / "runs"

LH, HS, READER = "liquid_handler", "heater_shaker", "plate_reader"

# Per plate: mean A450 of each condition's 8 replicates, keyed by dye volume.
Results = Dict[str, Dict[float, List[float]]]


@dataclass
class Workcell:
  lh: LiquidHandler
  heater_shaker: HeaterShaker
  reader: PlateReader
  tips: TipRack
  reagents: Plate
  assays: List[Plate]


def build_workcell(sim: Simulation, backends: str, plates: int) -> Workcell:
  if backends == "plr":
    lh_backend, hs_backend, reader_backend = (
      STARChatterboxBackend(), HeaterShakerChatterboxBackend(), PlateReaderChatterboxBackend()
    )
  else:
    lh_backend, hs_backend, reader_backend = (
      SimSTARBackend(sim), SimHeaterShakerBackend(sim), SimPlateReaderBackend(sim)
    )
  deck = STARLetDeck()
  lh = LiquidHandler(backend=lh_backend, deck=deck)
  tip_carrier = hamilton_tip_carrier_L5(name="tip_carrier")
  tip_carrier[0] = tips = hamilton_96_tiprack_1000uL_filter(name="tips")
  deck.assign_child_resource(tip_carrier, track=1)
  plate_carrier = hamilton_plate_carrier_L5_ac(name="plate_carrier")
  plate_carrier[0] = reagents = cor_96_wellplate_360uL_Fb(name="reagents")
  assays = []
  for p in range(1, plates + 1):
    plate_carrier[p] = plate = cor_96_wellplate_360uL_Fb(name=f"assay{p}")
    assays.append(plate)
  deck.assign_child_resource(plate_carrier, track=9)
  heater_shaker = HeaterShaker(
    name=HS, size_x=147.0, size_y=104.0, size_z=70.0,
    backend=hs_backend, child_location=Coordinate(9.66, 9.26, 65.0),
  )
  deck.assign_child_resource(heater_shaker, location=Coordinate(500.0, 150.0, 100.0))
  reader = PlateReader(name=READER, size_x=128.0, size_y=86.0, size_z=40.0, backend=reader_backend)
  deck.assign_child_resource(reader, location=Coordinate(700.0, 150.0, 100.0))
  return Workcell(lh, heater_shaker, reader, tips, reagents, assays)


def plate_job(wc: Workcell, p: int, results: Results) -> List[Task]:
  """The experiment for assay plate `p` (1-based), as schedulable tasks. Plate p uses tip and
  reagent columns 2p-1 (dye) and 2p (buffer)."""
  lh, hs, reader = wc.lh, wc.heater_shaker, wc.reader
  assay = wc.assays[p - 1]
  home = assay.parent
  assert home is not None
  dye_col, buffer_col = 2 * p - 1, 2 * p
  dye, buffer = wc.reagents[f"A{dye_col}:H{dye_col}"], wc.reagents[f"A{buffer_col}:H{buffer_col}"]

  async def add_buffer() -> None:
    await lh.pick_up_tips(wc.tips[f"A{buffer_col}:H{buffer_col}"])
    for col, dye_vol in enumerate(DYE_VOLUMES, start=1):
      await lh.aspirate(buffer, vols=[WELL_VOLUME - dye_vol] * 8)
      await lh.dispense(assay[f"A{col}:H{col}"], vols=[WELL_VOLUME - dye_vol] * 8)
    await lh.discard_tips()

  async def add_dye() -> None:
    await lh.pick_up_tips(wc.tips[f"A{dye_col}:H{dye_col}"])
    for col, dye_vol in enumerate(DYE_VOLUMES, start=1):
      if dye_vol > 0:
        await lh.aspirate(dye, vols=[dye_vol] * 8)
        await lh.dispense(assay[f"A{col}:H{col}"], vols=[dye_vol] * 8)
    await lh.discard_tips()

  async def load_heater_shaker() -> None:
    await lh.move_plate(assay, hs)

  async def incubate() -> None:
    await hs.set_temperature(INCUBATION_C)
    await hs.wait_for_temperature()
    await hs.shake(speed=INCUBATION_RPM, duration=INCUBATION_S)

  async def move_to_reader() -> None:
    await reader.open()
    await lh.move_plate(assay, reader)
    await reader.close()

  async def read() -> None:
    data = (await reader.read_absorbance(wavelength=450, use_new_return_type=True))[0]["data"]
    results[assay.name] = {
      dye_vol: [data[row][col] for row in range(8)] for col, dye_vol in enumerate(DYE_VOLUMES)
    }

  async def return_plate() -> None:
    await reader.open()
    await lh.move_plate(assay, home)
    await reader.close()

  return [
    Task("add buffer", add_buffer, uses=[LH]),
    Task("add dye", add_dye, uses=[LH]),
    Task("load heater-shaker", load_heater_shaker, uses=[LH], acquire=[HS]),
    Task(f"incubate {INCUBATION_C:g} C, {INCUBATION_RPM} rpm, {INCUBATION_S // 60} min",
         incubate, uses=[HS]),
    Task("move to plate reader", move_to_reader, uses=[LH, HS], acquire=[READER], release=[HS]),
    Task("read A450", read, uses=[READER]),
    Task("return plate to carrier", return_plate, uses=[LH, READER], release=[READER]),
  ]


def report(results: Results) -> None:
  print(f"\n{'plate':<8} {'dye (uL)':>8} {'dye (uM)':>9} {'A450 mean':>10} {'SD':>7}")
  points = []
  for plate, by_dye in results.items():
    for dye_vol, values in by_dye.items():
      conc = DYE_STOCK_UM * dye_vol / WELL_VOLUME
      points.append((conc, mean(values)))
      print(f"{plate:<8} {dye_vol:8.0f} {conc:9.1f} {mean(values):10.4f} {stdev(values):7.4f}")
  xs, ys = zip(*points)
  mx, my = mean(xs), mean(ys)
  sxx = sum((x - mx) ** 2 for x in xs)
  slope = sum((x - mx) * (y - my) for x, y in points) / sxx
  print(f"\nlinear fit (all plates): A450 = {slope:.5f} * c(uM) + {my - slope * mx:.4f}")


async def main(
  backends: str = "labsim",
  plates: int = 2,
  verbose: bool = True,
  full_commands: bool = False,
  save: bool = True,
) -> Results:
  if not 1 <= plates <= MAX_PLATES:
    raise ValueError(f"plates must be 1-{MAX_PLATES}")
  set_tip_tracking(True)
  set_volume_tracking(True)
  results: Results = {}

  with Simulation(seed=0) as sim:
    sim.world.register(Species("dye", absorptivity={450: 20_000.0}))
    wc = build_workcell(sim, backends, plates)

    # Load reagents: per plate, dye stock in column 2p-1 and buffer in column 2p.
    fill: Callable[..., None]
    if backends == "plr":
      def fill(well: Well, volume: float, **conc: float) -> None:
        well.tracker.set_volume(volume)  # PLR tracks volume only
    else:
      fill = sim.world.fill
    for p in range(1, plates + 1):
      for well in wc.reagents.get_wells(f"A{2 * p - 1}:H{2 * p - 1}"):
        fill(well, 300.0, dye=DYE_STOCK_UM)
      for well in wc.reagents.get_wells(f"A{2 * p}:H{2 * p}"):
        fill(well, 300.0)

    # Describe the run.
    protocol = ProtocolRecorder(
      f"Dye dose-response, absorbance at 450 nm ({plates} plate{'s' if plates > 1 else ''})",
      sim=sim,
    )
    protocol.add_instrument(wc.lh, LH, "Hamilton STARlet, 8 ch + iSWAP", firmware=True)
    protocol.add_instrument(wc.heater_shaker, HS, "heater-shaker, 4-105 C, 200-3000 rpm")
    protocol.add_instrument(wc.reader, READER, "absorbance plate reader")
    protocol.add_labware("tips", "Hamilton 96 tips 1000 uL, filter", "tip_carrier site 1, track 1",
                         f"columns 1-{2 * plates} used")
    protocol.add_labware("reagents", "Corning 96-well 360 uL flat", "plate_carrier site 1, track 9",
                         f"odd columns: {DYE_STOCK_UM:g} uM dye; even: buffer; 300 uL each")
    for p, plate in enumerate(wc.assays, start=1):
      protocol.add_labware(plate.name, "Corning 96-well 360 uL flat",
                           f"plate_carrier site {p + 1}, track 9", "empty")

    # The schedule.
    scheduler = Scheduler(sim, [LH, HS, READER], recorder=protocol)

    async def preheat() -> None:
      await wc.heater_shaker.set_temperature(INCUBATION_C)
      await wc.heater_shaker.wait_for_temperature()

    scheduler.add_job(
      "SYS", [Task(f"pre-heat heater-shaker to {INCUBATION_C:g} C", preheat, uses=[HS])],
      priority=0, tag="S", sample=False,
    )
    for p in range(1, plates + 1):
      scheduler.add_job(f"P{p}", plate_job(wc, p, results), priority=p, tag=str(p))

    with protocol.recording():
      protocol.step("Initialize instruments")
      for machine in (wc.lh, wc.heater_shaker, wc.reader):
        await machine.setup()
      await scheduler.run()
      protocol.step("Shut down instruments")  # stopping the heater-shaker also turns it off
      for machine in (wc.lh, wc.heater_shaker, wc.reader):
        await machine.stop()

  def full_report(width=None) -> str:
    return "\n\n".join([
      protocol.render_header(), scheduler.render(),
      "EXECUTION LOG" + protocol.render_steps(max_command_width=width),
    ])

  if save:
    RUNS_DIR.mkdir(exist_ok=True)
    saved = RUNS_DIR / f"simple_experiment_{backends}_{plates}plates.txt"
    saved.write_text(full_report() + "\n")
  if verbose:
    print(full_report(None if full_commands else 110))
    if save:
      print(f"\nFull report saved to {saved}")
    print(f"\n===== RESULTS ({backends} backends) =====")
    report(results)
  return results


if __name__ == "__main__":
  parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
  parser.add_argument("--backends", choices=["plr", "labsim"], default="labsim")
  parser.add_argument("--plates", type=int, default=2, help=f"assay plates, 1-{MAX_PLATES}")
  parser.add_argument("--full", action="store_true", help="print firmware commands untruncated")
  args = parser.parse_args()
  asyncio.run(main(args.backends, args.plates, full_commands=args.full))
