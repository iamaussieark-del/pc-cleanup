"""Windows services: what they are, and which ones are safe to set to Manual."""

from __future__ import annotations

import os

import psutil

from cleanup_app import procsnap
from cleanup_app.models import ServiceInfo
from cleanup_app.util import MB, extract_exe, run_hidden

CRITICAL = {
    "AudioEndpointBuilder",
    "Audiosrv",
    "BFE",
    "BrokerInfrastructure",
    "camsvc",
    "CDPSvc",
    "CoreMessagingRegistrar",
    "CryptSvc",
    "DcomLaunch",
    "Dhcp",
    "Dnscache",
    "DPS",
    "EventLog",
    "EventSystem",
    "FontCache",
    "gpsvc",
    "LSM",
    "mpssvc",
    "NlaSvc",
    "nsi",
    "PcaSvc",
    "PlugPlay",
    "Power",
    "ProfSvc",
    "RpcEptMapper",
    "RpcSs",
    "SamSs",
    "Schedule",
    "SecurityHealthService",
    "SENS",
    "ShellHWDetection",
    "Spooler",
    "StateRepository",
    "StorSvc",
    "SystemEventsBroker",
    "TextInputManagementService",
    "Themes",
    "TrkWks",
    "UserManager",
    "UsoSvc",
    "Wcmsvc",
    "WinDefend",
    "Winmgmt",
    "WlanSvc",
    "wscsvc",
    "wuauserv",
    "Appinfo",
    "BITS",
    "TrustedInstaller",
    "WinHttpAutoProxySvc",
    "KeyIso",
    "VaultSvc",
    "TokenBroker",
    "DispBrokerDesktopSvc",
    "WpnService",
    "ClickToRunSvc",
    "WdNisSvc",
    "MDCoreSvc",
    "Sense",
    "MsSense",
    "windefend",
}

OPTIONAL_WINDOWS = {
    "SysMain",
    "WSearch",
    "DiagTrack",
    "DoSvc",
    "MapsBroker",
    "Fax",
    "RemoteRegistry",
    "RetailDemo",
    "PhoneSvc",
    "WerSvc",
    "XblGameSave",
    "XboxGipSvc",
    "XboxNetApiSvc",
    "TabletInputService",
}

ADVICE = {
    "SysMain": "Prefetches apps you use often. On an SSD it rarely helps. On a PC with little memory it can cause extra disk activity. Manual is a reasonable choice on an SSD.",
    "WSearch": "Builds the index used by Windows search. It can keep a hard drive busy. Manual makes file search slower and quieter.",
    "DiagTrack": "Sends diagnostic data to Microsoft. Windows runs without it. This app can set it to Manual, which keeps the service available.",
    "DoSvc": "Delivery Optimization can use disk and network while sharing Windows updates. Manual is a normal home-PC setting.",
    "MapsBroker": "Downloads offline maps for apps. Manual is enough if you do not use the Maps app.",
    "Fax": "Fax support. Manual is enough if you do not send faxes.",
    "RemoteRegistry": "Lets other computers edit this PC's registry. Manual is the safer home setting.",
    "RetailDemo": "Retail demo mode for shop-floor PCs. Manual is the right setting on a personal computer.",
    "PhoneSvc": "Phone Link support. Manual is enough if you do not link a phone.",
    "WerSvc": "Windows Error Reporting. Manual is enough.",
    "XblGameSave": "Xbox game-save sync. You only need this running if you use Xbox sign-in or Game Pass saves.",
    "XboxGipSvc": "Supports Xbox controllers and accessories.",
    "XboxNetApiSvc": "Xbox Live networking for Xbox apps and some Microsoft Store games.",
    "TabletInputService": "Touch keyboard and handwriting. Manual is a good fit for a desktop used with a keyboard and mouse.",
    "Spooler": "Print spooler. Leave it running if you print.",
    "ClickToRunSvc": "Microsoft Office. Leave this alone if you use Word, Excel, or Outlook.",
    "WbioSrvc": "Windows Hello, fingerprint, and face sign-in.",
    "Audiosrv": "Windows audio. Leave it running.",
    "WlanSvc": "Wi-Fi. Leave it running if this PC uses wireless.",
    "Dhcp": "Gets an IP address from your router. Leave it running.",
    "Dnscache": "Remembers website names so pages open faster. Leave it running.",
    "WinDefend": "Microsoft Defender antivirus. Leave it running unless another antivirus has taken over.",
    "wuauserv": "Windows Update. Leave it running so security updates can install.",
}

SECURITY_WORDS = (
    "antivirus",
    "anti-virus",
    "defender",
    "firewall",
    "malware",
    "crowd",
    "sentinel",
    "kaspersky",
    "norton",
    "mcafee",
    "avast",
    "avg",
    "bitdefender",
    "eset",
    "sophos",
    "webroot",
    "vipre",
)


def _base_name(name: str) -> str:
    return name.split("_")[0]


def _is_critical(name: str) -> bool:
    base = _base_name(name)
    if name in CRITICAL or base in CRITICAL:
        return True
    lowered = name.lower()
    return lowered.startswith("wpnuserservice") or lowered.startswith("cbdhsvc") or "securityhealth" in lowered


def _advice_for(name: str) -> str:
    if name in ADVICE:
        return ADVICE[name]
    base = _base_name(name)
    if base in ADVICE:
        return ADVICE[base]
    lowered = name.lower()
    if "updater" in lowered or "update service" in lowered or lowered.endswith("update"):
        return "This is an updater. Manual means the app checks for updates when you open it, which is usually enough."
    return ""


def _looks_like_security(name: str, display: str, description: str) -> bool:
    blob = f"{name} {display} {description}".lower()
    return any(word in blob for word in SECURITY_WORDS)


def _classify(binpath: str) -> str:
    exe = extract_exe(binpath or "")
    lowered = exe.lower()
    windir = os.path.normcase(os.environ.get("WINDIR", r"C:\Windows"))
    if lowered.startswith(os.path.normcase(windir)):
        return "windows"
    if "microsoft" in lowered or "\\windowsapps\\" in lowered:
        return "microsoft"
    if any(token in lowered for token in ("program files", "appdata", "programdata")):
        return "third-party"
    if exe:
        return "third-party"
    return "unknown"


def _delayed_autostart(name: str) -> bool:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, rf"SYSTEM\CurrentControlSet\Services\{name}") as key:
            return bool(winreg.QueryValueEx(key, "DelayedAutostart")[0])
    except OSError:
        return False


def _service_info(service) -> dict | None:
    """psutil's as_dict, but a service whose description cannot be read is kept."""
    try:
        return service.as_dict()
    except (psutil.Error, OSError):
        pass
    try:
        return {
            "name": service.name(),
            "display_name": service.display_name(),
            "binpath": service.binpath(),
            "start_type": service.start_type(),
            "status": service.status(),
            "pid": service.pid(),
            "description": "",
        }
    except (psutil.Error, OSError):
        return None


def _impact(service: ServiceInfo) -> str:
    if service.critical:
        return "System"
    if service.security:
        return "Security"
    if service.cpu >= 8:
        return "High CPU"
    if not service.shared_process and service.memory >= 250 * MB:
        return "High memory"
    if service.shared_process and service.memory >= 400 * MB and service.publisher_kind != "windows":
        return "Shared host"
    if not service.shared_process and service.memory >= 100 * MB:
        return "Medium memory"
    automatic = service.start_type.startswith("automatic")
    if automatic and service.status == "running" and (service.publisher_kind == "third-party" or service.optional_windows):
        return "Boots with Windows"
    if service.status != "running":
        return "Not running"
    if service.publisher_kind == "windows" and not service.optional_windows:
        return "System"
    return "Low"


def _build_advice(service_name: str, description: str, publisher: str, shared: bool) -> str:
    known = _advice_for(service_name)
    if known:
        return known
    if publisher == "windows":
        text = description or "Windows runs this in the background."
    else:
        text = description or "No description is published for this service."
    if shared:
        text += " It shares a process with other services, so any memory figure is for the whole process."
    return text


def scan_services(cancel, progress, sample_cpu: bool = True) -> list[ServiceInfo]:
    progress("Reading services…")
    raw_items = []
    for service in psutil.win_service_iter():
        if cancel.is_set():
            return []
        info = _service_info(service)
        if info is not None:
            raw_items.append(info)

    memory, cpu_by_pid = _process_usage(cancel, sample_cpu)
    if cancel.is_set():
        return []

    services: list[ServiceInfo] = []
    for info in raw_items:
        if cancel.is_set():
            break
        name = info.get("name") or ""
        if not name:
            continue
        description = (info.get("description") or "").strip()
        display = info.get("display_name") or name
        binpath = info.get("binpath") or ""
        pid = info.get("pid") or None
        start = (info.get("start_type") or "unknown").lower()
        if start == "automatic" and _delayed_autostart(name):
            start = "automatic (delayed)"
        status = (info.get("status") or "unknown").lower()
        shared = "svchost.exe" in binpath.lower()
        used = memory(pid) if pid else 0
        cpu = cpu_by_pid.get(pid, 0.0) if pid else 0.0
        publisher = _classify(binpath)
        security = _looks_like_security(name, display, description)
        critical = _is_critical(name)
        optional = (name in OPTIONAL_WINDOWS or _base_name(name) in OPTIONAL_WINDOWS) and not critical
        draft = ServiceInfo(
            name=name,
            display_name=display,
            description=description,
            status=status,
            start_type=start,
            binpath=binpath,
            pid=pid,
            memory=used,
            cpu=cpu,
            shared_process=shared,
            publisher_kind=publisher,
            impact="",
            advice="",
            critical=critical,
            optional_windows=optional,
            security=security,
        )
        draft.impact = _impact(draft)
        draft.advice = _build_advice(name, description, publisher, shared)
        if security:
            draft.advice = "This looks like security software. Leave it running. " + draft.advice
        services.append(draft)
    services.sort(key=lambda item: (item.impact == "System", item.impact == "Low", item.display_name.lower()))
    return services


def _process_usage(cancel, sample_cpu: bool):
    """Memory and CPU for every process from one system snapshot.

    Falls back to asking psutil one process at a time.
    """
    sampled = procsnap.cpu_percent(0.35, cancel) if sample_cpu else None
    snap = sampled[0] if sampled else procsnap.snapshot()
    cpu_by_pid = sampled[1] if sampled else {}
    if snap is not None:
        return (lambda pid: snap[pid].working_set if pid in snap else 0), cpu_by_pid
    cache: dict[int, int] = {}

    def fallback(pid: int) -> int:
        if pid not in cache:
            try:
                cache[pid] = psutil.Process(pid).memory_info().rss
            except (psutil.Error, OSError):
                cache[pid] = 0
        return cache[pid]

    return fallback, cpu_by_pid


def set_start_type(name: str, mode: str) -> str:
    mapping = {"manual": "demand", "automatic": "auto"}
    if mode not in mapping:
        raise ValueError("Choose Manual or Automatic.")
    completed = run_hidden(["sc", "config", name, "start=", mapping[mode]], timeout=30)
    output = ((completed.stdout or "") + "\n" + (completed.stderr or "")).strip()
    if completed.returncode != 0:
        raise RuntimeError(output or f"Could not change {name}.")
    return output or "Updated."


def stop_service(name: str) -> str:
    completed = run_hidden(["sc", "stop", name], timeout=30)
    output = ((completed.stdout or "") + "\n" + (completed.stderr or "")).strip()
    if completed.returncode not in (0, 1062):
        raise RuntimeError(output or f"Could not stop {name}.")
    return output or "Stop requested."
