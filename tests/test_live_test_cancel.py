"""The live-test cancel script closes the program record, not only its workflows."""

import importlib.util
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/ops/live-test/cancel.py"


def _load():
    spec = importlib.util.spec_from_file_location("live_test_cancel", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LiveTestCancelTests(unittest.TestCase):
    def test_program_cancel_closes_the_program_with_its_workflows(self):
        cancel = _load()
        memory = mock.Mock()
        memory.cancel_program.return_value = {"status": "cancelled"}
        self.assertEqual(cancel.cancel_program("prog-1", actor="live-test", reason="done", memory=memory), 0)
        memory.cancel_program.assert_called_once_with("prog-1", actor="live-test", reason="done")

    def test_a_failed_cancel_is_reported(self):
        cancel = _load()
        memory = mock.Mock()
        memory.cancel_program.side_effect = RuntimeError("404")
        self.assertEqual(cancel.cancel_program("prog-1", actor="a", reason="r", memory=memory), 1)

    def test_main_uses_the_program_cancel_for_program(self):
        cancel = _load()
        with mock.patch.object(cancel, "cancel_program", return_value=0) as program:
            self.assertEqual(cancel.main(["--program", "prog-1"]), 0)
        program.assert_called_once_with("prog-1", actor="live-test", reason="live test done", compensate=False)


if __name__ == "__main__":
    unittest.main()
