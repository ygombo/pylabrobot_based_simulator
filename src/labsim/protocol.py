"""Record a run as a readable protocol: instruments, labware, steps, operations and commands.

Three levels, captured from three sources:
  step       labels the experiment sets with `recorder.step("...")` (or a scheduler sets per task)
  operation  each instrument operation, from PyLabRobot's event bus
  command    what each operation sent to the instrument:
               - firmware strings, for a backend that builds them (the Hamilton STAR backends log
                 each firmware command at PLR's IO log level)
               - otherwise the driver commands the front end sent to the backend, e.g.
                 `start_shaking(speed=800)`, captured by wrapping the backend's methods.

The current step is a context variable, so concurrent asyncio tasks (e.g. scheduled jobs) each
record into their own step, and a command is attached to the latest operation of the same
instrument in the same step. Firmware capture supports one firmware instrument per recorder.
"""

import contextlib
import contextvars
import functools
import inspect
import io
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from pylabrobot.events import EventBus, PLREvent, use_event_bus
from pylabrobot.io.validation_utils import LOG_LEVEL_IO
from pylabrobot.legacy.machines.machine import Machine
from pylabrobot.resources import Resource

from labsim.simulation import Simulation

# Hamilton STAR master-module (C0) firmware commands, for annotating the protocol.
STAR_FIRMWARE: Dict[str, str] = {
  "C0TT": "define tip type",
  "C0TP": "pick up tips",
  "C0TR": "discard tips",
  "C0AS": "aspirate",
  "C0DS": "dispense",
  "C0EP": "96-head: pick up tips",
  "C0ER": "96-head: drop tips",
  "C0EA": "96-head: aspirate",
  "C0ED": "96-head: dispense",
  "C0PP": "iSWAP: get plate",
  "C0PR": "iSWAP: put plate",
  "C0PM": "iSWAP: move plate",
  "C0FY": "iSWAP: open gripper",
  "C0ZP": "CO-RE gripper: get plate",
  "C0ZR": "CO-RE gripper: put plate",
  "C0JX": "move arm in X",
  "C0KX": "move iSWAP in X",
}

_current_step: contextvars.ContextVar[str] = contextvars.ContextVar("labsim_step", default="")


@dataclass
class Command:
  kind: str  # "firmware" | "driver"
  text: str
  note: str = ""
  repeat: int = 1


@dataclass
class Operation:
  step: str
  device: str
  text: str
  start: Optional[float] = None
  end: Optional[float] = None
  note: str = ""
  commands: List[Command] = field(default_factory=list)


@dataclass
class _Instrument:
  label: str
  model: str
  frontend: str
  backend: str


@dataclass
class ProtocolRecorder:
  title: str
  sim: Optional[Simulation] = None
  instruments: List[_Instrument] = field(default_factory=list)
  labware: List[Tuple[str, str, str, str]] = field(default_factory=list)
  steps: List[str] = field(default_factory=list)
  operations: List[Operation] = field(default_factory=list)
  _device_labels: Dict[str, str] = field(default_factory=dict)
  _open_ops: Dict[str, Operation] = field(default_factory=dict)
  _firmware_device: str = ""
  _active: bool = False

  # Describing the setup.

  def add_instrument(self, machine: Machine, label: str, model: str, firmware: bool = False) -> None:
    """Register an instrument. With `firmware`, its backend's firmware log is recorded; otherwise
    the driver commands sent to its backend are."""
    name = machine.name if isinstance(machine, Resource) else label
    self._device_labels[name] = label
    self.instruments.append(
      _Instrument(label, model, type(machine).__name__, type(machine.backend).__name__)
    )
    if firmware:
      self._firmware_device = label
    else:
      self._trace_backend(machine.backend, label)

  def add_labware(self, name: str, description: str, location: str, contents: str = "") -> None:
    self.labware.append((name, description, location, contents))

  # Steps.

  def _register_step(self, title: str) -> None:
    if title not in self.steps:
      self.steps.append(title)

  def step(self, title: str) -> None:
    """Start a step in the current asyncio context (sequential protocols)."""
    self._register_step(title)
    _current_step.set(title)

  @contextlib.contextmanager
  def in_step(self, title: str) -> Iterator[None]:
    """Record everything inside the block under `title` (used by the scheduler, per task)."""
    self._register_step(title)
    token = _current_step.set(title)
    try:
      yield
    finally:
      _current_step.reset(token)

  # Capturing.

  def _now(self) -> Optional[float]:
    return self.sim.now() if self.sim else None

  def _add_command(self, device: str, command: Command) -> None:
    if not self._active:
      return
    step = _current_step.get()
    target = next(
      (op for op in reversed(self.operations) if op.device == device and op.step == step), None
    )
    if target is None:
      target = Operation(step, device, "(outside a PLR operation)", start=self._now())
      self._register_step(step)
      self.operations.append(target)
    last = target.commands[-1] if target.commands else None
    if last and last.kind == command.kind and last.text == command.text:
      last.repeat += 1
      last.note = command.note
    else:
      target.commands.append(command)

  def _trace_backend(self, backend: Any, label: str) -> None:
    for name, method in inspect.getmembers(backend, inspect.iscoroutinefunction):
      if not name.startswith("_"):
        setattr(backend, name, self._traced(method, name, label))

  def _traced(self, method: Callable, name: str, label: str) -> Callable:
    @functools.wraps(method)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
      result = await method(*args, **kwargs)
      numeric = isinstance(result, (int, float)) and not isinstance(result, bool)
      note = f"-> {result:g}" if numeric else ""
      self._add_command(label, Command("driver", f"{name}({_format_args(args, kwargs)})", note))
      return result

    return wrapper

  def _on_event(self, event: PLREvent) -> None:
    d = event.as_dict()
    name: str = d["name"]
    data: Dict[str, Any] = d.get("data") or {}
    op_id = (d.get("context") or {}).get("operation_id", "")
    if name.startswith("resource.") or "." not in name:
      return
    operation, _, phase = name.rpartition(".")
    if phase == "started":
      device = self._device_labels.get((data.get("device") or {}).get("name", ""), "?")
      step = _current_step.get()
      self._register_step(step)
      op = Operation(step, device, describe_operation(operation, data), start=self._now())
      self.operations.append(op)
      self._open_ops[op_id] = op
    elif phase in ("completed", "failed"):
      op = self._open_ops.pop(op_id, None)
      if op is not None:
        op.end = self._now()
        if phase == "failed":
          op.note = "FAILED"

  @contextlib.contextmanager
  def recording(self, quiet_backends: bool = True) -> Iterator["ProtocolRecorder"]:
    """Capture events and firmware while the context is open. `quiet_backends` hides what
    backends print (PLR's chatterbox backends print every call)."""
    bus = EventBus()
    bus.subscribe(self._on_event)
    logger = logging.getLogger("pylabrobot")
    handler = _FirmwareHandler(self)
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(LOG_LEVEL_IO)
    stdout = contextlib.redirect_stdout(io.StringIO()) if quiet_backends else contextlib.nullcontext()
    self._active = True
    try:
      with use_event_bus(bus), stdout:
        yield self
    finally:
      self._active = False
      logger.removeHandler(handler)
      logger.setLevel(previous_level)

  # Rendering.

  def render_header(self) -> str:
    out = [f"PROTOCOL: {self.title}", "=" * (10 + len(self.title)), "", "INSTRUMENTS"]
    out += _table(
      ["instrument", "model", "PLR front end", "backend"],
      [(i.label, i.model, i.frontend, i.backend) for i in self.instruments],
    )
    out += ["", "LABWARE"]
    out += _table(["name", "type", "location", "contents"], self.labware)
    return "\n".join(out)

  def render_steps(self, max_command_width: Optional[int] = None) -> str:
    out: List[str] = []
    for number, step in enumerate(self.steps, start=1):
      ops = [op for op in self.operations if op.step == step]
      out += ["", f"Step {number}. {step}" if step else f"Step {number}. (unlabelled)"]
      for op in ops:
        when = "" if op.start is None else f"t={_clock(op.start)}  "
        took = ""
        if op.end is not None and op.start is not None and op.end > op.start:
          took = f"  ({op.end - op.start:.1f} s)"
        note = f"  [{op.note}]" if op.note else ""
        out.append(f"  {when}{op.device:<15} {op.text}{took}{note}")
        for c in op.commands:
          text = c.text
          if max_command_width and len(text) > max_command_width:
            text = text[: max_command_width - 3] + "..."
          extra = " ".join(x for x in (c.note, f"(x{c.repeat})" if c.repeat > 1 else "") if x)
          prefix = "firmware >" if c.kind == "firmware" else "driver   >"
          out.append(f"{'':16}{prefix} {text}{'  ' + extra if extra else ''}")
    return "\n".join(out)

  def render(self, max_command_width: Optional[int] = None) -> str:
    out = [self.render_header(), "", "STEPS", self.render_steps(max_command_width)]
    total = self._now()
    if total is not None:
      out += ["", f"Total simulated time: {total:.1f} s ({total / 60:.1f} min)"]
    return "\n".join(out)


class _FirmwareHandler(logging.Handler):
  def __init__(self, recorder: ProtocolRecorder) -> None:
    super().__init__(level=LOG_LEVEL_IO)
    self.recorder = recorder

  def emit(self, record: logging.LogRecord) -> None:
    if record.levelno != LOG_LEVEL_IO or not self.recorder._firmware_device:
      return
    command = record.getMessage()
    meaning = STAR_FIRMWARE.get(command[:4], "")
    self.recorder._add_command(
      self.recorder._firmware_device,
      Command("firmware", command, note=f"# {meaning}" if meaning else ""),
    )


def _clock(seconds: float) -> str:
  """hh:mm:ss.s"""
  h, rem = divmod(seconds, 3600)
  m, s = divmod(rem, 60)
  return f"{int(h):02d}:{int(m):02d}:{s:04.1f}"


def _format_value(value: Any) -> str:
  if isinstance(value, Resource):
    return value.name
  if isinstance(value, (list, tuple)):
    if value and all(isinstance(v, Resource) for v in value):
      return f"[{len(value)} {type(value[0]).__name__.lower()}s]"
    items = ", ".join(_format_value(v) for v in value[:4])
    return f"[{items}{', ...' if len(value) > 4 else ''}]"
  if isinstance(value, float):
    return f"{value:g}"
  return repr(value)


def _format_args(args: Sequence[Any], kwargs: Dict[str, Any]) -> str:
  parts = [_format_value(a) for a in args]
  parts += [f"{k}={_format_value(v)}" for k, v in kwargs.items()]
  return ", ".join(parts)


_SPOT_NAME = re.compile(r"^(?P<parent>.+)_(?:well|tipspot)_(?P<spot>[A-Z]+\d+)$")


def _wells(ops: List[Dict[str, Any]]) -> str:
  """'reagents A2..H2' (or 'tips A1..H1') from the per-channel operations of an event."""
  names = [((op.get("resource") or {}).get("name", "")) for op in ops]
  matches = [_SPOT_NAME.match(n) for n in names]
  if not names or not all(matches):
    return ", ".join(sorted(set(names)))
  short = [m.group("spot") for m in matches if m]
  parent = matches[0].group("parent") if matches[0] else ""
  span = short[0] if len(short) == 1 else f"{short[0]}..{short[-1]}"
  return f"{parent} {span}"


def describe_operation(operation: str, data: Dict[str, Any]) -> str:
  """A one-line, human-readable description of a PLR event-bus operation."""
  ops = data.get("liquid_operations") or data.get("tip_operations") or []
  channels = [op.get("channel") for op in ops]
  ch = f" (channels {channels[0]}-{channels[-1]})" if len(channels) > 1 else ""
  resources = data.get("resources") or [{}]
  first = (resources[0] or {}).get("name", "")

  def named(key: str) -> str:
    return ((data.get(key) or {}).get("name")) or "?"

  short = operation.split(".", 1)[-1]
  if short in ("aspirate", "dispense") and ops:
    volumes = sorted({op.get("volume") for op in ops})
    vol = f"{volumes[0]:g} uL" if len(volumes) == 1 else f"{volumes[0]:g}-{volumes[-1]:g} uL"
    where = "from" if short == "aspirate" else "into"
    return f"{short} {vol} x{len(ops)} {where} {_wells(ops)}{ch}"
  descriptions: Dict[str, Callable[[], str]] = {
    "setup": lambda: "initialize",
    "stop": lambda: "shut down",
    "tip_pickup": lambda: f"pick up tips from {_wells(ops)}{ch}",
    "tip_drop": lambda: f"drop tips into {first}{ch}",
    "resource_pickup": lambda: f"gripper: pick up {first} from {named('source')}",
    "resource_move": lambda: f"gripper: move {first}",
    "resource_drop": lambda: f"gripper: place {first} on {named('destination')}",
    "set_temperature": lambda: f"set temperature to {data.get('target_temperature'):g} C",
    "wait_for_temperature": lambda: (
      f"wait until {data.get('target_temperature'):g} +/- {data.get('tolerance'):g} C"
    ),
    "hold_temperature": lambda: f"hold temperature for {data.get('duration'):g} s",
    "deactivate": lambda: "turn heating off",
    "shake": lambda: f"shake at {data.get('speed_rpm'):g} rpm for {data.get('duration'):g} s",
    "stop_shaking": lambda: "stop shaking",
    "open": lambda: "open drawer",
    "close": lambda: "close drawer",
    "read_absorbance": lambda: (
      f"read absorbance at {data.get('wavelength_nm')} nm, {data.get('well_count')} wells"
    ),
    "read_fluorescence": lambda: f"read fluorescence, {data.get('well_count')} wells",
    "read_luminescence": lambda: f"read luminescence, {data.get('well_count')} wells",
  }
  describe = descriptions.get(short)
  return describe() if describe else operation


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> List[str]:
  widths = [max(len(str(x)) for x in col) for col in zip(headers, *rows)]

  def line(cells: Sequence[Any]) -> str:
    return "  " + "  ".join(str(c).ljust(w) for c, w in zip(cells, widths)).rstrip()

  return [line(headers), "  " + "  ".join("-" * w for w in widths)] + [line(r) for r in rows]


__all__ = ["Command", "Operation", "ProtocolRecorder", "STAR_FIRMWARE", "describe_operation"]
