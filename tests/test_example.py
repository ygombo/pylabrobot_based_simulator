import importlib.util
import pathlib
import unittest

EXAMPLES = pathlib.Path(__file__).parent.parent / "examples"


def load_example(name: str):
  spec = importlib.util.spec_from_file_location(name, EXAMPLES / f"{name}.py")
  assert spec is not None and spec.loader is not None
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


class TestSimpleExperiment(unittest.IsolatedAsyncioTestCase):
  async def test_labsim_absorbance_rises_with_dye_on_every_plate(self):
    example = load_example("simple_experiment")
    results = await example.main("labsim", plates=3, verbose=False, save=False)
    self.assertEqual(sorted(results), ["assay1", "assay2", "assay3"])
    for by_dye in results.values():
      means = [sum(v) / len(v) for v in by_dye.values()]
      self.assertEqual(means, sorted(means))
      self.assertGreater(means[-1] - means[0], 0.2)

  async def test_runs_on_pylabrobot_builtin_simulators(self):
    results = await load_example("simple_experiment").main("plr", verbose=False, save=False)
    self.assertEqual(len(results), 2)


class TestSerialDilutionExample(unittest.IsolatedAsyncioTestCase):
  async def test_standard_curve_is_linear_and_run_is_deterministic(self):
    example = load_example("serial_dilution_assay")
    first = await example.run(seed=0, verbose=False)
    second = await example.run(seed=0, verbose=False)
    self.assertGreater(first["r2"], 0.999)
    self.assertGreater(first["slope"], 0)
    self.assertEqual(first, second)


if __name__ == "__main__":
  unittest.main()
