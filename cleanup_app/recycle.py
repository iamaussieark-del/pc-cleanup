"""Recycle Bin queries and 'send to Recycle Bin' deletes."""

from __future__ import annotations

import ctypes
import json
import os
from ctypes import wintypes

from cleanup_app.util import run_hidden

FO_DELETE = 3
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040
FOF_NOERRORUI = 0x0400
SHERB_NOCONFIRMATION = 0x0001
SHERB_NOPROGRESSUI = 0x0002
SHERB_NOSOUND = 0x0004


class SHQUERYRBINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("_pad", ctypes.c_uint32),
        ("i64Size", ctypes.c_int64),
        ("i64NumItems", ctypes.c_int64),
    ]


class SHQUERYRBINFO_PACKED(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("i64Size", ctypes.c_int64),
        ("i64NumItems", ctypes.c_int64),
    ]


class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", ctypes.c_uint),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_uint16),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


def _shell32():
    shell32 = ctypes.windll.shell32
    shell32.SHQueryRecycleBinW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p]
    shell32.SHQueryRecycleBinW.restype = ctypes.c_long
    shell32.SHEmptyRecycleBinW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.DWORD]
    shell32.SHEmptyRecycleBinW.restype = ctypes.c_long
    shell32.SHFileOperationW.argtypes = [ctypes.POINTER(SHFILEOPSTRUCTW)]
    shell32.SHFileOperationW.restype = ctypes.c_int
    return shell32


def query_recycle_bin() -> tuple[int, int]:
    shell32 = _shell32()
    for struct in (SHQUERYRBINFO, SHQUERYRBINFO_PACKED):
        info = struct()
        info.cbSize = ctypes.sizeof(struct)
        result = shell32.SHQueryRecycleBinW(None, ctypes.byref(info))
        if result == 0:
            return max(0, int(info.i64Size)), max(0, int(info.i64NumItems))
    return 0, 0


def empty_recycle_bin() -> bool:
    result = _shell32().SHEmptyRecycleBinW(None, None, SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND)
    return result == 0


def _recycle_ctypes(paths: list[str]) -> bool:
    absolute = [os.path.abspath(path) for path in paths]
    buffer = ctypes.create_unicode_buffer("\0".join(absolute) + "\0")
    operation = SHFILEOPSTRUCTW()
    operation.wFunc = FO_DELETE
    operation.pFrom = ctypes.cast(buffer, wintypes.LPCWSTR)
    operation.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
    result = _shell32().SHFileOperationW(ctypes.byref(operation))
    return result == 0 and not operation.fAnyOperationsAborted


def _recycle_powershell(paths: list[str]) -> list[dict]:
    script = r"""
Add-Type -AssemblyName Microsoft.VisualBasic
$paths = ConvertFrom-Json -InputObject ([Console]::In.ReadToEnd())
$results = foreach ($p in $paths) {
  try {
    if (Test-Path -LiteralPath $p -PathType Container) {
      [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory(
        $p,
        [Microsoft.VisualBasic.FileIO.UIOption]::OnlyErrorDialogs,
        [Microsoft.VisualBasic.FileIO.RecycleOption]::SendToRecycleBin)
    } else {
      [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile(
        $p,
        [Microsoft.VisualBasic.FileIO.UIOption]::OnlyErrorDialogs,
        [Microsoft.VisualBasic.FileIO.RecycleOption]::SendToRecycleBin)
    }
    [pscustomobject]@{ Path = $p; Ok = $true; Error = '' }
  } catch {
    [pscustomobject]@{ Path = $p; Ok = $false; Error = $_.Exception.Message }
  }
}
$results | ConvertTo-Json -Compress
"""
    completed = run_hidden(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        timeout=180,
        input_text=json.dumps(paths),
    )
    raw = (completed.stdout or "").lstrip("\ufeff").strip()
    if not raw:
        message = (completed.stderr or "Could not move files to the Recycle Bin.").strip()
        return [{"Path": path, "Ok": False, "Error": message} for path in paths]
    data = json.loads(raw)
    if isinstance(data, dict):
        return [data]
    return list(data)


def send_to_recycle(paths: list[str]) -> list[dict]:
    existing = [os.path.abspath(path) for path in paths if os.path.exists(path)]
    missing = [path for path in paths if not os.path.exists(path)]
    results = [{"Path": path, "Ok": True, "Error": "Already gone"} for path in missing]
    if not existing:
        return results
    if _recycle_ctypes(existing) and all(not os.path.exists(path) for path in existing):
        results.extend({"Path": path, "Ok": True, "Error": ""} for path in existing)
        return results
    results.extend(_recycle_powershell(existing))
    return results
