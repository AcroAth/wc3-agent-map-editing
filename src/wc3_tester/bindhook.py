"""Built-in per-process socket binding (the useful part of ForceBindIP).

Warcraft III's LAN transport always uses UDP/TCP port 6112 and binds its
sockets to ``INADDR_ANY``.  On one PC only one process can therefore hold the
port.  ForceBindIP solves this by injecting a DLL that rewrites the game's
socket addresses, so instance N binds ``127.0.0.N`` instead.  This module
implements the same idea without an external tool:

* find every loaded module's import slots for ``WS2_32.dll`` (and
  ``WSOCK32.dll``) functions ``bind``, ``connect``, ``sendto``, ``WSAConnect``
  and ``WSASendTo``;
* redirect those slots to small stubs allocated in the game's address space;
* the ``bind`` stub replaces a wildcard (``0.0.0.0``) IPv4 address with the
  configured address; the other stubs bind a not-yet-bound socket to that
  address before forwarding the original call;
* keep watching for modules loaded later, because the game may load its
  networking DLL lazily.

Only the target process's import table and a small owned memory block are
changed.  No file is patched, and the block stays valid when this server exits.
The stubs are x86 (classic WC3) and x64 (Reforged); both are generated as
position-dependent machine code in the game process.
"""

from __future__ import annotations

import ctypes
import logging
import os
import socket
import struct
import sys
import threading
import time
from ctypes import wintypes as wt
from pathlib import Path

log = logging.getLogger(__name__)

MAGIC = b"WC3N"
VERSION = 1
FUNCS = ("bind", "connect", "sendto", "WSAConnect", "WSASendTo")
IMPORT_DLLS = ("ws2_32.dll", "wsock32.dll")

IMAGE_FILE_MACHINE_I386 = 0x014C
IMAGE_FILE_MACHINE_AMD64 = 0x8664
ARCHES = {IMAGE_FILE_MACHINE_I386: "x86", IMAGE_FILE_MACHINE_AMD64: "x64"}

# Block layout.  The header is pointer-size dependent; the code starts at
# HEADER and is generated after allocation because it embeds absolute addresses.
MAGIC_OFF = 0
VERSION_OFF = 4
IP_OFF = 8
SOCKADDR_OFF = 16
ORIG_OFF = 32
STUB_OFF_X86 = 52
STUB_OFF_X64 = 72
HEADER_X86 = 80
HEADER_X64 = 112

PROCESS_ACCESS = 0x0008 | 0x0010 | 0x0020 | 0x0400  # VM_OP|VM_READ|VM_WRITE|QUERY
MEM_COMMIT_RESERVE = 0x1000 | 0x2000
PAGE_EXECUTE_READWRITE = 0x40
PAGE_READWRITE = 0x04
TH32CS_SNAPMODULE = 0x08
TH32CS_SNAPMODULE32 = 0x10


class BindError(RuntimeError):
    """The bind hook could not be installed or maintained."""


_k32 = None
_k32_lock = threading.Lock()


class MODULEENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD), ("th32ModuleID", wt.DWORD), ("th32ProcessID", wt.DWORD),
        ("GlblcntUsage", wt.DWORD), ("ProccntUsage", wt.DWORD),
        ("modBaseAddr", ctypes.c_void_p), ("modBaseSize", wt.DWORD), ("hModule", wt.HMODULE),
        ("szModule", ctypes.c_wchar * 256), ("szExePath", ctypes.c_wchar * 260),
    ]


def _kernel32():
    """Lazily configure kernel32 so this module is importable on Linux."""
    global _k32
    with _k32_lock:
        if _k32 is not None:
            return _k32
        if sys.platform != "win32":
            raise BindError("the socket bind hook is Windows-only")
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        k.OpenProcess.restype = wt.HANDLE
        k.CloseHandle.argtypes = [wt.HANDLE]
        k.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                        ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        k.ReadProcessMemory.restype = wt.BOOL
        k.WriteProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                         ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        k.WriteProcessMemory.restype = wt.BOOL
        k.VirtualAllocEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wt.DWORD, wt.DWORD]
        k.VirtualAllocEx.restype = ctypes.c_void_p
        k.VirtualProtectEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wt.DWORD,
                                       ctypes.POINTER(wt.DWORD)]
        k.VirtualProtectEx.restype = wt.BOOL
        k.VirtualFreeEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wt.DWORD]
        k.VirtualFreeEx.restype = wt.BOOL
        k.FlushInstructionCache.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t]
        k.FlushInstructionCache.restype = wt.BOOL
        k.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
        k.CreateToolhelp32Snapshot.restype = wt.HANDLE
        k.Module32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
        k.Module32FirstW.restype = wt.BOOL
        k.Module32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
        k.Module32NextW.restype = wt.BOOL
        _k32 = k
        return k


# ---------------------------------------------------------------------------
# PE readers (import + export tables)
# ---------------------------------------------------------------------------


class _PE:
    """Minimal PE32/PE32+ reader for import and export directory RVAs."""

    def __init__(self, data: bytes):
        self.data = data
        if len(data) < 0x40 or data[:2] != b"MZ":
            raise ValueError("not a PE file")
        self.pe = struct.unpack_from("<I", data, 0x3C)[0]
        if self.pe + 24 > len(data) or data[self.pe:self.pe + 4] != b"PE\0\0":
            raise ValueError("not a PE file")
        self.machine = struct.unpack_from("<H", data, self.pe + 4)[0]
        n_sections = struct.unpack_from("<H", data, self.pe + 6)[0]
        opt_size = struct.unpack_from("<H", data, self.pe + 20)[0]
        self.opt = self.pe + 24
        self.opt_magic = struct.unpack_from("<H", data, self.opt)[0]
        if self.opt_magic == 0x20B:  # PE32+
            self.dir_base = self.opt + 112
        else:  # PE32
            self.dir_base = self.opt + 96
        self.sections = []
        for i in range(n_sections):
            s = self.opt + opt_size + i * 40
            vsize, va, rawsize, rawptr = struct.unpack_from("<IIII", data, s + 8)
            self.sections.append((va, max(vsize, rawsize), rawptr))

    def off(self, rva: int) -> int:
        for va, size, raw in self.sections:
            if va <= rva < va + size:
                return rva - va + raw
        raise ValueError(f"rva {rva:#x} is not in a section")

    def cstr(self, rva: int) -> str:
        o = self.off(rva)
        end = self.data.find(b"\0", o)
        if end < 0:
            raise ValueError(f"unterminated string at {rva:#x}")
        return self.data[o:end].decode("ascii", "replace")

    def directory(self, index: int) -> tuple[int, int]:
        return struct.unpack_from("<II", self.data, self.dir_base + index * 8)

    def import_slots(
        self,
        dlls: tuple[str, ...],
        funcs: tuple[str, ...],
        ordinal_maps: dict[str, dict[int, str]] | None = None,
    ) -> dict[str, list[int]]:
        """IAT RVAs for imports of *funcs* from any of *dlls*.

        ``ordinal_maps`` maps a lower-case DLL name to ``{ordinal: function}``;
        it is needed for modules (such as CPython's ``_socket.pyd``) that import
        Winsock by ordinal.  Without it only name imports are found.
        """
        result: dict[str, list[int]] = {f: [] for f in funcs}
        imp_rva, _imp_size = self.directory(1)
        if not imp_rva:
            return result
        dll_set = {d.lower() for d in dlls}
        func_set = set(funcs)
        entry_size = 8 if self.opt_magic == 0x20B else 4
        ordinal_bit = 1 << (entry_size * 8 - 1)
        entry_fmt = "<Q" if entry_size == 8 else "<I"
        desc = self.off(imp_rva)
        while True:
            ilt, _stamp, _forwarder, name_rva, iat = struct.unpack_from("<IIIII", self.data, desc)
            if not name_rva:
                break
            dll = self.cstr(name_rva).lower()
            if dll in dll_set and iat:
                lookup = ilt or iat
                i = 0
                while True:
                    entry = struct.unpack_from(
                        entry_fmt, self.data, self.off(lookup) + i * entry_size
                    )[0]
                    if not entry:
                        break
                    if entry & ordinal_bit:  # imported by ordinal
                        func = (ordinal_maps or {}).get(dll, {}).get(entry & 0xFFFF)
                        if func in func_set:
                            result[func].append(iat + i * entry_size)
                    else:  # imported by name
                        func = self.cstr(entry + 2)
                        if func in func_set:
                            result[func].append(iat + i * entry_size)
                    i += 1
            desc += 20
        return result

    def export_ordinal_names(self, funcs: tuple[str, ...]) -> dict[int, str]:
        """Ordinal number -> function name, for exports in *funcs*."""
        result: dict[int, str] = {}
        exp_rva, _exp_size = self.directory(0)
        if not exp_rva:
            return result
        o = self.off(exp_rva)
        (_chars, _stamp, _maj, _min, _name, base, _nfunc, nnames,
         _addr_funcs, addr_names, addr_ords) = struct.unpack_from("<IIHHIIIIIII", self.data, o)
        wanted = set(funcs)
        for i in range(nnames):
            name_rva = struct.unpack_from("<I", self.data, self.off(addr_names) + i * 4)[0]
            name = self.cstr(name_rva)
            if name in wanted:
                ordinal = struct.unpack_from("<H", self.data, self.off(addr_ords) + i * 2)[0]
                result[base + ordinal] = name
        return result

    def export_rvas(self, funcs: tuple[str, ...]) -> dict[str, int | None]:
        """RVA of each export, or None for a forwarder/missing export."""
        result: dict[str, int | None] = {f: None for f in funcs}
        exp_rva, exp_size = self.directory(0)
        if not exp_rva:
            return result
        o = self.off(exp_rva)
        (_chars, _stamp, _maj, _min, _name, _base, nfunc, nnames,
         addr_funcs, addr_names, addr_ords) = struct.unpack_from("<IIHHIIIIIII", self.data, o)
        wanted = set(funcs)
        for i in range(nnames):
            name_rva = struct.unpack_from("<I", self.data, self.off(addr_names) + i * 4)[0]
            name = self.cstr(name_rva)
            if name not in wanted:
                continue
            ordinal = struct.unpack_from("<H", self.data, self.off(addr_ords) + i * 2)[0]
            if ordinal >= nfunc:
                continue
            func_rva = struct.unpack_from("<I", self.data, self.off(addr_funcs) + ordinal * 4)[0]
            if not func_rva:
                continue
            if exp_rva <= func_rva < exp_rva + exp_size:
                # Forwarder string, not a code address.
                result[name] = None
            else:
                result[name] = func_rva
        return result


def pe_machine(path: str) -> int | None:
    """IMAGE_FILE_MACHINE_* of a PE file, or None if it cannot be read."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(0x1000)
        if head[:2] != b"MZ":
            return None
        pe = struct.unpack_from("<I", head, 0x3C)[0]
        if pe + 6 > len(head):
            return None
        return struct.unpack_from("<H", head, pe + 4)[0]
    except Exception:
        return None


def import_slots(
    path: str,
    funcs: tuple[str, ...] = FUNCS,
    ordinal_maps: dict[str, dict[int, str]] | None = None,
) -> dict[str, list[int]]:
    """IAT RVAs of ws2_32/wsock32 imports in one module file (empty on error)."""
    try:
        data = Path(path).read_bytes()
        return _PE(data).import_slots(IMPORT_DLLS, funcs, ordinal_maps)
    except Exception:
        return {f: [] for f in funcs}


def export_ordinal_names(path: str, funcs: tuple[str, ...] = FUNCS) -> dict[int, str]:
    """Ordinal -> name map for the Winsock functions in *funcs*."""
    try:
        data = Path(path).read_bytes()
        return _PE(data).export_ordinal_names(funcs)
    except Exception:
        return {}


def export_rvas(path: str, funcs: tuple[str, ...] = FUNCS) -> dict[str, int | None]:
    """Export RVAs in one module file (empty on error)."""
    try:
        data = Path(path).read_bytes()
        return _PE(data).export_rvas(funcs)
    except Exception:
        return {f: None for f in funcs}


# ---------------------------------------------------------------------------
# Target process memory and modules
# ---------------------------------------------------------------------------


class _Memory:
    def __init__(self, pid: int):
        k = _kernel32()
        self.k = k
        self.pid = int(pid)
        self.handle = k.OpenProcess(PROCESS_ACCESS, False, self.pid)
        if not self.handle:
            raise BindError(f"OpenProcess({self.pid}) failed ({ctypes.get_last_error()})")

    def close(self) -> None:
        if self.handle:
            try:
                self.k.CloseHandle(self.handle)
            except Exception:
                pass
            self.handle = None

    def read(self, addr: int, size: int) -> bytes:
        buf = ctypes.create_string_buffer(size)
        n = ctypes.c_size_t()
        if not self.k.ReadProcessMemory(self.handle, ctypes.c_void_p(int(addr)), buf, size,
                                        ctypes.byref(n)) or n.value != size:
            raise BindError(f"ReadProcessMemory({addr:#x}) failed ({ctypes.get_last_error()})")
        return buf.raw

    def write(self, addr: int, data: bytes, protect: int | None = None) -> None:
        old = wt.DWORD(0)
        changed = False
        if protect is not None:
            if not self.k.VirtualProtectEx(self.handle, ctypes.c_void_p(int(addr)), len(data),
                                           protect, ctypes.byref(old)):
                raise BindError(f"VirtualProtectEx({addr:#x}) failed ({ctypes.get_last_error()})")
            changed = True
        try:
            n = ctypes.c_size_t()
            buf = ctypes.create_string_buffer(data, len(data))
            if not self.k.WriteProcessMemory(self.handle, ctypes.c_void_p(int(addr)), buf, len(data),
                                             ctypes.byref(n)) or n.value != len(data):
                raise BindError(f"WriteProcessMemory({addr:#x}) failed ({ctypes.get_last_error()})")
        finally:
            if changed:
                self.k.VirtualProtectEx(self.handle, ctypes.c_void_p(int(addr)), len(data),
                                        old.value, ctypes.byref(wt.DWORD()))

    def read_ptr(self, addr: int, size: int) -> int:
        fmt = "<I" if size == 4 else "<Q"
        return struct.unpack(fmt, self.read(addr, size))[0]

    def write_ptr(self, addr: int, value: int, size: int, protect: int | None = None) -> None:
        fmt = "<I" if size == 4 else "<Q"
        self.write(addr, struct.pack(fmt, value), protect=protect)

    def alloc(self, size: int = 4096) -> int:
        block = self.k.VirtualAllocEx(self.handle, None, size, MEM_COMMIT_RESERVE,
                                      PAGE_EXECUTE_READWRITE)
        if not block:
            raise BindError(f"VirtualAllocEx failed ({ctypes.get_last_error()})")
        return int(block)

    def flush(self, addr: int, size: int) -> None:
        try:
            self.k.FlushInstructionCache(self.handle, ctypes.c_void_p(int(addr)), size)
        except Exception:
            pass


def list_modules(pid: int) -> list[dict]:
    """Loaded modules of *pid* as ``{name, path, base}`` (main module first)."""
    k = _kernel32()
    snap = k.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, int(pid))
    if not snap or snap == wt.HANDLE(-1).value:
        raise BindError(f"CreateToolhelp32Snapshot({pid}) failed ({ctypes.get_last_error()})")
    modules: list[dict] = []
    try:
        me = MODULEENTRY32W()
        me.dwSize = ctypes.sizeof(me)
        ok = k.Module32FirstW(snap, ctypes.byref(me))
        while ok:
            modules.append({
                "name": str(me.szModule),
                "path": str(me.szExePath),
                "base": int(me.modBaseAddr or 0),
            })
            ok = k.Module32NextW(snap, ctypes.byref(me))
    finally:
        k.CloseHandle(snap)
    return modules


def _process_arch(pid: int, modules: list[dict] | None = None) -> str:
    path = ""
    try:
        import psutil

        path = psutil.Process(int(pid)).exe()
    except Exception:
        pass
    if not path:
        modules = modules if modules is not None else list_modules(pid)
        for module in modules:
            if (module.get("name") or "").lower().endswith(".exe"):
                path = module.get("path") or ""
                break
    machine = pe_machine(path) if path else None
    arch = ARCHES.get(machine or -1)
    if arch is None:
        raise BindError(
            f"cannot determine the architecture of pid {pid} ({path or 'unknown exe'}); "
            "only 32-bit classic and 64-bit Reforged builds are supported"
        )
    return arch


# ---------------------------------------------------------------------------
# Machine code stubs
# ---------------------------------------------------------------------------


def _pack_ptr(value: int, bits: int) -> bytes:
    return struct.pack("<I" if bits == 32 else "<Q", value)


def _bind_stub_x86(block: int) -> bytes:
    """bool __stdcall bind(SOCKET, const sockaddr*, int): replace 0.0.0.0."""
    orig = block + ORIG_OFF
    body = (
        b"\x8B\x4C\x24\x08"                        # 0  mov ecx, [esp+8]  ; name
        b"\x85\xC9"                                # 4  test ecx, ecx
        b"\x74\x14"                                # 6  jz orig
        b"\x66\x83\x39\x02"                        # 8  cmp word [ecx], AF_INET
        b"\x75\x0E"                                # 12 jne orig
        b"\x83\x79\x04\x00"                        # 14 cmp dword [ecx+4], 0
        b"\x75\x08"                                # 18 jne orig
        b"\xA1" + _pack_ptr(block + IP_OFF, 32)    # 20 mov eax, [ip]
        + b"\x89\x41\x04"                           # 25 mov [ecx+4], eax
    )
    return body + b"\xFF\x25" + _pack_ptr(orig, 32)  # 28 jmp [orig bind]


def _prebind_stub_x86(block: int, index: int) -> bytes:
    """Before func #index, bind the socket to our address if it is unbound."""
    bind_orig = block + ORIG_OFF
    func_orig = block + ORIG_OFF + index * 4
    return (
        b"\x8B\x44\x24\x04"                          # mov eax, [esp+4]  ; s
        b"\x6A\x10"                                  # push 16
        b"\x68" + _pack_ptr(block + SOCKADDR_OFF, 32)  # push &local
        + b"\x50"                                     # push eax
        + b"\xFF\x15" + _pack_ptr(bind_orig, 32)      # call [orig bind]
        + b"\xFF\x25" + _pack_ptr(func_orig, 32)      # jmp [orig func]
    )


def _bind_stub_x64(block: int) -> bytes:
    """Windows x64 bind: rcx=s, rdx=name, r8d=namelen."""
    orig_target = 46
    code = bytearray()
    code += b"\x48\x85\xD2"                          #  0 test rdx, rdx
    jz_pos = len(code)
    code += b"\x0F\x84\x00\x00\x00\x00"              #  3 jz orig
    code += b"\x66\x83\x3A\x02"                      #  9 cmp word [rdx], AF_INET
    jne1 = len(code)
    code += b"\x0F\x85\x00\x00\x00\x00"              # 13 jne orig
    code += b"\x83\x7A\x04\x00"                      # 19 cmp dword [rdx+4], 0
    jne2 = len(code)
    code += b"\x0F\x85\x00\x00\x00\x00"              # 23 jne orig
    code += b"\x49\xBA" + _pack_ptr(block + IP_OFF, 64)  # 29 mov r10, &ip
    code += b"\x45\x8B\x12"                          # 39 mov r10d, [r10]
    code += b"\x44\x89\x52\x04"                      # 42 mov [rdx+4], r10d
    assert len(code) == orig_target
    for pos in (jz_pos, jne1, jne2):
        struct.pack_into("<i", code, pos + 2, orig_target - (pos + 6))
    code += b"\x49\xBB" + _pack_ptr(block + ORIG_OFF, 64)  # 46 mov r11, &orig
    code += b"\x49\x8B\x03"                          # 56 mov rax, [r11]
    code += b"\xFF\xE0"                              # 59 jmp rax
    return bytes(code)


def _prebind_stub_x64(block: int, index: int) -> bytes:
    """Before func #index, call bind(s, &local, 16), then tail-call original."""
    local = block + SOCKADDR_OFF
    bind_orig = block + ORIG_OFF
    func_orig = block + ORIG_OFF + index * 8
    return (
        b"\x48\x83\xEC\x48"                                # sub rsp, 0x48
        + b"\x48\x89\x4C\x24\x20"                           # mov [rsp+0x20], rcx
        + b"\x48\x89\x54\x24\x28"                           # mov [rsp+0x28], rdx
        + b"\x4C\x89\x44\x24\x30"                           # mov [rsp+0x30], r8
        + b"\x4C\x89\x4C\x24\x38"                           # mov [rsp+0x38], r9
        + b"\x48\xBA" + _pack_ptr(local, 64)                # mov rdx, &local
        + b"\x41\xB8\x10\x00\x00\x00"                       # mov r8d, 16
        + b"\x49\xBB" + _pack_ptr(bind_orig, 64)            # mov r11, &orig bind
        + b"\x49\x8B\x03"                                   # mov rax, [r11]
        + b"\xFF\xD0"                                       # call rax
        + b"\x48\x8B\x4C\x24\x20"                           # mov rcx, [rsp+0x20]
        + b"\x48\x8B\x54\x24\x28"                           # mov rdx, [rsp+0x28]
        + b"\x4C\x8B\x44\x24\x30"                           # mov r8, [rsp+0x30]
        + b"\x4C\x8B\x4C\x24\x38"                           # mov r9, [rsp+0x38]
        + b"\x48\x83\xC4\x48"                               # add rsp, 0x48
        + b"\x49\xBB" + _pack_ptr(func_orig, 64)            # mov r11, &orig func
        + b"\x49\x8B\x03"                                   # mov rax, [r11]
        + b"\xFF\xE0"                                       # jmp rax
    )


def _header_size(arch: str) -> int:
    return HEADER_X86 if arch == "x86" else HEADER_X64


def _stub_off(arch: str) -> int:
    return STUB_OFF_X86 if arch == "x86" else STUB_OFF_X64


def _ptr_size(arch: str) -> int:
    return 4 if arch == "x86" else 8


def _build_image(arch: str, block: int, ip_bytes: bytes, originals: list[int]):
    """Return (header+code bytes, stub addresses) for a fresh block."""
    header = _header_size(arch)
    ptr = _ptr_size(arch)
    if arch == "x86":
        code = _bind_stub_x86(block)
        stubs = [block + header]
        for index in range(1, len(FUNCS)):
            stubs.append(block + header + len(code))
            code += _prebind_stub_x86(block, index)
    else:
        code = _bind_stub_x64(block)
        stubs = [block + header]
        for index in range(1, len(FUNCS)):
            stubs.append(block + header + len(code))
            code += _prebind_stub_x64(block, index)

    image = bytearray(header + len(code))
    image[MAGIC_OFF:MAGIC_OFF + 4] = MAGIC
    struct.pack_into("<I", image, VERSION_OFF, VERSION)
    image[IP_OFF:IP_OFF + 4] = ip_bytes
    struct.pack_into("<HH4s8x", image, SOCKADDR_OFF, socket.AF_INET, 0, ip_bytes)
    for index, value in enumerate(originals):
        struct.pack_into("<I" if ptr == 4 else "<Q", image, ORIG_OFF + index * ptr, value)
    for index, value in enumerate(stubs):
        struct.pack_into("<I" if ptr == 4 else "<Q", image, _stub_off(arch) + index * ptr, value)
    image[header:] = code
    return bytes(image), stubs


# ---------------------------------------------------------------------------
# The hook
# ---------------------------------------------------------------------------


class BindHook:
    """One installed bind hook in one target process."""

    def __init__(self, pid: int, ip: str, arch: str, mem: _Memory):
        self.pid = int(pid)
        self.ip = ip
        self.arch = arch
        self.mem = mem
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self.block: int | None = None
        self.stubs: list[int] = []
        self.originals: list[int] = [0] * len(FUNCS)
        self._imports: dict[str, dict[str, list[int]]] = {}
        self._ord_map: dict[str, dict[int, str]] | None = None
        self._slots: dict[int, int] = {}          # slot address -> func index
        self._slot_orig: dict[int, int] = {}      # slot -> original IAT value
        self.patched = 0
        self.modules: set[str] = set()
        self.reused = False
        self._installed = False

    # -- helpers --------------------------------------------------------
    @property
    def ptr(self) -> int:
        return _ptr_size(self.arch)

    def _write_ip(self) -> None:
        assert self.block is not None
        ip_bytes = socket.inet_aton(self.ip)
        self.mem.write(self.block + IP_OFF, ip_bytes)
        self.mem.write(
            self.block + SOCKADDR_OFF,
            struct.pack("<HH4s8x", socket.AF_INET, 0, ip_bytes),
        )

    def _set_original(self, index: int, value: int) -> None:
        assert self.block is not None
        self.originals[index] = int(value)
        self.mem.write_ptr(self.block + ORIG_OFF + index * self.ptr, int(value), self.ptr)

    def _ordinal_maps(self, modules: list[dict]) -> dict[str, dict[int, str]]:
        """Ordinal -> name maps for the target architecture's Winsock DLLs."""
        if self._ord_map is not None:
            return self._ord_map
        paths: dict[str, str] = {}
        for module in modules:
            name = (module.get("name") or "").lower()
            if name in IMPORT_DLLS and module.get("path"):
                paths[name] = str(module["path"])
        root = Path(os.environ.get("SystemRoot") or r"C:\Windows")
        sub = "SysWOW64" if self.arch == "x86" else "System32"
        for dll in IMPORT_DLLS:
            if dll in paths:
                continue
            for candidate in (root / sub / dll, root / "System32" / dll):
                if candidate.is_file():
                    paths[dll] = str(candidate)
                    break
        self._ord_map = {
            dll: export_ordinal_names(path) for dll, path in paths.items()
        }
        return self._ord_map

    def _scan(self, modules: list[dict]) -> dict[str, list[int]]:
        """Collect IAT slot addresses from modules, parsing each file once."""
        slots: dict[str, list[int]] = {f: [] for f in FUNCS}
        ordinal_maps = self._ordinal_maps(modules)
        for module in modules:
            path = module.get("path") or ""
            base = int(module.get("base") or 0)
            if not path or not base:
                continue
            if path not in self._imports:
                self._imports[path] = import_slots(path, ordinal_maps=ordinal_maps)
            rvas = self._imports[path]
            for func, func_rvas in rvas.items():
                for rva in func_rvas:
                    slots[func].append(base + rva)
            if any(rvas.values()):
                self.modules.add(path)
        for func in slots:
            slots[func] = sorted(set(slots[func]))
        return slots

    def _find_ws2(self, modules: list[dict]) -> dict | None:
        for module in modules:
            if (module.get("name") or "").lower() == "ws2_32.dll":
                return module
        return None

    def _resolve_originals(self, modules: list[dict], slots: dict[str, list[int]]) -> list[int]:
        """Real function addresses: exports first, then the most common IAT value."""
        originals = [0] * len(FUNCS)
        ws = self._find_ws2(modules)
        if ws and ws.get("path"):
            rvas = export_rvas(ws["path"])
            for index, func in enumerate(FUNCS):
                rva = rvas.get(func)
                if rva:
                    originals[index] = int(ws.get("base") or 0) + int(rva)
        for index, func in enumerate(FUNCS):
            if originals[index] or not slots.get(func):
                continue
            counts: dict[int, int] = {}
            for slot in slots[func]:
                try:
                    value = self.mem.read_ptr(slot, self.ptr)
                except BindError:
                    continue
                if value:
                    counts[value] = counts.get(value, 0) + 1
            if counts:
                originals[index] = max(counts, key=counts.get)  # type: ignore[arg-type]
        return originals

    def _find_existing_block(self, slots: dict[str, list[int]]) -> int | None:
        """Reuse the block of an earlier server process if the bind slot has one."""
        header = _header_size(self.arch)
        for slot in slots.get("bind", []):
            try:
                value = self.mem.read_ptr(slot, self.ptr)
            except BindError:
                continue
            candidate = value - header
            if candidate <= 0:
                continue
            try:
                head = self.mem.read(candidate, 8)
            except BindError:
                continue
            if head[:4] != MAGIC or struct.unpack_from("<I", head, 4)[0] != VERSION:
                continue
            try:
                stub0 = self.mem.read_ptr(candidate + _stub_off(self.arch), self.ptr)
            except BindError:
                continue
            if stub0 == value:
                return candidate
        return None

    def _adopt_block(self, block: int) -> None:
        """Read back a block installed by an earlier server process."""
        self.block = block
        self.stubs = [
            self.mem.read_ptr(block + _stub_off(self.arch) + i * self.ptr, self.ptr)
            for i in range(len(FUNCS))
        ]
        self.originals = [
            self.mem.read_ptr(block + ORIG_OFF + i * self.ptr, self.ptr)
            for i in range(len(FUNCS))
        ]
        if not all(self.stubs) or not self.stubs[0] or not self.originals[0]:
            raise BindError("the existing bind-hook block is malformed")
        log.info("bind hook for pid %s reuses block %#x", self.pid, block)
        self.reused = True
        self._write_ip()

    def _create_block(self, originals: list[int]) -> None:
        assert self.block is None
        block = self.mem.alloc()
        if self.arch == "x86" and block >= 1 << 32:
            raise BindError(f"VirtualAllocEx returned a non-32-bit address {block:#x}")
        image, stubs = _build_image(self.arch, block, socket.inet_aton(self.ip), originals)
        self.mem.write(block, image)
        self.mem.flush(block, len(image) - _header_size(self.arch))
        self.block = block
        self.stubs = stubs
        self.originals = list(originals)
        log.info("bind hook installed in pid %s at %#x (%s)", self.pid, block, self.arch)

    def _patch(self, slots: dict[str, list[int]]) -> int:
        assert self.block is not None
        stub_addrs = self.stubs
        patched = 0
        for index, func in enumerate(FUNCS):
            stub = stub_addrs[index]
            # Every pre-bind stub calls the original bind, so it must be known.
            if not stub or not self.originals[index] or (index and not self.originals[0]):
                continue
            for slot in slots.get(func, []):
                try:
                    current = self.mem.read_ptr(slot, self.ptr)
                except BindError:
                    continue
                if current == stub:
                    self._slots.setdefault(slot, index)
                    continue
                self.mem.write_ptr(slot, stub, self.ptr, protect=PAGE_READWRITE)
                self._slots[slot] = index
                self._slot_orig[slot] = current
                patched += 1
        if patched:
            self.patched += patched
            log.info("bind hook pid %s: %s new import slot(s)", self.pid, patched)
        return patched

    def patch_new_modules(self) -> int:
        """Patch slots of modules loaded since the last scan. Never raises."""
        if not self._installed:
            return 0
        with self._lock:
            try:
                modules = list_modules(self.pid)
                slots = self._scan(modules)
                if not slots.get("bind") and not any(slots.values()):
                    return 0
                # A function that was unavailable during install may be
                # resolvable now (its module or its import has just loaded).
                for index, func in enumerate(FUNCS):
                    if self.originals[index] or not slots.get(func):
                        continue
                    for slot in slots[func]:
                        try:
                            value = self.mem.read_ptr(slot, self.ptr)
                        except BindError:
                            continue
                        if value:
                            self._set_original(index, value)
                            break
                return self._patch(slots)
            except Exception as exc:
                log.debug("bind hook pid %s: late module scan failed: %s", self.pid, exc)
                return 0

    # -- lifecycle ------------------------------------------------------
    def install(self, timeout: float = 20.0) -> "BindHook":
        deadline = time.time() + max(1.0, float(timeout))
        last = "no WS2_32 bind import found"
        while time.time() < deadline:
            try:
                modules = list_modules(self.pid)
            except BindError:
                modules = []
            if not modules:
                time.sleep(0.1)
                continue
            slots = self._scan(modules)
            last = "no WS2_32 bind import found"
            if not slots.get("bind"):
                time.sleep(0.1)
                continue
            existing = self._find_existing_block(slots)
            if existing:
                self._adopt_block(existing)
            else:
                originals = self._resolve_originals(modules, slots)
                if not originals[0]:
                    last = "WS2_32 bind address is not resolved yet"
                    time.sleep(0.1)
                    continue
                self._create_block(originals)
            # Fill function pointers that were not known when the block was
            # created (also applies to a reused block).
            for index, func in enumerate(FUNCS):
                if self.originals[index] or not slots.get(func):
                    continue
                for slot in slots[func]:
                    try:
                        value = self.mem.read_ptr(slot, self.ptr)
                    except BindError:
                        continue
                    if value:
                        self._set_original(index, value)
                        break
            self._write_ip()
            self._patch(slots)
            self._installed = True
            self._thread = threading.Thread(
                target=self._watch, name=f"wc3-bind-{self.pid}", daemon=True
            )
            self._thread.start()
            log.info(
                "bind hook ready for pid %s: %s on %s (%s slots)",
                self.pid, self.arch, self.ip, self.patched,
            )
            return self
        raise BindError(f"pid {self.pid}: {last} within {timeout:g}s")

    def _watch(self) -> None:
        while not self._stop.wait(1.0):
            try:
                import psutil

                if not psutil.pid_exists(self.pid):
                    break
                proc = psutil.Process(self.pid)
                if not proc.is_running():
                    break
            except Exception:
                break
            self.patch_new_modules()
        log.debug("bind hook watcher for pid %s stopped", self.pid)

    def uninstall(self) -> dict:
        """Restore the import slots patched by this object (best effort)."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        restored = 0
        with self._lock:
            for slot, index in list(self._slots.items()):
                try:
                    current = self.mem.read_ptr(slot, self.ptr)
                except BindError:
                    continue
                stub = self.stubs[index] if index < len(self.stubs) else 0
                if not stub or current != stub:
                    continue
                original = self._slot_orig.get(slot) or self.originals[index]
                if not original:
                    continue
                try:
                    self.mem.write_ptr(slot, original, self.ptr, protect=PAGE_READWRITE)
                    restored += 1
                except BindError:
                    pass
            self._slots.clear()
            self._slot_orig.clear()
        self._installed = False
        log.info("bind hook uninstalled from pid %s (%s slots)", self.pid, restored)
        return {"pid": self.pid, "restored_slots": restored}

    def info(self) -> dict:
        return {
            "pid": self.pid,
            "ip": self.ip,
            "arch": self.arch,
            "block": f"0x{self.block:x}" if self.block else None,
            "patched_slots": self.patched,
            "functions": [
                func for index, func in enumerate(FUNCS) if self.originals[index]
            ],
            "modules": sorted(Path(p).name for p in self.modules),
            "reused": self.reused,
            "watching": bool(self._thread and self._thread.is_alive()),
        }

    def close(self) -> None:
        self._installed = False
        self._stop.set()
        self.mem.close()


# ---------------------------------------------------------------------------
# Public API / cache
# ---------------------------------------------------------------------------

_hooks: dict[int, BindHook] = {}
_hooks_lock = threading.RLock()


def install(pid: int, ip: str, timeout: float = 20.0) -> BindHook:
    """Force *pid*'s sockets onto *ip*; idempotent per process and address."""
    try:
        socket.inet_aton(ip)
    except OSError as exc:
        raise BindError(f"invalid IPv4 address {ip!r}") from exc
    pid = int(pid)
    with _hooks_lock:
        existing = _hooks.get(pid)
        if existing is not None:
            if existing.ip != ip:
                raise BindError(
                    f"pid {pid} is already bound to {existing.ip}; kill the game "
                    "before changing its LAN address"
                )
            existing.patch_new_modules()
            return existing
    modules = list_modules(pid)
    arch = _process_arch(pid, modules)
    hook = BindHook(pid, ip, arch, _Memory(pid))
    try:
        hook.install(timeout=timeout)
    except Exception:
        hook.close()
        raise
    with _hooks_lock:
        other = _hooks.get(pid)
        if other is not None:
            hook.close()
            return other
        _hooks[pid] = hook
    return hook


def status(pid: int | None = None) -> dict | None:
    with _hooks_lock:
        if pid is None:
            return {
                "hooks": [
                    {k: v for k, v in hook.info().items() if k != "modules"}
                    for hook in _hooks.values()
                ]
            }
        hook = _hooks.get(int(pid))
        return hook.info() if hook else None


def forget(pid: int | None = None) -> None:
    """Stop watching (after a kill) without touching the dying process."""
    with _hooks_lock:
        if pid is None:
            hooks = list(_hooks.values())
            _hooks.clear()
        else:
            hook = _hooks.pop(int(pid), None)
            hooks = [hook] if hook else []
    for hook in hooks:
        try:
            hook._installed = False
            hook._stop.set()
            hook.mem.close()
        except Exception:
            pass
