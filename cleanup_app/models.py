"""Shared records passed between the scanners and the window."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class JunkCategory:
    id: str
    name: str
    group: str
    safety: str
    description: str
    paths: list[str]
    mode: str = "tree"
    patterns: list[str] = field(default_factory=list)
    special: str = ""
    size: int = 0
    count: int = 0
    skipped: int = 0
    note: str = ""
    checked: bool = False


@dataclass
class FileHit:
    path: str
    size: int
    modified: float
    accessed: float
    score: int
    verdict: str
    reasons: list[str]
    precheck: bool
    root: str
    checked: bool = False


@dataclass
class FileScanResult:
    hits: list[FileHit]
    matched: int
    capped: bool
    folders: int
    elapsed: float
    roots: list[str]


@dataclass
class ServiceInfo:
    name: str
    display_name: str
    description: str
    status: str
    start_type: str
    binpath: str
    pid: int | None
    memory: int
    cpu: float
    shared_process: bool
    publisher_kind: str
    impact: str
    advice: str
    critical: bool
    optional_windows: bool
    security: bool

    @property
    def actions(self) -> list[str]:
        if self.critical or self.security:
            return []
        if self.optional_windows:
            return ["manual", "automatic"]
        if self.publisher_kind == "windows":
            return []
        return ["stop", "manual", "automatic"]


@dataclass
class StartupItem:
    id: str
    name: str
    command: str
    location: str
    scope: str
    kind: str
    enabled: bool
    missing: bool
    description: str
    payload: dict


@dataclass
class RegistryIssue:
    id: str
    kind: str
    hive_name: str
    key: str
    value_name: str
    problem: str
    detail: str
    checked: bool = False
    recommendation: str = ""
    fix: str = ""


@dataclass
class Disk:
    letter: str
    label: str
    total: int
    used: int
    free: int
    media: str
    health: str
    bus: str
    model: str
    drive_type: str


@dataclass
class Finding:
    title: str
    detail: str
    level: str
    page: str


@dataclass
class DashboardData:
    disks: list[Disk]
    recycle_bytes: int
    recycle_items: int
    temp_bytes: int
    temp_files: int
    startup_count: int
    startup_broken: int
    service_notes: list[str]
    findings: list[Finding]
    warnings: list[str] = field(default_factory=list)
    hiber_bytes: int = 0
    page_bytes: int = 0
    windows_old: bool = False
    startup_items: list = field(default_factory=list)
    service_items: list = field(default_factory=list)


@dataclass
class SpaceSlice:
    name: str
    size: int
    path: str = ""
    kind: str = "folder"


@dataclass
class InstalledApp:
    name: str
    publisher: str
    size: int
    location: str


@dataclass
class BigFile:
    path: str
    size: int


@dataclass(slots=True)
class FolderNode:
    name: str
    path: str
    size: int
    children: list["FolderNode"] = field(default_factory=list)


@dataclass
class DriveMap:
    root: str
    slices: list[SpaceSlice]
    measured: int
    files_seen: int
    tree: FolderNode | None = None


@dataclass
class SpaceReport:
    drives: list[DriveMap]
    user_folders: list[SpaceSlice]
    apps: list[InstalledApp]
    files: list[BigFile]
    notes: list[str]
    measured: int
    files_seen: int
    unknown_apps: int
    partial: bool = False
    explanations: list = field(default_factory=list)
    tree_id: str = ""


@dataclass
class CleanReport:
    deleted_files: int = 0
    deleted_bytes: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
