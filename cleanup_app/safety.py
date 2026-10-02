"""Path gates. Deletes have to stay inside an approved folder."""

from __future__ import annotations

import os
import stat


SKIP_DIR_NAMES = {
    ".git",
    ".svn",
    ".vs",
    "__pycache__",
    "node_modules",
    "$recycle.bin",
    "system volume information",
    "recovery",
    "config.msi",
    "winsxs",
}

JUNK_SEQUENCES = (
    ("temp",),
    ("cache",),
    ("cache2",),
    ("code cache",),
    ("gpucache",),
    ("shadercache",),
    ("crashdumps",),
    ("inetcache",),
    ("d3dscache",),
    ("dxcache",),
    ("glcache",),
    ("dxccache",),
    ("wer",),
    ("deliveryoptimization",),
    ("softwaredistribution", "download"),
    ("pip", "cache"),
    ("npm-cache",),
    ("fontcache",),
    ("htmlcache",),
    ("cachestorage",),
)


def _parts(path: str) -> list[str]:
    return [part for part in os.path.normcase(os.path.abspath(path)).split("\\") if part]


def is_inside(child: str, parent: str) -> bool:
    child_path = os.path.normcase(os.path.abspath(child))
    parent_path = os.path.normcase(os.path.abspath(parent))
    if child_path == parent_path:
        return True
    if not parent_path.endswith("\\"):
        parent_path += "\\"
    return child_path.startswith(parent_path)


def system_prefixes() -> list[str]:
    defaults = (
        ("WINDIR", r"C:\Windows"),
        ("ProgramFiles", r"C:\Program Files"),
        ("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        ("ProgramData", r"C:\ProgramData"),
    )
    return [os.path.abspath(os.environ.get(name, default)) for name, default in defaults]


def is_system_location(path: str) -> bool:
    return any(is_inside(path, prefix) for prefix in system_prefixes())


def is_reparse(path: str) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _contains_sequence(parts: list[str], sequence: tuple[str, ...]) -> bool:
    width = len(sequence)
    for index in range(0, len(parts) - width + 1):
        if tuple(parts[index : index + width]) == sequence:
            return True
    return False


def approved_junk_path(path: str, mode: str, patterns: list[str] | None) -> bool:
    """True only for cache locations this app knows how to clean."""
    raw = os.path.abspath(path)
    lowered = os.path.normcase(raw)
    parts = _parts(raw)
    if len(parts) < 2:
        return False
    if lowered.endswith("\\windows\\memory.dmp"):
        return True
    if "epicgameslauncher" in parts and any(part.startswith("webcache") for part in parts):
        return True
    if len(parts) >= 2 and parts[-2:] == ["windows", "explorer"]:
        return mode == "glob" and bool(patterns)
    return any(_contains_sequence(parts, sequence) for sequence in JUNK_SEQUENCES)


def assert_inside_roots(path: str, roots: list[str]) -> None:
    if not any(is_inside(path, root) for root in roots):
        raise PermissionError(f"Refusing to touch a file outside the chosen folder: {path}")


def assert_personal_file(path: str, roots: list[str]) -> None:
    assert_inside_roots(path, roots)
    if is_reparse(path):
        raise PermissionError(f"Refusing to delete a link: {path}")
    if is_system_location(path):
        raise PermissionError(f"Refusing to delete a Windows or program file: {path}")
