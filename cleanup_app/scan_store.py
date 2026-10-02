"""Save the last scan on this PC so the overview is still there next time.

The folder trees from a disk map are large and only change with a new map, so
they live in their own file (space_tree.json) that is written once, from the
scan thread. The rest of the scan is small and is saved whenever it changes.
"""

from __future__ import annotations

import dataclasses
import gc
import json
import os
import uuid
from datetime import datetime

from cleanup_app.models import (
    BigFile,
    DashboardData,
    Disk,
    DriveMap,
    FileHit,
    FileScanResult,
    Finding,
    FolderNode,
    InstalledApp,
    JunkCategory,
    RegistryIssue,
    ServiceInfo,
    SpaceReport,
    SpaceSlice,
    StartupItem,
)
from cleanup_app.util import app_dir

TREE_FILE = "space_tree.json"
SNAPSHOT_VERSIONS = {1, 2}


def snapshot_path() -> str:
    return os.path.join(app_dir(), "last_scan.json")


def tree_path() -> str:
    return os.path.join(app_dir(), TREE_FILE)


def format_scan_time(stamp: float | None) -> str:
    if not stamp:
        return ""
    try:
        moment = datetime.fromtimestamp(float(stamp))
    except (OSError, OverflowError, TypeError, ValueError):
        return ""
    clock = moment.strftime("%I:%M %p").lstrip("0")
    return f"{moment.day} {moment.strftime('%b %Y')}, {clock}"


def _write_json(destination: str, payload) -> None:
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    temporary = destination + ".tmp"
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(temporary, destination)


def write_snapshot(payload: dict, path: str | None = None) -> None:
    _write_json(path or snapshot_path(), payload)


def write_trees(report, path: str | None = None) -> None:
    """Save the folder trees of a disk map. Call it from a worker thread.

    The report gets a tree_id that the small snapshot records, so a tree file
    left over from a different scan is never paired with it.
    """
    if report is None or not any(drive.tree for drive in report.drives):
        return
    report.tree_id = report.tree_id or uuid.uuid4().hex
    payload = {
        "version": 1,
        "id": report.tree_id,
        "drives": {drive.root: _encode_tree(drive.tree) for drive in report.drives if drive.tree is not None},
    }
    _write_json(path or tree_path(), payload)


def read_trees(path: str | None = None) -> tuple[str, dict[str, FolderNode]] | None:
    try:
        with open(path or tree_path(), encoding="utf-8") as handle:
            raw = json.load(handle)
        if not isinstance(raw, dict) or raw.get("version") != 1 or not isinstance(raw.get("drives"), dict):
            return None
        # Building ~100,000 nodes trips the cyclic garbage collector many times
        # over, and none of them can be garbage yet.
        collecting = gc.isenabled()
        gc.disable()
        try:
            return str(raw.get("id") or ""), {str(root): _decode_tree(data) for root, data in raw["drives"].items()}
        finally:
            if collecting:
                gc.enable()
    except (OSError, json.JSONDecodeError, TypeError, ValueError, IndexError):
        return None


def read_snapshot(path: str | None = None, trees: str | None = None) -> dict | None:
    destination = path or snapshot_path()
    try:
        with open(destination, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or raw.get("version") not in SNAPSHOT_VERSIONS:
        return None
    try:
        stamp = raw.get("saved_at")
        saved_at = float(stamp) if stamp else None
    except (TypeError, ValueError):
        saved_at = None
    space = _optional(_space, raw.get("space"))
    if space is not None and space.tree_id and not any(drive.tree for drive in space.drives):
        _attach_trees(space, trees or os.path.join(os.path.dirname(destination), TREE_FILE))
    return {
        "saved_at": saved_at,
        "dashboard": _optional(_dashboard, raw.get("dashboard")),
        "space": space,
        "junk": _optional(_junk_list, raw.get("junk")),
        "registry": _optional(_registry_list, raw.get("registry")),
        "files": _optional(_files, raw.get("files")),
        "startup": _optional(_startup_list, raw.get("startup")),
        "services": _optional(_service_list, raw.get("services")),
        "ai_report": raw.get("ai_report") if isinstance(raw.get("ai_report"), str) else "",
        "ai_answer": raw.get("ai_answer") if isinstance(raw.get("ai_answer"), str) else "",
    }


def pack_snapshot(
    saved_at: float | None,
    dashboard,
    space,
    junk,
    registry,
    files,
    startup,
    services,
    ai_report: str,
    ai_answer: str,
) -> dict:
    return {
        "version": 2,
        "saved_at": saved_at,
        "dashboard": _dump(dashboard),
        "space": _dump(space),
        "junk": _dump(junk),
        "registry": _dump(registry),
        "files": _dump(files),
        "startup": _dump(startup),
        "services": _dump(services),
        "ai_report": ai_report or "",
        "ai_answer": ai_answer or "",
    }


_FIELD_NAMES: dict[type, tuple[str, ...]] = {}


def _dump(value):
    """Plain JSON data for a record. Folder trees are left out, because write_trees saves them.

    dataclasses.asdict deep-copies every value, which is slow for thousands of records.
    """
    if isinstance(value, FolderNode):
        return None
    cls = type(value)
    names = _FIELD_NAMES.get(cls)
    if names is None and dataclasses.is_dataclass(value) and not isinstance(value, type):
        names = _FIELD_NAMES[cls] = tuple(item.name for item in dataclasses.fields(cls))
    if names is not None:
        return {name: _dump(getattr(value, name)) for name in names}
    if isinstance(value, (list, tuple)):
        return [_dump(item) for item in value]
    if isinstance(value, dict):
        return {key: _dump(item) for key, item in value.items()}
    return value


def _attach_trees(space: SpaceReport, path: str) -> None:
    loaded = read_trees(path)
    if loaded is None or loaded[0] != space.tree_id:
        return
    trees = loaded[1]
    for drive in space.drives:
        drive.tree = trees.get(drive.root)


# A folder is stored as [name, size, path] or [name, size, path, [children]].
# The path is nearly always implied by the parent, so it is stored as a code:
# 0 = the parent's path joined with the name, 1 = the parent's own path,
# 2 = no path. Anything else is the path itself.
def _child_path(parent: str, name: str) -> str:
    # os.path.join is slow in a loop of 100,000 folders. Encode and decode both
    # use this, so a path is only implied when this rebuilds it exactly.
    return parent + name if parent.endswith("\\") else parent + "\\" + name


def _encode_tree(root: FolderNode) -> list:
    encoded: list = [root.name, root.size, root.path]
    pending = [(root, encoded)]
    while pending:
        node, out = pending.pop()
        if not node.children:
            continue
        children = []
        for child in node.children:
            if child.path == node.path:
                code = 1
            elif not child.path:
                code = 2
            elif child.path == _child_path(node.path, child.name):
                code = 0
            else:
                code = child.path
            item = [child.name, child.size, code]
            children.append(item)
            if child.children:
                pending.append((child, item))
        out.append(children)
    return encoded


def _decode_tree(data: list) -> FolderNode:
    root = FolderNode(str(data[0]), str(data[2]), int(data[1]), [])
    pending = [(root, data)]
    join = _child_path
    while pending:
        node, raw = pending.pop()
        if len(raw) < 4:
            continue
        for child in raw[3]:
            name, code = str(child[0]), child[2]
            if code == 0:
                path = join(node.path, name)
            elif code == 1:
                path = node.path
            elif code == 2:
                path = ""
            else:
                path = str(code)
            item = FolderNode(name, path, int(child[1]), [])
            node.children.append(item)
            if len(child) > 3:
                pending.append((item, child))
    return root


def _optional(loader, value):
    if value is None:
        return None
    try:
        return loader(value)
    except (TypeError, ValueError, KeyError):
        return None


def _take(data: dict, cls, lists: dict | None = None):
    if not isinstance(data, dict):
        raise TypeError("expected an object")
    lists = lists or {}
    kwargs = {}
    for item in dataclasses.fields(cls):
        if item.name in lists:
            kwargs[item.name] = lists[item.name](data.get(item.name))
        elif item.name in data:
            kwargs[item.name] = data[item.name]
    return cls(**kwargs)


def _rows(cls, value) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        raise TypeError("expected a list")
    return [_take(item, cls) for item in value]


def _folder(value) -> FolderNode | None:
    """Read a tree stored inside an older snapshot, from before trees had their own file."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TypeError("expected a folder")
    root = _take(value, FolderNode)
    pending = [(root, value)]
    while pending:
        node, raw = pending.pop()
        children = raw.get("children") or []
        if not isinstance(children, list):
            raise TypeError("expected folder children")
        node.children = []
        for child in children:
            item = _take(child, FolderNode)
            node.children.append(item)
            pending.append((item, child))
    return root


def _drive(value) -> DriveMap:
    drive = _take(value, DriveMap, {"slices": lambda items: _rows(SpaceSlice, items)})
    drive.tree = _folder(value.get("tree"))
    return drive


def _space(value) -> SpaceReport:
    report = _take(
        value,
        SpaceReport,
        {
            "drives": lambda items: [_drive(item) for item in items or []],
            "user_folders": lambda items: _rows(SpaceSlice, items),
            "apps": lambda items: _rows(InstalledApp, items),
            "files": lambda items: _rows(BigFile, items),
        },
    )
    explanations = value.get("explanations") or []
    if not isinstance(explanations, list):
        raise TypeError("expected explanations")
    report.explanations = explanations
    report.tree_id = str(report.tree_id or "")
    return report


def _dashboard(value) -> DashboardData:
    return _take(
        value,
        DashboardData,
        {
            "disks": lambda items: _rows(Disk, items),
            "findings": lambda items: _rows(Finding, items),
            "startup_items": _startup_list,
            "service_items": _service_list,
        },
    )


def _files(value) -> FileScanResult:
    return _take(value, FileScanResult, {"hits": lambda items: _rows(FileHit, items)})


def _junk_list(value):
    return _rows(JunkCategory, value)


def _registry_list(value):
    return _rows(RegistryIssue, value)


def _startup_list(value):
    return _rows(StartupItem, value)


def _service_list(value):
    return _rows(ServiceInfo, value)
