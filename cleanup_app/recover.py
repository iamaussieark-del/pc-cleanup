"""Find files in the Recycle Bin and deleted NTFS records, then copy them back.

Drive recovery reads the file table. It does not write onto the deleted space.
Restored copies go to a folder you choose, or Recycle Bin items can be put back.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import struct
from dataclasses import dataclass, field
from datetime import datetime

from cleanup_app.diskinfo import DRIVE_CDROM, DRIVE_REMOTE, drive_letters, drive_type
from cleanup_app.safety import is_system_location
from cleanup_app.util import is_admin

FILETIME_EPOCH = 11644473600
GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x1
FILE_SHARE_WRITE = 0x2
FILE_SHARE_DELETE = 0x4
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x80
INVALID_HANDLE = ctypes.c_void_p(-1).value
MAX_HITS = 12000
BITMAP_CAP = 96 * 1024 * 1024
# Raw volume reads must start and end on a sector boundary. 4096 covers both
# 512-byte and 4K-sector drives.
ALIGN = 4096
TYPE_PRESETS = {
    "All files": "",
    "Documents": "pdf,doc,docx,xls,xlsx,ppt,pptx,txt,rtf,odt,csv",
    "Pictures": "jpg,jpeg,png,gif,bmp,heic,webp,tif,tiff,raw,svg",
    "Videos": "mp4,mov,avi,mkv,wmv,webm",
    "Music": "mp3,wav,flac,aac,m4a,ogg",
    "Archives": "zip,rar,7z,tar,gz,iso",
}


@dataclass
class DeletedFile:
    id: str
    name: str
    original_path: str
    size: int
    when: float
    source: str
    chance: str
    drive: str
    note: str
    checked: bool = False
    recycle_data: str = ""
    recycle_info: str = ""
    resident: bytes | None = None
    runs: list = field(default_factory=list)
    cluster_size: int = 0


@dataclass
class RecoverScan:
    files: list[DeletedFile]
    notes: list[str]
    partial: bool = False
    seen: int = 0


def parse_day(text: str, end: bool = False) -> float | None:
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        moment = datetime.strptime(raw, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("Use dates like 2026-01-31, or leave them blank for any date.") from exc
    if end:
        moment = moment.replace(hour=23, minute=59, second=59)
    return moment.timestamp()


def parse_extensions(text: str) -> set[str]:
    found = set()
    for part in (text or "").replace(";", ",").split(","):
        token = part.strip().lower().lstrip(".")
        if token and token != "*":
            found.add(token)
    return found


def in_range(when: float, start: float | None, end: float | None) -> bool:
    if when <= 0:
        return start is None and end is None
    if start is not None and when < start:
        return False
    if end is not None and when > end:
        return False
    return True


def path_under(original: str, folder: str) -> bool:
    if not (folder or "").strip():
        return True

    def norm(path: str) -> str:
        text = os.path.normcase(path).replace("/", "\\")
        if len(text) >= 2 and text[1] == ":":
            text = text[2:]
        return text.strip("\\")

    child = norm(original)
    parent = norm(folder)
    return child == parent or child.startswith(parent + "\\")


def name_matches(name: str, path: str, query: str, extensions: set[str]) -> bool:
    if query and query.casefold() not in name.casefold() and query.casefold() not in path.casefold():
        return False
    if not extensions:
        return True
    suffix = os.path.splitext(name)[1].lower().lstrip(".")
    return suffix in extensions


def filetime_to_unix(value: int) -> float:
    if value <= 0:
        return 0.0
    return value / 10_000_000 - FILETIME_EPOCH


def build_path(parent: int, name: str, directories: dict[int, tuple[int, str]]) -> str:
    parts = [name] if name else []
    seen = set()
    current = parent
    while current and current not in seen and current in directories:
        seen.add(current)
        current, folder = directories[current]
        if folder and folder not in {".", ".."}:
            parts.append(folder)
        if len(parts) > 32:
            break
    parts.reverse()
    return "\\".join(parts)


def decode_runs(buf: bytes) -> list[tuple[int, int | None]]:
    runs: list[tuple[int, int | None]] = []
    offset = 0
    lcn = 0
    while offset < len(buf) and buf[offset] != 0:
        header = buf[offset]
        offset += 1
        length_size = header & 0x0F
        offset_size = header >> 4
        if length_size == 0 or offset + length_size + offset_size > len(buf):
            break
        length = int.from_bytes(buf[offset : offset + length_size], "little")
        offset += length_size
        if offset_size == 0:
            runs.append((length, None))
            continue
        delta = int.from_bytes(buf[offset : offset + offset_size], "little", signed=True)
        offset += offset_size
        lcn += delta
        if lcn < 0:
            break
        runs.append((length, lcn))
    return runs


def apply_fixup(record: bytearray, sector_size: int) -> bool:
    if len(record) < 32 or record[0:4] != b"FILE":
        return False
    usa_offset, usa_words = struct.unpack_from("<HH", record, 4)
    if usa_words < 2 or usa_offset < 0 or usa_offset + usa_words * 2 > len(record):
        return False
    usn = struct.unpack_from("<H", record, usa_offset)[0]
    for index in range(1, usa_words):
        sector_end = index * sector_size - 2
        if sector_end < 0 or sector_end + 2 > len(record):
            return False
        if struct.unpack_from("<H", record, sector_end)[0] != usn:
            return False
        patch = struct.unpack_from("<H", record, usa_offset + index * 2)[0]
        struct.pack_into("<H", record, sector_end, patch)
    return True


def _attributes(record: bytes):
    if len(record) < 22:
        return
    offset = struct.unpack_from("<H", record, 0x14)[0]
    while offset + 8 <= len(record):
        attr_type, length = struct.unpack_from("<II", record, offset)
        if attr_type == 0xFFFFFFFF or length < 16 or offset + length > len(record):
            break
        yield attr_type, record[offset : offset + length]
        offset += length


def parse_file_record(record: bytes, sector_size: int = 512) -> dict | None:
    raw = bytearray(record)
    if not apply_fixup(raw, sector_size):
        return None
    flags = struct.unpack_from("<H", raw, 0x16)[0]
    base = int.from_bytes(raw[0x20:0x26], "little")
    parsed = {
        "in_use": bool(flags & 0x01),
        "is_dir": bool(flags & 0x02),
        "extension": base != 0,
        "names": [],
        "modified": 0.0,
        "changed": 0.0,
        "resident": None,
        "runs": [],
        "size": 0,
        "compressed": False,
        "encrypted": False,
        "has_data": False,
    }
    for attr_type, attr in _attributes(raw):
        nonresident = attr[8] == 1
        attr_flags = struct.unpack_from("<H", attr, 0x0C)[0]
        name_length = attr[9]
        if attr_type == 0x10 and not nonresident:
            value_off = struct.unpack_from("<H", attr, 0x14)[0]
            if value_off + 32 <= len(attr):
                parsed["modified"] = filetime_to_unix(struct.unpack_from("<Q", attr, value_off + 8)[0])
                parsed["changed"] = filetime_to_unix(struct.unpack_from("<Q", attr, value_off + 16)[0])
            if value_off + 36 <= len(attr):
                file_flags = struct.unpack_from("<I", attr, value_off + 32)[0]
                parsed["encrypted"] = parsed["encrypted"] or bool(file_flags & 0x4000)
                parsed["compressed"] = parsed["compressed"] or bool(file_flags & 0x800)
        elif attr_type == 0x30 and not nonresident and name_length == 0:
            value_off = struct.unpack_from("<H", attr, 0x14)[0]
            value_len = struct.unpack_from("<I", attr, 0x10)[0]
            value = attr[value_off : value_off + value_len]
            if len(value) < 0x42:
                continue
            parent = int.from_bytes(value[0:6], "little")
            chars = value[0x40]
            namespace = value[0x41]
            name = value[0x42 : 0x42 + chars * 2].decode("utf-16le", errors="replace")
            if name:
                parsed["names"].append((namespace, parent, name))
        elif attr_type == 0x80 and name_length == 0:
            parsed["has_data"] = True
            parsed["compressed"] = parsed["compressed"] or bool(attr_flags & 0x0001)
            parsed["encrypted"] = parsed["encrypted"] or bool(attr_flags & 0x4000)
            if nonresident:
                run_off = struct.unpack_from("<H", attr, 0x20)[0]
                parsed["size"] = struct.unpack_from("<Q", attr, 0x30)[0]
                parsed["runs"] = decode_runs(attr[run_off:])
            else:
                value_off = struct.unpack_from("<H", attr, 0x14)[0]
                value_len = struct.unpack_from("<I", attr, 0x10)[0]
                parsed["size"] = value_len
                parsed["resident"] = bytes(attr[value_off : value_off + value_len])
    if not parsed["names"]:
        return None
    win32 = [item for item in parsed["names"] if item[0] in {1, 3}]
    chosen = win32[-1] if win32 else parsed["names"][-1]
    parsed["parent"] = chosen[1]
    parsed["name"] = chosen[2]
    return parsed


def cluster_allocated(bitmap: bytes | None, cluster: int) -> bool | None:
    if bitmap is None or cluster < 0:
        return None
    index = cluster // 8
    if index >= len(bitmap):
        return None
    return bool(bitmap[index] & (1 << (cluster % 8)))


def sample_clusters(runs: list[tuple[int, int | None]], limit: int = 24) -> list[int]:
    clusters: list[int] = []
    for length, lcn in runs:
        if lcn is None or length <= 0:
            continue
        if length <= 4:
            clusters.extend(range(lcn, lcn + length))
        else:
            clusters.extend((lcn, lcn + length // 2, lcn + length - 1))
        if len(clusters) >= limit:
            break
    return clusters[:limit]


def recovery_chance(parsed: dict, bitmap: bytes | None) -> str:
    if parsed.get("encrypted"):
        return "Encrypted"
    if parsed.get("compressed") and not parsed.get("resident"):
        return "Compressed"
    if parsed.get("resident") is not None or parsed.get("size", 0) == 0 and parsed.get("has_data"):
        return "Good"
    if not parsed.get("runs"):
        return "Name only"
    samples = sample_clusters(parsed["runs"])
    if not samples or bitmap is None:
        return "Unknown"
    states = [cluster_allocated(bitmap, cluster) for cluster in samples]
    if any(state is None for state in states):
        return "Unknown"
    if any(states):
        return "Poor"
    return "Good"


def parse_recycle_info(blob: bytes) -> tuple[int, float, str] | None:
    if len(blob) < 24:
        return None
    version, size, deleted = struct.unpack_from("<QQQ", blob, 0)
    if version == 2 and len(blob) >= 28:
        chars = struct.unpack_from("<I", blob, 24)[0]
        if chars <= 0 or chars > 4096:
            return None
        path = blob[28 : 28 + chars * 2].decode("utf-16le", errors="replace").rstrip("\x00")
    elif version == 1:
        path = blob[24:24 + 520].decode("utf-16le", errors="replace").split("\x00", 1)[0]
    else:
        return None
    path = path.strip()
    if not path:
        return None
    return size, filetime_to_unix(deleted), path


def _ready_drives() -> list[str]:
    letters = []
    for letter in drive_letters():
        kind = drive_type(letter)
        if kind in {DRIVE_CDROM, DRIVE_REMOTE}:
            continue
        letters.append(letter)
    return letters


def _filesystem(letter: str) -> str:
    name = ctypes.create_unicode_buffer(64)
    ok = ctypes.windll.kernel32.GetVolumeInformationW(f"{letter}:\\", None, 0, None, None, None, name, 64)
    if not ok:
        return ""
    return name.value


def list_recycle_bin(folder: str | None = None) -> list[tuple[str, str, int, float, str]]:
    """Return data path, info path, size, deleted time, original path."""
    roots = [folder] if folder else [f"{letter}:\\$Recycle.Bin" for letter in _ready_drives()]
    found = []
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for current, dirs, files in os.walk(root):
            # A deleted folder is kept as a $R folder beside its $I record.
            names = {name.lower(): name for name in files}
            data_names = {**names, **{name.lower(): name for name in dirs if name.lower().startswith("$r")}}
            # Its contents belong to that one entry, so the walk does not go inside.
            dirs[:] = [name for name in dirs if not name.lower().startswith("$r")]
            for lowered, name in names.items():
                if not lowered.startswith("$i"):
                    continue
                suffix = name[2:]
                data_name = data_names.get("$r" + suffix.lower())
                if not data_name:
                    continue
                info_path = os.path.join(current, name)
                data_path = os.path.join(current, data_name)
                try:
                    with open(info_path, "rb") as handle:
                        parsed = parse_recycle_info(handle.read(8192))
                except OSError:
                    continue
                if not parsed:
                    continue
                size, when, path = parsed
                found.append((data_path, info_path, size, when, path))
    return found


def _open_volume(letter: str):
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.CreateFileW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    handle = kernel32.CreateFileW(
        f"\\\\.\\{letter}:",
        GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        None,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL,
        None,
    )
    if not handle or handle == INVALID_HANDLE:
        code = ctypes.get_last_error()
        if code == 5:
            raise PermissionError(f"{letter}: needs administrator before deleted files on the drive can be read.")
        raise OSError(f"Could not open {letter}: (Windows error {code}).")
    return kernel32, handle


def _read_at(kernel32, handle, offset: int, size: int) -> bytes:
    """Read size bytes at offset. Windows only reads a raw volume in whole sectors,
    so the read is widened to sector boundaries and the asked-for part cut out."""
    if size <= 0:
        return b""
    start = offset - offset % ALIGN
    end = -(-(offset + size) // ALIGN) * ALIGN
    kernel32.SetFilePointerEx.argtypes = [ctypes.c_void_p, ctypes.c_longlong, ctypes.POINTER(ctypes.c_longlong), ctypes.c_uint32]
    kernel32.ReadFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p]
    new_pos = ctypes.c_longlong()
    if not kernel32.SetFilePointerEx(handle, ctypes.c_longlong(start), ctypes.byref(new_pos), 0):
        raise OSError("Could not move to a position on the drive.")
    buffer = ctypes.create_string_buffer(end - start)
    read = ctypes.c_ulong()
    if not kernel32.ReadFile(handle, buffer, end - start, ctypes.byref(read), None):
        raise OSError("Could not read the drive.")
    skip = offset - start
    return buffer.raw[skip : min(read.value, skip + size)]


def _close_volume(kernel32, handle) -> None:
    kernel32.CloseHandle(handle)


def _boot(kernel32, handle) -> tuple[int, int, int, int]:
    sector = _read_at(kernel32, handle, 0, 512)
    if sector[3:7] != b"NTFS":
        raise OSError("This drive is not NTFS.")
    bytes_per_sector = struct.unpack_from("<H", sector, 0x0B)[0]
    sectors_per_cluster = sector[0x0D]
    if bytes_per_sector < 512 or sectors_per_cluster == 0:
        raise OSError("The drive layout could not be read.")
    cluster_size = bytes_per_sector * sectors_per_cluster
    mft_cluster = struct.unpack_from("<Q", sector, 0x30)[0]
    raw = struct.unpack_from("<b", sector, 0x40)[0]
    record_size = (1 << (-raw)) if raw < 0 else raw * cluster_size
    if record_size < 512 or record_size > 4096:
        raise OSError("The file-record size on this drive is not supported.")
    return bytes_per_sector, cluster_size, mft_cluster, record_size


def _record_at(kernel32, handle, offset: int, record_size: int, sector_size: int) -> dict | None:
    blob = _read_at(kernel32, handle, offset, record_size)
    if len(blob) < record_size:
        return None
    return parse_file_record(blob, sector_size)


def _read_runs(kernel32, handle, runs, cluster_size: int, size: int, cancel, limit: int | None = None) -> bytes:
    remaining = size if limit is None else min(size, limit)
    out = bytearray()
    for length, lcn in runs:
        if cancel is not None and cancel.is_set():
            break
        if length <= 0:
            continue
        if lcn is None:
            take = min(remaining, length * cluster_size)
            out += b"\x00" * take
            remaining -= take
        else:
            start = lcn * cluster_size
            left = length * cluster_size
            while left > 0 and remaining > 0:
                chunk = min(1024 * 1024, left, remaining)
                out += _read_at(kernel32, handle, start, chunk)
                start += chunk
                left -= chunk
                remaining -= chunk
        if remaining <= 0:
            break
    return bytes(out[: size if limit is None else min(size, limit)])


def scan_deleted(
    drives: list[str],
    folder: str,
    name_query: str,
    extensions: str,
    start: float | None,
    end: float | None,
    include_bin: bool,
    include_disk: bool,
    cancel,
    progress,
) -> RecoverScan:
    result = RecoverScan(files=[], notes=[])
    suffixes = parse_extensions(extensions)
    if include_bin:
        progress("Reading the Recycle Bin…")
        for data_path, info_path, size, when, original in list_recycle_bin():
            if cancel.is_set():
                result.partial = True
                break
            drive = original[:2] if len(original) >= 2 and original[1] == ":" else ""
            if drives and drive[:1].upper() not in {item[:1].upper() for item in drives}:
                continue
            name = os.path.basename(original) or original
            if not path_under(original, folder) or not name_matches(name, original, name_query, suffixes):
                continue
            if not in_range(when, start, end):
                continue
            result.seen += 1
            if len(result.files) >= MAX_HITS:
                continue
            result.files.append(
                DeletedFile(
                    id=f"recycle|{info_path}",
                    name=name,
                    original_path=original,
                    size=size,
                    when=when,
                    source="recycle",
                    chance="In Recycle Bin",
                    drive=drive[:1].upper(),
                    note="Still in the Recycle Bin. It can be put back or copied to a folder.",
                    recycle_data=data_path,
                    recycle_info=info_path,
                )
            )
    if include_disk and not cancel.is_set():
        if not is_admin():
            result.notes.append("Deleted files that are no longer in the Recycle Bin need administrator. Restart as administrator to scan the drives.")
        else:
            for letter in drives:
                if cancel.is_set():
                    result.partial = True
                    break
                if _filesystem(letter) != "NTFS":
                    result.notes.append(f"{letter}: is not NTFS, so only its Recycle Bin can be searched.")
                    continue
                progress(f"Reading deleted files on {letter}: …")
                try:
                    found, partial = _scan_ntfs(letter, folder, name_query, suffixes, start, end, cancel, progress, len(result.files))
                except (OSError, PermissionError) as exc:
                    result.notes.append(str(exc))
                    continue
                result.files.extend(found)
                result.seen += len(found)
                result.partial = result.partial or partial
    if len(result.files) >= MAX_HITS:
        result.notes.append(f"The list stops at {MAX_HITS} files. Narrow the dates, folder, or file type to see a shorter list.")
        result.partial = True
    if result.partial and cancel.is_set():
        result.notes.append("The scan was stopped early.")
    result.files.sort(key=lambda item: item.when, reverse=True)
    return result


def _scan_ntfs(letter, folder, name_query, suffixes, start, end, cancel, progress, already: int):
    kernel32, handle = _open_volume(letter)
    found = []
    partial = False
    try:
        sector_size, cluster_size, mft_cluster, record_size = _boot(kernel32, handle)
        base = _record_at(kernel32, handle, mft_cluster * cluster_size, record_size, sector_size)
        if not base or not base.get("runs"):
            raise OSError(f"{letter}: the file table could not be read.")
        bitmap = _load_bitmap(kernel32, handle, cluster_size, record_size, sector_size, base["runs"])
        directories: dict[int, tuple[int, str]] = {}
        pending = []
        seen_records = 0
        logical = 0
        for length, lcn in base["runs"]:
            if cancel.is_set():
                partial = True
                break
            span = length * cluster_size
            if lcn is None:
                logical += span
                continue
            position = 0
            while position < span:
                if cancel.is_set():
                    partial = True
                    break
                chunk = min(1024 * 1024, span - position)
                blob = _read_at(kernel32, handle, lcn * cluster_size + position, chunk)
                for offset in range(0, len(blob) - record_size + 1, record_size):
                    seen_records += 1
                    if seen_records % 50000 == 0:
                        progress(f"Reading deleted files on {letter}: … {seen_records:,} records")
                    # Most records are files still in use. Their flags sit in the
                    # first sector, which the fixup leaves alone, so they are
                    # skipped before the costly full parse. Folders are kept for paths.
                    if blob[offset : offset + 4] != b"FILE":
                        continue
                    flags = blob[offset + 0x16]
                    if flags & 0x01 and not flags & 0x02:
                        continue
                    index = (logical + position + offset) // record_size
                    parsed = parse_file_record(blob[offset : offset + record_size], sector_size)
                    if not parsed or parsed["extension"]:
                        continue
                    if parsed["is_dir"]:
                        directories[index] = (parsed["parent"], parsed["name"])
                        continue
                    if parsed["in_use"] or parsed["name"].startswith("$") or index < 24:
                        continue
                    pending.append((index, parsed))
                position += chunk
                if partial:
                    break
            logical += span
        room = MAX_HITS - already
        for index, parsed in pending:
            if len(found) >= room:
                partial = True
                break
            original = f"{letter}:\\" + build_path(parsed["parent"], parsed["name"], directories)
            if not path_under(original, folder) or not name_matches(parsed["name"], original, name_query, suffixes):
                continue
            when = parsed["changed"] or parsed["modified"]
            if not in_range(when, start, end):
                continue
            chance = recovery_chance(parsed, bitmap)
            resident = parsed["resident"] if parsed["resident"] is not None and len(parsed["resident"]) <= 256 * 1024 else None
            found.append(
                DeletedFile(
                    id=f"disk|{letter}|{index}",
                    name=parsed["name"],
                    original_path=original,
                    size=int(parsed["size"]),
                    when=when,
                    source="disk",
                    chance=chance,
                    drive=letter.upper(),
                    note=_disk_note(chance),
                    resident=resident,
                    runs=[] if resident is not None else parsed["runs"],
                    cluster_size=cluster_size,
                )
            )
        return found, partial
    finally:
        _close_volume(kernel32, handle)


def _disk_note(chance: str) -> str:
    if chance == "Good":
        return "The file record still holds the contents, or the old disk space still looks unused."
    if chance == "Poor":
        return "Some of the old disk space has been reused. The restored copy may be incomplete."
    if chance == "Encrypted":
        return "Windows encrypted this file. The copy would still be encrypted, so it is not restored."
    if chance == "Compressed":
        return "Windows stored this file compressed. The name is listed, but the contents are not unpacked."
    if chance == "Name only":
        return "Only the name remains. There is no file content to restore."
    return "The old disk space could not be checked, so the copy might be incomplete."


def _load_bitmap(kernel32, handle, cluster_size, record_size, sector_size, mft_runs) -> bytes | None:
    record = None
    logical = 0
    target = 6 * record_size
    for length, lcn in mft_runs:
        span = length * cluster_size
        if lcn is not None and logical <= target < logical + span:
            record = _record_at(kernel32, handle, lcn * cluster_size + (target - logical), record_size, sector_size)
            break
        logical += span
    if not record or record.get("name") != "$Bitmap" or not record.get("runs"):
        return None
    size = min(int(record["size"]), BITMAP_CAP)
    if int(record["size"]) > BITMAP_CAP:
        return None
    try:
        return _read_runs(kernel32, handle, record["runs"], cluster_size, size, None)
    except OSError:
        return None


def _unique_path(path: str) -> str:
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    for number in range(2, 1000):
        candidate = f"{stem} ({number}){ext}"
        if not os.path.exists(candidate):
            return candidate
    raise OSError(f"Too many copies of {os.path.basename(path)} already exist.")


def _clear_attributes(path: str) -> None:
    ctypes.windll.kernel32.SetFileAttributesW(path, FILE_ATTRIBUTE_NORMAL)


def _safe_relative(original: str, name: str) -> list[str]:
    text = original
    if len(text) >= 2 and text[1] == ":":
        text = text[2:]
    parts = [part for part in text.replace("/", "\\").split("\\") if part and part not in {".", ".."}]
    if not parts:
        parts = [name or "recovered file"]
    return parts


def restore_files(items: list[DeletedFile], destination: str, put_back: bool, cancel, progress) -> tuple[int, int, list[str]]:
    restored = 0
    skipped = 0
    errors: list[str] = []
    folder = os.path.abspath(destination) if destination else ""
    if folder:
        if is_system_location(folder):
            raise PermissionError("Choose a folder outside Windows and Program Files.")
        os.makedirs(folder, exist_ok=True)
    if folder and any(item.checked and item.source == "disk" and same_drive(folder, item.drive) for item in items):
        drives = sorted({item.drive for item in items if item.checked and item.source == "disk"})
        raise PermissionError(
            f"Choose a folder on a different drive from {', '.join(letter + ':' for letter in drives)}. "
            "Writing to the same drive can overwrite the deleted files before they are copied."
        )
    for item in items:
        if cancel.is_set():
            errors.append("Stopped early.")
            break
        if not item.checked:
            continue
        progress(f"Restoring {item.name}…")
        try:
            if item.source == "recycle" and put_back:
                if is_system_location(item.original_path):
                    raise PermissionError("That original location is a Windows folder. Save it to a folder instead.")
                _restore_recycle(item, None)
            elif item.source == "recycle":
                _restore_recycle(item, folder)
            else:
                if not folder:
                    raise PermissionError("Choose a folder for files that are no longer in the Recycle Bin.")
                _restore_disk(item, folder, cancel)
            restored += 1
        except (OSError, PermissionError) as exc:
            skipped += 1
            errors.append(f"{item.name}: {exc}")
    return restored, skipped, errors


def same_drive(folder: str, letter: str) -> bool:
    drive = os.path.splitdrive(os.path.abspath(folder))[0]
    return bool(letter) and drive[:1].upper() == letter[:1].upper()


def _restore_recycle(item: DeletedFile, folder: str | None) -> None:
    if not os.path.exists(item.recycle_data):
        raise OSError("The Recycle Bin copy is already gone.")
    if folder:
        parts = _safe_relative(item.original_path, item.name)
        target = os.path.join(folder, *parts)
    else:
        target = item.original_path
        if not target or len(target) < 3 or target[1] != ":":
            raise OSError("The original location is missing. Save it to a folder instead.")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    target = _unique_path(target)
    _clear_attributes(item.recycle_data)
    # shutil.move renames on the same drive and copies when the folder is on another one.
    shutil.move(item.recycle_data, target)
    if item.recycle_info and os.path.isfile(item.recycle_info):
        _clear_attributes(item.recycle_info)
        os.remove(item.recycle_info)


def _restore_disk(item: DeletedFile, folder: str, cancel) -> None:
    if item.chance in {"Encrypted", "Compressed", "Name only"}:
        raise OSError(item.note)
    if same_drive(folder, item.drive):
        raise PermissionError("Choose a folder on a different drive, so the deleted data is not overwritten.")
    parts = _safe_relative(item.original_path, item.name)
    target = _unique_path(os.path.join(folder, *parts))
    os.makedirs(os.path.dirname(target), exist_ok=True)
    try:
        free = shutil.disk_usage(os.path.dirname(target)).free
    except OSError:
        free = None
    if free is not None and item.size > free:
        raise OSError("There is not enough free space in that folder.")
    if item.resident is not None:
        with open(target, "wb") as handle:
            handle.write(item.resident)
        return
    if not item.runs:
        raise OSError("No file content was found.")
    kernel32, handle = _open_volume(item.drive)
    partial = target + ".partial"
    try:
        remaining = item.size
        with open(partial, "wb") as out:
            for length, lcn in item.runs:
                if cancel.is_set():
                    raise OSError("Stopped early.")
                if length <= 0:
                    continue
                if lcn is None:
                    take = min(remaining, length * item.cluster_size)
                    out.write(b"\x00" * take)
                    remaining -= take
                    continue
                start = lcn * item.cluster_size
                left = length * item.cluster_size
                while left > 0 and remaining > 0:
                    chunk = min(1024 * 1024, left, remaining)
                    out.write(_read_at(kernel32, handle, start, chunk))
                    start += chunk
                    left -= chunk
                    remaining -= chunk
                if remaining <= 0:
                    break
        os.replace(partial, target)
    except Exception:
        if os.path.isfile(partial):
            os.remove(partial)
        raise
    finally:
        _close_volume(kernel32, handle)
