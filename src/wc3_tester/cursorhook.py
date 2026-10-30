"""Virtual cursor for background mouse input.

Classic Warcraft III (1.2x, 32-bit) takes the cursor position from
``USER32!GetCursorPos`` every frame and ignores the coordinates carried by
WM_MOUSEMOVE / WM_LBUTTONDOWN. Posted clicks therefore land wherever the
user's real mouse is. To click a point without moving the real cursor, we
redirect the game's import-table slots for GetCursorPos to a tiny stub that,
while enabled, returns a position we write into the game's memory:

    block = [magic "WC3V"][enabled][x][y][original GetCursorPos][stub code]

In a match the game also confines (ClipCursor) and warps (SetCursorPos) the
real cursor, even from the background.  Those imports are redirected too:
while the game is not the foreground window, ClipCursor becomes
ClipCursor(NULL) and SetCursorPos does nothing; in the foreground both pass
through unchanged.  Their data and stubs live at ``block + GUARD``.

The slots of every module loaded from the game's folder are redirected: the
exe on 1.28+, and on 1.26/1.27 also Game.dll, which holds the engine.  Only
import slots change (no code is patched), the change is reverted with
``uninstall()``, and nothing persists after the game exits.  x64 builds
(Reforged) are not supported yet; ``for_pid`` returns None for them.
"""

from __future__ import annotations

import ctypes
import logging
import os
import struct
import time
from ctypes import wintypes as wt

log = logging.getLogger(__name__)

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)

PROCESS_ACCESS = 0x0008 | 0x0010 | 0x0020 | 0x0400  # VM_OPERATION|VM_READ|VM_WRITE|QUERY_INFORMATION
MEM_COMMIT_RESERVE = 0x1000 | 0x2000
PAGE_EXECUTE_READWRITE = 0x40
PAGE_READWRITE = 0x04
MAGIC = b"WC3V"
HEADER = 20  # magic + enabled + x + y + original
# Guard area: [game pid][&GetForegroundWindow][&GetWindowThreadProcessId]
# [original ClipCursor][original SetCursorPos], then the two stubs.
GUARD = 0x100
CLIP_STUB = GUARD + 0x20
SETPOS_STUB = GUARD + 0x60
GUARDED = ("ClipCursor", "SetCursorPos")
IMAGE_FILE_MACHINE_I386 = 0x014C
RETRY_SEC = 2.0


class Unsupported(RuntimeError):
    """The virtual cursor cannot work in this process; retrying will not help."""


_k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
_k32.OpenProcess.restype = wt.HANDLE
_k32.CloseHandle.argtypes = [wt.HANDLE]
_k32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
_k32.WriteProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
_k32.VirtualAllocEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wt.DWORD, wt.DWORD]
_k32.VirtualAllocEx.restype = ctypes.c_void_p
_k32.VirtualProtectEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wt.DWORD, ctypes.POINTER(wt.DWORD)]


def _stub(block: int) -> bytes:
    """x86 stdcall BOOL GetCursorPos(LPPOINT) replacement; addresses absolute."""
    en, x, y, orig = block + 4, block + 8, block + 12, block + 16
    body = (
        b"\x8B\x4C\x24\x04"                      # mov ecx, [esp+4]   ; lpPoint
        + b"\xA1" + struct.pack("<I", x)          # mov eax, [x]
        + b"\x89\x01"                             # mov [ecx], eax
        + b"\xA1" + struct.pack("<I", y)          # mov eax, [y]
        + b"\x89\x41\x04"                         # mov [ecx+4], eax
        + b"\xB8\x01\x00\x00\x00"                 # mov eax, 1
        + b"\xC2\x04\x00"                         # ret 4
    )
    return (
        b"\xA1" + struct.pack("<I", en)           # mov eax, [enabled]
        + b"\x85\xC0"                             # test eax, eax
        + b"\x74" + bytes([len(body)])            # jz original
        + body
        + b"\xFF\x25" + struct.pack("<I", orig)   # original: jmp [orig]
    )


def _is_foreground(guard: int) -> bytes:
    """x86: ZF set when the foreground window belongs to the game's pid.
    Clobbers eax/ecx/edx only, as any stdcall callee may."""
    return (
        b"\xFF\x15" + struct.pack("<I", guard + 4)    # call [GetForegroundWindow]
        + b"\x6A\x00"                                 # push 0        ; pid out
        + b"\x54"                                     # push esp      ; &pid
        + b"\x50"                                     # push eax      ; hwnd
        + b"\xFF\x15" + struct.pack("<I", guard + 8)  # call [GetWindowThreadProcessId]
        + b"\x58"                                     # pop eax       ; pid
        + b"\x3B\x05" + struct.pack("<I", guard)      # cmp eax, [game pid]
    )


def _clip_stub(block: int) -> bytes:
    """BOOL ClipCursor(const RECT*): foreground -> original; else ClipCursor(NULL)."""
    guard = block + GUARD
    orig = struct.pack("<I", guard + 12)
    return (
        _is_foreground(guard)
        + b"\x75\x06"              # jne background
        + b"\xFF\x25" + orig       # jmp [original]
        + b"\x6A\x00"              # background: push 0
        + b"\xFF\x15" + orig       # call [original]   ; release any confinement
        + b"\xC2\x04\x00"          # ret 4
    )


def _setpos_stub(block: int) -> bytes:
    """BOOL SetCursorPos(int, int): foreground -> original; else a no-op."""
    guard = block + GUARD
    return (
        _is_foreground(guard)
        + b"\x75\x06"                                   # jne background
        + b"\xFF\x25" + struct.pack("<I", guard + 16)   # jmp [original]
        + b"\xB8\x01\x00\x00\x00"                       # background: mov eax, 1
        + b"\xC2\x08\x00"                               # ret 8
    )


def _user32_slots(pid: int, exe_path: str, funcs: tuple[str, ...]) -> dict[str, list[int]]:
    """Addresses of the USER32 import slots for *funcs* in every 32-bit
    module loaded from the game's folder."""
    from . import bindhook

    game_dir = os.path.normcase(os.path.dirname(os.path.abspath(exe_path)))
    slots: dict[str, list[int]] = {f: [] for f in funcs}
    for module in bindhook.list_modules(pid):
        path = module.get("path") or ""
        base = int(module.get("base") or 0)
        if not base or os.path.normcase(os.path.dirname(os.path.abspath(path))) != game_dir:
            continue
        if bindhook.pe_machine(path) != IMAGE_FILE_MACHINE_I386:
            continue
        for func, rvas in bindhook.import_slots(path, funcs, dlls=("user32.dll",)).items():
            slots[func].extend(base + rva for rva in rvas)
    return slots


def _user32_exports(pid: int, funcs: tuple[str, ...]) -> dict[str, int]:
    """Addresses of USER32 exports inside the (32-bit) game process."""
    from . import bindhook

    for module in bindhook.list_modules(pid):
        if (module.get("name") or "").lower() == "user32.dll" and module.get("path"):
            base = int(module.get("base") or 0)
            rvas = bindhook.export_rvas(module["path"], funcs)
            if base and all(rvas.values()):
                return {f: base + int(rvas[f]) for f in funcs}
    raise Unsupported("USER32 exports not found in the game process")


class VirtualCursor:
    """Owns the GetCursorPos redirect inside one game process."""

    def __init__(self, pid: int, exe_path: str):
        self.pid = pid
        self.exe_path = exe_path
        self.handle = _k32.OpenProcess(PROCESS_ACCESS, False, pid)
        if not self.handle:
            raise Unsupported(f"OpenProcess failed ({ctypes.get_last_error()}); is the game elevated?")
        self.block: int | None = None
        self.slots: list[int] = []
        self.guard_slots: dict[str, list[int]] = {}

    def close(self) -> None:
        if self.handle:
            _k32.CloseHandle(self.handle)
            self.handle = None

    # -- memory helpers -------------------------------------------------
    def _read(self, addr: int, size: int) -> bytes:
        buf = ctypes.create_string_buffer(size)
        n = ctypes.c_size_t()
        if not _k32.ReadProcessMemory(self.handle, addr, buf, size, ctypes.byref(n)) or n.value != size:
            raise OSError(f"ReadProcessMemory({addr:#x}) failed ({ctypes.get_last_error()})")
        return buf.raw

    def _write(self, addr: int, data: bytes) -> None:
        n = ctypes.c_size_t()
        if not _k32.WriteProcessMemory(self.handle, addr, data, len(data), ctypes.byref(n)) or n.value != len(data):
            raise OSError(f"WriteProcessMemory({addr:#x}) failed ({ctypes.get_last_error()})")

    def _write_slot(self, slot: int, value: int) -> None:
        old = wt.DWORD()
        _k32.VirtualProtectEx(self.handle, slot, 4, PAGE_READWRITE, ctypes.byref(old))
        try:
            self._write(slot, struct.pack("<I", value))
        finally:
            _k32.VirtualProtectEx(self.handle, slot, 4, old.value, ctypes.byref(wt.DWORD()))

    def _is_block(self, addr: int) -> bool:
        try:
            return addr > 0 and self._read(addr, 4) == MAGIC
        except OSError:
            return False

    # -- public ---------------------------------------------------------
    def install(self) -> None:
        found = _user32_slots(self.pid, self.exe_path, ("GetCursorPos",) + GUARDED)
        slots = found["GetCursorPos"]
        if not slots:
            raise Unsupported("no 32-bit game module imports GetCursorPos (x64 build?)")
        values = [struct.unpack("<I", self._read(slot, 4))[0] for slot in slots]
        # Already redirected (e.g. by an earlier server process)? Reuse it.
        block = next((v - HEADER for v in values if self._is_block(v - HEADER)), None)
        if block is None:
            block = _k32.VirtualAllocEx(self.handle, None, 4096, MEM_COMMIT_RESERVE, PAGE_EXECUTE_READWRITE)
            if not block or block >= 1 << 32:
                raise OSError(f"VirtualAllocEx failed ({ctypes.get_last_error()})")
            self._write(block, MAGIC + struct.pack("<IiiI", 0, 0, 0, values[0]) + _stub(block))
        self.block, self.slots = block, slots
        for slot, value in zip(slots, values):
            if value != block + HEADER:
                self._write_slot(slot, block + HEADER)
        log.info("virtual cursor installed in pid %s (%s slot(s))", self.pid, len(slots))
        try:
            self._install_guard(found)
        except Exception as exc:
            log.warning("cursor guard not installed in pid %s: %s", self.pid, exc)

    def _install_guard(self, found: dict[str, list[int]]) -> None:
        """Keep a background game from confining or moving the real cursor."""
        addr = _user32_exports(self.pid, ("GetForegroundWindow", "GetWindowThreadProcessId") + GUARDED)
        if any(a >= 1 << 32 for a in addr.values()):
            raise Unsupported("USER32 is not mapped below 4 GB (not a 32-bit process?)")
        block = self.block
        # Data and stubs first, then the slots: a slot must never point at
        # half-written code.
        self._write(block + GUARD, struct.pack(
            "<5I", self.pid, addr["GetForegroundWindow"], addr["GetWindowThreadProcessId"],
            addr["ClipCursor"], addr["SetCursorPos"]))
        self._write(block + CLIP_STUB, _clip_stub(block))
        self._write(block + SETPOS_STUB, _setpos_stub(block))
        stubs = {"ClipCursor": block + CLIP_STUB, "SetCursorPos": block + SETPOS_STUB}
        for func in GUARDED:
            for slot in found[func]:
                if struct.unpack("<I", self._read(slot, 4))[0] != stubs[func]:
                    self._write_slot(slot, stubs[func])
        self.guard_slots = {f: found[f] for f in GUARDED}
        log.info("cursor guard installed in pid %s (%s)", self.pid,
                 ", ".join(f"{f}: {len(found[f])} slot(s)" for f in GUARDED))

    def set(self, screen_x: int, screen_y: int) -> None:
        self._write(self.block + 4, struct.pack("<Iii", 1, int(screen_x), int(screen_y)))

    def release(self) -> None:
        """Hand the cursor back to the real mouse (redirect stays installed)."""
        if self.block:
            self._write(self.block + 4, struct.pack("<I", 0))

    def uninstall(self) -> None:
        if self.block and self.slots:
            orig = struct.unpack("<I", self._read(self.block + 16, 4))[0]
            for slot in self.slots:
                self._write_slot(slot, orig)
            for index, func in enumerate(GUARDED):
                orig = struct.unpack("<I", self._read(self.block + GUARD + 12 + 4 * index, 4))[0]
                for slot in self.guard_slots.get(func, []):
                    self._write_slot(slot, orig)
            self.release()


_cache: dict[int, VirtualCursor | None] = {}
_retry_at: dict[int, float] = {}


def for_pid(pid: int) -> VirtualCursor | None:
    """The virtual cursor for a game process, installing it on first use.

    Returns None when disabled (WC3_VIRTUAL_CURSOR=0) or unsupported.  Other
    failures (modules still loading, for example) are retried after
    RETRY_SEC instead of being remembered for the life of the game.
    """
    if os.environ.get("WC3_VIRTUAL_CURSOR", "1") == "0":
        return None
    if pid in _cache:
        return _cache[pid]
    if time.time() < _retry_at.get(pid, 0.0):
        return None
    vc = None
    try:
        import psutil

        vc = VirtualCursor(pid, psutil.Process(pid).exe())
        vc.install()
    except Exception as exc:
        if vc is not None:
            vc.close()
        if isinstance(exc, Unsupported):
            log.info("virtual cursor not available for pid %s: %s", pid, exc)
            _cache[pid] = None
        else:
            log.warning("virtual cursor install failed for pid %s, will retry: %s", pid, exc)
            _retry_at[pid] = time.time() + RETRY_SEC
        return None
    _cache[pid] = vc
    return vc


def release_all() -> None:
    for vc in _cache.values():
        if vc:
            try:
                vc.release()
            except Exception:
                pass
