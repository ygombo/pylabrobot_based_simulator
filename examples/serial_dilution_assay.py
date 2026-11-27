"""Serial-dilution absorbance assay, fully simulated.

1. Two-fold serial dilution of a dye across 11 columns with the 8-channel head.
2. Move the plate to the heater-shaker; incubate 10 min at 37 C while the liquid handler,
   in parallel, prepares the next run's stock.
3. Move the plate to the reader, read A450, fit a standard curve.

Run: python examples/serial_dilution_assay.py
"""

import asyncio
from statistics import mean
from typing import Dict, List, Tuple

from pylabrobot.resources import set_tip_tracking, set_volume_tracking

from labsim import Simulation, Species
from labsim.workcell import AssayWorkcell, build_assay_workcell

STOCK_UM = 100.0
DYE = Species("dye", absorptivity={450: 20_000.0})  # 1/(M*cm)


async def serial_dilution(wc: AssayWorkcell) -> List[float]:
  """Returns the nominal concentration (uM) of assay columns 1..11; column 12 is the blank."""
  await wc.lh.pick_up_tips(wc.tips["A1:H1"])
  await wc.lh.aspirate(wc.source["A1:H1"], vols=[100.0] * 8)
  await wc.lh.dispense(wc.assay["A1:H1"], vols=[100.0] * 8)
  for col in range(1, 11):
    await wc.lh.aspirate(wc.assay[f"A{col}:H{col}"], vols=[100.0] * 8)
    await wc.lh.dispense(wc.assay[f"A{col + 1}:H{col + 1}"], vols=[100.0] * 8)
  await wc.lh.aspirate(wc.assay["A11:H11"], vols=[100.0] * 8)  # keep 100 uL in every well
  await wc.lh.discard_tips()
  return [STOCK_UM / 2 ** (col + 1) for col in range(11)]


async def incubate(wc: AssayWorkcell) -> None:
  await wc.heater_shaker.set_temperature(37.0)
  await wc.heater_shaker.wait_for_temperature(tolerance=0.5)
  await wc.heater_shaker.shake(speed=800, duration=600)


async def prepare_next_stock(wc: AssayWorkcell) -> None:
  await wc.lh.pick_up_tips(wc.tips2["A1:H1"])
  await wc.lh.aspirate(wc.source["A1:H1"], vols=[50.0] * 8)
  await wc.lh.dispense(wc.source["A2:H2"], vols=[50.0] * 8)
  await wc.lh.discard_tips()


def fit_line(points: List[Tuple[float, float]]) -> Tuple[float, float, float]:
  """Least-squares slope, intercept and R^2."""
  xs, ys = [p[0] for p in points], [p[1] for p in points]
  mx, my = mean(xs), mean(ys)
  sxx = sum((x - mx) ** 2 for x in xs)
  slope = sum((x - mx) * (y - my) for x, y in points) / sxx
  intercept = my - slope * mx
  ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in points)
  ss_tot = sum((y - my) ** 2 for y in ys)
  return slope, intercept, 1 - ss_res / ss_tot


async def run(seed: int = 0, verbose: bool = True) -> Dict[str, float]:
  set_tip_tracking(True)
  set_volume_tracking(True)
  with Simulation(seed=seed) as sim:
    sim.world.register(DYE)
    wc = build_assay_workcell(sim)
    await wc.setup()

    for well in wc.source.get_wells("A1:H1"):
      sim.world.fill(well, 300.0, dye=STOCK_UM)
    for well in wc.assay.get_wells("A1:H12"):
      sim.world.fill(well, 100.0)  # buffer

    concentrations = await serial_dilution(wc)

    await wc.lh.move_plate(wc.assay, wc.heater_shaker)
    await asyncio.gather(incubate(wc), prepare_next_stock(wc))
    await wc.heater_shaker.deactivate()
    await wc.lh.move_plate(wc.assay, wc.reader)

    reads = await wc.reader.read_absorbance(wavelength=450, use_new_return_type=True)
    data = reads[0]["data"]
    await wc.stop()

  blank = mean(data[row][11] for row in range(8))
  points = [(c, mean(data[row][col] for row in range(8)) - blank)
            for col, c in enumerate(concentrations)]
  slope, intercept, r2 = fit_line(points)

  if verbose:
    print(f"{'conc (uM)':>10} {'A450 - blank':>13}")
    for c, od in points:
      print(f"{c:10.3f} {od:13.4f}")
    print(f"\nslope {slope:.5f} OD/uM, intercept {intercept:.4f}, R^2 {r2:.5f}")
    print(f"simulated run time: {sim.now() / 60:.1f} min\n")
    print(sim.format_timeline())
  return {"slope": slope, "intercept": intercept, "r2": r2, "duration": sim.now()}


if __name__ == "__main__":
  asyncio.run(run())
