"""Find and remove disposable caches. Personal files are not in here."""

from __future__ import annotations

import fnmatch
import os
import stat
import time
from dataclasses import replace

from cleanup_app.models import CleanReport, JunkCategory
from cleanup_app.recycle import empty_recycle_bin, query_recycle_bin
from cleanup_app.safety import approved_junk_path, is_reparse

FRESH_SECONDS = 120
KEEP_NAMES = {"desktop.ini", "ntuser.dat", "ntuser.dat.log"}
REPARSE = stat.FILE_ATTRIBUTE_REPARSE_POINT
DIRECTORY = stat.FILE_ATTRIBUTE_DIRECTORY


def _dedupe(paths: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for path in paths:
        if not path:
            continue
        absolute = os.path.abspath(os.path.expandvars(path))
        marker = os.path.normcase(absolute)
        if marker in seen:
            continue
        seen.add(marker)
        ordered.append(absolute)
    return ordered


def _exists(path: str) -> bool:
    return os.path.exists(path)


def _chromium_caches(user_data: str) -> list[str]:
    if not os.path.isdir(user_data):
        return []
    found: list[str] = []
    for relative in ("Cache", "Code Cache", "GPUCache", os.path.join("Service Worker", "CacheStorage")):
        candidate = os.path.join(user_data, relative)
        if os.path.isdir(candidate):
            found.append(candidate)
    try:
        names = os.listdir(user_data)
    except OSError:
        return found
    for name in names:
        profile = os.path.join(user_data, name)
        if not os.path.isdir(profile):
            continue
        for relative in (
            "Cache",
            "Code Cache",
            "GPUCache",
            os.path.join("Service Worker", "CacheStorage"),
        ):
            candidate = os.path.join(profile, relative)
            if os.path.isdir(candidate):
                found.append(candidate)
    return found


def _editor_caches(roaming: str) -> list[str]:
    found = []
    for app in ("Code", "Cursor"):
        root = os.path.join(roaming, app)
        for name in ("Cache", "Code Cache", "GPUCache"):
            path = os.path.join(root, name)
            if os.path.isdir(path):
                found.append(path)
    return found


def _epic_caches(local: str) -> list[str]:
    saved = os.path.join(local, r"EpicGamesLauncher\Saved")
    if not os.path.isdir(saved):
        return []
    found = []
    try:
        names = os.listdir(saved)
    except OSError:
        return []
    for name in names:
        if name.lower().startswith("webcache"):
            path = os.path.join(saved, name)
            if os.path.isdir(path):
                found.append(path)
    return found


def _firefox_caches() -> list[str]:
    root = os.path.expandvars(r"%LOCALAPPDATA%\Mozilla\Firefox\Profiles")
    if not os.path.isdir(root):
        return []
    found = []
    try:
        names = os.listdir(root)
    except OSError:
        return []
    for name in names:
        cache = os.path.join(root, name, "cache2")
        if os.path.isdir(cache):
            found.append(cache)
    return found


def _category(**kwargs) -> JunkCategory | None:
    mode = kwargs.get("mode", "tree")
    patterns = kwargs.get("patterns") or []
    special = kwargs.get("special", "")
    paths = []
    for path in kwargs.get("paths") or []:
        if special == "recycle":
            paths.append(path)
            continue
        if not _exists(path):
            continue
        if not approved_junk_path(path, mode, patterns):
            continue
        paths.append(path)
    if not paths and special != "recycle":
        return None
    return JunkCategory(
        id=kwargs["id"],
        name=kwargs["name"],
        group=kwargs["group"],
        safety=kwargs["safety"],
        description=kwargs["description"],
        paths=paths,
        mode=mode,
        patterns=list(patterns),
        special=special,
    )


def discover_categories() -> list[JunkCategory]:
    local = os.environ.get("LOCALAPPDATA", "")
    roaming = os.environ.get("APPDATA", "")
    windir = os.environ.get("WINDIR", r"C:\Windows")
    temp_paths = _dedupe(
        [
            os.environ.get("TEMP", ""),
            os.environ.get("TMP", ""),
            os.path.join(local, "Temp"),
        ]
    )
    specs = [
        dict(
            id="user_temp",
            name="User temporary files",
            group="Safe",
            safety="safe",
            description="Scratch files left behind after installs and updates.",
            paths=temp_paths,
        ),
        dict(
            id="windows_temp",
            name="Windows temporary files",
            group="Safe",
            safety="safe",
            description="The system temp folder. Reading it needs administrator on some PCs.",
            paths=[os.path.join(windir, "Temp")],
        ),
        dict(
            id="thumbnails",
            name="Thumbnail and icon cache",
            group="Safe",
            safety="safe",
            description="Picture previews. Windows rebuilds them. A few may stay locked until you sign out.",
            paths=[os.path.join(local, r"Microsoft\Windows\Explorer")],
            mode="glob",
            patterns=["thumbcache_*.db", "iconcache_*.db"],
        ),
        dict(
            id="inet_cache",
            name="Internet file cache",
            group="Safe",
            safety="safe",
            description="Cached bits of web pages. Sign-ins and passwords are left alone.",
            paths=[os.path.join(local, r"Microsoft\Windows\INetCache")],
        ),
        dict(
            id="wer",
            name="Windows error reports",
            group="Safe",
            safety="safe",
            description="Old crash reports that have already been stored.",
            paths=_dedupe(
                [
                    os.path.join(local, r"Microsoft\Windows\WER"),
                    os.path.join(os.environ.get("PROGRAMDATA", r"C:\ProgramData"), r"Microsoft\Windows\WER"),
                ]
            ),
        ),
        dict(
            id="crashdumps",
            name="App crash dumps",
            group="Safe",
            safety="safe",
            description="Memory dumps from apps that crashed. Useful only while you are debugging.",
            paths=[os.path.join(local, "CrashDumps")],
        ),
        dict(
            id="memory_dmp",
            name="Windows memory dump",
            group="System leftovers",
            safety="review",
            description="A full memory dump from a blue screen. It can be several gigabytes.",
            paths=[os.path.join(windir, "MEMORY.DMP")],
        ),
        dict(
            id="delivery",
            name="Delivery Optimization cache",
            group="System leftovers",
            safety="review",
            description="Copies of Windows Update files used to share downloads.",
            paths=_dedupe(
                [
                    os.path.join(windir, r"SoftwareDistribution\DeliveryOptimization"),
                    os.path.join(
                        windir,
                        r"ServiceProfiles\NetworkService\AppData\Local\Microsoft\Windows\DeliveryOptimization\Cache",
                    ),
                ]
            ),
        ),
        dict(
            id="update_download",
            name="Windows Update downloads",
            group="System leftovers",
            safety="review",
            description="Downloaded updates. Leave this if Windows is installing updates right now.",
            paths=[os.path.join(windir, r"SoftwareDistribution\Download")],
        ),
        dict(
            id="fontcache",
            name="Font cache",
            group="System leftovers",
            safety="review",
            description="Rebuilt the next time Windows draws text. Skip this if fonts look wrong afterwards.",
            paths=[os.path.join(local, r"Microsoft\FontCache")],
        ),
    ]

    browsers = [
        ("chrome", "Google Chrome cache", os.path.join(local, r"Google\Chrome\User Data")),
        ("edge", "Microsoft Edge cache", os.path.join(local, r"Microsoft\Edge\User Data")),
        ("brave", "Brave cache", os.path.join(local, r"BraveSoftware\Brave-Browser\User Data")),
        ("vivaldi", "Vivaldi cache", os.path.join(local, r"Vivaldi\User Data")),
        ("opera", "Opera cache", os.path.join(local, r"Opera Software\Opera Stable")),
    ]
    for ident, title, root in browsers:
        specs.append(
            dict(
                id=ident,
                name=title,
                group="App caches",
                safety="review",
                description="Page cache only. History, cookies, and saved passwords stay.",
                paths=_chromium_caches(root),
            )
        )
    specs.append(
        dict(
            id="firefox",
            name="Firefox cache",
            group="App caches",
            safety="review",
            description="Page cache only. Bookmarks, history, and passwords stay.",
            paths=_firefox_caches(),
        )
    )
    for ident, title, relatives in (
        ("discord", "Discord cache", [r"discord\Cache", r"discord\Code Cache", r"discord\GPUCache"]),
        ("slack", "Slack cache", [r"Slack\Cache", r"Slack\Code Cache", r"Slack\GPUCache"]),
        ("teams", "Teams cache", [r"Microsoft\Teams\Cache", r"Microsoft\Teams\Code Cache"]),
    ):
        paths = [os.path.join(roaming, relative) for relative in relatives]
        specs.append(
            dict(
                id=ident,
                name=title,
                group="App caches",
                safety="review",
                description="The app rebuilds this the next time it opens.",
                paths=paths,
            )
        )
    specs.append(
        dict(
            id="steam_local",
            name="Steam HTML cache",
            group="App caches",
            safety="review",
            description="The Steam window cache. Your games stay installed.",
            paths=[os.path.join(local, r"Steam\htmlcache")],
        )
    )
    specs.append(
        dict(
            id="editors",
            name="Code editor caches",
            group="App caches",
            safety="review",
            description="Visual Studio Code and Cursor window caches. Extensions and projects stay.",
            paths=_editor_caches(roaming),
        )
    )
    specs.append(
        dict(
            id="epic",
            name="Epic Games launcher cache",
            group="App caches",
            safety="review",
            description="The launcher's web cache. Installed games stay.",
            paths=_epic_caches(local),
        )
    )
    specs.append(
        dict(
            id="battlenet",
            name="Battle.net cache",
            group="App caches",
            safety="review",
            description="The Battle.net window cache. Installed games stay.",
            paths=[os.path.join(roaming, r"Battle.net\Cache")],
        )
    )
    specs.append(
        dict(
            id="gpu",
            name="GPU shader cache",
            group="GPU caches",
            safety="review",
            description="The next launch of a game may stutter once while shaders rebuild.",
            paths=_dedupe(
                [
                    os.path.join(local, "D3DSCache"),
                    os.path.join(local, r"NVIDIA\DXCache"),
                    os.path.join(local, r"NVIDIA\GLCache"),
                    os.path.join(local, r"AMD\DxCache"),
                    os.path.join(local, r"AMD\DxcCache"),
                ]
            ),
        )
    )
    specs.append(
        dict(
            id="pip",
            name="Python pip cache",
            group="Developer caches",
            safety="review",
            description="Downloaded Python packages. pip will download them again when needed.",
            paths=[os.path.join(local, r"pip\Cache")],
        )
    )
    specs.append(
        dict(
            id="npm",
            name="npm cache",
            group="Developer caches",
            safety="review",
            description="Cached JavaScript packages. npm will download them again when needed.",
            paths=[os.path.join(local, "npm-cache")],
        )
    )
    specs.append(
        dict(
            id="recycle",
            name="Recycle Bin",
            group="Safe",
            safety="safe",
            description="Files you already deleted. Emptying this removes them for good.",
            paths=[],
            special="recycle",
        )
    )

    categories = []
    for spec in specs:
        category = _category(**spec)
        if category is not None:
            categories.append(category)
    return categories


def _matches(name: str, patterns: list[str]) -> bool:
    lowered = name.lower()
    return any(fnmatch.fnmatch(lowered, pattern.lower()) for pattern in patterns)


def iter_junk_entries(category: JunkCategory, cancel):
    """Yield (path, size) for each file in the category that is old enough to remove.

    Sizes and dates come from the folder listing itself, so no file is opened twice.
    Links and junctions are never followed.
    """
    fresh_cutoff = time.time() - FRESH_SECONDS
    for root in category.paths:
        if cancel.is_set():
            return
        try:
            info = os.stat(root)
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode):
            if info.st_mtime <= fresh_cutoff:
                yield root, info.st_size
            continue
        if not stat.S_ISDIR(info.st_mode):
            continue
        if category.mode == "glob":
            try:
                with os.scandir(root) as scanner:
                    for entry in scanner:
                        if cancel.is_set():
                            return
                        try:
                            entry_info = entry.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        if entry_info.st_file_attributes & (DIRECTORY | REPARSE):
                            continue
                        if entry.name.lower() in KEEP_NAMES:
                            continue
                        if _matches(entry.name, category.patterns) and entry_info.st_mtime <= fresh_cutoff:
                            yield entry.path, entry_info.st_size
            except OSError:
                continue
            continue
        stack = [root]
        while stack:
            if cancel.is_set():
                return
            current = stack.pop()
            try:
                with os.scandir(current) as scanner:
                    for entry in scanner:
                        try:
                            entry_info = entry.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        attributes = entry_info.st_file_attributes
                        if attributes & REPARSE:
                            continue
                        if attributes & DIRECTORY:
                            stack.append(entry.path)
                        elif entry_info.st_mtime <= fresh_cutoff and entry.name.lower() not in KEEP_NAMES:
                            yield entry.path, entry_info.st_size
            except OSError:
                continue


def iter_junk_files(category: JunkCategory, cancel):
    for path, _size in iter_junk_entries(category, cancel):
        yield path


def measure_tree(path: str, cancel, progress=None) -> tuple[int, int, int]:
    category = JunkCategory(
        id="measure",
        name="measure",
        group="Safe",
        safety="safe",
        description="",
        paths=[path],
        mode="tree",
    )
    if not approved_junk_path(path, "tree", []):
        return 0, 0, 0
    return _measure(category, cancel, progress)


def _measure(category: JunkCategory, cancel, progress=None) -> tuple[int, int, int]:
    total = 0
    count = 0
    for _path, size in iter_junk_entries(category, cancel):
        total += size
        count += 1
        if progress and count % 2000 == 0:
            progress(f"Checking {category.name}… {count} files")
    return total, count, 0


def can_read(path: str) -> bool:
    try:
        with os.scandir(path) as scanner:
            next(scanner, None)
        return True
    except PermissionError:
        return False
    except FileNotFoundError:
        return False
    except OSError:
        return True


def scan_junk(cancel, progress) -> list[JunkCategory]:
    categories = discover_categories()
    for category in categories:
        if cancel.is_set():
            break
        progress(f"Checking {category.name}…")
        if category.special == "recycle":
            category.size, category.count = query_recycle_bin()
            category.checked = category.size > 0
            continue
        readable = False
        for path in category.paths:
            if os.path.isdir(path) and can_read(path):
                readable = True
            elif os.path.isfile(path):
                readable = True
        if category.paths and not readable:
            category.note = "Needs administrator to read this folder."
            continue
        size, count, skipped = _measure(category, cancel, progress)
        category.size = size
        category.count = count
        category.skipped = skipped
        category.checked = category.safety == "safe" and category.size > 0
    return categories


def _remove_file(path: str) -> None:
    try:
        os.remove(path)
    except PermissionError:
        os.chmod(path, stat.S_IWRITE)
        os.remove(path)


def _remove_empty_dirs(roots: list[str]) -> None:
    for root in roots:
        if not os.path.isdir(root):
            continue
        for current, dirnames, _filenames in os.walk(root, topdown=False):
            if os.path.normcase(current) == os.path.normcase(root):
                continue
            if is_reparse(current):
                continue
            dirnames[:] = [name for name in dirnames if not is_reparse(os.path.join(current, name))]
            try:
                os.rmdir(current)
            except OSError:
                pass


def clean_categories(categories: list[JunkCategory], cancel, progress) -> CleanReport:
    report = CleanReport()
    for category in categories:
        if cancel.is_set():
            report.notes.append("Stopped early. Everything after this point was left in place.")
            break
        if not category.checked:
            continue
        progress(f"Cleaning {category.name}…")
        if category.special == "recycle":
            before_size, before_count = query_recycle_bin()
            if before_count == 0 and before_size == 0:
                continue
            if empty_recycle_bin():
                report.deleted_bytes += before_size
                report.deleted_files += before_count
                report.notes.append(f"Emptied the Recycle Bin ({before_size} bytes scanned).")
            else:
                report.errors.append("Could not empty the Recycle Bin.")
            continue
        approved = []
        for path in category.paths:
            if approved_junk_path(path, category.mode, category.patterns):
                approved.append(os.path.abspath(path))
            else:
                report.errors.append(f"Skipped an unapproved folder: {path}")
        if not approved:
            continue
        allowed = replace(category, paths=approved)
        prefixes = [os.path.normcase(path).rstrip("\\") for path in approved]
        removed_here = 0
        for path, size in list(iter_junk_entries(allowed, cancel)):
            if cancel.is_set():
                break
            norm = os.path.normcase(path)
            if not any(norm == prefix or norm.startswith(prefix + "\\") for prefix in prefixes):
                report.skipped += 1
                continue
            try:
                _remove_file(path)
            except OSError:
                report.skipped += 1
                continue
            report.deleted_bytes += size
            report.deleted_files += 1
            removed_here += 1
        if category.mode == "tree":
            _remove_empty_dirs(approved)
        report.notes.append(f"{category.name}: removed {removed_here} files.")
    return report
