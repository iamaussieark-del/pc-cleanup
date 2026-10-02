"""Build a local PC report and send it to an OpenAI-compatible chat API."""

from __future__ import annotations

import json
import os
import platform
import time
import urllib.error
import urllib.request
from collections import Counter
from urllib.parse import urlparse

import psutil

from cleanup_app.diskinfo import get_disks, known_folders
from cleanup_app.recycle import query_recycle_bin
from cleanup_app.registry_scan import scan_registry
from cleanup_app.services_scan import scan_services
from cleanup_app.startup_scan import scan_startup
from cleanup_app.util import app_dir, extract_exe, format_size

PROVIDERS = {
    "ChatGPT (OpenAI)": ("https://api.openai.com/v1", "gpt-4o-mini"),
    "OpenRouter": ("https://openrouter.ai/api/v1", "openai/gpt-4o-mini"),
    "LM Studio on this PC": ("http://127.0.0.1:1234/v1", "local-model"),
}

SYSTEM_PROMPT = """You are analyzing a Windows PC for the person who owns it.
They collected this report with a local cleanup app and chose to send it to you.

Write a direct, specific report in plain sentences. Use these sections:
1. Do this first
2. Memory
3. Disk space
4. Programs that start at sign-in
5. Leave these alone

Rules:
- Only mention programs, paths, and numbers that appear in the report.
- If a startup program describes itself as Windows but lives outside the Windows folder, say so plainly and put it first.
- A full SSD, a nearly idle service, and an old icon-cache entry are different kinds of issues. Rank them that way.
- Hibernation (hiberfil.sys) and the page file (pagefile.sys) belong to Windows. Do not tell the user to delete them.
- Do not recommend a registry cleaner, a defrag of an SSD, or a RAM booster.
- Do not invent file contents. Nearby filenames are names only.
- Keep it under about 700 words.
"""


def settings_path() -> str:
    return os.path.join(app_dir(), "ai_settings.json")


def load_ai_settings() -> dict:
    try:
        with open(settings_path(), encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def save_ai_settings(provider: str, base_url: str, model: str, api_key: str) -> None:
    payload = {
        "provider": provider,
        "base_url": base_url.strip(),
        "model": model.strip(),
        "api_key": api_key.strip(),
    }
    path = settings_path()
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
    os.replace(temporary, path)


def validate_endpoint(base_url: str) -> str:
    parsed = urlparse(base_url.strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "https":
        return base_url.strip().rstrip("/")
    if parsed.scheme == "http" and host in {"127.0.0.1", "localhost"}:
        return base_url.strip().rstrip("/")
    raise ValueError("Use an https address, or http://127.0.0.1 for a model running on this PC.")


def _sample_folder(path: str, seconds: float = 2.5) -> tuple[int, int, bool]:
    total = 0
    files = 0
    partial = False
    started = time.time()
    for root, dirnames, filenames in os.walk(path):
        depth = root[len(os.path.abspath(path)) :].count(os.sep)
        if depth > 2:
            dirnames[:] = []
            continue
        dirnames[:] = [name for name in dirnames if name.lower() not in {"node_modules", ".git"}]
        for name in filenames:
            try:
                total += os.path.getsize(os.path.join(root, name))
                files += 1
            except OSError:
                continue
        if time.time() - started > seconds:
            partial = True
            break
    return total, files, partial


def _siblings(folder: str) -> list[str]:
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    return names[:24]


def _startup_notes(command: str, description: str, name: str) -> tuple[list[str], list[str]]:
    notes: list[str] = []
    siblings: list[str] = []
    exe = extract_exe(command)
    if not exe:
        return notes, siblings
    windir = os.path.normcase(os.environ.get("WINDIR", r"C:\Windows"))
    outside = not os.path.normcase(exe).startswith(windir)
    blob = f"{description} {name}".lower()
    looks_like_windows = any(word in blob for word in ("windows", "system cache", "microsoft corporation"))
    folder = os.path.dirname(exe)
    if outside and looks_like_windows:
        notes.append("This startup item describes itself as Windows, but the program is outside the Windows folder.")
        siblings = _siblings(folder)
    if not os.path.exists(exe):
        notes.append("The file this entry launches is missing.")
    return notes, siblings


def _saved(collected: dict | None, key: str):
    if not collected:
        return None
    return collected.get(key)


def build_report(cancel, progress, collected: dict | None = None) -> str:
    saved_space = _saved(collected, "space")
    saved_dashboard = _saved(collected, "dashboard")
    if saved_space is not None or saved_dashboard is not None:
        progress("Writing the report from the scan already on this page…")
    else:
        progress("Reading memory, drives, and startup…")
    memory = psutil.virtual_memory()
    lines = [
        "PC Cleanup local report",
        f"Collected: {time.strftime('%Y-%m-%d %H:%M')}",
        "",
        "Computer",
        f"- System: {platform.platform()}",
        f"- CPU: {psutil.cpu_count(logical=False) or '?'} cores, {psutil.cpu_count() or '?'} threads",
        f"- RAM: {format_size(memory.total)} total, {memory.percent:.0f}% in use, {format_size(memory.available)} available",
        f"- Uptime: {int((time.time() - psutil.boot_time()) / 3600)} hours since boot",
        "",
        "Drives",
    ]
    disks = list(saved_dashboard.disks) if saved_dashboard is not None else None
    if disks is None:
        try:
            disks = get_disks()
        except (OSError, RuntimeError, ValueError):
            disks = []
    if not disks:
        lines.append("- Drives could not be read.")
    for disk in disks:
        label = f" ({disk.label})" if disk.label else ""
        health = f", {disk.health}" if disk.health else ""
        lines.append(
            f"- {disk.letter}:{label} {disk.media} {disk.drive_type}{health}, "
            f"{format_size(disk.free)} free of {format_size(disk.total)}"
            + (f", {disk.model}" if disk.model else "")
        )
    if saved_dashboard is not None:
        lines.append(f"- Recycle Bin: {format_size(saved_dashboard.recycle_bytes)}, {saved_dashboard.recycle_items} items")
        lines.append(f"- Temp folder: {format_size(saved_dashboard.temp_bytes)}, {saved_dashboard.temp_files} files")
    else:
        try:
            recycle_bytes, recycle_items = query_recycle_bin()
            lines.append(f"- Recycle Bin: {format_size(recycle_bytes)}, {recycle_items} items")
        except OSError:
            lines.append("- Recycle Bin could not be read.")

    if cancel.is_set():
        return "\n".join(lines)

    lines.extend(["", "Startup programs"])
    startup = _saved(collected, "startup")
    if startup is None and saved_dashboard is not None:
        startup = saved_dashboard.startup_items
    if startup is None:
        progress("Reading startup programs…")
        try:
            startup = scan_startup(cancel, progress)
        except (OSError, RuntimeError, ValueError):
            startup = []
    enabled = [item for item in startup if item.enabled]
    lines.append(f"- {len(enabled)} enabled")
    for item in enabled[:40]:
        notes, siblings = _startup_notes(item.command, item.description, item.name)
        lines.append(f"- {item.name}")
        if item.description:
            lines.append(f"  Description: {item.description}")
        lines.append(f"  Command: {item.command}")
        lines.append(f"  Where: {item.location}")
        for note in notes:
            lines.append(f"  Note: {note}")
        if siblings:
            lines.append("  Nearby names: " + ", ".join(siblings))

    if cancel.is_set():
        return "\n".join(lines)

    lines.extend(["", "Services"])
    services = _saved(collected, "services")
    if services is None and saved_dashboard is not None:
        services = saved_dashboard.service_items
    if services is None:
        progress("Reading services and open programs…")
        try:
            services = scan_services(cancel, progress, sample_cpu=False)
        except (OSError, RuntimeError, ValueError):
            services = []
    counts = Counter(service.impact for service in services)
    lines.append(f"- {len(services)} services. Counts by impact: " + ", ".join(f"{name} {count}" for name, count in counts.most_common()))
    interesting = [
        service
        for service in services
        if service.status == "running"
        and service.start_type.startswith("automatic")
        and (service.publisher_kind == "third-party" or service.optional_windows or service.impact in {"High memory", "Medium memory", "High CPU"})
    ]
    interesting.sort(key=lambda service: service.memory, reverse=True)
    for service in interesting[:20]:
        shared = " (shared process)" if service.shared_process else ""
        lines.append(
            f"- {service.display_name} [{service.name}] {service.start_type}, {service.impact}, "
            f"{format_size(service.memory)}{shared}"
        )
        if service.advice and service.publisher_kind != "windows":
            lines.append(f"  Note: {service.advice[:220]}")

    lines.extend(["", "Open programs by memory"])
    totals: dict[str, int] = {}
    for proc in psutil.process_iter(["name", "memory_info"]):
        try:
            info = proc.info
            name = info.get("name") or "unknown"
            mem = info.get("memory_info")
            totals[name] = totals.get(name, 0) + (mem.rss if mem else 0)
        except (psutil.Error, OSError):
            continue
    for name, size in sorted(totals.items(), key=lambda item: item[1], reverse=True)[:12]:
        lines.append(f"- {name}: {format_size(size)}")

    _append_space(lines, saved_space, cancel, progress)
    _append_junk(lines, _saved(collected, "junk"))
    _append_files(lines, _saved(collected, "files"))
    _append_performance(lines, _saved(collected, "performance"))
    _append_drivers(lines, _saved(collected, "drivers"))

    lines.extend(["", "Registry leftovers"])
    issues = _saved(collected, "registry")
    if issues is None:
        progress("Checking registry leftovers…")
        try:
            issues = scan_registry(cancel, progress)
        except (OSError, RuntimeError, ValueError):
            issues = []
    kinds = Counter(issue.kind for issue in issues)
    if not issues:
        lines.append("- None found in startup, uninstall, application paths, shared files, fonts, help files, installer folders, or icon names.")
    else:
        lines.append("- " + ", ".join(f"{name}: {count}" for name, count in kinds.items()))
        shown = 0
        for issue in issues:
            if issue.kind == "mui":
                continue
            advice = f" Recommended: {issue.recommendation}." if issue.recommendation else ""
            fix = f" {issue.fix}" if issue.fix else ""
            lines.append(f"- {issue.problem}: {issue.detail}.{advice}{fix}")
            shown += 1
            if shown >= 16:
                lines.append("- Further broken entries were omitted from this report.")
                break

    lines.extend(
        [
            "",
            "What this report leaves out",
            "- File contents, passwords, browser history, and message text.",
            "- A folder map of the whole drive." if saved_space is None else "- Nothing was deleted to produce this report.",
        ]
    )
    return "\n".join(lines)


def _append_space(lines: list[str], report, cancel, progress) -> None:
    if report is None:
        lines.extend(["", "Personal folders (sampled two levels deep, so these are minimums)"])
        for label, path in known_folders():
            if cancel.is_set():
                break
            progress(f"Sampling {label}…")
            size, count, partial = _sample_folder(path)
            suffix = ", sample stopped early" if partial else ""
            lines.append(f"- {label}: {format_size(size)}, {count} files seen{suffix}")
        return
    lines.extend(["", "Where the space is"])
    lines.append(f"- Measured {format_size(report.measured)} across {report.files_seen} files.")
    for drive in report.drives:
        letter = drive.root[:2]
        for item in drive.slices:
            lines.append(f"- {letter} {item.name}: {format_size(item.size)}")
    for title, detail, _level in getattr(report, "explanations", []) or []:
        lines.append(f"- {title}: {detail}")
    if report.user_folders:
        lines.append("- Your folders: " + ", ".join(f"{item.name} {format_size(item.size)}" for item in report.user_folders[:8]))
    if report.apps:
        lines.append("- Biggest installed apps:")
        for app in report.apps[:12]:
            lines.append(f"  {app.name}: {format_size(app.size)}" + (f" ({app.publisher})" if app.publisher else ""))
    if report.unknown_apps:
        lines.append(f"- {report.unknown_apps} installed programs did not report a size to Windows.")
    if report.files:
        lines.append("- Biggest files:")
        for item in report.files[:12]:
            lines.append(f"  {item.path}: {format_size(item.size)}")


def _append_junk(lines: list[str], junk) -> None:
    lines.extend(["", "Caches"])
    if junk is None:
        lines.append("- Caches were not scanned in this session.")
        return
    visible = [item for item in junk if item.size > 0]
    lines.append(f"- {len(visible)} cache groups, {format_size(sum(item.size for item in visible))} total.")
    for item in sorted(visible, key=lambda entry: entry.size, reverse=True)[:12]:
        lines.append(f"- {item.name}: {format_size(item.size)} ({item.safety})")


def _append_files(lines: list[str], result) -> None:
    lines.extend(["", "Old personal files"])
    if result is None:
        lines.append("- Old personal files were not scanned in this session.")
        return
    lines.append(f"- {result.matched} files matched the old-file rules.")
    for hit in result.hits[:12]:
        lines.append(f"- {hit.path}: {format_size(hit.size)}, {hit.verdict}")


def _append_performance(lines: list[str], report) -> None:
    if report is None:
        return
    lines.extend(["", "Performance check"])
    lines.append(
        f"- Health {report.score}/100. Processor {report.cpu_percent:.0f}% busy, memory {report.ram_percent:.0f}% used, "
        f"on for {report.uptime_hours:.0f} hours, power plan {report.power_plan}."
    )
    for finding in report.findings:
        lines.append(f"- {finding.title}: {finding.detail}")
    for tweak in report.tweaks:
        lines.append(f"- Setting {tweak.name}: {tweak.state}" + (" (changing it is recommended)" if tweak.recommended else ""))


def _append_drivers(lines: list[str], report) -> None:
    if report is None:
        return
    lines.extend(["", "Drivers"])
    lines.append(f"- {len(report.devices)} devices, {len(report.problems)} with problems.")
    for device in report.problems[:8]:
        lines.append(f"- Problem: {device.device}. {device.problem}")
    if report.checked_online:
        lines.append(f"- Newer drivers found for {len(report.outdated)} devices (Windows Update, Microsoft Update Catalog, and NVIDIA were checked).")
        for device in report.outdated[:12]:
            best = device.best
            lines.append(f"  {device.device}: {device.version} ({device.date}) -> {device.shown_version(best)} ({best.date}) from {best.source}")
    for device in report.devices:
        if device.is_gpu:
            lines.append(f"- Graphics: {device.device}, driver {device.version} from {device.date}.")
    if report.packages:
        total = sum(item.size for item in report.packages)
        lines.append(f"- {len(report.packages)} unused older driver packages take {format_size(total)}.")


def _message_text(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("text"):
                parts.append(str(block["text"]))
        return "\n".join(parts).strip()
    return ""


def ask_model(base_url: str, api_key: str, model: str, report: str, progress) -> str:
    endpoint = validate_endpoint(base_url)
    key = api_key.strip()
    chosen = model.strip()
    if not key:
        raise ValueError("Paste an API key first. ChatGPT's website login is separate from an API key.")
    if not chosen:
        raise ValueError("Type a model name, such as gpt-4o-mini.")
    if not report.strip():
        raise ValueError("Build the local report first.")
    progress("Sending the report…")
    url = endpoint + "/chat/completions"
    payload = {
        "model": chosen,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": report},
        ],
    }
    raw = _post_chat(url, key, payload)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("The AI service returned something that was not a chat reply.") from exc
    try:
        text = _message_text(data["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("The AI service reply did not include an answer.") from exc
    if not text:
        raise RuntimeError("The AI service returned an empty answer.")
    return text


def _post_chat(url: str, api_key: str, payload: dict) -> str:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(_http_message(exc.code, detail)) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach the AI service. {exc.reason}") from exc
    except TimeoutError as exc:
        raise RuntimeError("The AI service took too long to answer.") from exc


def _http_message(code: int, detail: str) -> str:
    extracted = ""
    try:
        parsed = json.loads(detail)
        error = parsed.get("error")
        if isinstance(error, dict):
            extracted = str(error.get("message") or "")
        elif isinstance(error, str):
            extracted = error
    except json.JSONDecodeError:
        extracted = detail.strip()[:300]
    if code in {401, 403}:
        return "The API key was rejected. Check the key, and that the provider matches the address."
    if code == 404:
        return "That model or address was not found. " + extracted
    if code == 429:
        return "The AI service is rate limiting this key. Wait a minute and try again."
    return extracted or f"The AI service returned HTTP {code}."
