"""Drive free space, media type, and health."""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess

from cleanup_app.models import Disk
from cleanup_app.util import run_hidden

DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3
DRIVE_REMOTE = 4
DRIVE_CDROM = 5
DRIVE_RAMDISK = 6

BUS_NAMES = {
    1: "SCSI",
    7: "USB",
    8: "ATA",
    10: "SAS",
    11: "SATA",
    17: "NVMe",
}


def drive_letters() -> list[str]:
    mask = ctypes.windll.kernel32.GetLogicalDrives()
    return [chr(65 + index) for index in range(26) if mask & (1 << index)]


def drive_type(letter: str) -> int:
    return int(ctypes.windll.kernel32.GetDriveTypeW(f"{letter}:\\"))


def volume_label(letter: str) -> str:
    name = ctypes.create_unicode_buffer(261)
    try:
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            f"{letter}:\\",
            name,
            261,
            None,
            None,
            None,
            None,
            0,
        )
    except (OSError, ValueError, ctypes.ArgumentError):
        return ""
    if not ok:
        return ""
    return name.value


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _enum_text(value, mapping: dict[int, str]) -> str:
    if value is None:
        return ""
    if isinstance(value, str) and not value.isdigit():
        return value
    try:
        return mapping.get(int(value), str(value))
    except (TypeError, ValueError):
        return str(value)


def _query_hardware() -> dict[str, dict]:
    script = r"""
$ErrorActionPreference = 'SilentlyContinue'
$parts = @(Get-Partition | Where-Object { $_.DriveLetter } | Select-Object DriveLetter, DiskNumber)
$disks = @(Get-Disk | Select-Object Number, FriendlyName, HealthStatus, BusType)
$phys = @(Get-PhysicalDisk | Select-Object DeviceId, FriendlyName, MediaType, HealthStatus)
[pscustomobject]@{ Partitions = $parts; Disks = $disks; Physical = $phys } | ConvertTo-Json -Depth 4
"""
    completed = run_hidden(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        timeout=30,
    )
    raw = (completed.stdout or "").lstrip("\ufeff").strip()
    if not raw:
        return {}
    payload = json.loads(raw)
    disks = {int(item["Number"]): item for item in _as_list(payload.get("Disks")) if item and item.get("Number") is not None}
    physical = {}
    for item in _as_list(payload.get("Physical")):
        if not item:
            continue
        physical[str(item.get("DeviceId"))] = item
    found: dict[str, dict] = {}
    for part in _as_list(payload.get("Partitions")):
        if not part or not part.get("DriveLetter"):
            continue
        letter = str(part["DriveLetter"]).strip()[:1].upper()
        number = part.get("DiskNumber")
        disk = disks.get(int(number), {}) if number is not None else {}
        phys = physical.get(str(number), {})
        found[letter] = {"disk": disk, "physical": phys}
    return found


def _media(drive: int, physical: dict, disk: dict) -> str:
    media = physical.get("MediaType")
    text = _enum_text(media, {0: "Unspecified", 3: "HDD", 4: "SSD", 5: "SCM"}).upper()
    if text == "SSD":
        return "SSD"
    if text == "HDD":
        return "HDD"
    if drive == DRIVE_REMOTE:
        return "Network"
    if drive == DRIVE_REMOVABLE:
        return "Removable"
    bus = disk.get("BusType")
    bus_name = _enum_text(bus, BUS_NAMES).upper()
    if bus_name == "NVME":
        return "SSD"
    if drive == DRIVE_FIXED:
        return "Unknown"
    return "Unknown"


def _type_name(drive: int) -> str:
    return {
        DRIVE_REMOVABLE: "Removable",
        DRIVE_FIXED: "Fixed",
        DRIVE_REMOTE: "Network",
        DRIVE_RAMDISK: "RAM disk",
    }.get(drive, "Unknown")


def get_disks() -> list[Disk]:
    hardware: dict[str, dict] = {}
    try:
        hardware = _query_hardware()
    except (OSError, json.JSONDecodeError, ValueError, subprocess.SubprocessError):
        hardware = {}
    disks: list[Disk] = []
    for letter in drive_letters():
        kind = drive_type(letter)
        if kind in (0, 1, DRIVE_CDROM):
            continue
        root = f"{letter}:\\"
        try:
            usage = shutil.disk_usage(root)
        except OSError:
            continue
        info = hardware.get(letter, {})
        disk = info.get("disk", {})
        physical = info.get("physical", {})
        health = _enum_text(physical.get("HealthStatus") or disk.get("HealthStatus"), {0: "Healthy", 1: "Warning", 2: "Unhealthy"})
        bus = _enum_text(disk.get("BusType"), BUS_NAMES)
        model = str(physical.get("FriendlyName") or disk.get("FriendlyName") or "")
        disks.append(
            Disk(
                letter=letter,
                label=volume_label(letter),
                total=int(usage.total),
                used=int(usage.used),
                free=int(usage.free),
                media=_media(kind, physical, disk),
                health=health,
                bus=bus,
                model=model,
                drive_type=_type_name(kind),
            )
        )
    return disks


def data_drive_letters() -> list[str]:
    """Fixed and removable drives. Network locations are chosen as folders instead."""
    letters = []
    for letter in drive_letters():
        if drive_type(letter) in (DRIVE_FIXED, DRIVE_REMOVABLE, DRIVE_RAMDISK):
            letters.append(letter)
    return letters


def known_folders() -> list[tuple[str, str]]:
    mapping = (
        ("Desktop", "Desktop"),
        ("Personal", "Documents"),
        ("{374DE290-123F-4565-9164-39C4925E467B}", "Downloads"),
        ("My Pictures", "Pictures"),
        ("My Video", "Videos"),
        ("My Music", "Music"),
    )
    found: list[tuple[str, str]] = []
    try:
        import winreg

        key_path = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            for value_name, fallback in mapping:
                path = ""
                try:
                    raw, _typ = winreg.QueryValueEx(key, value_name)
                    path = os.path.expandvars(str(raw))
                except OSError:
                    path = ""
                if not path or not os.path.isdir(path):
                    path = os.path.join(os.path.expanduser("~"), fallback)
                if os.path.isdir(path):
                    found.append((fallback, os.path.abspath(path)))
    except OSError:
        home = os.path.expanduser("~")
        for _value, fallback in mapping:
            path = os.path.join(home, fallback)
            if os.path.isdir(path):
                found.append((fallback, path))
    unique: list[tuple[str, str]] = []
    seen: set[str] = set()
    for label, path in found:
        marker = os.path.normcase(path)
        if marker in seen:
            continue
        seen.add(marker)
        unique.append((label, path))
    return unique
