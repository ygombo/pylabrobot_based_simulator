"""A simple automated experiment, run by a scheduler: does absorbance scale with dye amount?

Workcell (Hamilton STARlet deck):
  - Hamilton STARlet liquid handler, 8 channels + iSWAP plate gripper
  - heater-shaker (on deck)
  - absorbance plate reader (reachable by the gripper)

Each assay plate is one job; the scheduler interleaves the jobs on the shared instruments:
  T1 add buffer           liquid_handler           well volume minus the dye volume, per column
  T2 add dye              liquid_handler           one dye volume per column (8 replicates)
  T3 load heater-shaker   liquid_handler, heater_shaker (held from here)
  T4 incubate             heater_shaker            set temperature, shake for the set time
  T5 move to reader       liquid_handler, plate_reader (held from here); frees heater_shaker
  T6 read absorbance      plate_reader
  T7 return plate         liquid_handler; frees plate_reader
A system job pre-heats the heater-shaker at t=0, in parallel with the first pipetting.

Every experimental parameter is in ExperimentConfig (defaults: 0/20/40/80 uL of 50 uM dye in
100 uL, 37 C, 800 rpm, 5 min, read at 450 nm). Change them with flags, or with a JSON file:
  python examples/simple_experiment.py --print-config > my_run.json   # edit, then:
  python examples/simple_experiment.py --config my_run.json [--plates 3 ...]   # flags win
The parameters are checked before anything runs.

Output: the parameters, the instruments and labware, the schedule as executed (with queue
times), a Gantt chart per instrument, utilization and throughput, then the execution log of every
task down to the low-level commands (Hamilton STAR firmware; driver commands for the
heater-shaker and reader), then the results. Everything is also saved under runs/.

Backends:
  --backends labsim  This project's simulators: STAR firmware + physics, realistic absorbances.
  --backends plr     PyLabRobot's built-in simulators only (the reader returns zeros).
"""

import argparse
import asyncio
import dataclasses
import json
import pathlib
from dataclasses import dataclass, field
from statistics import mean, stdev
from typing import Any, Callable, Dict, List, Optional

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
  HeaterShakerModel,
  SimHeaterShakerBackend,
  SimPlateReaderBackend,
  SimSTARBackend,
  Simulation,
  Species,
)
from labsim.protocol import ProtocolRecorder
from labsim.scheduler import Scheduler, Task

# Fixed by the workcell, not by the experiment.
MAX_PLATES = 4  # plate carrier: 1 reagent plate + 4 assay plates
MAX_CONDITIONS = 12  # one condition per assay plate column
WELL_MAX_VOLUME = 360.0  # uL, Corning 96-well 360 uL plate
DEAD_VOLUME = 20.0  # uL left in each reagent well so the tips never run dry
RUNS_DIR = pathlib.Path(__file__).resolve().parent.parent / "runs"

LH, HS, READER = "liquid_handler", "heater_shaker", "plate_reader"

# Per plate: the 8 replicate absorbances of each condition, keyed by dye volume.
Results = Dict[str, Dict[float, List[float]]]


@dataclass
class ExperimentConfig:
  """Everything about the experiment that a user may change. Units are in the field names.

  Each assay plate gets one condition per column (column 1 = first dye volume, ...), with 8
  replicates (rows A-H, one per channel).
  """

  title: str = ""  # empty: built from the wavelength and plate count
  backends: str = "labsim"  # "labsim" or "plr"
  plates: int = 2
  seed: int = 0  # plate reader noise
  dye_volumes_ul: List[float] = field(default_factory=lambda: [0.0, 20.0, 40.0, 80.0])
  well_volume_ul: float = 100.0  # final volume per well; buffer makes up the rest
  dye_stock_um: float = 50.0
  dye_absorptivity: float = 20_000.0  # 1/(M*cm) at wavelength_nm
  wavelength_nm: int = 450
  temperature_c: float = 37.0
  shake_rpm: int = 800
  incubation_min: float = 5.0
  preheat: bool = True  # warm the heater-shaker at t=0 while the first plate is pipetted

  @property
  def buffer_per_reagent_well_ul(self) -> float:
    """Buffer each reagent well must hold for one plate, dead volume included."""
    return sum(self.well_volume_ul - v for v in self.dye_volumes_ul) + DEAD_VOLUME

  @property
  def dye_per_reagent_well_ul(self) -> float:
    return sum(self.dye_volumes_ul) + DEAD_VOLUME

  def concentration_um(self, dye_volume_ul: float) -> float:
    return self.dye_stock_um * dye_volume_ul / self.well_volume_ul

  def display_title(self) -> str:
    if self.title:
      return self.title
    plates = f"{self.plates} plate{'s' if self.plates > 1 else ''}"
    return f"Dye dose-response, absorbance at {self.wavelength_nm} nm ({plates})"

  def validate(self) -> None:
    """Raise ValueError listing every parameter the workcell cannot run."""
    hs = HeaterShakerModel()
    errors = []
    if self.backends not in ("labsim", "plr"):
      errors.append(f"backends must be 'labsim' or 'plr', not {self.backends!r}")
    if not 1 <= self.plates <= MAX_PLATES:
      errors.append(f"plates must be 1-{MAX_PLATES} (plate carrier sites)")
    if not 1 <= len(self.dye_volumes_ul) <= MAX_CONDITIONS:
      errors.append(f"dye_volumes_ul needs 1-{MAX_CONDITIONS} values (one per plate column)")
    if len(set(self.dye_volumes_ul)) != len(self.dye_volumes_ul):
      errors.append("dye_volumes_ul must not repeat a value")
    if not 0 < self.well_volume_ul <= WELL_MAX_VOLUME:
      errors.append(f"well_volume_ul must be in (0, {WELL_MAX_VOLUME:g}]")
    bad = [v for v in self.dye_volumes_ul if not 0 <= v <= self.well_volume_ul]
    if bad:
      errors.append(f"dye volumes {bad} are outside 0-{self.well_volume_ul:g} uL (the well volume)")
    for name, need in (("buffer", self.buffer_per_reagent_well_ul),
                       ("dye", self.dye_per_reagent_well_ul)):
      if need > WELL_MAX_VOLUME:
        errors.append(
          f"each {name} reagent well would need {need:g} uL (incl. {DEAD_VOLUME:g} uL dead "
          f"volume) but holds {WELL_MAX_VOLUME:g} uL: use fewer conditions or a smaller well volume"
        )
    if self.dye_stock_um <= 0:
      errors.append("dye_stock_um must be > 0")
    if self.dye_absorptivity < 0:
      errors.append("dye_absorptivity must be >= 0")
    if not 230 <= self.wavelength_nm <= 1000:
      errors.append("wavelength_nm must be 230-1000")
    if not hs.ambient <= self.temperature_c <= hs.max_temperature:
      errors.append(
        f"temperature_c must be {hs.ambient:g}-{hs.max_temperature:g} C (the heater-shaker "
        "heats only; it cannot cool below room temperature)"
      )
    if not hs.min_speed <= self.shake_rpm <= hs.max_speed:
      errors.append(f"shake_rpm must be {hs.min_speed:g}-{hs.max_speed:g}")
    if self.incubation_min <= 0:
      errors.append("incubation_min must be > 0")
    if errors:
      raise ValueError("invalid experiment parameters:\n  - " + "\n  - ".join(errors))

  # JSON.

  @classmethod
  def from_dict(cls, data: Dict[str, Any]) -> "ExperimentConfig":
    known = {f.name for f in dataclasses.fields(cls)}
    unknown = sorted(set(data) - known)
    if unknown:
      raise ValueError(f"unknown parameters {unknown}; valid ones: {sorted(known)}")
    return dataclasses.replace(cls(), **data)

  @classmethod
  def load(cls, path: pathlib.Path) -> "ExperimentConfig":
    return cls.from_dict(json.loads(pathlib.Path(path).read_text()))

  def to_json(self) -> str:
    return json.dumps(dataclasses.asdict(self), indent=2)

  def render(self) -> str:
    rows = [f"  {name:<20} {value}" for name, value in dataclasses.asdict(self).items()]
    return "PARAMETERS\n" + "\n".join(rows)


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


def plate_job(wc: Workcell, cfg: ExperimentConfig, p: int, results: Results) -> List[Task]:
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
    for col, dye_vol in enumerate(cfg.dye_volumes_ul, start=1):
      buffer_vol = cfg.well_volume_ul - dye_vol
      if buffer_vol > 0:
        await lh.aspirate(buffer, vols=[buffer_vol] * 8)
        await lh.dispense(assay[f"A{col}:H{col}"], vols=[buffer_vol] * 8)
    await lh.discard_tips()

  async def add_dye() -> None:
    await lh.pick_up_tips(wc.tips[f"A{dye_col}:H{dye_col}"])
    for col, dye_vol in enumerate(cfg.dye_volumes_ul, start=1):
      if dye_vol > 0:
        await lh.aspirate(dye, vols=[dye_vol] * 8)
        await lh.dispense(assay[f"A{col}:H{col}"], vols=[dye_vol] * 8)
    await lh.discard_tips()

  async def load_heater_shaker() -> None:
    await lh.move_plate(assay, hs)

  async def incubate() -> None:
    await hs.set_temperature(cfg.temperature_c)
    await hs.wait_for_temperature()
    await hs.shake(speed=cfg.shake_rpm, duration=cfg.incubation_min * 60)

  async def move_to_reader() -> None:
    await reader.open()
    await lh.move_plate(assay, reader)
    await reader.close()

  async def read() -> None:
    reads = await reader.read_absorbance(wavelength=cfg.wavelength_nm, use_new_return_type=True)
    data = reads[0]["data"]
    results[assay.name] = {
      dye_vol: [data[row][col] for row in range(8)]
      for col, dye_vol in enumerate(cfg.dye_volumes_ul)
    }

  async def return_plate() -> None:
    await reader.open()
    await lh.move_plate(assay, home)
    await reader.close()

  incubation = f"{cfg.temperature_c:g} C, {cfg.shake_rpm} rpm, {cfg.incubation_min:g} min"
  return [
    Task("add buffer", add_buffer, uses=[LH]),
    Task("add dye", add_dye, uses=[LH]),
    Task("load heater-shaker", load_heater_shaker, uses=[LH], acquire=[HS]),
    Task(f"incubate {incubation}", incubate, uses=[HS]),
    Task("move to plate reader", move_to_reader, uses=[LH, HS], acquire=[READER], release=[HS]),
    Task(f"read A{cfg.wavelength_nm}", read, uses=[READER]),
    Task("return plate to carrier", return_plate, uses=[LH, READER], release=[READER]),
  ]


def report(cfg: ExperimentConfig, results: Results) -> None:
  a = f"A{cfg.wavelength_nm}"
  print(f"\n{'plate':<8} {'dye (uL)':>8} {'dye (uM)':>9} {a + ' mean':>10} {'SD':>7}")
  points = []
  for plate, by_dye in results.items():
    for dye_vol, values in by_dye.items():
      conc = cfg.concentration_um(dye_vol)
      points.append((conc, mean(values)))
      print(f"{plate:<8} {dye_vol:8g} {conc:9.2f} {mean(values):10.4f} {stdev(values):7.4f}")
  xs, ys = zip(*points)
  mx, my = mean(xs), mean(ys)
  sxx = sum((x - mx) ** 2 for x in xs)
  if sxx == 0:
    print("\nlinear fit: needs at least two different dye volumes")
    return
  slope = sum((x - mx) * (y - my) for x, y in points) / sxx
  print(f"\nlinear fit (all plates): {a} = {slope:.5f} * c(uM) + {my - slope * mx:.4f}")


async def main(
  config: Optional[ExperimentConfig] = None,
  verbose: bool = True,
  full_commands: bool = False,
  save: bool = True,
  run_name: str = "simple_experiment",
) -> Results:
  cfg = config or ExperimentConfig()
  cfg.validate()
  set_tip_tracking(True)
  set_volume_tracking(True)
  results: Results = {}

  with Simulation(seed=cfg.seed) as sim:
    sim.world.register(Species("dye", absorptivity={cfg.wavelength_nm: cfg.dye_absorptivity}))
    wc = build_workcell(sim, cfg.backends, cfg.plates)

    # Load reagents: per plate, dye stock in column 2p-1 and buffer in column 2p.
    fill: Callable[..., None]
    if cfg.backends == "plr":
      def fill(well: Well, volume: float, **conc: float) -> None:
        well.tracker.set_volume(volume)  # PLR tracks volume only
    else:
      fill = sim.world.fill
    for p in range(1, cfg.plates + 1):
      for well in wc.reagents.get_wells(f"A{2 * p - 1}:H{2 * p - 1}"):
        fill(well, cfg.dye_per_reagent_well_ul, dye=cfg.dye_stock_um)
      for well in wc.reagents.get_wells(f"A{2 * p}:H{2 * p}"):
        fill(well, cfg.buffer_per_reagent_well_ul)

    # Describe the run.
    protocol = ProtocolRecorder(cfg.display_title(), sim=sim)
    protocol.add_instrument(wc.lh, LH, "Hamilton STARlet, 8 ch + iSWAP", firmware=True)
    protocol.add_instrument(wc.heater_shaker, HS, "heater-shaker, 4-105 C, 200-3000 rpm")
    protocol.add_instrument(wc.reader, READER, "absorbance plate reader")
    protocol.add_labware("tips", "Hamilton 96 tips 1000 uL, filter", "tip_carrier site 1, track 1",
                         f"columns 1-{2 * cfg.plates} used")
    protocol.add_labware(
      "reagents", "Corning 96-well 360 uL flat", "plate_carrier site 1, track 9",
      f"odd columns: {cfg.dye_per_reagent_well_ul:g} uL of {cfg.dye_stock_um:g} uM dye; "
      f"even: {cfg.buffer_per_reagent_well_ul:g} uL buffer",
    )
    n = len(cfg.dye_volumes_ul)
    for p, plate in enumerate(wc.assays, start=1):
      protocol.add_labware(plate.name, "Corning 96-well 360 uL flat",
                           f"plate_carrier site {p + 1}, track 9",
                           f"empty; columns 1-{n} used" if n > 1 else "empty; column 1 used")

    # The schedule.
    scheduler = Scheduler(sim, [LH, HS, READER], recorder=protocol)

    if cfg.preheat:
      async def preheat() -> None:
        await wc.heater_shaker.set_temperature(cfg.temperature_c)
        await wc.heater_shaker.wait_for_temperature()

      scheduler.add_job(
        "SYS", [Task(f"pre-heat heater-shaker to {cfg.temperature_c:g} C", preheat, uses=[HS])],
        priority=0, tag="S", sample=False,
      )
    for p in range(1, cfg.plates + 1):
      scheduler.add_job(f"P{p}", plate_job(wc, cfg, p, results), priority=p, tag=str(p))

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
      cfg.render(), protocol.render_header(), scheduler.render(),
      "EXECUTION LOG" + protocol.render_steps(max_command_width=width),
    ])

  if save:
    RUNS_DIR.mkdir(exist_ok=True)
    saved = RUNS_DIR / f"{run_name}_{cfg.backends}_{cfg.plates}plates.txt"
    saved.write_text(full_report() + "\n")
  if verbose:
    print(full_report(None if full_commands else 110))
    if save:
      print(f"\nFull report saved to {saved}")
    print(f"\n===== RESULTS ({cfg.backends} backends) =====")
    report(cfg, results)
  return results


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
  parser = argparse.ArgumentParser(
    description=__doc__.split("\n")[0],
    epilog="Flags override values from --config. Unset flags keep the config/default value.",
  )
  parser.add_argument("--config", type=pathlib.Path, help="JSON file with ExperimentConfig fields")
  parser.add_argument("--print-config", action="store_true",
                      help="print the resulting parameters as JSON and exit")
  parser.add_argument("--full", action="store_true", help="print firmware commands untruncated")
  p = parser.add_argument_group("experiment parameters")
  p.add_argument("--title")
  p.add_argument("--backends", choices=["plr", "labsim"])
  p.add_argument("--plates", type=int, help=f"assay plates, 1-{MAX_PLATES}")
  p.add_argument("--seed", type=int, help="plate reader noise seed")
  p.add_argument("--dye-volumes", dest="dye_volumes_ul", metavar="UL,UL,...",
                 type=lambda s: [float(v) for v in s.split(",")],
                 help="dye volume per column, e.g. 0,10,20,40,80")
  p.add_argument("--well-volume", dest="well_volume_ul", type=float, metavar="UL")
  p.add_argument("--dye-stock", dest="dye_stock_um", type=float, metavar="UM")
  p.add_argument("--absorptivity", dest="dye_absorptivity", type=float, metavar="PER_M_CM")
  p.add_argument("--wavelength", dest="wavelength_nm", type=int, metavar="NM")
  p.add_argument("--temperature", dest="temperature_c", type=float, metavar="C")
  p.add_argument("--rpm", dest="shake_rpm", type=int)
  p.add_argument("--minutes", dest="incubation_min", type=float, help="incubation time")
  p.add_argument("--no-preheat", dest="preheat", action="store_false", default=None)
  return parser.parse_args(argv)


def config_from_args(args: argparse.Namespace) -> ExperimentConfig:
  cfg = ExperimentConfig.load(args.config) if args.config else ExperimentConfig()
  fields = {f.name for f in dataclasses.fields(ExperimentConfig)}
  overrides = {k: v for k, v in vars(args).items() if k in fields and v is not None}
  return dataclasses.replace(cfg, **overrides)


if __name__ == "__main__":
  args = parse_args()
  try:
    cfg = config_from_args(args)
    cfg.validate()
  except ValueError as e:
    raise SystemExit(str(e))
  if args.print_config:
    print(cfg.to_json())
  else:
    name = args.config.stem if args.config else "simple_experiment"
    asyncio.run(main(cfg, full_commands=args.full, run_name=name))
