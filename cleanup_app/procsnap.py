"""Every running process in one system call: name, memory, and CPU time.

psutil opens each process on its own. Without administrator, protected
processes refuse, and psutil then falls back to a full system snapshot for
every single one. One NtQuerySystemInformation call reads them all at once.
"""

from __future__ import annotations

import ctypes
import struct
import time
from dataclasses import dataclass

SYSTEM_PROCESS_INFORMATION = 5
STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
_ENTRY = struct.Struct("<IIqIIQqqqHH4xQl4xQQIIQQQI4xQQQQQQQQ")


@dataclass
class ProcSample:
    pid: int
    parent: int
    name: str
    threads: int
    handles: int
    session: int
    working_set: int
    private_working_set: int
    private_bytes: int
    cpu_time: float


def snapshot() -> dict[int, ProcSample] | None:
    """Return pid -> sample, or None when this Python cannot make the call."""
    if ctypes.sizeof(ctypes.c_void_p) != 8:
        return None
    try:
        query = ctypes.WinDLL("ntdll").NtQuerySystemInformation
    except (OSError, AttributeError):
        return None
    query.argtypes = [ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]
    query.restype = ctypes.c_ulong
    size = 1 << 20
    for _attempt in range(6):
        buffer = ctypes.create_string_buffer(size)
        needed = ctypes.c_ulong(0)
        status = query(SYSTEM_PROCESS_INFORMATION, buffer, size, ctypes.byref(needed))
        if status == STATUS_INFO_LENGTH_MISMATCH:
            size = max(size * 2, needed.value + (1 << 16))
            continue
        if status != 0:
            return None
        return _parse(buffer)
    return None


def _parse(buffer) -> dict[int, ProcSample]:
    raw = buffer.raw
    base = ctypes.addressof(buffer)
    found: dict[int, ProcSample] = {}
    offset = 0
    while True:
        (
            next_offset,
            threads,
            private_working_set,
            _hard_faults,
            _thread_peak,
            _cycles,
            _created,
            user_time,
            kernel_time,
            name_length,
            _name_max,
            name_pointer,
            _priority,
            pid,
            parent,
            handles,
            session,
            _key,
            _peak_virtual,
            _virtual,
            _faults,
            _peak_working_set,
            working_set,
            _peak_paged,
            _paged,
            _peak_nonpaged,
            _nonpaged,
            private_bytes,
            _peak_private,
        ) = _ENTRY.unpack_from(raw, offset)
        if name_pointer and name_length:
            start = name_pointer - base
            if 0 <= start and start + name_length <= len(raw):
                name = raw[start : start + name_length].decode("utf-16-le", errors="replace")
            else:
                name = ""
        else:
            name = "System Idle Process" if pid == 0 else ""
        found[pid] = ProcSample(
            pid=pid,
            parent=parent,
            name=name,
            threads=threads,
            handles=handles,
            session=session,
            working_set=working_set,
            private_working_set=private_working_set,
            private_bytes=private_bytes,
            cpu_time=(user_time + kernel_time) / 10_000_000,
        )
        if not next_offset:
            return found
        offset += next_offset


def cpu_percent(interval: float = 0.4, cancel=None) -> tuple[dict[int, ProcSample], dict[int, float]] | None:
    """Two snapshots apart. Percent is of one CPU core, the same scale psutil uses."""
    first = snapshot()
    if first is None:
        return None
    started = time.perf_counter()
    if cancel is not None:
        cancel.wait(interval)
    else:
        time.sleep(interval)
    second = snapshot()
    if second is None:
        return None
    elapsed = max(0.001, time.perf_counter() - started)
    percent: dict[int, float] = {}
    for pid, sample in second.items():
        before = first.get(pid)
        if before is None or pid == 0:
            continue
        percent[pid] = max(0.0, (sample.cpu_time - before.cpu_time) / elapsed * 100)
    return second, percent
