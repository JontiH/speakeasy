import subprocess
import unittest
from unittest import mock

from speakeasy import output


def completed(code=0):
    return subprocess.CompletedProcess([], code)


def timed_out(*args, **kwargs):
    raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])


def fake_run(ydotool_results):
    """subprocess.run stand-in: ydotool calls pop from the list, systemctl succeeds."""
    calls = []
    results = list(ydotool_results)

    def run(cmd, **kwargs):
        calls.append(cmd[0])
        if cmd[0] == "systemctl":
            return completed()
        result = results.pop(0)
        return result(cmd, **kwargs) if callable(result) else result

    return run, calls


class YdotoolTimeout(unittest.TestCase):
    def press(self, results, out=None):
        run, calls = fake_run(results)
        with mock.patch.object(output.subprocess, "run", run):
            return output.press_keys("ctrl+shift+v", out or {}), calls

    def test_success_does_not_restart(self):
        ok, calls = self.press([completed()])
        self.assertTrue(ok)
        self.assertEqual(calls, ["ydotool"])

    def test_timeout_restarts_and_retries_keys(self):
        ok, calls = self.press([timed_out, completed()])
        self.assertTrue(ok)
        self.assertEqual(calls, ["ydotool", "systemctl", "ydotool"])

    def test_retry_that_also_times_out_fails(self):
        ok, calls = self.press([timed_out, timed_out])
        self.assertFalse(ok)
        self.assertEqual(calls, ["ydotool", "systemctl", "ydotool"])

    def test_restart_can_be_turned_off(self):
        ok, calls = self.press([timed_out], {"restart_ydotoold": False})
        self.assertFalse(ok)
        self.assertEqual(calls, ["ydotool"])

    def test_typing_restarts_but_is_not_retried(self):
        run, calls = fake_run([timed_out])
        with mock.patch.object(output.subprocess, "run", run):
            self.assertFalse(output.type_text("hello", {}))
        self.assertEqual(calls, ["ydotool", "systemctl"])

    def test_typing_timeout_grows_with_text(self):
        seen = []

        def run(cmd, **kwargs):
            seen.append(kwargs["timeout"])
            return completed()

        with mock.patch.object(output.subprocess, "run", run):
            output.type_text("x" * 1000, {"ydotool_timeout_s": 3, "key_delay_ms": 4})
        self.assertEqual(seen, [7])


if __name__ == "__main__":
    unittest.main()
