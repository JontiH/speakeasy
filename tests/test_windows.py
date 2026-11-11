import json
import socket
import struct
import sys
import tempfile
import threading
import tomllib
import unittest
import wave
from pathlib import Path
from unittest import mock

from speakeasy import config, recorder, worker

WINDOWS = sys.platform == "win32"


class FixWavHeader(unittest.TestCase):
    """A killed ffmpeg leaves placeholder sizes; the wave module then sees no samples."""

    def write(self, riff_size: int, data_size: int, samples: int) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "record.wav"
        fmt = struct.pack("<HHIIHH", 1, 1, 16000, 32000, 2, 16)
        path.write_bytes(b"RIFF" + struct.pack("<I", riff_size) + b"WAVE" + b"fmt " + struct.pack("<I", 16) + fmt
                         + b"LIST" + struct.pack("<I", 4) + b"INFO"
                         + b"data" + struct.pack("<I", data_size) + b"\x01\x00" * samples)
        return path

    def test_placeholder_sizes_are_fixed(self):
        path = self.write(0xFFFFFFFF, 0, 16000)
        recorder.fix_wav_header(path)
        with wave.open(str(path)) as fh:
            self.assertEqual(fh.getnframes(), 16000)

    def test_correct_header_is_left_alone(self):
        path = self.write(0, 0, 10)
        recorder.fix_wav_header(path)
        before = path.read_bytes()
        recorder.fix_wav_header(path)
        self.assertEqual(path.read_bytes(), before)

    def test_non_wav_is_ignored(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "x.wav"
        path.write_bytes(b"not a wav file")
        recorder.fix_wav_header(path)
        self.assertEqual(path.read_bytes(), b"not a wav file")


class DshowInput(unittest.TestCase):
    def test_named_device(self):
        args = recorder.input_args({"backend": "dshow", "device": "Microphone (USB)"})
        self.assertEqual(args, ["-f", "dshow", "-audio_buffer_size", "50", "-i", "audio=Microphone (USB)"])

    def test_default_resolves_through_pick_default(self):
        with mock.patch.object(recorder, "dshow_devices", return_value=["Mic A", "Mic B"]), \
                mock.patch.object(recorder, "win32", create=True) as fake:
            fake.default_capture_name.return_value = "Mic B"
            args = recorder.input_args({"backend": "dshow", "device": "default"})
        self.assertEqual(args[-1], "audio=Mic B" if WINDOWS else "audio=Mic A")

    def test_pulse_is_unchanged(self):
        # The Linux command line must stay exactly what it was before Windows support.
        self.assertEqual(recorder.input_args({"backend": "pulse", "device": "default"}),
                         ["-f", "pulse", "-i", "default"])


class PickDefault(unittest.TestCase):
    NAMES = ["Microphone (HD Pro Webcam C920)", "Microphone (USB Advanced Audio Device)"]
    IGNORE = ["webcam", "camera"]

    def test_windows_default_is_used(self):
        self.assertEqual(recorder.pick_default(self.NAMES, self.NAMES[1], self.IGNORE), self.NAMES[1])

    def test_webcam_default_is_skipped(self):
        self.assertEqual(recorder.pick_default(self.NAMES, self.NAMES[0], self.IGNORE), self.NAMES[1])

    def test_unknown_default_uses_first_non_ignored(self):
        self.assertEqual(recorder.pick_default(self.NAMES, None, self.IGNORE), self.NAMES[1])

    def test_only_a_webcam_still_records(self):
        self.assertEqual(recorder.pick_default(self.NAMES[:1], self.NAMES[0], self.IGNORE), self.NAMES[0])

    def test_no_mics_raises(self):
        with self.assertRaises(RuntimeError):
            recorder.pick_default([], None, self.IGNORE)


class WindowsDefaults(unittest.TestCase):
    def test_overlay_parses_and_only_changes_known_settings(self):
        with config.WINDOWS_CONFIG.open("rb") as fh:
            overlay = tomllib.load(fh)
        with config.DEFAULT_CONFIG.open("rb") as fh:
            base = tomllib.load(fh)
        for section, values in overlay.items():
            self.assertIn(section, base)
            for key in values:
                if key not in ("sounds", "ignore_devices"):  # Windows only
                    self.assertIn(key, base[section], f"{section}.{key}")
        self.assertEqual(overlay["audio"]["backend"], "dshow")


@unittest.skipUnless(WINDOWS, "Windows only")
class Hotkeys(unittest.TestCase):
    def test_gnome_syntax(self):
        from speakeasy import win32

        flags, mods, vk = win32.parse_combo("<Super>slash")
        self.assertTrue(flags & win32.MOD_WIN)
        self.assertEqual(mods[0], win32.VK_LWIN)
        self.assertEqual(vk & 0xFF, vk)

    def test_plus_syntax_and_letters(self):
        from speakeasy import win32

        flags, mods, vk = win32.parse_combo("ctrl+shift+v")
        self.assertEqual(flags, win32.MOD_CONTROL | win32.MOD_SHIFT)
        self.assertEqual(vk, ord("V"))
        self.assertEqual(win32.parse_combo("Enter"), (0, [], 0x0D))
        self.assertEqual(win32.parse_combo("<Ctrl><Alt>F9")[2], 0x78)

    def test_bad_names_raise(self):
        from speakeasy import win32

        for bad in ("<Hyper>x", "ctrl+nosuchkey", "<Super>"):
            with self.assertRaises(ValueError):
                win32.parse_combo(bad)

    def test_label(self):
        from speakeasy import winlisten

        self.assertEqual(winlisten.hotkey_label("<Super>slash"), "Win+/")
        self.assertEqual(winlisten.hotkey_label("<Ctrl><Alt>space"), "Ctrl+Alt+space")


class WorkerTransport(unittest.TestCase):
    """The worker socket on this OS: Unix socket on Linux, localhost TCP plus token on Windows."""

    def serve_once(self, srv, seen):
        def answer():
            conn, _ = srv.accept()
            with conn:
                req = json.loads(worker._read_line(conn))
                seen.append(req)
                conn.sendall(json.dumps({"echo": req.get("cmd")}).encode() + b"\n")

        thread = threading.Thread(target=answer, daemon=True)
        thread.start()
        return thread

    def test_round_trip(self):
        srv, token = worker._listen()
        self.addCleanup(srv.close)
        self.addCleanup(config.WORKER_SOCKET.unlink, missing_ok=True)
        self.assertTrue(config.WORKER_SOCKET.exists())  # transcribe() checks this first
        seen = []
        thread = self.serve_once(srv, seen)
        self.assertEqual(worker.request({"cmd": "ping"}, timeout=5), {"echo": "ping"})
        thread.join(5)
        if WINDOWS:
            self.assertEqual(seen[0]["token"], token)
        else:
            self.assertEqual(token, "")
            self.assertNotIn("token", seen[0])

    def test_no_worker_means_none(self):
        config.WORKER_SOCKET.unlink(missing_ok=True)
        self.assertIsNone(worker.request({"cmd": "ping"}, timeout=1))

    @unittest.skipUnless(WINDOWS, "Windows only")
    def test_address_file_holds_port_and_token(self):
        srv, token = worker._listen()
        self.addCleanup(srv.close)
        self.addCleanup(config.WORKER_SOCKET.unlink, missing_ok=True)
        port, saved = config.WORKER_SOCKET.read_text().split()
        self.assertEqual((int(port), saved), (srv.getsockname()[1], token))

    def test_handle_rejects_a_wrong_token(self):
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        a.sendall(b'{"cmd": "quit", "token": "wrong"}\n')
        self.assertTrue(worker._handle(b, streamer=None, token="right"))  # True: keeps serving
        self.assertIn("bad token", worker._read_line(a).decode())

    def test_lock_is_exclusive(self):
        path = config.STATE_DIR / "test.lock"
        first = worker.take_lock(path)
        self.assertIsNotNone(first)
        self.assertIsNone(worker.take_lock(path))
        worker.release_lock(first)
        again = worker.take_lock(path)
        self.assertIsNotNone(again)
        worker.release_lock(again)


if __name__ == "__main__":
    unittest.main()
