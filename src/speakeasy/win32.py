"""Windows API calls through ctypes, so Windows needs no extra packages.

Processes, Ctrl+C to ffmpeg, the clipboard, SendInput typing, the focused
window and file locks. Only imported on Windows.
"""
from __future__ import annotations

import ctypes
import msvcrt
import os
import subprocess
import sys
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

ULONG_PTR = ctypes.c_size_t

# Process creation flags.
CREATE_NEW_PROCESS_GROUP = 0x00000200
DETACHED_PROCESS = 0x00000008
CREATE_NO_WINDOW = 0x08000000

PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
WAIT_TIMEOUT = 0x102

kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                ctypes.POINTER(wintypes.DWORD)]
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.restype = wintypes.LPVOID
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalFree.restype = wintypes.HGLOBAL


# --- processes -------------------------------------------------------------

def _open(pid: int, access: int):
    return kernel32.OpenProcess(access, False, pid) if pid > 0 else None


def image_name(pid: int) -> str:
    """Lower-case file name of a process's executable, or "" if unknown."""
    handle = _open(pid, PROCESS_QUERY_LIMITED_INFORMATION)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return ""
        return os.path.basename(buf.value).lower()
    finally:
        kernel32.CloseHandle(handle)


def pid_alive(pid: int, image: str | None = None) -> bool:
    """True if the process runs, and (if given) its exe name contains image.

    The image check guards against Windows reusing the PID of an ffmpeg that
    exited while nobody was looking. (os.kill(pid, 0) can't be used here: on
    Windows it terminates the process.)
    """
    handle = _open(pid, PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE)
    if not handle:
        return False
    try:
        if kernel32.WaitForSingleObject(handle, 0) != WAIT_TIMEOUT:
            return False
    finally:
        kernel32.CloseHandle(handle)
    return image is None or image in image_name(pid)


def wait_exit(pid: int, timeout: float) -> bool:
    """Wait for the process to exit. True if it has (or can't be opened)."""
    handle = _open(pid, SYNCHRONIZE)
    if not handle:
        return True
    try:
        return kernel32.WaitForSingleObject(handle, int(timeout * 1000)) != WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)


def terminate(pid: int) -> bool:
    handle = _open(pid, PROCESS_TERMINATE)
    if not handle:
        return False
    try:
        return bool(kernel32.TerminateProcess(handle, 1))
    finally:
        kernel32.CloseHandle(handle)


# Runs in its own short-lived process: attaching to ffmpeg's console and
# raising Ctrl+C there would also interrupt the caller if it did it itself.
_CTRL_C = """
import ctypes, sys
k = ctypes.WinDLL("kernel32")
k.FreeConsole()
if not k.AttachConsole(int(sys.argv[1])):
    sys.exit(1)
k.SetConsoleCtrlHandler(None, True)
sys.exit(0 if k.GenerateConsoleCtrlEvent(0, 0) else 2)
"""


def send_ctrl_c(pid: int) -> bool:
    """Ctrl+C to a console process started with CREATE_NO_WINDOW, like ffmpeg.

    ffmpeg treats it like SIGINT: it stops and finalizes the WAV header.
    """
    python = getattr(sys, "_base_executable", None) or sys.executable
    python = python.replace("pythonw.exe", "python.exe")
    try:
        done = subprocess.run([python, "-c", _CTRL_C, str(pid)], capture_output=True, timeout=5,
                              creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0


def detached_flags() -> int:
    """Popen creationflags for a background process with no console window."""
    return DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW


# --- default recording device -----------------------------------------------

class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, text: str) -> "GUID":
        import uuid

        b = uuid.UUID(text).bytes_le
        return cls.from_buffer_copy(b)


class PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", GUID), ("pid", wintypes.DWORD)]


class PROPVARIANT(ctypes.Structure):
    _fields_ = [("vt", wintypes.WORD), ("r1", wintypes.WORD), ("r2", wintypes.WORD), ("r3", wintypes.WORD),
                ("ptr", ctypes.c_void_p), ("extra", ctypes.c_void_p)]


def _com_method(obj: ctypes.c_void_p, index: int, *argtypes):
    """Method number `index` of a COM object's vtable, as a callable taking the remaining args."""
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    proto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)
    return lambda *args: proto(vtable[index])(obj, *args)


def default_capture_name() -> str | None:
    """Friendly name of Windows' default recording device, e.g. "Microphone (USB Audio)".

    It's the same name DirectShow (and so ffmpeg) lists the device under.
    """
    ole32 = ctypes.WinDLL("ole32")
    initialised = ole32.CoInitializeEx(None, 0x2) in (0, 1)  # COINIT_APARTMENTTHREADED; S_OK or S_FALSE
    enum, device, store = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    try:
        clsid = GUID.parse("BCDE0395-E52F-467C-8E3D-C4579291692E")  # MMDeviceEnumerator
        iid = GUID.parse("A95664D2-9614-4F35-A746-DE8DB63617E6")    # IMMDeviceEnumerator
        if ole32.CoCreateInstance(ctypes.byref(clsid), None, 0x17, ctypes.byref(iid), ctypes.byref(enum)) != 0:
            return None
        # GetDefaultAudioEndpoint(eCapture, eConsole)
        if _com_method(enum, 4, ctypes.c_int, ctypes.c_int, ctypes.c_void_p)(1, 0, ctypes.byref(device)) != 0:
            return None
        if _com_method(device, 4, wintypes.DWORD, ctypes.c_void_p)(0, ctypes.byref(store)) != 0:  # OpenPropertyStore
            return None
        key = PROPERTYKEY(GUID.parse("A45C254E-DF1C-4EFD-8020-67D146A850E0"), 14)  # PKEY_Device_FriendlyName
        value = PROPVARIANT()
        if _com_method(store, 5, ctypes.c_void_p, ctypes.c_void_p)(ctypes.byref(key), ctypes.byref(value)) != 0:
            return None
        if value.vt != 31 or not value.ptr:  # VT_LPWSTR
            return None
        name = ctypes.wstring_at(value.ptr)
        ole32.PropVariantClear(ctypes.byref(value))
        return name
    except OSError:
        return None
    finally:
        for obj in (store, device, enum):
            if obj:
                _com_method(obj, 2)()  # Release
        if initialised:
            ole32.CoUninitialize()


# --- file locks --------------------------------------------------------------

def try_lock(fh) -> bool:
    """Non-blocking exclusive lock on the open file. Released on close or exit."""
    try:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        return True
    except OSError:
        return False


def unlock(fh) -> None:
    try:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


# --- clipboard ---------------------------------------------------------------

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE


def set_clipboard(text: str) -> bool:
    data = text.encode("utf-16-le") + b"\0\0"
    for _ in range(20):  # another app may have it open for a moment
        if user32.OpenClipboard(None):
            break
        time.sleep(0.02)
    else:
        return False
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            return False
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            kernel32.GlobalFree(handle)
            return False
        ctypes.memmove(ptr, data, len(data))
        kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            kernel32.GlobalFree(handle)  # still ours if the clipboard didn't take it
            return False
        return True
    finally:
        user32.CloseClipboard()


# --- keyboard ----------------------------------------------------------------

INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT
user32.VkKeyScanW.argtypes = [wintypes.WCHAR]
user32.VkKeyScanW.restype = ctypes.c_short
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short

VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN = 0x10, 0x11, 0x12, 0x5B, 0x5C
# RegisterHotKey modifier flags.
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000

MODIFIERS = {
    "ctrl": (MOD_CONTROL, VK_CONTROL), "control": (MOD_CONTROL, VK_CONTROL), "primary": (MOD_CONTROL, VK_CONTROL),
    "shift": (MOD_SHIFT, VK_SHIFT),
    "alt": (MOD_ALT, VK_MENU),
    "super": (MOD_WIN, VK_LWIN), "win": (MOD_WIN, VK_LWIN), "windows": (MOD_WIN, VK_LWIN), "meta": (MOD_WIN, VK_LWIN),
}
NAMED_KEYS = {
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "space": 0x20, "escape": 0x1B, "esc": 0x1B,
    "backspace": 0x08, "delete": 0x2E, "insert": 0x2D, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "page_up": 0x21, "prior": 0x21, "pagedown": 0x22, "page_down": 0x22, "next": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28, "pause": 0x13, "print": 0x2C,
    "scroll_lock": 0x91,
}
EXTENDED = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, VK_LWIN, VK_RWIN}
# GNOME/X keysym names for punctuation, so the Linux hotkey syntax works as is.
CHAR_NAMES = {
    "slash": "/", "backslash": "\\", "apostrophe": "'", "quoteright": "'", "grave": "`",
    "semicolon": ";", "comma": ",", "period": ".", "minus": "-", "equal": "=",
    "bracketleft": "[", "bracketright": "]", "plus": "+",
}


def key_code(name: str) -> tuple[int, int]:
    """(virtual key, extra MOD_* flags the current layout needs) for a key name."""
    low = name.lower()
    if low in NAMED_KEYS:
        return NAMED_KEYS[low], 0
    if low[:1] == "f" and low[1:].isdigit() and 1 <= int(low[1:]) <= 24:
        return 0x70 + int(low[1:]) - 1, 0
    char = CHAR_NAMES.get(low, name if len(name) == 1 else None)
    if char is None:
        raise ValueError(f"unknown key {name!r}")
    scan = user32.VkKeyScanW(char)
    if scan == -1:
        raise ValueError(f"no key for {char!r} on this keyboard layout")
    vk, state = scan & 0xFF, (scan >> 8) & 0xFF
    if char.isalpha():
        state = 0  # "v" in "ctrl+v" means the V key, not a capital letter
    extra = (MOD_SHIFT if state & 1 else 0) | (MOD_CONTROL if state & 2 else 0) | (MOD_ALT if state & 4 else 0)
    return vk, extra


def parse_combo(combo: str) -> tuple[int, list[int], int]:
    """(MOD_* flags, modifier virtual keys, key) for "<Super>slash" or "ctrl+shift+v"."""
    mods: list[str] = []
    rest = combo.strip()
    while rest.startswith("<") and ">" in rest:
        end = rest.index(">")
        mods.append(rest[1:end])
        rest = rest[end + 1:].strip()
    if "+" in rest and len(rest) > 1:
        # "ctrl+shift+v"; a trailing "++" means the + key itself.
        parts = rest[:-2].split("+") + ["+"] if rest.endswith("++") else rest.split("+")
        mods += [p for p in parts[:-1] if p]
        rest = parts[-1]
    if not rest:
        raise ValueError(f"no key in {combo!r}")
    flags, vks = 0, []
    for mod in mods:
        if mod.lower() not in MODIFIERS:
            raise ValueError(f"unknown modifier {mod!r} in {combo!r}")
        flag, vk = MODIFIERS[mod.lower()]
        flags |= flag
        vks.append(vk)
    key, extra = key_code(rest)
    for flag, vk in ((MOD_SHIFT, VK_SHIFT), (MOD_CONTROL, VK_CONTROL), (MOD_ALT, VK_MENU)):
        if extra & flag and not flags & flag:
            flags |= flag
            vks.append(vk)
    return flags, vks, key


def _key(vk: int = 0, scan: int = 0, flags: int = 0) -> INPUT:
    if vk in EXTENDED:
        flags |= KEYEVENTF_EXTENDEDKEY
    return INPUT(type=INPUT_KEYBOARD, u=_INPUTUNION(ki=KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags)))


def _send(events: list[INPUT]) -> bool:
    if not events:
        return True
    array = (INPUT * len(events))(*events)
    return user32.SendInput(len(events), array, ctypes.sizeof(INPUT)) == len(events)


def send_keys(combo: str) -> bool:
    """Press and release a combination like "ctrl+v" or "Enter"."""
    try:
        _flags, mods, key = parse_combo(combo)
    except ValueError:
        return False
    down = [_key(vk) for vk in mods] + [_key(key)]
    up = [_key(key, flags=KEYEVENTF_KEYUP)] + [_key(vk, flags=KEYEVENTF_KEYUP) for vk in reversed(mods)]
    return _send(down + up)


def type_unicode(text: str, key_delay_ms: float = 0) -> bool:
    """Type text as Unicode key events, which works for any character."""
    units = text.encode("utf-16-le")
    events = []
    for i in range(0, len(units), 2):
        code = int.from_bytes(units[i:i + 2], "little")
        events.append([_key(scan=code, flags=KEYEVENTF_UNICODE),
                       _key(scan=code, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)])
    if key_delay_ms <= 0:
        return _send([e for pair in events for e in pair])
    for pair in events:
        if not _send(pair):
            return False
        time.sleep(key_delay_ms / 1000)
    return True


def modifiers_down() -> list[int]:
    return [vk for vk in (VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN) if user32.GetAsyncKeyState(vk) & 0x8000]


def wait_modifiers_released(timeout: float = 2.0) -> bool:
    """Wait until no modifier is held, so typed keys don't become shortcuts.

    The hotkey fires on key down: if Win is still held when the text is
    ready, a pasted Ctrl+V would turn into Win+Ctrl+V.
    """
    deadline = time.time() + timeout
    while modifiers_down():
        if time.time() > deadline:
            return False
        time.sleep(0.02)
    return True


# --- windows -----------------------------------------------------------------

user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]


def foreground_window() -> dict | None:
    """Title, class and program of the focused window, keyed like the GNOME extension."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    title = ctypes.create_unicode_buffer(user32.GetWindowTextLengthW(hwnd) + 1)
    user32.GetWindowTextW(hwnd, title, len(title))
    cls = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, cls, len(cls))
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return {"wm_class": cls.value, "title": title.value, "app_id": image_name(pid.value)}
