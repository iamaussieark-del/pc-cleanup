"""Startup apps from Run keys and the Startup folders, with a way to turn them back on."""

from __future__ import annotations

import json
import os
import shutil
import winreg

from cleanup_app.models import StartupItem
from cleanup_app.util import app_dir, extract_exe, file_description, load_state, run_hidden, save_state

RUN_KEYS = (
    ("HKCU", winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0),
    ("HKLM", winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Run", winreg.KEY_WOW64_64KEY),
    ("HKLM", winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run", winreg.KEY_WOW64_64KEY),
)


def _startup_folders() -> list[tuple[str, str]]:
    roaming = os.environ.get("APPDATA", "")
    programdata = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
    return [
        ("Your Startup folder", os.path.join(roaming, r"Microsoft\Windows\Start Menu\Programs\Startup")),
        ("Common Startup folder", os.path.join(programdata, r"Microsoft\Windows\Start Menu\Programs\Startup")),
    ]


def _shortcut_targets(paths: list[str]) -> dict[str, str]:
    if not paths:
        return {}
    script = r"""
$paths = ConvertFrom-Json -InputObject ([Console]::In.ReadToEnd())
$shell = New-Object -ComObject WScript.Shell
$out = foreach ($p in $paths) {
  try {
    $item = $shell.CreateShortcut($p)
    [pscustomobject]@{ Path = $p; Target = $item.TargetPath }
  } catch {
    [pscustomobject]@{ Path = $p; Target = '' }
  }
}
$out | ConvertTo-Json -Compress
"""
    completed = run_hidden(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        timeout=40,
        input_text=json.dumps(paths),
    )
    raw = (completed.stdout or "").lstrip("\ufeff").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if isinstance(data, dict):
        data = [data]
    return {item.get("Path"): item.get("Target") or "" for item in data}


def _reg_items() -> list[StartupItem]:
    items = []
    for scope, hive, subkey, view in RUN_KEYS:
        try:
            with winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ | view) as key:
                count = winreg.QueryInfoKey(key)[1]
                for index in range(count):
                    try:
                        name, value, regtype = winreg.EnumValue(key, index)
                    except OSError:
                        continue
                    if not isinstance(value, str):
                        continue
                    exe = extract_exe(value)
                    missing = bool(exe) and os.path.basename(exe).lower() != "rundll32.exe" and not os.path.exists(exe)
                    items.append(
                        StartupItem(
                            id=f"registry|{scope}|{subkey}|{name}",
                            name=name,
                            command=value,
                            location=f"{scope}\\{subkey}",
                            scope=scope,
                            kind="registry",
                            enabled=True,
                            missing=missing,
                            description=file_description(exe),
                            payload={
                                "scope": scope,
                                "key": subkey,
                                "name": name,
                                "value": value,
                                "regtype": int(regtype),
                                "view": int(view),
                            },
                        )
                    )
        except OSError:
            continue
    return items


def _folder_items() -> list[StartupItem]:
    folders = [(label, path) for label, path in _startup_folders() if os.path.isdir(path)]
    files: list[tuple[str, str, str]] = []
    for label, folder in folders:
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for name in names:
            if name.lower() in {"desktop.ini"}:
                continue
            full = os.path.join(folder, name)
            if os.path.isfile(full):
                files.append((label, folder, full))
    targets = _shortcut_targets([path for _label, _folder, path in files if path.lower().endswith(".lnk")])
    items = []
    for label, folder, full in files:
        target = targets.get(full) or (full if not full.lower().endswith(".lnk") else "")
        exe = extract_exe(target) if target else ""
        missing = bool(exe) and not os.path.exists(exe)
        items.append(
            StartupItem(
                id=f"folder|{folder}|{os.path.basename(full)}",
                name=os.path.splitext(os.path.basename(full))[0],
                command=target or full,
                location=label,
                scope="HKCU" if "appdata" in folder.lower() else "HKLM",
                kind="folder",
                enabled=True,
                missing=missing,
                description=file_description(exe or full),
                payload={"original": full, "folder": folder},
            )
        )
    return items


def _disabled_items(enabled_ids: set[str]) -> list[StartupItem]:
    state = load_state()
    items = []
    kept = []
    for record in state.get("disabled_startup", []):
        if record.get("id") in enabled_ids:
            continue
        kept.append(record)
        payload = record.get("payload") or {}
        if record.get("kind") == "folder":
            moved = payload.get("moved") or ""
            if moved and not os.path.exists(moved):
                continue
        items.append(
            StartupItem(
                id=record["id"],
                name=record.get("name") or "Startup item",
                command=record.get("command") or "",
                location=record.get("location") or "",
                scope=record.get("scope") or "",
                kind=record.get("kind") or "registry",
                enabled=False,
                missing=False,
                description=record.get("description") or "",
                payload=payload,
            )
        )
    if len(kept) != len(state.get("disabled_startup", [])):
        state["disabled_startup"] = kept
        save_state(state)
    return items


def scan_startup(cancel, progress) -> list[StartupItem]:
    progress("Reading startup programs…")
    if cancel.is_set():
        return []
    items = _reg_items() + _folder_items()
    enabled_ids = {item.id for item in items}
    items.extend(_disabled_items(enabled_ids))
    items.sort(key=lambda item: (not item.enabled, item.missing, item.name.lower()))
    return items


def _hive(scope: str):
    return winreg.HKEY_LOCAL_MACHINE if scope == "HKLM" else winreg.HKEY_CURRENT_USER


def disable_startup(item: StartupItem) -> None:
    state = load_state()
    records = [record for record in state.get("disabled_startup", []) if record.get("id") != item.id]
    if item.kind == "registry":
        payload = dict(item.payload)
        hive = _hive(payload["scope"])
        access = winreg.KEY_SET_VALUE | int(payload.get("view") or 0)
        with winreg.OpenKey(hive, payload["key"], 0, access) as key:
            winreg.DeleteValue(key, payload["name"])
        records.append(
            {
                "id": item.id,
                "name": item.name,
                "command": item.command,
                "location": item.location,
                "scope": item.scope,
                "kind": "registry",
                "description": item.description,
                "payload": payload,
            }
        )
    else:
        original = item.payload["original"]
        disabled_dir = os.path.join(app_dir(), "disabled_startup")
        os.makedirs(disabled_dir, exist_ok=True)
        destination = os.path.join(disabled_dir, os.path.basename(original))
        if os.path.exists(destination):
            base, ext = os.path.splitext(os.path.basename(original))
            destination = os.path.join(disabled_dir, f"{base}-{int(time_now())}{ext}")
        shutil.move(original, destination)
        payload = {"original": original, "moved": destination, "folder": item.payload.get("folder", "")}
        records.append(
            {
                "id": item.id,
                "name": item.name,
                "command": item.command,
                "location": item.location,
                "scope": item.scope,
                "kind": "folder",
                "description": item.description,
                "payload": payload,
            }
        )
    state["disabled_startup"] = records
    save_state(state)


def enable_startup(item: StartupItem) -> None:
    payload = item.payload
    if item.kind == "registry":
        hive = _hive(payload["scope"])
        access = winreg.KEY_SET_VALUE | int(payload.get("view") or 0)
        with winreg.CreateKeyEx(hive, payload["key"], 0, access) as key:
            winreg.SetValueEx(key, payload["name"], 0, int(payload.get("regtype") or winreg.REG_SZ), payload["value"])
    else:
        moved = payload.get("moved") or ""
        original = payload.get("original") or ""
        if moved and original and os.path.exists(moved):
            os.makedirs(os.path.dirname(original), exist_ok=True)
            if os.path.exists(original):
                raise FileExistsError(f"{original} is already in the Startup folder.")
            shutil.move(moved, original)
    state = load_state()
    state["disabled_startup"] = [record for record in state.get("disabled_startup", []) if record.get("id") != item.id]
    save_state(state)


def time_now() -> int:
    import time

    return int(time.time())
