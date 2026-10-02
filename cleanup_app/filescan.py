"""Score old and large files so the tick list starts with likely leftovers."""

from __future__ import annotations

import heapq
import os
import stat
import time

from cleanup_app.models import FileHit, FileScanResult
from cleanup_app.safety import SKIP_DIR_NAMES, is_inside, is_reparse, system_prefixes
from cleanup_app.util import MB

CAP = 4000
PERSONAL_EXTENSIONS = {
    ".doc",
    ".docx",
    ".pdf",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".txt",
    ".rtf",
    ".odt",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".bmp",
    ".heic",
    ".raw",
    ".cr2",
    ".nef",
    ".mp4",
    ".mkv",
    ".avi",
    ".mov",
    ".wmv",
    ".mp3",
    ".wav",
    ".flac",
}
DUMP_EXTENSIONS = {".dmp", ".log", ".tmp", ".temp", ".bak", ".old", ".chk"}
ARCHIVE_EXTENSIONS = {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".cab"}
INSTALLER_EXTENSIONS = {".iso", ".img", ".msi", ".exe", ".msix", ".appx"}
CLOUD_ATTRIBUTES = 0x40000 | 0x400000
REPARSE = stat.FILE_ATTRIBUTE_REPARSE_POINT
DIRECTORY = stat.FILE_ATTRIBUTE_DIRECTORY


def _verdict(score: int) -> str:
    if score >= 75:
        return "Likely unused"
    if score >= 55:
        return "Probably unused"
    if score >= 35:
        return "Review"
    return "Likely keep"


def analyze_file(
    path: str,
    size: int,
    accessed: float,
    modified: float,
    now: float,
    cloud: bool = False,
) -> FileHit:
    ext = os.path.splitext(path)[1].lower()
    name = os.path.basename(path).lower()
    parent = path.lower()
    write_days = (now - modified) / 86400
    reasons: list[str] = []
    score = 0

    if write_days >= 365:
        score += 40
        reasons.append(f"Not modified in {int(write_days)} days")
    elif write_days >= 180:
        score += 35
        reasons.append(f"Not modified in {int(write_days)} days")
    elif write_days >= 90:
        score += 20
        reasons.append(f"Last modified {int(write_days)} days ago")
    elif write_days >= 30:
        score += 8
        reasons.append(f"Last modified {int(write_days)} days ago")
    else:
        score -= 30
        reasons.append("Modified recently")

    if accessed > modified + 86400:
        opened = int((now - accessed) / 86400)
        if opened >= 180:
            score += 8
            reasons.append(f"Last opened about {opened} days ago")

    if size >= 5 * MB * 1024:
        score += 15
        reasons.append("Very large file")
    elif size >= 1024 * MB:
        score += 10
        reasons.append("Large file")
    elif size >= 500 * MB:
        score += 5

    installer_name = any(word in name for word in ("setup", "install", "installer"))
    if installer_name:
        score += 20
        reasons.append("The name looks like an installer")
    if ext in INSTALLER_EXTENSIONS and "download" in parent:
        score += 12
        reasons.append("Installer sitting in a downloads folder")
    if ext in {".iso", ".img"}:
        score += 12
        reasons.append("Disk image, often kept after an install")
    if ext in ARCHIVE_EXTENSIONS:
        score += 8
        reasons.append("Archive, often left behind after it was extracted")
    if ext in DUMP_EXTENSIONS:
        score += 25
        reasons.append("Log, dump, or backup leftover")
    if "copy" in name or " (" in name:
        score += 8
        reasons.append("The name looks like a duplicate copy")
    if "downloads" in parent:
        score += 8

    if cloud or "onedrive" in parent:
        score = min(score, 45)
        reasons.append("Stored in OneDrive. Deleting it deletes the cloud copy too.")
    if "\\appdata\\" in parent:
        score = min(score, 40)
        reasons.append("Inside AppData. Apps often still need files that look old.")
    if ext in PERSONAL_EXTENSIONS:
        score = min(score, 50)
        reasons.append("Personal file. The date alone does not mean you want it gone.")
    if ext in {".dll", ".sys", ".drv"}:
        score = 0
        reasons.append("Program component. Leave it installed.")

    score = max(0, min(100, score))
    leaf_ok = (
        ext in DUMP_EXTENSIONS
        or ext in {".iso", ".img", ".msi", ".cab", ".msix", ".appx"}
        or (ext in {".exe", ".zip", ".rar", ".7z"} and installer_name)
        or (ext in ARCHIVE_EXTENSIONS and "download" in parent)
    )
    precheck = (
        score >= 75
        and leaf_ok
        and "onedrive" not in parent
        and "\\appdata\\" not in parent
        and ext not in PERSONAL_EXTENSIONS
    )
    return FileHit(
        path=path,
        size=size,
        modified=modified,
        accessed=accessed,
        score=score,
        verdict=_verdict(score),
        reasons=reasons,
        precheck=precheck,
        root="",
        checked=precheck,
    )


def should_skip_dir(path: str, roots: list[str], include_appdata: bool) -> bool:
    if is_reparse(path):
        return True
    return _SkipRules(roots, include_appdata).skip(os.path.abspath(path))


class _SkipRules:
    """Folder rules for the walk, worked out once instead of for every folder."""

    def __init__(self, roots: list[str], include_appdata: bool):
        self.roots = {os.path.normcase(os.path.abspath(root)) for root in roots}
        self.system = [os.path.normcase(prefix).rstrip("\\") for prefix in system_prefixes()]
        self.skip_appdata = not include_appdata and not any("\appdata" in root for root in self.roots)

    def is_system(self, norm: str) -> bool:
        return any(norm == prefix or norm.startswith(prefix + "\\") for prefix in self.system)

    def skip(self, path: str) -> bool:
        norm = os.path.normcase(path)
        if norm in self.roots:
            return False
        if self.is_system(norm):
            return True
        base = os.path.basename(norm)
        return base in SKIP_DIR_NAMES or (self.skip_appdata and base == "appdata")


def _root_for(path: str, roots: list[str]) -> str:
    matches = [root for root in roots if is_inside(path, root)]
    if not matches:
        return roots[0] if roots else ""
    return max(matches, key=lambda item: len(os.path.abspath(item)))


def scan_files(
    roots: list[str],
    min_bytes: int,
    min_idle_days: int | None,
    include_appdata: bool,
    cancel,
    progress,
) -> FileScanResult:
    started = time.time()
    now = started
    heap: list[tuple[int, int, FileHit]] = []
    matched = 0
    folders = 0
    counter = 0
    rules = _SkipRules(roots, include_appdata)
    idle_cutoff = None if min_idle_days is None else now - min_idle_days * 86400
    stack = [os.path.abspath(root) for root in roots if os.path.isdir(root)]
    seen_dirs: set[str] = set()
    last_progress = 0.0

    while stack:
        if cancel.is_set():
            break
        current = stack.pop()
        marker = os.path.normcase(current)
        if marker in seen_dirs:
            continue
        seen_dirs.add(marker)
        folders += 1
        if folders % 64 == 0:
            moment = time.monotonic()
            if moment - last_progress >= 0.25:
                last_progress = moment
                progress(f"Reading {current}")
        try:
            iterator = os.scandir(current)
        except OSError:
            continue
        with iterator:
            for entry in iterator:
                # scandir already knows each entry's attributes, size, and dates,
                # so nothing here goes back to the disk per file.
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                attributes = info.st_file_attributes
                if attributes & REPARSE:
                    continue
                if attributes & DIRECTORY:
                    if not rules.skip(entry.path):
                        stack.append(entry.path)
                    continue
                if info.st_size < min_bytes:
                    continue
                if idle_cutoff is not None and max(info.st_atime, info.st_mtime) > idle_cutoff:
                    continue
                if rules.is_system(os.path.normcase(entry.path)):
                    continue
                hit = analyze_file(
                    entry.path,
                    info.st_size,
                    info.st_atime,
                    info.st_mtime,
                    now,
                    cloud=bool(attributes & CLOUD_ATTRIBUTES),
                )
                hit.root = _root_for(entry.path, roots)
                matched += 1
                counter += 1
                item = (hit.size, counter, hit)
                if len(heap) < CAP:
                    heapq.heappush(heap, item)
                elif hit.size > heap[0][0]:
                    heapq.heapreplace(heap, item)

    hits = [item[2] for item in heap]
    hits.sort(key=lambda hit: (hit.score, hit.size), reverse=True)
    return FileScanResult(
        hits=hits,
        matched=matched,
        capped=matched > CAP,
        folders=folders,
        elapsed=time.time() - started,
        roots=list(roots),
    )
