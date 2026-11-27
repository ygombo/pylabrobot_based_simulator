# labsim

Simulated lab instruments for [PyLabRobot](https://github.com/PyLabRobot/pylabrobot). Protocols
written against PyLabRobot's standard front ends (`LiquidHandler`, `PlateReader`, `HeaterShaker`)
run unchanged on these simulators; swap the backend to run them on hardware.

What the simulators add over PyLabRobot's built-in "chatterbox" backends:

- **Virtual time.** Inside a `Simulation`, `asyncio.sleep` advances a simulated clock, so a 1 h
  incubation finishes instantly and every run gives the same timeline. Parallel work
  (`asyncio.gather`) takes as long as its slowest branch.
- **A chemistry model.** `sim.world` tracks which species are in every container and tip (PLR's
  volume tracker only tracks volume), so readers respond to what you actually pipetted.
- **Physical rules.** Overflowing wells, over-aspirating, shaking an unlocked plate, out-of-range
  temperatures or speeds, and reading with the drawer open all raise `SimulationError`.
- **A timeline.** Every operation is logged with simulated start and end times, for throughput and
  scheduling analysis.

PyLabRobot is pinned to `1.0.0b1`, where these front ends live under `pylabrobot.legacy.*`.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
.venv/bin/python examples/serial_dilution_assay.py
.venv/bin/python examples/simple_experiment.py --backends labsim  # or --backends plr
```

`examples/simple_experiment.py` runs the experiment through the scheduler, one job per assay
plate (`--plates 1-4`, default 2). It prints what workcell scheduling software would show: the
schedule as executed (start, end, queue time per task), a Gantt chart per instrument,
utilization and throughput, then the execution log of every task down to the low-level commands
(Hamilton STAR firmware, or driver commands for the other instruments). The full report is saved
to `runs/` (`--full` prints firmware commands untruncated). With `--backends plr` it uses only
PyLabRobot's built-in simulators.

To schedule your own workflow, use `labsim.scheduler.Scheduler`: one `Task` per unit of work,
naming the instruments it `uses` (busy during the task), `acquire`s (a plate now sits on it) and
`release`s; group a sample's tasks into a job with `add_job`; `await scheduler.run()`.

To record your own protocols, use `labsim.protocol.ProtocolRecorder`: register instruments with
`add_instrument`, label steps with `step(...)`, run inside `with recorder.recording():`, then
`print(recorder.render())`.

## Usage

```python
from pylabrobot.legacy.liquid_handling import LiquidHandler
from pylabrobot.resources.hamilton import STARLetDeck
from labsim import Simulation, SimLiquidHandlerBackend, Species

async def main():
  with Simulation(seed=0) as sim:
    sim.world.register(Species("dye", absorptivity={450: 20_000.0}))
    lh = LiquidHandler(backend=SimLiquidHandlerBackend(sim), deck=STARLetDeck())
    await lh.setup()
    # ... build the deck, then sim.world.fill(well, 200, dye=100) ...
    print(sim.format_timeline())
```

`labsim.workcell.build_assay_workcell` builds a complete reference layout: a STARlet deck with tips,
two plates, a heater-shaker and a plate reader.

## Layout

| Path | What it is |
|---|---|
| `src/labsim/clock.py` | `VirtualClock`: deterministic virtual time that replaces `asyncio.sleep` |
| `src/labsim/world.py` | `Species`, `Mixture`, `World`: what is in each container and tip |
| `src/labsim/simulation.py` | `Simulation`: clock + world + seeded RNG + timeline |
| `src/labsim/backends/` | Simulated liquid handler, Hamilton STAR (firmware + physics), plate reader, heater-shaker |
| `src/labsim/protocol.py` | `ProtocolRecorder`: prints a run as a protocol, down to firmware commands |
| `src/labsim/scheduler.py` | `Scheduler`: jobs and tasks dispatched on shared instruments; schedule, Gantt, utilization |
| `src/labsim/workcell.py` | Reference deck layout |
| `examples/` | End-to-end protocols |
| `tests/` | Unit tests and an example run |

## Instrument models

| Backend | Model | Tune with |
|---|---|---|
| `SimLiquidHandlerBackend` | 8 channels + 96-head + gripper arm; moves species proportionally | `LiquidHandlerTiming` |
| `SimSTARBackend` | The above, plus PyLabRobot's Hamilton STAR firmware generator; protocol errors raise before any firmware is built | `LiquidHandlerTiming` |
| `SimPlateReaderBackend` | A = Σ ε·c·l + blank + noise (l from liquid height); fluorescence and luminescence linear in concentration | `PlateReaderModel` |
| `SimHeaterShakerBackend` | First-order heating, optional active cooling, lock and speed limits | `HeaterShakerModel` |

## Adding an instrument

1. Find the abstract backend in `pylabrobot/legacy/<type>/backend.py` (for example
   `IncubatorBackend`, `CentrifugeBackend`, `ThermocyclerBackend`, `SealerBackend`).
2. Subclass it in `src/labsim/backends/`. Take `sim: Simulation` in the constructor.
3. Spend time with `await self.sim.operation(name, action, seconds)`. Read and change contents
   through `self.sim.world`. Raise `SimulationError` for anything the real device would refuse.
4. Attach it to the matching PyLabRobot front end and add a test in `tests/`.

## Limitations

- Only `asyncio.sleep` takes virtual time; any other await counts as instant. Don't run a live
  PyLabRobot visualizer inside a `Simulation`, because its own sleeps would be virtual too.
- Wells are treated as instantly well mixed, and nothing reacts. Kinetics (e.g. temperature-dependent
  reactions during incubation) would go in `World`.
- The timing defaults are placeholders. Calibrate them against logs from your instruments.
# pylabrobot_based_simulator
