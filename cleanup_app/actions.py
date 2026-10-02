"""Deletes for files the user ticked, plus the dashboard summary."""

from __future__ import annotations

import os
import stat
from concurrent.futures import ThreadPoolExecutor

from cleanup_app.diskinfo import get_disks
from cleanup_app.junk import measure_tree
from cleanup_app.models import CleanReport, DashboardData, FileHit, Finding
from cleanup_app.recycle import query_recycle_bin, send_to_recycle
from cleanup_app.safety import assert_personal_file
from cleanup_app.services_scan import scan_services
from cleanup_app.startup_scan import scan_startup
from cleanup_app.util import GB, MB, format_size


def delete_personal_files(hits: list[FileHit], roots: list[str], recycle: bool, cancel, progress) -> tuple[CleanReport, list[str]]:
    report = CleanReport()
    removed: list[str] = []
    chosen = [hit for hit in hits if hit.checked and os.path.isfile(hit.path)]
    approved: list[FileHit] = []
    for hit in chosen:
        if cancel.is_set():
            break
        try:
            assert_personal_file(hit.path, roots or [hit.root])
            approved.append(hit)
        except PermissionError as exc:
            report.skipped += 1
            report.errors.append(str(exc))
    if recycle:
        batch: list[FileHit] = []
        for hit in approved:
            batch.append(hit)
            if len(batch) == 40:
                _recycle_batch(batch, report, removed, progress)
                batch = []
            if cancel.is_set():
                break
        if batch and not cancel.is_set():
            _recycle_batch(batch, report, removed, progress)
    else:
        for hit in approved:
            if cancel.is_set():
                break
            progress(f"Deleting {os.path.basename(hit.path)}")
            try:
                os.chmod(hit.path, stat.S_IWRITE)
            except OSError:
                pass
            try:
                os.remove(hit.path)
            except OSError as exc:
                report.skipped += 1
                report.errors.append(f"{hit.path}: {exc}")
                continue
            report.deleted_files += 1
            report.deleted_bytes += hit.size
            removed.append(hit.path)
    if cancel.is_set():
        report.notes.append("Stopped early. Untouched files are still in the list.")
    return report, removed


def _recycle_batch(batch: list[FileHit], report: CleanReport, removed: list[str], progress) -> None:
    progress(f"Moving {len(batch)} files to the Recycle Bin…")
    results = send_to_recycle([hit.path for hit in batch])
    by_path = {os.path.normcase(item.get("Path", "")): item for item in results}
    for hit in batch:
        item = by_path.get(os.path.normcase(hit.path))
        if item and item.get("Ok") and not os.path.exists(hit.path):
            report.deleted_files += 1
            report.deleted_bytes += hit.size
            removed.append(hit.path)
        else:
            report.skipped += 1
            message = (item or {}).get("Error") or "Could not move it to the Recycle Bin."
            report.errors.append(f"{hit.path}: {message}")


def _findings(data: DashboardData) -> list[Finding]:
    findings: list[Finding] = []
    for disk in data.disks:
        if disk.total <= 0:
            continue
        free_ratio = disk.free / disk.total
        health = (disk.health or "").lower()
        if health and health not in {"healthy", "ok"}:
            findings.append(
                Finding(
                    f"{disk.letter}: reports {disk.health}",
                    "Copy anything important off this disk before you delete files from it.",
                    "warn",
                    "dashboard",
                )
            )
        if free_ratio < 0.10 and disk.drive_type == "Fixed":
            where = "hard drive" if disk.media == "HDD" else "drive"
            findings.append(
                Finding(
                    f"{disk.letter}: is nearly full",
                    f"This {where} has {format_size(disk.free)} free of {format_size(disk.total)}. A full disk makes installs, updates, and browsing slower.",
                    "warn",
                    "files",
                )
            )
        elif disk.drive_type == "Fixed" and disk.free < 15 * GB:
            findings.append(
                Finding(
                    f"{disk.letter}: has little free space",
                    f"{format_size(disk.free)} free of {format_size(disk.total)}. Windows wants some room for updates and temporary files.",
                    "warn",
                    "files",
                )
            )
        elif disk.media == "HDD" and disk.drive_type == "Fixed" and free_ratio < 0.20:
            findings.append(
                Finding(
                    f"{disk.letter}: is a filling hard drive",
                    f"{format_size(disk.free)} free of {format_size(disk.total)}. Hard drives slow down as they fill up. Clearing old files here helps more than clearing an SSD.",
                    "warn",
                    "files",
                )
            )
    if data.recycle_bytes >= 500 * MB:
        findings.append(
            Finding(
                "Recycle Bin is holding a lot",
                f"{format_size(data.recycle_bytes)} across {data.recycle_items} items. Emptying it frees that space immediately.",
                "info",
                "junk",
            )
        )
    if data.temp_bytes >= 300 * MB:
        findings.append(
            Finding(
                "Temporary files have piled up",
                f"Your temp folder is about {format_size(data.temp_bytes)}. These are safe to clear.",
                "info",
                "junk",
            )
        )
    if data.startup_broken:
        findings.append(
            Finding(
                "A startup item points at a missing file",
                f"{data.startup_broken} startup entry no longer has a program to launch.",
                "info",
                "startup",
            )
        )
    elif data.startup_count >= 12:
        findings.append(
            Finding(
                "A lot of programs start with Windows",
                f"{data.startup_count} startup items. Each one adds time before the desktop is idle.",
                "info",
                "startup",
            )
        )
    for note in data.service_notes[:3]:
        findings.append(Finding("Background service", note, "info", "services"))
    if data.windows_old:
        findings.append(
            Finding(
                "A previous Windows installation is still here",
                "Windows.old can be tens of gigabytes. Remove it from Settings, System, Storage, Temporary files. It is not a cache.",
                "warn",
                "dashboard",
            )
        )
    if data.hiber_bytes >= 2 * GB:
        findings.append(
            Finding(
                "Hibernation is using disk space",
                f"hiberfil.sys is {format_size(data.hiber_bytes)}. Turning hibernation off is a Windows power setting. This app will not delete that file.",
                "info",
                "dashboard",
            )
        )
    if data.page_bytes >= 8 * GB:
        findings.append(
            Finding(
                "The page file is large",
                f"pagefile.sys is {format_size(data.page_bytes)}. Windows uses it as extra memory. Leave it alone.",
                "info",
                "dashboard",
            )
        )
    if not findings:
        findings.append(
            Finding(
                "Nothing urgent",
                "Drives have room, and there is no huge recycle bin or temp pile. You can still look for old installers and extra startup apps.",
                "info",
                "files",
            )
        )
    return findings


def collect_snapshot(cancel, progress) -> DashboardData:
    warnings: list[str] = []
    progress("Checking drives…")
    # The drive query waits on PowerShell for a couple of seconds, so it runs
    # alongside the other checks instead of before them.
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending_disks = None if cancel.is_set() else pool.submit(get_disks)
        data = _collect_rest(cancel, progress, warnings)
        try:
            data.disks = pending_disks.result() if pending_disks is not None else []
        except (OSError, RuntimeError, ValueError) as exc:
            data.disks = []
            warnings.append(str(exc))
    data.findings = _findings(data)
    return data


def _collect_rest(cancel, progress, warnings: list[str]) -> DashboardData:
    progress("Checking the Recycle Bin…")
    try:
        recycle_bytes, recycle_items = query_recycle_bin()
    except OSError:
        recycle_bytes, recycle_items = 0, 0
        warnings.append("Could not read the Recycle Bin.")
    progress("Checking startup programs…")
    try:
        startup = [] if cancel.is_set() else scan_startup(cancel, progress)
    except Exception as exc:
        startup = []
        warnings.append(f"Startup check failed: {exc}")
    enabled = [item for item in startup if item.enabled]
    progress("Checking services…")
    notes: list[str] = []
    services = []
    try:
        services = [] if cancel.is_set() else scan_services(cancel, progress, sample_cpu=False)
        interesting = [
            service
            for service in services
            if service.status == "running"
            and service.start_type.startswith("automatic")
            and not service.critical
            and not service.security
            and (service.publisher_kind == "third-party" or service.optional_windows)
        ]
        interesting.sort(key=lambda service: (service.publisher_kind != "third-party", -(0 if service.shared_process else service.memory)))
        for service in interesting[:3]:
            extra = ""
            if not service.shared_process and service.memory >= 80 * MB:
                extra = f" It is using {format_size(service.memory)}."
            notes.append(f"{service.display_name} starts with Windows and is running.{extra}")
    except Exception as exc:
        warnings.append(f"Service check failed: {exc}")
    progress("Measuring temporary files…")
    temp_bytes = 0
    temp_files = 0
    temp = os.environ.get("TEMP") or ""
    if temp and os.path.isdir(temp) and not cancel.is_set():
        try:
            temp_bytes, temp_files, _skipped = measure_tree(temp, cancel, progress)
        except OSError as exc:
            warnings.append(str(exc))
    progress("Checking large system files…")
    windir = os.environ.get("WINDIR", r"C:\Windows")
    drive = os.path.splitdrive(os.path.abspath(windir))[0] + "\\"
    hiber_bytes = _file_size(os.path.join(drive, "hiberfil.sys"))
    page_bytes = _file_size(os.path.join(drive, "pagefile.sys"))
    windows_old = os.path.isdir(os.path.join(drive, "Windows.old"))
    return DashboardData(
        disks=[],
        recycle_bytes=recycle_bytes,
        recycle_items=recycle_items,
        temp_bytes=temp_bytes,
        temp_files=temp_files,
        startup_count=len(enabled),
        startup_broken=sum(1 for item in enabled if item.missing),
        service_notes=notes,
        findings=[],
        warnings=warnings,
        hiber_bytes=hiber_bytes,
        page_bytes=page_bytes,
        windows_old=windows_old,
        startup_items=startup,
        service_items=services,
    )


def _file_size(path: str) -> int:
    try:
        return os.path.getsize(path) if os.path.isfile(path) else 0
    except OSError:
        return 0
