"""Windows hotkeys and tray icon: `speakeasy --listen`.

Windows has no setting that runs a command on a key combination, so this
small resident process registers the hotkeys with RegisterHotKey and runs
the same start / stop / cancel flows as the command line, in a background
thread so the icon stays responsive. `--setup` adds it to the Startup folder.

The icon shows the state: grey idle, red recording, amber transcribing.
Clicking it opens a menu. Notifications from the flows update its tooltip,
and errors (or every step, with notifications = "normal") pop up.

The model still lives in the separate worker process, so this one stays
small (~30 MB) and the worker's idle timeout works as on Linux.
"""
from __future__ import annotations

import ctypes
import os
import queue
import sys
import threading
import time
import traceback
from ctypes import wintypes

from . import cli, config, notify as notify_mod, recorder, win32, worker
from .config import LISTENER_LOCK, LISTENER_LOG, USER_CONFIG, mark

user32, kernel32 = win32.user32, win32.kernel32
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

WINDOW_CLASS = "SpeakeasyListener"
LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

WM_DESTROY, WM_CLOSE, WM_COMMAND, WM_TIMER, WM_HOTKEY = 0x0002, 0x0010, 0x0111, 0x0113, 0x0312
WM_NULL, WM_LBUTTONUP, WM_RBUTTONUP = 0x0000, 0x0202, 0x0205
WM_TRAY = 0x8000 + 1  # WM_APP + 1
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x1, 0x2, 0x4, 0x10
NIIF_INFO, NIIF_ERROR, NIIF_NOSOUND = 0x1, 0x3, 0x10
TPM_RIGHTBUTTON, TPM_RETURNCMD, TPM_NONOTIFY = 0x2, 0x100, 0x80
MF_STRING, MF_SEPARATOR, MF_GRAYED = 0x0, 0x800, 0x1
ERROR_HOTKEY_ALREADY_REGISTERED = 1409

HOTKEY_TOGGLE, HOTKEY_CANCEL = 1, 2
CMD_TOGGLE, CMD_CANCEL, CMD_CONFIG, CMD_LOGS, CMD_QUIT = 1, 2, 3, 4, 9
TIMER_ID = 1

COLOURS = {"idle": (128, 128, 128), "recording": (220, 40, 40), "busy": (235, 160, 20)}


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
                ("szInfo", wintypes.WCHAR * 256), ("uVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64),
                ("dwInfoFlags", wintypes.DWORD), ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", wintypes.HICON)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


class ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD), ("yHotspot", wintypes.DWORD),
                ("hbmMask", wintypes.HBITMAP), ("hbmColor", wintypes.HBITMAP)]


user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = wintypes.ATOM
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowW.restype = wintypes.HWND
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, wintypes.LPVOID]
user32.SetTimer.restype = ctypes.c_size_t
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.restype = LRESULT
user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
user32.RegisterWindowMessageW.restype = wintypes.UINT
user32.CreatePopupMenu.restype = wintypes.HMENU
user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                  wintypes.HWND, wintypes.LPVOID]
user32.DestroyMenu.argtypes = [wintypes.HMENU]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.CreateIconIndirect.argtypes = [ctypes.POINTER(ICONINFO)]
user32.CreateIconIndirect.restype = wintypes.HICON
user32.DestroyIcon.argtypes = [wintypes.HICON]
gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p),
                                   wintypes.HANDLE, wintypes.DWORD]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.CreateBitmap.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.UINT, wintypes.UINT, ctypes.c_void_p]
gdi32.CreateBitmap.restype = wintypes.HBITMAP
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE


def make_icon(rgb: tuple[int, int, int]):
    """A filled, anti-aliased dot in this colour, at the small-icon size."""
    size = user32.GetSystemMetrics(49) or 16  # SM_CXSMICON
    header = BITMAPINFOHEADER(biSize=ctypes.sizeof(BITMAPINFOHEADER), biWidth=size, biHeight=-size,
                              biPlanes=1, biBitCount=32)
    bits = ctypes.c_void_p()
    hdc = user32.GetDC(None)
    colour = gdi32.CreateDIBSection(hdc, ctypes.byref(header), 0, ctypes.byref(bits), None, 0)
    user32.ReleaseDC(None, hdc)
    pixels = (ctypes.c_uint32 * (size * size)).from_address(bits.value)
    centre, radius = (size - 1) / 2, size * 0.42
    r, g, b = rgb
    for y in range(size):
        for x in range(size):
            dist = ((x - centre) ** 2 + (y - centre) ** 2) ** 0.5
            alpha = int(255 * max(0.0, min(1.0, radius - dist + 0.5)))
            pixels[y * size + x] = (alpha << 24) | (r << 16) | (g << 8) | b
    mask = gdi32.CreateBitmap(size, size, 1, 1, None)
    info = ICONINFO(fIcon=True, hbmMask=mask, hbmColor=colour)
    icon = user32.CreateIconIndirect(ctypes.byref(info))
    gdi32.DeleteObject(mask)
    gdi32.DeleteObject(colour)
    return icon


def is_running() -> bool:
    return bool(user32.FindWindowW(WINDOW_CLASS, None))


def stop(timeout: float = 5) -> bool:
    """Ask a running listener to quit. True if one was running."""
    hwnd = user32.FindWindowW(WINDOW_CLASS, None)
    if not hwnd:
        return False
    user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    deadline = time.time() + timeout
    while is_running() and time.time() < deadline:
        time.sleep(0.05)
    return True


def hotkey_label(binding: str) -> str:
    """'<Super>slash' -> 'Win+/' for the tooltip and messages."""
    names = {"super": "Win", "ctrl": "Ctrl", "control": "Ctrl", "primary": "Ctrl", "alt": "Alt", "shift": "Shift"}
    mods, rest = [], binding
    while rest.startswith("<") and ">" in rest:
        end = rest.index(">")
        mods.append(names.get(rest[1:end].lower(), rest[1:end]))
        rest = rest[end + 1:]
    return "+".join(mods + [win32.CHAR_NAMES.get(rest.lower(), rest)])


class Listener:
    def __init__(self) -> None:
        self.cfg = config.load()
        self.actions: queue.Queue[str] = queue.Queue()
        self.running: str | None = None  # the action in progress
        self.state = ""
        self.tip = "Speakeasy"
        self.icons = {name: make_icon(rgb) for name, rgb in COLOURS.items()}
        self._wndproc = WNDPROC(self._on_message)  # must outlive the window
        self.taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")
        self.hwnd = None

    # --- window and tray ---

    def create_window(self) -> None:
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW(lpfnWndProc=self._wndproc, hInstance=hinst, lpszClassName=WINDOW_CLASS)
        user32.RegisterClassW(ctypes.byref(wc))
        # A hidden top-level window (not message-only) so it receives
        # TaskbarCreated and FindWindow can find it.
        self.hwnd = user32.CreateWindowExW(0, WINDOW_CLASS, "Speakeasy", 0, 0, 0, 0, 0, None, None, hinst, None)
        if not self.hwnd:
            raise OSError(f"CreateWindowExW failed ({ctypes.get_last_error()})")

    def _nid(self, flags: int) -> NOTIFYICONDATAW:
        return NOTIFYICONDATAW(cbSize=ctypes.sizeof(NOTIFYICONDATAW), hWnd=self.hwnd, uID=1, uFlags=flags,
                               uCallbackMessage=WM_TRAY, hIcon=self.icons[self.state or "idle"])

    def add_icon(self) -> None:
        nid = self._nid(NIF_MESSAGE | NIF_ICON | NIF_TIP)
        nid.szTip = self.tip[:127]
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))

    def update_icon(self) -> None:
        nid = self._nid(NIF_ICON | NIF_TIP)
        nid.szTip = self.tip[:127]
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    def balloon(self, msg: str, important: bool) -> None:
        nid = self._nid(NIF_INFO)
        nid.szInfo = msg[:255]
        nid.szInfoTitle = "Speakeasy"
        nid.dwInfoFlags = (NIIF_ERROR if important else NIIF_INFO) | NIIF_NOSOUND
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    def on_notify(self, msg: str, important: bool, popup: bool) -> None:
        """notify() sink: the tooltip always shows the latest message."""
        self.tip = f"Speakeasy: {msg}"
        self.update_icon()
        if popup:
            self.balloon(msg, important)

    def refresh_state(self) -> None:
        if recorder.is_recording():
            state = "recording"
        elif self.running is not None or not self.actions.empty():
            state = "busy"
        else:
            state = "idle"
        if state != self.state:
            self.state = state
            if state == "idle":
                keys = self.cfg.get("hotkeys", {})
                self.tip = f"Speakeasy: {hotkey_label(keys.get('toggle', ''))} to dictate"
            elif state == "recording":
                self.tip = "Speakeasy: recording"
            self.update_icon()

    # --- hotkeys ---

    def register_hotkeys(self) -> list[str]:
        """Register both hotkeys. Returns a message for each that failed."""
        problems = []
        keys = self.cfg.get("hotkeys", {})
        for hk_id, name in ((HOTKEY_TOGGLE, "toggle"), (HOTKEY_CANCEL, "cancel")):
            binding = keys.get(name)
            if not binding:
                continue
            try:
                flags, _vks, vk = win32.parse_combo(binding)
            except ValueError as exc:
                problems.append(f"hotkeys.{name}: {exc}")
                continue
            if not user32.RegisterHotKey(self.hwnd, hk_id, flags | win32.MOD_NOREPEAT, vk):
                err = ctypes.get_last_error()
                why = "another program already uses it" if err == ERROR_HOTKEY_ALREADY_REGISTERED else f"error {err}"
                problems.append(f"{hotkey_label(binding)} ({name}): {why}")
            else:
                mark(f"hotkey {name} = {binding}")
        return problems

    # --- actions, run one at a time off the UI thread ---

    def run_actions(self) -> None:
        while True:
            action = self.actions.get()
            self.running = action
            try:
                cfg = config.load()
                notify_mod.configure(cfg)
                config.reset_clock()
                if action == "toggle":
                    if recorder.is_recording():
                        cli.stop_and_transcribe(cfg)
                    else:
                        cli.start(cfg)
                elif action == "cancel":
                    cli.cancel()
            except BaseException as exc:  # keep the listener alive whatever happens
                mark(f"{action} failed: {type(exc).__name__}: {exc}")
                traceback.print_exc()
                notify_mod.notify(f"{action} failed: {exc}", important=True)
            finally:
                self.running = None

    # --- window procedure ---

    def _on_message(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_HOTKEY:
                self.actions.put("toggle" if wparam == HOTKEY_TOGGLE else "cancel")
                self.refresh_state()
                return 0
            if msg == WM_TIMER:
                self.refresh_state()
                return 0
            if msg == WM_TRAY and lparam in (WM_LBUTTONUP, WM_RBUTTONUP):
                self.show_menu()
                return 0
            if msg == WM_COMMAND:
                self.on_command(wparam & 0xFFFF)
                return 0
            if msg == self.taskbar_created:  # Explorer restarted
                self.add_icon()
                return 0
            if msg == WM_CLOSE:
                user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                nid = self._nid(0)
                shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
                user32.UnregisterHotKey(hwnd, HOTKEY_TOGGLE)
                user32.UnregisterHotKey(hwnd, HOTKEY_CANCEL)
                user32.PostQuitMessage(0)
                return 0
        except Exception:
            traceback.print_exc()
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def show_menu(self) -> None:
        keys = self.cfg.get("hotkeys", {})
        recording = recorder.is_recording()
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, MF_STRING, CMD_TOGGLE,
                           f"{'Stop and insert' if recording else 'Start dictation'}\t{hotkey_label(keys.get('toggle', ''))}")
        user32.AppendMenuW(menu, MF_STRING | (0 if recording else MF_GRAYED), CMD_CANCEL,
                           f"Cancel recording\t{hotkey_label(keys.get('cancel', ''))}")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, CMD_CONFIG, "Open personal config")
        user32.AppendMenuW(menu, MF_STRING, CMD_LOGS, "Open log folder")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, CMD_QUIT, "Quit Speakeasy")
        pos = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pos))
        user32.SetForegroundWindow(self.hwnd)  # otherwise the menu doesn't close on click-away
        cmd = user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY,
                                    pos.x, pos.y, 0, self.hwnd, None)
        user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(menu)
        if cmd:
            self.on_command(cmd)

    def on_command(self, cmd: int) -> None:
        if cmd == CMD_TOGGLE:
            self.actions.put("toggle")
        elif cmd == CMD_CANCEL:
            self.actions.put("cancel")
        elif cmd == CMD_CONFIG:
            if not USER_CONFIG.exists():
                USER_CONFIG.parent.mkdir(parents=True, exist_ok=True)
                USER_CONFIG.write_text("# Personal settings. Anything here overrides the repo's config.toml.\n",
                                       encoding="utf-8")
            os.startfile(str(USER_CONFIG))
        elif cmd == CMD_LOGS:
            config.STATE_DIR.mkdir(parents=True, exist_ok=True)
            os.startfile(str(config.STATE_DIR))
        elif cmd == CMD_QUIT:
            user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)

    def loop(self) -> None:
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))


def _log_to_file() -> None:
    # pythonw.exe has no stdout or stderr; keep tracebacks somewhere.
    if sys.stdout is None or sys.stderr is None:
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        log = open(LISTENER_LOG, "a", buffering=1, encoding="utf-8", errors="replace")
        sys.stdout = sys.stdout or log
        sys.stderr = sys.stderr or log


def run() -> None:
    _log_to_file()
    config.set_role("listener: ")
    lock = worker.take_lock(LISTENER_LOCK)
    if lock is None:
        print("speakeasy: the tray listener is already running", file=sys.stderr)
        return
    listener = Listener()
    listener.create_window()
    listener.refresh_state()
    listener.add_icon()
    notify_mod.configure(listener.cfg)
    notify_mod.set_sink(listener.on_notify)
    problems = listener.register_hotkeys()
    mark("started" + (f", hotkey problems: {problems}" if problems else ""))
    if problems:
        listener.balloon("Hotkeys not set: " + "; ".join(problems)
                         + ". Change [hotkeys] in your config and re-run --setup.", True)
    threading.Thread(target=listener.run_actions, daemon=True).start()
    user32.SetTimer(listener.hwnd, TIMER_ID, 300, None)
    try:
        listener.loop()
    finally:
        notify_mod.set_sink(None)
        mark("stopped")
        worker.release_lock(lock)
