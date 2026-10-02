"""Map every internal drive: folder sizes, large files, and installed apps."""

from __future__ import annotations

import heapq
import os
import queue
import shutil
import stat
import threading
import time
import winreg

from cleanup_app.models import BigFile, DriveMap, FolderNode, InstalledApp, SpaceReport, SpaceSlice
from cleanup_app.util import GB, MB, format_size

BIG_KEEP = 20
BIG_FLOOR = 20 * MB
MIN_FOLDER = MB
WORKERS = 4
UNINSTALL_KEYS = (
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
)
FRIENDLY = {
    "windows": "Windows",
    "program files": "Program Files",
    "program files (x86)": "Program Files (x86)",
    "users": "Users",
    "programdata": "ProgramData",
    "windows.old": "Previous Windows (Windows.old)",
    "recovery": "Recovery",
    "$recycle.bin": "Recycle Bin",
    "files in the drive root": "Files in the drive root",
}


def windows_drive() -> str:
    windir = os.environ.get("WINDIR", r"C:\Windows")
    letter = os.path.splitdrive(os.path.abspath(windir))[0]
    return (letter or "C:") + "\\"


def collapse_slices(slices: list[SpaceSlice], keep: int = 7) -> list[SpaceSlice]:
    special = [item for item in slices if item.kind != "folder"]
    folders = sorted((item for item in slices if item.kind == "folder" and item.size > 0), key=lambda item: item.size, reverse=True)
    if len(folders) > keep:
        rest = folders[keep:]
        folders = folders[:keep]
        folders.append(SpaceSlice("Other folders", sum(item.size for item in rest), "", "folder"))
    return folders + [item for item in special if item.size > 0]


def list_installed_apps() -> tuple[list[InstalledApp], int]:
    found: dict[str, InstalledApp] = {}
    unknown = 0
    for hive, key_path in UNINSTALL_KEYS:
        try:
            root = winreg.OpenKey(hive, key_path)
        except OSError:
            continue
        try:
            count = winreg.QueryInfoKey(root)[0]
            for index in range(count):
                try:
                    name = winreg.EnumKey(root, index)
                    sub = winreg.OpenKey(root, name)
                except OSError:
                    continue
                try:
                    app = _read_uninstall(sub)
                finally:
                    winreg.CloseKey(sub)
                if app is None:
                    continue
                if app.size <= 0:
                    unknown += 1
                    continue
                key = app.name.casefold()
                current = found.get(key)
                if current is None or app.size > current.size:
                    found[key] = app
        finally:
            winreg.CloseKey(root)
    apps = sorted(found.values(), key=lambda item: item.size, reverse=True)
    return apps[:20], unknown


def fixed_drive_roots() -> list[str]:
    from cleanup_app.diskinfo import DRIVE_FIXED, drive_letters, drive_type

    roots = [f"{letter}:\\" for letter in drive_letters() if drive_type(letter) == DRIVE_FIXED]
    return roots or [windows_drive()]


def scan_space(cancel, progress, root: str | None = None) -> SpaceReport:
    targets = [_as_root(root)] if root else fixed_drive_roots()
    known = [] if root else _known_folder_index()
    users: dict[str, list] = {}
    heap: list[tuple[int, str]] = []
    drives: list[DriveMap] = []
    notes: list[tuple[str, str, str]] = []
    measured = 0
    seen = 0
    partial = False
    for target in targets:
        if cancel.is_set():
            partial = True
            break
        progress(f"Reading {target}")
        folders, flags, drive_measured, drive_seen, drive_partial, tree = _walk(target, cancel, progress, known, users, heap)
        partial = partial or drive_partial
        measured += drive_measured
        seen += drive_seen
        drive, drive_notes = _drive_summary(target, folders, flags, drive_measured, drive_seen, tree)
        drives.append(drive)
        notes.extend(drive_notes)
    if partial:
        notes.append(("Scan stopped early", "The sizes above are incomplete because the scan was stopped.", "warn"))
    progress("Reading installed apps…")
    if cancel.is_set() or root:
        apps, unknown_apps = [], 0
    else:
        try:
            apps, unknown_apps = list_installed_apps()
        except OSError:
            apps, unknown_apps = [], 0
            notes.append(("Installed apps", "The list of installed programs could not be read.", "info"))
    if unknown_apps:
        notes.append(
            (
                "Programs without a size",
                f"{unknown_apps} installed programs did not tell Windows how big they are, so they are left out of Biggest installed apps. Their files are still counted in the folder breakdown.",
                "info",
            )
        )
    user_folders = [
        SpaceSlice(name, info[0], info[1])
        for name, info in sorted(users.items(), key=lambda item: item[1][0], reverse=True)[:8]
        if info[0] > 0
    ]
    files = [BigFile(path, size) for size, path in sorted(heap, reverse=True)]
    return SpaceReport(
        drives=drives,
        user_folders=user_folders,
        apps=apps,
        files=files,
        notes=[detail for _title, detail, _level in notes],
        measured=measured,
        files_seen=seen,
        unknown_apps=unknown_apps,
        partial=partial,
        explanations=notes,
    )


def _walk(root: str, cancel, progress, known: list[tuple[str, str, str]], users: dict[str, list], heap: list[tuple[int, str]]):
    buckets = _Buckets(known, os.environ.get("USERPROFILE", "") or "")
    start = root if _is_drive_root(root) else root.rstrip("\\")
    drive = _Dir(_label(os.path.basename(start.rstrip("\\")) or start[:2]), start, 0, buckets.for_root(start))
    work: queue.Queue = queue.Queue()
    work.put(drive)
    stats = _WalkStats()
    lock = threading.Lock()
    last_tick = [0.0]

    def tick(path: str) -> None:
        now = time.monotonic()
        if now - last_tick[0] < 0.35:
            return
        last_tick[0] = now
        progress(f"Reading {path}")

    def worker() -> None:
        # Each worker walks its own subtree depth-first and only hands folders
        # to the shared queue while other workers are short of work. Sending
        # every folder through the queue costs more than reading it.
        while True:
            directory = work.get()
            try:
                if directory is None:
                    return
                local = [directory]
                while local:
                    if cancel.is_set():
                        stats.partial = True
                        break
                    children = _scan_level(local.pop(), buckets, stats, lock, tick)
                    if len(children) > 1 and work.qsize() < WORKERS:
                        for child in children[1:]:
                            work.put(child)
                        local.append(children[0])
                    else:
                        local.extend(children)
            except Exception:
                stats.partial = True
            finally:
                work.task_done()

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(WORKERS)]
    for thread in threads:
        thread.start()
    work.join()
    for _ in threads:
        work.put(None)
    for thread in threads:
        thread.join(timeout=2)
    tree = _freeze(drive)
    folders = {child.name: child.size for child in tree.children}
    flags = {"hiber": stats.hiber, "page": stats.page, "old": stats.old}
    with lock:
        _merge_users(users, stats.users)
        for size, path in stats.heap:
            heapq.heappush(heap, (size, path))
            if len(heap) > BIG_KEEP:
                heapq.heappop(heap)
    return folders, flags, stats.measured, stats.seen, stats.partial or cancel.is_set(), tree


REPARSE = stat.FILE_ATTRIBUTE_REPARSE_POINT
DIRECTORY = stat.FILE_ATTRIBUTE_DIRECTORY
PROFILE_FILES = "Files in your user folder"
_PROFILE = ("", "")


class _Dir:
    __slots__ = ("name", "path", "depth", "file_size", "dirs", "bucket")

    def __init__(self, name: str, path: str, depth: int, bucket: tuple[str, str] | None = None):
        self.name = name
        self.path = path
        self.depth = depth
        self.file_size = 0
        self.dirs: dict[str, _Dir] = {}
        self.bucket = bucket


class _Buckets:
    """Decide which "Your folders" total a folder counts toward.

    Every file in a folder lands in the same bucket, so this runs once per
    folder rather than once per file. A known folder such as Documents wins,
    then the top-level folder under the user profile it sits in.
    """

    def __init__(self, known: list[tuple[str, str, str]], profile: str):
        self.known = {prefix: (label, display) for label, prefix, display in known}
        self.profile_key = os.path.normcase(os.path.abspath(profile)) if profile else ""
        self.profile = os.path.abspath(profile) if profile else ""
        # A path longer than every known folder cannot be one, so most folders skip normcase.
        self.longest = max([len(self.profile_key), *(len(prefix) for prefix in self.known)])

    def for_root(self, path: str) -> tuple[str, str] | None:
        """The bucket for where the walk starts, which can be deep inside a known folder."""
        norm = os.path.normcase(os.path.abspath(path))
        for prefix in sorted(self.known, key=len, reverse=True):
            if norm == prefix or norm.startswith(prefix + "\\"):
                return self.known[prefix]
        if self.profile_key and norm == self.profile_key:
            return _PROFILE
        if self.profile_key and norm.startswith(self.profile_key + "\\"):
            top = os.path.abspath(path)[len(self.profile) + 1 :].split("\\")[0]
            return (top, os.path.join(self.profile, top))
        return None

    def for_child(self, parent: tuple[str, str] | None, name: str, path: str) -> tuple[str, str] | None:
        norm = os.path.normcase(path) if len(path) <= self.longest else ""
        if norm:
            hit = self.known.get(norm)
            if hit is not None:
                return hit
        if parent is _PROFILE:
            return (name, path)
        if parent is not None:
            return parent
        if norm and norm == self.profile_key:
            return _PROFILE
        return None

    def label(self, bucket: tuple[str, str]) -> tuple[str, str]:
        if bucket is _PROFILE:
            return PROFILE_FILES, self.profile
        return bucket


class _WalkStats:
    def __init__(self) -> None:
        self.measured = 0
        self.seen = 0
        self.partial = False
        self.hiber = 0
        self.page = 0
        self.old = False
        self.users: dict[str, list] = {}
        self.heap: list[tuple[int, str]] = []


def _scan_level(directory: _Dir, buckets: _Buckets, stats: _WalkStats, lock: threading.Lock, tick) -> list[_Dir]:
    """Read one folder. This loop runs for every file on the drive, so it stays lean."""
    tick(directory.path)
    children: list[_Dir] = []
    big: list[tuple[int, str]] = []
    seen = 0
    measured = 0
    depth = directory.depth
    child_depth = depth + 1
    bucket = directory.bucket
    dirs = directory.dirs
    try:
        iterator = os.scandir(directory.path)
    except OSError:
        return []
    with iterator:
        for entry in iterator:
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            attributes = info.st_file_attributes
            if attributes & REPARSE:
                continue
            if attributes & DIRECTORY:
                name = entry.name
                path = entry.path
                child = _Dir(name, path, child_depth, buckets.for_child(bucket, name, path))
                dirs[name] = child
                children.append(child)
                continue
            size = info.st_size
            seen += 1
            measured += size
            if size >= BIG_FLOOR:
                big.append((size, entry.path))
    directory.file_size = measured
    with lock:
        stats.seen += seen
        stats.measured += measured
        if bucket is not None and measured:
            label, display = buckets.label(bucket)
            total = stats.users.get(label)
            if total is None:
                stats.users[label] = [measured, display]
            else:
                total[0] += measured
        if big:
            stats.heap.extend(big)
            if len(stats.heap) > BIG_KEEP * 4:
                stats.heap = heapq.nlargest(BIG_KEEP, stats.heap)
    if depth == 0:
        _root_flags(directory, stats)
    return children


def _root_flags(directory: _Dir, stats: _WalkStats) -> None:
    """Spot hiberfil.sys, pagefile.sys, and Windows.old in a drive root."""
    for name in directory.dirs:
        if name.lower() == "windows.old":
            stats.old = True
    for name, attr in (("hiberfil.sys", "hiber"), ("pagefile.sys", "page")):
        try:
            info = os.stat(os.path.join(directory.path, name), follow_symlinks=False)
        except OSError:
            continue
        setattr(stats, attr, info.st_size)


def _merge_users(target: dict[str, list], source: dict[str, list]) -> None:
    for name, info in source.items():
        bucket = target.setdefault(name, [0, info[1]])
        bucket[0] += info[0]


def _freeze(directory: _Dir) -> FolderNode:
    """Turn the walk into the folder tree the breakdown window shows.

    Sizes are summed first, so nodes are only built for folders that are kept.
    Folders under MIN_FOLDER are folded into one "Smaller folders" row.
    """
    order: list[_Dir] = []
    stack = [directory]
    while stack:
        current = stack.pop()
        order.append(current)
        stack.extend(current.dirs.values())
    totals: dict[int, int] = {}
    for current in reversed(order):
        totals[id(current)] = current.file_size + sum(totals[id(child)] for child in current.dirs.values())
    del order

    def node_for(current: _Dir) -> FolderNode:
        name = current.name if current.depth > 1 else _label(current.name)
        return FolderNode(name, current.path, totals[id(current)], [])

    root = node_for(directory)
    pending = [(directory, root)]
    while pending:
        current, node = pending.pop()
        kept: list[FolderNode] = []
        small = 0
        for child in current.dirs.values():
            size = totals[id(child)]
            if current.depth == 0 or size >= MIN_FOLDER:
                child_node = node_for(child)
                kept.append(child_node)
                if child.dirs:
                    pending.append((child, child_node))
            else:
                small += size
        if not kept and not small:
            continue
        if current.file_size > 0:
            label = "Files in the drive root" if current.depth == 0 else "Files here"
            kept.append(FolderNode(label, current.path, current.file_size, []))
        if small > 0:
            kept.append(FolderNode("Smaller folders", "", small, []))
        kept.sort(key=_by_size, reverse=True)
        node.children = kept
    return root


def _by_size(node: FolderNode) -> int:
    return node.size


def _as_root(path: str) -> str:
    target = os.path.abspath(path)
    return target if target.endswith("\\") else target + "\\"


def _known_folder_index() -> list[tuple[str, str, str]]:
    from cleanup_app.diskinfo import known_folders

    indexed = []
    for label, path in known_folders():
        absolute = os.path.abspath(path)
        indexed.append((label, os.path.normcase(absolute), absolute))
    indexed.sort(key=lambda item: len(item[1]), reverse=True)
    return indexed


def _drive_summary(target: str, folders: dict[str, int], flags: dict, measured: int, seen: int, tree: FolderNode):
    slices = [
        SpaceSlice(_label(name), size, os.path.join(target, name) if name != "Files in the drive root" else target)
        for name, size in folders.items()
        if size > 0
    ]
    notes: list[tuple[str, str, str]] = []
    letter = target[:2]
    if _is_drive_root(target):
        try:
            usage = shutil.disk_usage(target)
        except OSError:
            usage = None
        if usage is not None:
            unread = usage.used - measured
            if unread > 200 * MB:
                slices.append(SpaceSlice("Not readable from here", unread, "", "unknown"))
                notes.append(
                    (
                        f"Hidden on {letter}",
                        f"About {format_size(unread)} on {letter} is in use, but Windows did not let this scan open it. System Restore and protected system folders are the usual cause. The space is still on the drive.",
                        "info",
                    )
                )
            slices.append(SpaceSlice("Free space", usage.free, target, "free"))
        if flags["old"]:
            notes.append(
                (
                    f"Old Windows on {letter}",
                    f"{letter} still has Windows.old, a previous Windows installation. Remove it from Settings, System, Storage. This app will not delete it.",
                    "warn",
                )
            )
        if flags["hiber"] >= 1 * GB:
            notes.append(
                (
                    f"Hibernation on {letter}",
                    f"hiberfil.sys is {format_size(flags['hiber'])}. Windows uses that file to save memory when the PC sleeps. This app will not delete it. Turn hibernation off in Windows power settings if you do not use sleep.",
                    "info",
                )
            )
        if flags["page"] >= 1 * GB:
            notes.append(
                (
                    f"Page file on {letter}",
                    f"pagefile.sys is {format_size(flags['page'])}. Windows uses it when programs need more memory than the RAM in this PC. Leave it alone.",
                    "info",
                )
            )
    return DriveMap(target, collapse_slices(slices), measured, seen, tree), notes


def _label(name: str) -> str:
    return FRIENDLY.get(name.lower(), name)


def _is_drive_root(path: str) -> bool:
    return len(os.path.abspath(path).rstrip("\\")) <= 2


def _read_uninstall(key) -> InstalledApp | None:
    name = _reg_text(key, "DisplayName")
    if not name or name.lower().startswith(("kb", "update for ", "security update")):
        return None
    if _reg_int(key, "SystemComponent") == 1:
        return None
    if _reg_text(key, "ParentKeyName") or _reg_text(key, "ParentDisplayName"):
        return None
    release = _reg_text(key, "ReleaseType").lower()
    if release in {"update", "hotfix", "security update"}:
        return None
    size_kb = _reg_int(key, "EstimatedSize")
    return InstalledApp(
        name=name,
        publisher=_reg_text(key, "Publisher"),
        size=max(0, size_kb) * 1024,
        location=_reg_text(key, "InstallLocation"),
    )


def _reg_text(key, name: str) -> str:
    try:
        value, kind = winreg.QueryValueEx(key, name)
    except OSError:
        return ""
    if kind == winreg.REG_SZ or kind == winreg.REG_EXPAND_SZ:
        return str(value).strip()
    return ""


def _reg_int(key, name: str) -> int:
    try:
        value, kind = winreg.QueryValueEx(key, name)
    except OSError:
        return 0
    if kind == winreg.REG_DWORD:
        return int(value)
    return 0
