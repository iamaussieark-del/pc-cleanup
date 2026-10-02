"""Broken registry leftovers only. A .reg backup is written before each change."""

from __future__ import annotations

import os
import winreg
from datetime import datetime

from cleanup_app.models import CleanReport, RegistryIssue
from cleanup_app.util import backup_dir, extract_exe, run_hidden

HIVES = {
    "HKCU": winreg.HKEY_CURRENT_USER,
    "HKLM": winreg.HKEY_LOCAL_MACHINE,
}

RUN_KEYS = (
    ("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Run", winreg.KEY_READ),
    ("HKLM", r"Software\Microsoft\Windows\CurrentVersion\Run", winreg.KEY_READ | winreg.KEY_WOW64_64KEY),
    ("HKLM", r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run", winreg.KEY_READ | winreg.KEY_WOW64_64KEY),
)

UNINSTALL_KEYS = (
    ("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Uninstall", winreg.KEY_READ),
    ("HKLM", r"Software\Microsoft\Windows\CurrentVersion\Uninstall", winreg.KEY_READ | winreg.KEY_WOW64_64KEY),
    ("HKLM", r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall", winreg.KEY_READ | winreg.KEY_WOW64_64KEY),
)

MUI_KEY = r"Software\Classes\Local Settings\Software\Microsoft\Windows\Shell\MuiCache"
APP_PATHS = (
    ("HKCU", r"Software\Microsoft\Windows\CurrentVersion\App Paths", winreg.KEY_READ),
    ("HKLM", r"Software\Microsoft\Windows\CurrentVersion\App Paths", winreg.KEY_READ | winreg.KEY_WOW64_64KEY),
    ("HKLM", r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths", winreg.KEY_READ | winreg.KEY_WOW64_64KEY),
)
SHARED_DLLS = (
    ("HKLM", r"Software\Microsoft\Windows\CurrentVersion\SharedDLLs", winreg.KEY_READ | winreg.KEY_WOW64_64KEY),
    ("HKLM", r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\SharedDLLs", winreg.KEY_READ | winreg.KEY_WOW64_64KEY),
)
FONT_KEY = r"Software\Microsoft\Windows NT\CurrentVersion\Fonts"
HELP_KEY = r"Software\Microsoft\Windows\Help"
INSTALLER_FOLDERS = r"Software\Microsoft\Windows\CurrentVersion\Installer\Folders"
APPLICATIONS_KEY = r"Software\Classes\Applications"
RUNONCE_KEYS = (
    ("HKCU", r"Software\Microsoft\Windows\CurrentVersion\RunOnce", winreg.KEY_READ),
    ("HKLM", r"Software\Microsoft\Windows\CurrentVersion\RunOnce", winreg.KEY_READ | winreg.KEY_WOW64_64KEY),
)
MAX_ISSUES = 400
PER_KIND = {
    "startup": 40,
    "runonce": 20,
    "uninstall": 80,
    "apppath": 40,
    "shareddll": 60,
    "application": 40,
    "help": 30,
    "font": 40,
    "installer": 40,
    "mui": 80,
}
KEY_KINDS = {"uninstall", "apppath", "application"}
KIND_GUIDE = {
    "startup": (
        "Startup entries",
        "Safe to remove",
        "The program file is gone, so Windows cannot start it. Removing the entry stops a failed launch at sign-in.",
    ),
    "runonce": (
        "Run-once leftovers",
        "Safe to remove",
        "Windows was supposed to run this once, and the file is already gone. Removing it does not uninstall anything.",
    ),
    "uninstall": (
        "Uninstall records",
        "Safe to remove",
        "Apps & features still lists a program whose folder and uninstaller are both missing. Removing the record drops that dead listing.",
    ),
    "apppath": (
        "Application paths",
        "Safe to remove",
        "Windows has a shortcut path for a program that is no longer on this PC. Removing it does not delete any remaining files.",
    ),
    "shareddll": (
        "Shared DLL entries",
        "Safe to remove",
        "The registry still counts a shared file that is not on disk. Removing the count does not delete other programs.",
    ),
    "application": (
        "Your application registrations",
        "Safe to remove",
        "Your account still has an open-command for a program file that is gone. Removing it only affects this Windows user.",
    ),
    "help": (
        "Help file entries",
        "Safe to remove",
        "A help-file path points at a document that is not there. Removing it does not remove the program.",
    ),
    "font": (
        "Font entries",
        "Review first",
        "Windows lists a font file that is not in the Fonts folder. Remove it only if you recognise the name and no longer use that font.",
    ),
    "installer": (
        "Installer folder records",
        "Review first",
        "Windows Installer remembers a folder that is no longer there. Leave it if you still repair or change that program from Apps & features.",
    ),
    "mui": (
        "Icon names",
        "Safe to remove",
        "Windows remembered the names of programs that are no longer on this PC. Removing the names does not uninstall anything.",
    ),
}


def _values(hive, subkey: str, access: int) -> list[tuple[str, object]]:
    found = []
    try:
        with winreg.OpenKey(hive, subkey, 0, access) as key:
            count = winreg.QueryInfoKey(key)[1]
            for index in range(count):
                try:
                    name, value, _typ = winreg.EnumValue(key, index)
                    found.append((name, value))
                except OSError:
                    continue
    except OSError:
        return []
    return found


def _subkeys(hive, subkey: str, access: int) -> list[str]:
    found = []
    try:
        with winreg.OpenKey(hive, subkey, 0, access) as key:
            count = winreg.QueryInfoKey(key)[0]
            for index in range(count):
                try:
                    found.append(winreg.EnumKey(key, index))
                except OSError:
                    continue
    except OSError:
        return []
    return found


def _read_uninstall(hive, path: str, access: int) -> dict:
    values = {}
    try:
        with winreg.OpenKey(hive, path, 0, access) as key:
            count = winreg.QueryInfoKey(key)[1]
            for index in range(count):
                try:
                    name, value, _typ = winreg.EnumValue(key, index)
                    values[name] = value
                except OSError:
                    continue
    except OSError:
        return {}
    return values


def _target_missing(command: str) -> str:
    exe = extract_exe(str(command))
    if not exe or os.path.basename(exe).lower() == "rundll32.exe":
        return ""
    return missing_local_file(exe)


def missing_local_file(path: str) -> str:
    """A local path that is not on disk. Network paths are ignored so the scan does not stall."""
    text = os.path.expandvars(str(path or "")).strip().strip('"')
    if text.lower().startswith("\\??\\"):
        text = text[4:]
    if not text or text.startswith("\\\\") or text.startswith("//"):
        return ""
    if len(text) < 3 or text[1] != ":":
        return ""
    if os.path.exists(text):
        return ""
    return text


def _too_broad(path: str) -> bool:
    try:
        cleaned = os.path.abspath(path).rstrip("\\").lower()
    except (OSError, ValueError):
        return True
    if len(cleaned) <= 3:
        return True
    windir = os.path.abspath(os.environ.get("WINDIR", r"C:\Windows")).rstrip("\\").lower()
    program = os.path.abspath(os.environ.get("ProgramFiles", r"C:\Program Files")).rstrip("\\").lower()
    return cleaned in {windir, program, program + " (x86)"}


def _room(issues: list[RegistryIssue], kind: str) -> bool:
    if len(issues) >= MAX_ISSUES:
        return False
    limit = PER_KIND.get(kind, 40)
    return sum(1 for item in issues if item.kind == kind) < limit


def _add(issues: list[RegistryIssue], kind: str, hive_name: str, key: str, value_name: str, problem: str, detail: str) -> None:
    title, recommendation, fix = KIND_GUIDE[kind]
    issues.append(
        RegistryIssue(
            id=f"{kind}|{hive_name}|{key}|{value_name}",
            kind=kind,
            hive_name=hive_name,
            key=key,
            value_name=value_name,
            problem=problem or title,
            detail=detail,
            checked=recommendation == "Safe to remove",
            recommendation=recommendation,
            fix=fix,
        )
    )


def _scan_commands(issues: list[RegistryIssue], cancel, keys, kind: str, problem: str) -> None:
    for hive_name, subkey, access in keys:
        if cancel.is_set() or not _room(issues, kind):
            return
        for name, value in _values(HIVES[hive_name], subkey, access):
            if cancel.is_set() or not _room(issues, kind):
                return
            if not isinstance(value, str):
                continue
            missing = _target_missing(value)
            if not missing or not missing_local_file(missing):
                continue
            _add(issues, kind, hive_name, subkey, name, problem, f"{name} → {missing}")


def _scan_uninstall(issues: list[RegistryIssue], cancel) -> None:
    for hive_name, subkey, access in UNINSTALL_KEYS:
        if cancel.is_set() or not _room(issues, "uninstall"):
            return
        hive = HIVES[hive_name]
        for child in _subkeys(hive, subkey, access):
            if cancel.is_set() or not _room(issues, "uninstall"):
                return
            if child.upper().startswith("KB") and child[2:].isdigit():
                continue
            values = _read_uninstall(hive, subkey + "\\" + child, access)
            if values.get("SystemComponent") == 1 or values.get("ParentKeyName") or values.get("NoRemove") == 1:
                continue
            display = str(values.get("DisplayName") or "")
            if not display or display.startswith(("Update for", "Security Update", "Hotfix")):
                continue
            location = os.path.expandvars(str(values.get("InstallLocation") or "").strip().strip('"'))
            uninstaller = extract_exe(str(values.get("UninstallString") or ""))
            if not location or not uninstaller:
                continue
            if not missing_local_file(location) or not missing_local_file(uninstaller):
                continue
            _add(
                issues,
                "uninstall",
                hive_name,
                subkey + "\\" + child,
                "",
                "Uninstall record points at a program that is already gone",
                display,
            )


def _scan_app_paths(issues: list[RegistryIssue], cancel) -> None:
    for hive_name, subkey, access in APP_PATHS:
        if cancel.is_set() or not _room(issues, "apppath"):
            return
        hive = HIVES[hive_name]
        for child in _subkeys(hive, subkey, access):
            if cancel.is_set() or not _room(issues, "apppath"):
                return
            values = _read_uninstall(hive, subkey + "\\" + child, access)
            target = extract_exe(str(values.get("") or values.get("(default)") or ""))
            if not target:
                for name, value in values.items():
                    if isinstance(value, str) and value:
                        target = extract_exe(value)
                        break
            missing = missing_local_file(target)
            if not missing:
                continue
            _add(
                issues,
                "apppath",
                hive_name,
                subkey + "\\" + child,
                "",
                "Application path points at a missing program",
                f"{child} → {missing}",
            )


def _scan_shared_dlls(issues: list[RegistryIssue], cancel) -> None:
    for hive_name, subkey, access in SHARED_DLLS:
        if cancel.is_set() or not _room(issues, "shareddll"):
            return
        for index, (name, _value) in enumerate(_values(HIVES[hive_name], subkey, access)):
            if index >= 5000 or cancel.is_set() or not _room(issues, "shareddll"):
                return
            missing = missing_local_file(name)
            if not missing:
                continue
            _add(issues, "shareddll", hive_name, subkey, name, "Shared file is no longer on this PC", missing)


def _scan_applications(issues: list[RegistryIssue], cancel) -> None:
    hive = winreg.HKEY_CURRENT_USER
    for child in _subkeys(hive, APPLICATIONS_KEY, winreg.KEY_READ):
        if cancel.is_set() or not _room(issues, "application"):
            return
        command_key = APPLICATIONS_KEY + "\\" + child + r"\shell\open\command"
        default = ""
        for name, value in _values(hive, command_key, winreg.KEY_READ):
            if name == "" and isinstance(value, str):
                default = value
                break
        missing = missing_local_file(_target_missing(default) or extract_exe(default))
        if not missing:
            continue
        _add(
            issues,
            "application",
            "HKCU",
            APPLICATIONS_KEY + "\\" + child,
            "",
            "Your account still opens a program that is gone",
            f"{child} → {missing}",
        )


def _scan_help(issues: list[RegistryIssue], cancel) -> None:
    for hive_name in ("HKCU", "HKLM"):
        if cancel.is_set() or not _room(issues, "help"):
            return
        access = winreg.KEY_READ if hive_name == "HKCU" else winreg.KEY_READ | winreg.KEY_WOW64_64KEY
        for name, value in _values(HIVES[hive_name], HELP_KEY, access):
            if cancel.is_set() or not _room(issues, "help"):
                return
            if not isinstance(value, str):
                continue
            missing = missing_local_file(value)
            if not missing:
                continue
            _add(issues, "help", hive_name, HELP_KEY, name, "Help file is no longer on this PC", f"{name} → {missing}")


def _font_file(value: str) -> str:
    text = os.path.expandvars(str(value or "")).strip().strip('"')
    if not text or text.startswith("\\\\") or ".." in text.replace("\\", "/"):
        return ""
    if len(text) > 2 and text[1] == ":":
        return text
    name = os.path.basename(text)
    if not name or name in {".", ".."}:
        return ""
    return os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", name)


def _scan_fonts(issues: list[RegistryIssue], cancel) -> None:
    access = winreg.KEY_READ | winreg.KEY_WOW64_64KEY
    for name, value in _values(winreg.HKEY_LOCAL_MACHINE, FONT_KEY, access):
        if cancel.is_set() or not _room(issues, "font"):
            return
        if not isinstance(value, str):
            continue
        path = _font_file(value)
        if not path or os.path.exists(path):
            continue
        _add(issues, "font", "HKLM", FONT_KEY, name, "Listed font file is missing", f"{name} → {path}")


def _scan_installer_folders(issues: list[RegistryIssue], cancel) -> None:
    access = winreg.KEY_READ | winreg.KEY_WOW64_64KEY
    for index, (name, _value) in enumerate(_values(winreg.HKEY_LOCAL_MACHINE, INSTALLER_FOLDERS, access)):
        if index >= 5000 or cancel.is_set() or not _room(issues, "installer"):
            return
        folder = os.path.expandvars(str(name)).strip().strip('"')
        if _too_broad(folder) or not missing_local_file(folder.rstrip("\\")):
            continue
        _add(issues, "installer", "HKLM", INSTALLER_FOLDERS, name, "Installer remembers a folder that is gone", folder)


def _scan_mui(issues: list[RegistryIssue], cancel) -> None:
    for name, _value in _values(winreg.HKEY_CURRENT_USER, MUI_KEY, winreg.KEY_READ):
        if cancel.is_set() or not _room(issues, "mui"):
            return
        exe = name
        for suffix in (".FriendlyAppName", ".ApplicationCompany"):
            if name.endswith(suffix):
                exe = name[: -len(suffix)]
        missing = missing_local_file(exe)
        if not missing:
            continue
        _add(issues, "mui", "HKCU", MUI_KEY, name, "Cached program name for a file that is gone", missing)


def scan_registry(cancel, progress) -> list[RegistryIssue]:
    issues: list[RegistryIssue] = []
    steps = (
        ("Checking startup entries…", lambda: _scan_commands(issues, cancel, RUN_KEYS, "startup", "Startup entry points at a missing file")),
        ("Checking run-once leftovers…", lambda: _scan_commands(issues, cancel, RUNONCE_KEYS, "runonce", "Run-once entry points at a missing file")),
        ("Checking uninstall records…", lambda: _scan_uninstall(issues, cancel)),
        ("Checking application paths…", lambda: _scan_app_paths(issues, cancel)),
        ("Checking shared files…", lambda: _scan_shared_dlls(issues, cancel)),
        ("Checking your application registrations…", lambda: _scan_applications(issues, cancel)),
        ("Checking help files…", lambda: _scan_help(issues, cancel)),
        ("Checking fonts…", lambda: _scan_fonts(issues, cancel)),
        ("Checking installer folders…", lambda: _scan_installer_folders(issues, cancel)),
        ("Checking icon names…", lambda: _scan_mui(issues, cancel)),
    )
    for message, step in steps:
        if cancel.is_set():
            break
        progress(message)
        step()
    return issues


def _export_key(hive_name: str, key: str, destination: str) -> None:
    full = f"{hive_name}\\{key}"
    completed = run_hidden(["reg", "export", full, destination, "/y"], timeout=30)
    if completed.returncode != 0 or not os.path.isfile(destination):
        message = ((completed.stderr or "") + "\n" + (completed.stdout or "")).strip()
        raise RuntimeError(message or f"Could not back up {full}.")


def remove_issues(issues: list[RegistryIssue], cancel, progress) -> CleanReport:
    report = CleanReport()
    exported: set[str] = set()
    for issue in issues:
        if cancel.is_set():
            report.notes.append("Stopped early.")
            break
        if not issue.checked:
            continue
        progress(f"Backing up {issue.problem}…")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        safe = "".join(ch if ch.isalnum() or ch in ".-" else "_" for ch in issue.key)[-60:]
        destination = os.path.join(backup_dir(), f"{stamp}-{issue.hive_name}-{safe}.reg")
        ident = f"{issue.hive_name}\\{issue.key}"
        try:
            if not issue_is_contained(issue):
                raise RuntimeError("That registry location is outside the leftovers this tool is allowed to change.")
            if issue.kind not in KEY_KINDS and not issue.value_name:
                raise RuntimeError("Nothing to remove.")
            if ident not in exported:
                _export_key(issue.hive_name, issue.key, destination)
                exported.add(ident)
                report.notes.append(destination)
            hive = HIVES[issue.hive_name]
            access = winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY
            if issue.kind in KEY_KINDS:
                completed = run_hidden(["reg", "delete", ident, "/f"], timeout=30)
                if completed.returncode != 0:
                    raise RuntimeError((completed.stderr or completed.stdout or "Delete failed").strip())
            else:
                with winreg.OpenKey(hive, issue.key, 0, access) as key:
                    winreg.DeleteValue(key, issue.value_name)
            report.deleted_files += 1
        except (OSError, RuntimeError) as exc:
            report.errors.append(f"{issue.detail}: {exc}")
            report.skipped += 1
    return report


def is_inside_key(child: str, parent: str) -> bool:
    child_key = child.lower().rstrip("\\")
    parent_key = parent.lower().rstrip("\\")
    return child_key == parent_key or child_key.startswith(parent_key + "\\")


def issue_is_contained(issue: RegistryIssue) -> bool:
    """Edits stay inside leftover keys. Whole list keys, such as every font, are never deleted."""
    allowed = (
        r"Software\Microsoft\Windows\CurrentVersion\Run",
        r"Software\Microsoft\Windows\CurrentVersion\RunOnce",
        r"Software\Microsoft\Windows\CurrentVersion\Uninstall",
        r"Software\Microsoft\Windows\CurrentVersion\App Paths",
        r"Software\Microsoft\Windows\CurrentVersion\SharedDLLs",
        r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run",
        r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
        r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths",
        r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\SharedDLLs",
        FONT_KEY,
        HELP_KEY,
        INSTALLER_FOLDERS,
        APPLICATIONS_KEY,
        MUI_KEY,
    )
    if not any(is_inside_key(issue.key, item) for item in allowed):
        return False
    if issue.kind in KEY_KINDS:
        return not any(issue.key.lower().rstrip("\\") == item.lower().rstrip("\\") for item in allowed)
    return True
