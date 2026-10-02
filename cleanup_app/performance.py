"""Performance check and a small set of honest, reversible speed-ups.

Everything here is a normal Windows setting that can be put back with one
click. There is no "RAM cleaner": emptying memory makes Windows read it all
back from disk, which is slower. The biggest real wins on most PCs are fewer
startup apps, a restart now and then, enough free disk space, and closing the
app that is actually using the CPU or memory. The check points at those.
"""

from __future__ import annotations

import ctypes
import os
import re
import shutil
import subprocess
import time
import winreg
from ctypes import wintypes
from dataclasses import dataclass, field

import psutil

from cleanup_app import procsnap
from cleanup_app.models import Finding
from cleanup_app.util import GB, MB, decode_output, format_size, is_admin, load_state, run_hidden, save_state

BALANCED = "381b4222-f694-41f0-9685-ff5bb260df2e"
POWER_SAVER = "a1841308-3541-4fab-bc81-f71556f20b4a"
HIGH_PERFORMANCE = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"
ULTIMATE = "e9a42b02-d5df-448d-aa00-03f14749eb61"
OVERLAY_BEST_PERFORMANCE = "ded574b5-45a0-4f42-8737-46345c09c238"
OVERLAY_BALANCED = "00000000-0000-0000-0000-000000000000"
OVERLAY_BEST_EFFICIENCY = "961cc777-2547-4f9d-8174-7d86181b8a7a"
# Plan names can hold brackets of their own, e.g. "Ultimate Performance (ExitLag)",
# so the name runs to the last ")" and the active "*" comes after it.
GUID_PATTERN = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\s*\((.*)\)\s*(\*?)\s*$", re.IGNORECASE)

PERSONALIZE = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
GAME_BAR = r"Software\Microsoft\GameBar"
DEFRAG_TASK = r"\Microsoft\Windows\Defrag\ScheduledDefrag"

SPI_GETANIMATION = 0x0048
SPI_SETANIMATION = 0x0049
SPIF_UPDATEINIFILE = 0x01
SPIF_SENDCHANGE = 0x02
HWND_BROADCAST = 0xFFFF
WM_SETTINGCHANGE = 0x001A
# (get, set, name) for the animation switches in "Adjust for best performance".
ANIMATIONS = (
    (0x1042, 0x1043, "client_area"),
    (0x1002, 0x1003, "menu"),
    (0x1004, 0x1005, "combo_box"),
    (0x1006, 0x1007, "smooth_scroll"),
    (0x1014, 0x1015, "selection_fade"),
    (0x1016, 0x1017, "tooltip"),
)

# Ending these would sign you out, break the desktop, or turn off protection.
PROTECTED = {
    "system",
    "system idle process",
    "registry",
    "memory compression",
    "secure system",
    "smss.exe",
    "csrss.exe",
    "wininit.exe",
    "winlogon.exe",
    "services.exe",
    "lsass.exe",
    "lsaiso.exe",
    "svchost.exe",
    "fontdrvhost.exe",
    "dwm.exe",
    "explorer.exe",
    "sihost.exe",
    "ctfmon.exe",
    "taskhostw.exe",
    "runtimebroker.exe",
    "searchhost.exe",
    "searchindexer.exe",
    "startmenuexperiencehost.exe",
    "shellexperiencehost.exe",
    "textinputhost.exe",
    "lockapp.exe",
    "userinit.exe",
    "conhost.exe",
    "dllhost.exe",
    "audiodg.exe",
    "spoolsv.exe",
    "wudfhost.exe",
    "smartscreen.exe",
    "securityhealthsystray.exe",
    "securityhealthservice.exe",
    "msmpeng.exe",
    "nissrv.exe",
    "mpdefendercoreservice.exe",
    "applicationframehost.exe",
    "systemsettings.exe",
    "crossdeviceresume.exe",
}


@dataclass
class PerfTweak:
    id: str
    name: str
    detail: str
    state: str
    recommended: bool
    action: str
    in_boost: bool = False
    can_undo: bool = False
    available: bool = True


@dataclass
class ProcessGroup:
    name: str
    count: int
    memory: int
    cpu: float
    pids: list[int]
    can_end: bool
    note: str = ""


@dataclass
class PerfReport:
    cpu_percent: float
    cpu_threads: int
    ram_total: int
    ram_used: int
    ram_percent: float
    uptime_hours: float
    restart_pending: bool
    has_battery: bool
    on_battery: bool
    power_plan: str
    tweaks: list[PerfTweak]
    processes: list[ProcessGroup]
    findings: list[Finding]
    score: int
    checked_at: float = field(default_factory=time.time)


def check_performance(cancel, progress, startup_items=None, sample_seconds: float = 1.0) -> PerfReport:
    progress("Measuring CPU and memory…")
    usage = _process_groups(cancel, sample_seconds)
    memory = psutil.virtual_memory()
    threads = psutil.cpu_count() or 1
    cpu_total = min(100.0, usage[1])
    battery = _battery()
    progress("Reading power and display settings…")
    schemes, active = list_power_schemes()
    plan = schemes.get(active, "Unknown plan")
    tweaks = [
        _power_tweak(schemes, active, battery),
        _game_mode_tweak(),
        _animation_tweak(),
        _transparency_tweak(memory.total),
    ]
    backup = _backup()
    for tweak in tweaks:
        tweak.can_undo = tweak.id in backup
    uptime = (time.time() - psutil.boot_time()) / 3600
    pending = restart_pending()
    progress("Looking for anything slowing this PC down…")
    if startup_items is None:
        try:
            from cleanup_app.startup_scan import scan_startup

            startup_items = scan_startup(cancel, progress)
        except Exception:
            startup_items = []
    findings, score = _findings(memory, cpu_total, uptime, pending, startup_items, tweaks, usage[0], battery)
    return PerfReport(
        cpu_percent=cpu_total,
        cpu_threads=threads,
        ram_total=memory.total,
        ram_used=memory.total - memory.available,
        ram_percent=memory.percent,
        uptime_hours=uptime,
        restart_pending=pending,
        has_battery=battery[0],
        on_battery=battery[1],
        power_plan=plan,
        tweaks=tweaks,
        processes=usage[0],
        findings=findings,
        score=score,
    )


def live_usage() -> tuple[float, float, int, int]:
    """CPU percent since the last call, memory percent, memory used, memory total."""
    memory = psutil.virtual_memory()
    return psutil.cpu_percent(None), memory.percent, memory.total - memory.available, memory.total


# ---------------------------------------------------------------- processes


def _process_groups(cancel, seconds: float) -> tuple[list[ProcessGroup], float]:
    sampled = procsnap.cpu_percent(seconds, cancel)
    if sampled is None:
        return _process_groups_psutil(), 0.0
    snap, per_pid = sampled
    threads = psutil.cpu_count() or 1
    own = os.getpid()
    own_session = snap[own].session if own in snap else -1
    groups: dict[str, ProcessGroup] = {}
    total_cpu = 0.0
    for pid, sample in snap.items():
        if pid in (0, own) or not sample.name:
            continue
        cpu = per_pid.get(pid, 0.0) / threads
        if sample.name.lower() not in {"system idle process"}:
            total_cpu += cpu
        key = sample.name.lower()
        group = groups.get(key)
        if group is None:
            group = groups[key] = ProcessGroup(sample.name, 0, 0, 0.0, [], True)
        group.count += 1
        group.memory += sample.private_working_set
        group.cpu += cpu
        group.pids.append(pid)
        if sample.session == 0 or sample.session != own_session:
            group.can_end = False
            group.note = "Runs as a Windows service or for another user."
    for key, group in groups.items():
        if key in PROTECTED:
            group.can_end = False
            group.note = "Part of Windows. Ending it would break the desktop or sign you out."
    ordered = sorted(groups.values(), key=lambda item: (item.memory, item.cpu), reverse=True)
    return ordered, total_cpu


def _process_groups_psutil() -> list[ProcessGroup]:
    groups: dict[str, ProcessGroup] = {}
    for proc in psutil.process_iter(["name", "memory_info"]):
        try:
            name = proc.info["name"] or ""
            used = proc.info["memory_info"].rss if proc.info["memory_info"] else 0
        except (psutil.Error, OSError):
            continue
        if not name or proc.pid == os.getpid():
            continue
        group = groups.setdefault(name.lower(), ProcessGroup(name, 0, 0, 0.0, [], name.lower() not in PROTECTED))
        group.count += 1
        group.memory += used
        group.pids.append(proc.pid)
    return sorted(groups.values(), key=lambda item: item.memory, reverse=True)


def end_process_group(group: ProcessGroup) -> tuple[int, list[str]]:
    """Close every process in the group. Protected and other-user processes are refused."""
    if not group.can_end or group.name.lower() in PROTECTED:
        raise PermissionError(f"{group.name} is part of Windows or another user, so it is left running.")
    ended = 0
    errors: list[str] = []
    procs = []
    for pid in group.pids:
        if pid == os.getpid():
            continue
        try:
            proc = psutil.Process(pid)
            if proc.name().lower() != group.name.lower():
                continue
            proc.terminate()
            procs.append(proc)
        except psutil.NoSuchProcess:
            continue
        except (psutil.Error, OSError) as exc:
            errors.append(f"{group.name} ({pid}): {exc}")
    _gone, alive = psutil.wait_procs(procs, timeout=4)
    ended = len(procs) - len(alive)
    for proc in alive:
        errors.append(f"{group.name} ({proc.pid}) did not close.")
    return ended, errors


# ---------------------------------------------------------------- power plan


def list_power_schemes() -> tuple[dict[str, str], str]:
    completed = run_hidden(["powercfg", "/list"], timeout=20)
    schemes: dict[str, str] = {}
    active = ""
    for line in (completed.stdout or "").splitlines():
        match = GUID_PATTERN.search(line)
        if not match:
            continue
        guid = match.group(1).lower()
        schemes[guid] = match.group(2).strip()
        if match.group(3):
            active = guid
    return schemes, active


def _is_fast_plan(guid: str, name: str) -> bool:
    if guid in {HIGH_PERFORMANCE, ULTIMATE}:
        return True
    lowered = name.lower()
    return "high performance" in lowered or "ultimate" in lowered


def _power_tweak(schemes: dict[str, str], active: str, battery: tuple[bool, bool]) -> PerfTweak:
    name = schemes.get(active, "Unknown plan")
    overlay = get_overlay()
    fast = _is_fast_plan(active, name)
    if active == BALANCED and overlay == OVERLAY_BEST_PERFORMANCE:
        state = "Balanced, power mode Best performance"
        fast = True
    elif active == BALANCED and overlay == OVERLAY_BEST_EFFICIENCY:
        state = "Balanced, power mode Best power efficiency"
    else:
        state = name
    has_battery, on_battery = battery
    if fast:
        detail = "Already set for speed."
        recommended = False
    elif active == POWER_SAVER or overlay == OVERLAY_BEST_EFFICIENCY:
        detail = "Power saver holds the processor back. Best performance lets it run at full speed when it needs to."
        recommended = not on_battery
    elif active == BALANCED:
        detail = "Balanced is fine for most PCs. Best performance keeps the processor ready, which helps games and heavy apps a little and uses more power."
        recommended = False
    else:
        detail = "A custom plan is active. It is left as you set it."
        recommended = False
    if has_battery:
        detail += " This PC has a battery, so faster settings shorten battery life."
    return PerfTweak("power", "Power plan", detail, state, recommended, "Use Best performance", in_boost=not fast and not has_battery, available=not fast)


def apply_power() -> str:
    schemes, active = list_power_schemes()
    _remember("power", {"scheme": active, "overlay": get_overlay()})
    target = next((guid for guid in schemes if guid == HIGH_PERFORMANCE), "")
    if not target:
        target = next((guid for guid, name in schemes.items() if _is_fast_plan(guid, name)), "")
    if not target:
        made = run_hidden(["powercfg", "/duplicatescheme", HIGH_PERFORMANCE], timeout=20)
        match = GUID_PATTERN.search(made.stdout or "")
        target = match.group(1).lower() if match else ""
    if target:
        completed = run_hidden(["powercfg", "/setactive", target], timeout=20)
        if completed.returncode == 0:
            return f"Power plan set to {list_power_schemes()[0].get(target, 'High performance')}."
    # Some laptops only allow Balanced. Its power mode slider still goes to Best performance.
    if set_overlay(OVERLAY_BEST_PERFORMANCE):
        return "Power mode set to Best performance."
    raise RuntimeError("Windows did not allow a faster power plan on this PC.")


def undo_power(saved: dict) -> str:
    scheme = str(saved.get("scheme") or "")
    if scheme:
        run_hidden(["powercfg", "/setactive", scheme], timeout=20)
    overlay = str(saved.get("overlay") or "")
    if overlay:
        set_overlay(overlay)
    return "Power plan put back."


class _GUID(ctypes.Structure):
    _fields_ = [("a", ctypes.c_ulong), ("b", ctypes.c_ushort), ("c", ctypes.c_ushort), ("d", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, text: str) -> "_GUID":
        raw = bytes.fromhex(text.replace("-", ""))
        guid = cls()
        guid.a = int.from_bytes(raw[0:4], "big")
        guid.b = int.from_bytes(raw[4:6], "big")
        guid.c = int.from_bytes(raw[6:8], "big")
        for index in range(8):
            guid.d[index] = raw[8 + index]
        return guid

    def text(self) -> str:
        tail = bytes(self.d).hex()
        return f"{self.a:08x}-{self.b:04x}-{self.c:04x}-{tail[:4]}-{tail[4:]}"


def get_overlay() -> str:
    """The Windows 11 power mode slider. It sits on top of the Balanced plan."""
    try:
        function = ctypes.WinDLL("powrprof").PowerGetEffectiveOverlayScheme
    except (OSError, AttributeError):
        return ""
    guid = _GUID()
    if function(ctypes.byref(guid)) != 0:
        return ""
    return guid.text()


def set_overlay(text: str) -> bool:
    try:
        function = ctypes.WinDLL("powrprof").PowerSetActiveOverlayScheme
    except (OSError, AttributeError):
        return False
    return function(_GUID.parse(text)) == 0


# ---------------------------------------------------------------- Game Mode


def _game_mode_on() -> bool:
    value = _read_dword(winreg.HKEY_CURRENT_USER, GAME_BAR, "AutoGameModeEnabled")
    return value is None or value == 1


def _game_mode_tweak() -> PerfTweak:
    on = _game_mode_on()
    detail = (
        "Game Mode is on. Windows holds back updates and background work while a game is open."
        if on
        else "Game Mode is off. Turning it on stops Windows Update installing or restarting during a game, and gives the game first call on the processor."
    )
    return PerfTweak("game_mode", "Game Mode", detail, "On" if on else "Off", not on, "Turn on", in_boost=not on, available=not on)


def apply_game_mode() -> str:
    _remember("game_mode", {"value": _read_dword(winreg.HKEY_CURRENT_USER, GAME_BAR, "AutoGameModeEnabled")})
    _write_dword(winreg.HKEY_CURRENT_USER, GAME_BAR, "AutoGameModeEnabled", 1)
    return "Game Mode is on."


def undo_game_mode(saved: dict) -> str:
    value = saved.get("value")
    if value is None:
        _delete_value(winreg.HKEY_CURRENT_USER, GAME_BAR, "AutoGameModeEnabled")
    else:
        _write_dword(winreg.HKEY_CURRENT_USER, GAME_BAR, "AutoGameModeEnabled", int(value))
    return "Game Mode put back."


# ---------------------------------------------------------------- animations


class _ANIMATIONINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("iMinAnimate", ctypes.c_int)]


def _spi():
    function = ctypes.windll.user32.SystemParametersInfoW
    function.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT]
    function.restype = wintypes.BOOL
    return function


def read_animations() -> dict[str, bool]:
    spi = _spi()
    state: dict[str, bool] = {}
    info = _ANIMATIONINFO(ctypes.sizeof(_ANIMATIONINFO), 0)
    if spi(SPI_GETANIMATION, ctypes.sizeof(info), ctypes.byref(info), 0):
        state["window"] = bool(info.iMinAnimate)
    for getter, _setter, name in ANIMATIONS:
        value = wintypes.BOOL()
        if spi(getter, 0, ctypes.byref(value), 0):
            state[name] = bool(value.value)
    return state


def write_animations(state: dict[str, bool]) -> None:
    spi = _spi()
    flags = SPIF_UPDATEINIFILE | SPIF_SENDCHANGE
    if "window" in state:
        info = _ANIMATIONINFO(ctypes.sizeof(_ANIMATIONINFO), 1 if state["window"] else 0)
        spi(SPI_SETANIMATION, ctypes.sizeof(info), ctypes.byref(info), flags)
    for _getter, setter, name in ANIMATIONS:
        if name in state:
            # These switches take the new value in the pointer argument itself.
            spi(setter, 0, ctypes.c_void_p(1 if state[name] else 0), flags)


def _animation_tweak() -> PerfTweak:
    state = read_animations()
    on = sum(1 for value in state.values() if value)
    detail = (
        "Window, menu, and list animations. Turning them off makes the desktop feel snappier, mostly on older PCs and built-in graphics. Your wallpaper and theme stay."
    )
    label = "Off" if on == 0 else ("On" if on == len(state) else f"{on} of {len(state)} on")
    return PerfTweak("animations", "Animations", detail, label, False, "Turn off", in_boost=on > 0, available=on > 0)


def apply_animations() -> str:
    _remember("animations", read_animations())
    write_animations({name: False for name in read_animations()})
    return "Animations are off."


def undo_animations(saved: dict) -> str:
    write_animations({str(name): bool(value) for name, value in saved.items()})
    return "Animations put back."


# ---------------------------------------------------------------- transparency


def _transparency_tweak(ram_total: int) -> PerfTweak:
    value = _read_dword(winreg.HKEY_CURRENT_USER, PERSONALIZE, "EnableTransparency")
    on = value is None or value == 1
    low_end = ram_total and ram_total <= 8 * GB
    detail = "See-through taskbar, Start, and window backgrounds. Turning it off saves a little graphics work."
    if low_end:
        detail += " Worth doing on a PC with 8 GB of memory or less."
    return PerfTweak("transparency", "Transparency effects", detail, "On" if on else "Off", bool(on and low_end), "Turn off", in_boost=bool(on and low_end), available=on)


def apply_transparency() -> str:
    _remember("transparency", {"value": _read_dword(winreg.HKEY_CURRENT_USER, PERSONALIZE, "EnableTransparency")})
    _write_dword(winreg.HKEY_CURRENT_USER, PERSONALIZE, "EnableTransparency", 0)
    _broadcast("ImmersiveColorSet")
    return "Transparency effects are off."


def undo_transparency(saved: dict) -> str:
    value = saved.get("value")
    if value is None:
        _delete_value(winreg.HKEY_CURRENT_USER, PERSONALIZE, "EnableTransparency")
    else:
        _write_dword(winreg.HKEY_CURRENT_USER, PERSONALIZE, "EnableTransparency", int(value))
    _broadcast("ImmersiveColorSet")
    return "Transparency effects put back."


# ---------------------------------------------------------------- apply / undo

APPLY = {
    "power": apply_power,
    "game_mode": apply_game_mode,
    "animations": apply_animations,
    "transparency": apply_transparency,
}
UNDO = {
    "power": undo_power,
    "game_mode": undo_game_mode,
    "animations": undo_animations,
    "transparency": undo_transparency,
}


def apply_tweak(tweak_id: str) -> str:
    if tweak_id not in APPLY:
        raise ValueError(f"Unknown setting: {tweak_id}")
    return APPLY[tweak_id]()


def undo_tweak(tweak_id: str) -> str:
    backup = _backup()
    if tweak_id not in backup:
        raise ValueError("There is nothing to put back for that setting.")
    message = UNDO[tweak_id](backup[tweak_id])
    state = load_state()
    state.setdefault("performance_backup", {}).pop(tweak_id, None)
    save_state(state)
    return message


def boost(tweak_ids: list[str], progress) -> list[str]:
    lines = []
    for tweak_id in tweak_ids:
        progress(f"Changing {tweak_id.replace('_', ' ')}…")
        try:
            lines.append(apply_tweak(tweak_id))
        except (OSError, RuntimeError, ValueError) as exc:
            lines.append(f"{tweak_id.replace('_', ' ').capitalize()} was not changed. {exc}")
    return lines


def undo_all(progress) -> list[str]:
    lines = []
    for tweak_id in list(_backup()):
        progress(f"Putting back {tweak_id.replace('_', ' ')}…")
        try:
            lines.append(undo_tweak(tweak_id))
        except (OSError, RuntimeError, ValueError) as exc:
            lines.append(f"{tweak_id.replace('_', ' ').capitalize()} was not put back. {exc}")
    return lines


def has_backup() -> bool:
    return bool(_backup())


def _backup() -> dict:
    data = load_state().get("performance_backup")
    return data if isinstance(data, dict) else {}


def _remember(tweak_id: str, value) -> None:
    """Keep the first value seen, so pressing Boost twice still undoes to the original."""
    state = load_state()
    backup = state.setdefault("performance_backup", {})
    if tweak_id not in backup:
        backup[tweak_id] = value
        save_state(state)


# ---------------------------------------------------------------- drive optimisation


def scheduled_optimization_enabled() -> bool | None:
    completed = run_hidden(["schtasks", "/query", "/tn", DEFRAG_TASK, "/xml"], timeout=20)
    if completed.returncode != 0:
        return None
    match = re.search(r"<Settings>.*?<Enabled>(true|false)</Enabled>", completed.stdout or "", re.S | re.I)
    return True if match is None else match.group(1).lower() == "true"


def optimize_drives(letters: list[str], cancel, progress) -> list[str]:
    """Windows' own Optimize Drives: TRIM on an SSD, defragment on a hard drive. Needs administrator."""
    if not is_admin():
        raise PermissionError("Restart as administrator to optimize drives.")
    lines = []
    for letter in letters:
        if cancel.is_set():
            lines.append("Stopped. The rest were left for Windows' weekly schedule.")
            break
        progress(f"Optimizing {letter}: … this can take a while on a hard drive.")
        process = subprocess.Popen(
            ["defrag", f"{letter}:", "/O", "/H"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        while process.poll() is None:
            if cancel.is_set():
                process.terminate()
                break
            time.sleep(0.3)
        output = decode_output(process.communicate()[0]).strip()
        if process.returncode == 0:
            lines.append(f"{letter}: optimized.")
        elif cancel.is_set():
            lines.append(f"{letter}: stopped part way. That is safe.")
        else:
            last = [line for line in output.splitlines() if line.strip()]
            lines.append(f"{letter}: {last[-1] if last else 'Windows could not optimize it.'}")
    return lines


# ---------------------------------------------------------------- findings


def restart_pending() -> bool:
    keys = (
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending",
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired",
    )
    for path in keys:
        try:
            winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path).Close()
            return True
        except OSError:
            continue
    return False


def _battery() -> tuple[bool, bool]:
    try:
        battery = psutil.sensors_battery()
    except (psutil.Error, OSError, RuntimeError):
        battery = None
    if battery is None:
        return False, False
    return True, not battery.power_plugged


def _findings(memory, cpu: float, uptime: float, pending: bool, startup_items, tweaks: list[PerfTweak], groups: list[ProcessGroup], battery) -> tuple[list[Finding], int]:
    findings: list[Finding] = []
    score = 100

    def add(title: str, detail: str, level: str, page: str, cost: int) -> None:
        nonlocal score
        findings.append(Finding(title, detail, level, page))
        score -= cost

    if pending:
        add("Restart to finish updates", "Windows has updates waiting for a restart. Until then some files are held twice and update work keeps running.", "warn", "performance", 15)
    days = uptime / 24
    if days >= 7:
        add(
            f"Running for {int(days)} days",
            "A restart clears memory that apps did not give back and finishes background updates. Use Restart, not Shut down: Fast Startup keeps the old session when you shut down.",
            "warn" if days >= 14 else "info",
            "performance",
            10 if days >= 14 else 5,
        )
    if memory.percent >= 90:
        add("Memory is almost full", f"{memory.percent:.0f}% of {format_size(memory.total)} is in use, so Windows is swapping to disk. Close something heavy from the list below.", "warn", "performance", 20)
    elif memory.percent >= 80:
        add("Memory is getting full", f"{memory.percent:.0f}% of {format_size(memory.total)} is in use. The heaviest apps are listed below.", "info", "performance", 8)
    if memory.total and memory.total < 8 * GB - 512 * MB:
        add("This PC has little memory", f"{format_size(memory.total)} is tight for Windows 11 and a browser. More memory is the upgrade that helps most.", "info", "performance", 5)
    if cpu >= 85:
        busy = next((group for group in sorted(groups, key=lambda item: item.cpu, reverse=True) if group.cpu >= 5), None)
        who = f" {busy.name} is using {busy.cpu:.0f}%." if busy else ""
        add("The processor is busy", f"About {cpu:.0f}% of the processor was in use while this check ran.{who}", "warn", "performance", 10)
    enabled = [item for item in startup_items or [] if getattr(item, "enabled", False)]
    if len(enabled) >= 15:
        add(f"{len(enabled)} apps start with Windows", "Each one adds time before the desktop is ready and keeps running in the background. Turn off the ones you do not need at sign-in.", "warn", "startup", 10)
    elif len(enabled) >= 10:
        add(f"{len(enabled)} apps start with Windows", "Turning off the ones you do not need at sign-in makes the desktop ready sooner.", "info", "startup", 5)
    system_drive = os.path.splitdrive(os.environ.get("WINDIR", r"C:\Windows"))[0] + "\\"
    try:
        usage = shutil.disk_usage(system_drive)
        free = usage.free / usage.total if usage.total else 1
        if free < 0.10 or usage.free < 10 * GB:
            add(f"{system_drive[:2]} is nearly full", f"Only {format_size(usage.free)} is free. Windows slows down and updates fail when the system drive is full. Clear caches first.", "warn", "junk", 20)
        elif free < 0.15:
            add(f"{system_drive[:2]} is getting full", f"{format_size(usage.free)} free. Keep at least 15% free for updates and temporary files.", "info", "junk", 8)
    except OSError:
        pass
    by_id = {tweak.id: tweak for tweak in tweaks}
    power = by_id.get("power")
    if power and power.recommended and "saver" in power.state.lower():
        add("Power saver is on", "The processor is held back to save power.", "warn", "performance", 10)
    if battery[1]:
        add("Running on battery", "Windows slows the processor down on battery. Plug in for full speed.", "info", "performance", 0)
    if scheduled_optimization_enabled() is False:
        add("Weekly drive optimization is off", "Windows normally trims SSDs and defragments hard drives once a week. Optimize drives runs it now.", "info", "performance", 5)
    if not findings:
        findings.append(Finding("Nothing slowing this PC down", "Memory, processor, startup apps, and free space all look healthy.", "info", "performance"))
    return findings, max(0, min(100, score))


# ---------------------------------------------------------------- registry helpers


def _read_dword(hive, path: str, name: str) -> int | None:
    try:
        with winreg.OpenKey(hive, path) as key:
            value, kind = winreg.QueryValueEx(key, name)
    except OSError:
        return None
    return int(value) if kind == winreg.REG_DWORD else None


def _write_dword(hive, path: str, name: str, value: int) -> None:
    with winreg.CreateKeyEx(hive, path, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, int(value))


def _delete_value(hive, path: str, name: str) -> None:
    try:
        with winreg.OpenKey(hive, path, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, name)
    except FileNotFoundError:
        pass


def _broadcast(area: str) -> None:
    """Tell open windows a setting changed, the way the Settings app does."""
    result = wintypes.DWORD()
    try:
        ctypes.windll.user32.SendMessageTimeoutW(HWND_BROADCAST, WM_SETTINGCHANGE, 0, area, 0x0002, 1000, ctypes.byref(result))
    except OSError:
        pass
