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


class LinuxPaths(unittest.TestCase):
    """Runs the Linux code paths on any OS, so the tests never type for real on Windows."""

    def setUp(self):
        patcher = mock.patch.object(output, "WINDOWS", False)
        patcher.start()
        self.addCleanup(patcher.stop)


class YdotoolTimeout(LinuxPaths):
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


class EmitLog(LinuxPaths):
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


class WindowsEmit(unittest.TestCase):
    """The Windows path with the Win32 calls faked, so it runs on any OS."""

    OUT = {"mode": "type_text", "press_enter": True, "enter_only_in": ["oc | "],
           "paste_in": ["windowsterminal.exe", "consolewindowclass"], "paste_keys": "ctrl+v",
           "paste_enter_delay_ms": 0, "paste_enter_delay_per_char_ms": 0}
    TERMINAL = {"wm_class": "CASCADIA_HOSTING_WINDOW_CLASS", "title": "OC | x", "app_id": "windowsterminal.exe"}
    NOTEPAD = {"wm_class": "Notepad", "title": "Untitled - Notepad", "app_id": "notepad.exe"}

    def emit(self, window, out=None, text="hi"):
        fake = mock.Mock()
        fake.set_clipboard.return_value = True
        fake.wait_modifiers_released.return_value = True
        fake.type_unicode.return_value = True
        fake.send_keys.return_value = True
        fake.foreground_window.return_value = window
        marks = []
        with mock.patch.object(output, "WINDOWS", True), \
                mock.patch.object(output, "win32", fake, create=True), \
                mock.patch.object(output.subprocess, "run", side_effect=AssertionError("no subprocess on Windows")), \
                mock.patch.object(output, "mark", marks.append), \
                mock.patch.object(output, "notify"), \
                mock.patch.object(output.time, "sleep"):
            output.emit({"output": out or self.OUT}, text)
        return fake, marks

    def test_terminal_pastes_and_sends(self):
        fake, marks = self.emit(self.TERMINAL)
        fake.set_clipboard.assert_called_once_with("hi")
        fake.wait_modifiers_released.assert_called_once()
        self.assertEqual([c.args[0] for c in fake.send_keys.call_args_list], ["ctrl+v", "Enter"])
        fake.type_unicode.assert_not_called()
        self.assertIn("pasted", marks)
        self.assertIn("enter sent", marks)

    def test_other_apps_are_typed_without_enter(self):
        fake, marks = self.emit(self.NOTEPAD)
        fake.type_unicode.assert_called_once_with("hi", 4)
        fake.send_keys.assert_not_called()
        self.assertIn("typed", marks)
        self.assertIn("enter skipped: window doesn't match enter_only_in", marks)

    def test_clipboard_mode_only_copies(self):
        fake, marks = self.emit(self.TERMINAL, {"mode": "clipboard"})
        fake.set_clipboard.assert_called_once_with("hi")
        fake.send_keys.assert_not_called()
        fake.type_unicode.assert_not_called()
        self.assertEqual(marks, ["copied (exit 0)"])


if __name__ == "__main__":
    unittest.main()
