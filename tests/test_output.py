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


class EmitLog(unittest.TestCase):
    OUT = {"mode": "type_text", "press_enter": True, "enter_only_in": ["oc | "],
           "paste_in": ["gnome-terminal"], "paste_enter_delay_ms": 0,
           "paste_enter_delay_per_char_ms": 0}
    TERMINAL = {"wm_class": "gnome-terminal-server", "title": "OC | x", "app_id": "org.gnome.Terminal.desktop"}

    def emit(self, windows, out=None, text="hi"):
        marks = []
        run, _ = fake_run([completed()] * 3)  # wl-copy, then each ydotool call
        with mock.patch.object(output.subprocess, "run", run), \
                mock.patch.object(output, "focused_window", side_effect=windows), \
                mock.patch.object(output, "mark", marks.append), \
                mock.patch.object(output, "notify"), \
                mock.patch.object(output.time, "sleep"):
            output.emit({"output": out or self.OUT}, text)
        return marks

    CLIPBOARD = {"wm_class": "", "title": "wl-clipboard", "app_id": "window:1"}

    def test_logs_enter_and_window_after(self):
        marks = self.emit([self.TERMINAL, self.TERMINAL])
        self.assertEqual([m for m in marks if not m.startswith("focused")],
                         ["copied (exit 0)", "pasted", "waiting 0 ms before Enter", "enter sent"])
        self.assertTrue(marks[-1].startswith("focused window after Enter"))

    def test_enter_waits_longer_after_a_long_paste(self):
        out = {**self.OUT, "paste_enter_delay_ms": 150, "paste_enter_delay_per_char_ms": 0.1}
        marks = self.emit([self.TERMINAL, self.TERMINAL], out, "x" * 2000)
        self.assertIn("waiting 350 ms before Enter", marks)

    def test_logs_why_enter_was_skipped(self):
        bash = {"wm_class": "gnome-terminal-server", "title": "bash", "app_id": ""}
        marks = self.emit([bash])
        self.assertIn("pasted", marks)
        self.assertIn("enter skipped: window doesn't match enter_only_in", marks)

    def test_rereads_window_while_wl_clipboard_has_focus(self):
        marks = self.emit([self.CLIPBOARD, self.CLIPBOARD, self.TERMINAL, self.TERMINAL])
        retries = [m for m in marks if m.startswith("focus still on wl-clipboard")]
        self.assertEqual(len(retries), 2)
        self.assertIn(f"focused window: {self.TERMINAL}", marks)
        self.assertIn("pasted", marks)
        self.assertIn("enter sent", marks)

    def test_wl_clipboard_retries_are_bounded(self):
        windows = [self.CLIPBOARD] * (output.WL_CLIPBOARD_RETRIES + 1)
        marks = self.emit(windows)
        retries = [m for m in marks if m.startswith("focus still on wl-clipboard")]
        self.assertEqual(len(retries), output.WL_CLIPBOARD_RETRIES)
        self.assertIn("typed", marks)
        self.assertIn("enter skipped: window doesn't match enter_only_in", marks)


if __name__ == "__main__":
    unittest.main()
