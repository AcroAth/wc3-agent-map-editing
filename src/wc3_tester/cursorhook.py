"""Virtual cursor for background mouse input.

Classic Warcraft III (1.2x, 32-bit) takes the cursor position from
``USER32!GetCursorPos`` every frame and ignores the coordinates carried by
WM_MOUSEMOVE / WM_LBUTTONDOWN. Posted clicks therefore land wherever the
user's real mouse is. To click a point without moving the real cursor, we
redirect the game's import-table slot for GetCursorPos to a tiny stub that,
while enabled, returns a position we write into the game's memory:

    block = [magic "WC3V"][enabled][x][y][original GetCursorPos][stub code]

Only the game's own import slot is changed (no code is patched), the change
is reverted with ``uninstall()``, and nothing persists after the game exits.
x64 builds (Reforged) are not supported yet; ``available()`` says so.
"""

from __future__ import annotations

import ctypes
import logging
import os
import struct
from ctypes import wintypes as wt

log = logging.getLogger(__name__)

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)

PROCESS_ACCESS = 0x0008 | 0x0010 | 0x0020 | 0x0400  # VM_OPERATION|VM_READ|VM_WRITE|QUERY_INFORMATION
MEM_COMMIT_RESERVE = 0x1000 | 0x2000
PAGE_EXECUTE_READWRITE = 0x40
PAGE_READWRITE = 0x04
TH32CS_SNAPMODULE = 0x08
TH32CS_SNAPMODULE32 = 0x10
MAGIC = b"WC3V"
HEADER = 20  # magic + enabled + x + y + original
IMAGE_FILE_MACHINE_I386 = 0x014C


class MODULEENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD), ("th32ModuleID", wt.DWORD), ("th32ProcessID", wt.DWORD),
        ("GlblcntUsage", wt.DWORD), ("ProccntUsage", wt.DWORD),
        ("modBaseAddr", ctypes.c_void_p), ("modBaseSize", wt.DWORD), ("hModule", wt.HMODULE),
        ("szModule", ctypes.c_wchar * 256), ("szExePath", ctypes.c_wchar * 260),
    ]


_k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
_k32.OpenProcess.restype = wt.HANDLE
_k32.CloseHandle.argtypes = [wt.HANDLE]
_k32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
_k32.WriteProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
_k32.VirtualAllocEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wt.DWORD, wt.DWORD]
_k32.VirtualAllocEx.restype = ctypes.c_void_p
_k32.VirtualProtectEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wt.DWORD, ctypes.POINTER(wt.DWORD)]
_k32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
_k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
_k32.Module32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
_k32.Module32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(MODULEENTRY32W)]


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


def _import_slot_rva(exe_path: str, dll: str, func: str) -> int | None:
    """RVA of the import-address-table slot for dll!func, parsed from the PE
    file on disk (no third-party parser needed)."""
    with open(exe_path, "rb") as fh:
        data = fh.read()
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    machine = struct.unpack_from("<H", data, pe + 4)[0]
    if machine != IMAGE_FILE_MACHINE_I386:
        return None
    opt = pe + 24
    n_sections = struct.unpack_from("<H", data, pe + 6)[0]
    opt_size = struct.unpack_from("<H", data, pe + 20)[0]
    imp_rva = struct.unpack_from("<I", data, opt + 104)[0]  # data directory 1 (PE32)
    sections = []
    for i in range(n_sections):
        s = opt + opt_size + i * 40
        vsize, va, rawsize, rawptr = struct.unpack_from("<IIII", data, s + 8)
        sections.append((va, max(vsize, rawsize), rawptr))

    def off(rva: int) -> int:
        for va, size, raw in sections:
            if va <= rva < va + size:
                return rva - va + raw
        raise ValueError(f"rva {rva:#x} not in any section")

    def cstr(rva: int) -> str:
        o = off(rva)
        return data[o:data.index(b"\0", o)].decode("ascii", "replace")

    desc = off(imp_rva)
    while True:
        ilt, _, _, name_rva, iat = struct.unpack_from("<IIIII", data, desc)
        if not name_rva:
            return None
        if cstr(name_rva).lower() == dll.lower():
            lookup = ilt or iat
            i = 0
            while True:
                entry = struct.unpack_from("<I", data, off(lookup) + i * 4)[0]
                if not entry:
                    break
                if not entry & 0x80000000 and cstr(entry + 2) == func:
                    return iat + i * 4
                i += 1
        desc += 20


def _module_base(pid: int, exe_name: str) -> int | None:
    snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
    if not snap or snap == wt.HANDLE(-1).value:
        return None
    try:
        me = MODULEENTRY32W()
        me.dwSize = ctypes.sizeof(me)
        ok = _k32.Module32FirstW(snap, ctypes.byref(me))
        while ok:
            if me.szModule.lower() == exe_name.lower():
                return int(me.modBaseAddr)
            ok = _k32.Module32NextW(snap, ctypes.byref(me))
    finally:
        _k32.CloseHandle(snap)
    return None


class VirtualCursor:
    """Owns the GetCursorPos redirect inside one game process."""

    def __init__(self, pid: int, exe_path: str):
        self.pid = pid
        self.exe_path = exe_path
        self.handle = _k32.OpenProcess(PROCESS_ACCESS, False, pid)
        if not self.handle:
            raise OSError(f"OpenProcess failed ({ctypes.get_last_error()}); is the game elevated?")
        self.block: int | None = None
        self.slot: int | None = None

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

    def _write_slot(self, value: int) -> None:
        old = wt.DWORD()
        _k32.VirtualProtectEx(self.handle, self.slot, 4, PAGE_READWRITE, ctypes.byref(old))
        try:
            self._write(self.slot, struct.pack("<I", value))
        finally:
            _k32.VirtualProtectEx(self.handle, self.slot, 4, old.value, ctypes.byref(wt.DWORD()))

    # -- public ---------------------------------------------------------
    def install(self) -> None:
        exe_name = os.path.basename(self.exe_path)
        rva = _import_slot_rva(self.exe_path, "USER32.dll", "GetCursorPos")
        if rva is None:
            raise RuntimeError("GetCursorPos import not found (x64 build or different import layout)")
        base = _module_base(self.pid, exe_name)
        if base is None:
            raise RuntimeError(f"module {exe_name} not found in pid {self.pid}")
        self.slot = base + rva
        current = struct.unpack("<I", self._read(self.slot, 4))[0]
        # Already redirected (e.g. by an earlier server process)? Reuse it.
        try:
            if self._read(current - HEADER, 4) == MAGIC:
                self.block = current - HEADER
                return
        except OSError:
            pass
        block = _k32.VirtualAllocEx(self.handle, None, 4096, MEM_COMMIT_RESERVE, PAGE_EXECUTE_READWRITE)
        if not block or block >= 1 << 32:
            raise OSError(f"VirtualAllocEx failed ({ctypes.get_last_error()})")
        self._write(block, MAGIC + struct.pack("<IiiI", 0, 0, 0, current) + _stub(block))
        self.block = block
        self._write_slot(block + HEADER)
        log.info("virtual cursor installed in pid %s (slot %#x)", self.pid, self.slot)

    def set(self, screen_x: int, screen_y: int) -> None:
        self._write(self.block + 4, struct.pack("<Iii", 1, int(screen_x), int(screen_y)))

    def release(self) -> None:
        """Hand the cursor back to the real mouse (redirect stays installed)."""
        if self.block:
            self._write(self.block + 4, struct.pack("<I", 0))

    def uninstall(self) -> None:
        if self.block and self.slot:
            orig = struct.unpack("<I", self._read(self.block + 16, 4))[0]
            self._write_slot(orig)
            self.release()


_cache: dict[int, VirtualCursor | None] = {}


def for_pid(pid: int) -> VirtualCursor | None:
    """The virtual cursor for a game process, installing it on first use.
    Returns None when unsupported or disabled (WC3_VIRTUAL_CURSOR=0)."""
    if os.environ.get("WC3_VIRTUAL_CURSOR", "1") == "0":
        return None
    if pid in _cache:
        return _cache[pid]
    vc = None
    try:
        import psutil

        vc = VirtualCursor(pid, psutil.Process(pid).exe())
        vc.install()
    except Exception as exc:
        log.warning("virtual cursor unavailable for pid %s: %s", pid, exc)
        vc = None
    _cache[pid] = vc
    return vc


def release_all() -> None:
    for vc in _cache.values():
        if vc:
            try:
                vc.release()
            except Exception:
                pass
