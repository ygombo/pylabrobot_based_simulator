"""The labsim app with a scripted assistant backend, for the browser smoke test (no API calls).

  python tests/ui/fake_ai_server.py PORT DATA_DIR
"""

import json
import sys
from pathlib import Path

import uvicorn

from labsim.ai import AssistantOutput, Completion
from labsim.app import create_app


def _out(**kw) -> AssistantOutput:
  base = dict(intent="answer", reply="", new_experiment=None, parameter_updates=[], other_changes=[],
              missing_information=[], assumptions=[], suggested_objective=None)
  return AssistantOutput.model_validate({**base, **kw})


def _value(name, value, quote):
  return {"name": name, "value_json": json.dumps(value), "quote": quote}


class KeywordBackend:
  """Answers like the real assistant would for the prompts the smoke test sends."""

  model = "scripted-test-backend"

  def complete(self, system: str, user: str) -> Completion:
    prompt = user.rsplit("<<<", 1)[-1].lower()
    if "fluorescence" in prompt:
      out = _out(
        intent="create_experiment",
        reply="A fluorescence assay on two plates with a 24 h CO2 incubation, in the robot-arm cell.",
        new_experiment={"template": "fluorescence_incubation_assay", "name": "Fluorescence assay (assistant)",
                        "lab_id": None,
                        "values": [_value("plates", 2, "two plates"), _value("media_volume_ul", 200, "200 µL media"),
                                   _value("reagent_volume_ul", 50, "50 µL reagent"), _value("incubation_h", 24, "24 h")]},
        missing_information=["Which fluorophore? I need the excitation and emission wavelengths."],
        assumptions=["5% CO2"])
    elif "minutes" in prompt:
      out = _out(intent="modify_experiment", reply="Incubation set to 10 minutes.",
                 parameter_updates=[_value("incubation_min", 10, "10 minutes")])
    elif "bottleneck" in prompt:
      out = _out(intent="answer", reply="The heater-shaker is the busiest instrument.", suggested_objective="balance")
    else:
      out = _out(intent="clarify", reply="Could you say what you would like to change?")
    return Completion(output=out, model=self.model, usage={"input_tokens": 1, "output_tokens": 1})


if __name__ == "__main__":
  port, data = int(sys.argv[1]), Path(sys.argv[2])
  uvicorn.run(create_app(data, assistant_backend=KeywordBackend()), host="127.0.0.1", port=port, log_level="warning")
