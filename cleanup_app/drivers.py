"""Device drivers: what is installed, what is broken, and what is newer.

"Newer" comes from three sources, asked at once (see find_updates):
Windows Update, which matches its catalogue to this PC the way Settings does;
the Microsoft Update Catalog, searched per device and kept only when a package
lists the device's exact hardware ID; and NVIDIA's own service for GeForce
cards. The lookups themselves live in driver_lookup.

Device details come from pnputil, which ships with Windows and answers in XML,
so the parsing does not depend on the language Windows is set to.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date

from cleanup_app.driver_lookup import DriverOffer, device_query, lookup_catalog, lookup_nvidia, version_key
from cleanup_app.util import NO_WINDOW, POWERSHELL_UTF8, decode_output, run_hidden

OLD_DAYS = 4 * 365
GPU_OLD_DAYS = 180
SEARCH_TIMEOUT = 240
DRIVER_STORE = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32", "DriverStore", "FileRepository")
INF_DIR = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "INF")

CLASS_NAMES = {
    "display": "Display adapters",
    "net": "Network adapters",
    "media": "Sound, video and game controllers",
    "audioendpoint": "Audio inputs and outputs",
    "hidclass": "Human interface devices",
    "usb": "USB controllers",
    "usbdevice": "USB devices",
    "system": "System devices",
    "softwarecomponent": "Software components",
    "scsiadapter": "Storage controllers",
    "hdc": "Storage controllers",
    "diskdrive": "Disk drives",
    "monitor": "Monitors",
    "bluetooth": "Bluetooth",
    "camera": "Cameras",
    "image": "Cameras",
    "keyboard": "Keyboards",
    "mouse": "Mice and pointing devices",
    "ports": "Ports",
    "printer": "Printers",
    "printqueue": "Print queues",
    "processor": "Processors",
    "securitydevices": "Security devices",
    "biometric": "Biometric devices",
    "sensor": "Sensors",
    "firmware": "Firmware",
    "battery": "Batteries",
    "extension": "Driver extensions",
    "softwaredevice": "Software devices",
    "wpd": "Portable devices",
    "volume": "Storage volumes",
    "volumesnapshot": "Storage volume shadow copies",
    "computer": "Computer",
    "net service": "Network services",
}
QUIET_CLASSES = {"softwaredevice", "volume", "volumesnapshot", "legacydriver", "computer", "processor", "printqueue"}

PROBLEMS = {
    1: "Windows has not configured this device correctly.",
    3: "The driver may be damaged, or Windows is running low on memory.",
    10: "The device cannot start.",
    12: "There are not enough free resources for this device.",
    14: "The device needs a restart to work.",
    18: "The drivers need to be reinstalled.",
    19: "Its settings in the registry are incomplete or damaged.",
    21: "Windows is removing this device.",
    22: "The device is turned off in Device Manager.",
    24: "The device is not present, not working, or has no drivers.",
    28: "No driver is installed for this device.",
    29: "The device is turned off in the firmware (BIOS/UEFI).",
    31: "Windows cannot load the driver this device needs.",
    32: "The driver's service is turned off.",
    37: "The driver failed to start.",
    38: "An older copy of the driver is still in memory. A restart usually fixes this.",
    39: "The driver is damaged or missing.",
    40: "The driver's registry entry is missing or damaged.",
    41: "The driver loaded, but the hardware was not found.",
    43: "Windows stopped this device because it reported problems.",
    45: "The device is not connected.",
    47: "The device is ready to be safely removed.",
    48: "The driver was blocked because it is known to cause problems.",
    52: "Windows cannot verify the driver's digital signature.",
    54: "The device failed and is being reset.",
}

VENDOR_PAGES = (
    ("nvidia", "NVIDIA", "https://www.nvidia.com/en-us/drivers/"),
    ("amd", "AMD", "https://www.amd.com/en/support/download/drivers.html"),
    ("advanced micro devices", "AMD", "https://www.amd.com/en/support/download/drivers.html"),
    ("intel", "Intel", "https://www.intel.com/content/www/us/en/support/detect.html"),
)


@dataclass
class DriverUpdate:
    update_id: str
    title: str
    provider: str
    model: str
    driver_class: str
    date: str
    version: str
    hardware_id: str
    size: int
    checked: bool = False


@dataclass
class DeviceDriver:
    instance_id: str
    device: str
    device_class: str
    class_name: str
    manufacturer: str
    status: str
    problem_code: int
    problem: str
    inf: str
    original_inf: str
    provider: str
    version: str
    date: str
    signer: str
    hardware_ids: list[str] = field(default_factory=list)
    age_days: int = -1
    inbox: bool = False
    update: DriverUpdate | None = None
    matching_id: str = ""
    offers: list[DriverOffer] = field(default_factory=list)

    @property
    def is_gpu(self) -> bool:
        return self.device_class.lower() == "display"

    @property
    def is_nvidia(self) -> bool:
        return "nvidia" in f"{self.manufacturer} {self.provider}".lower()

    @property
    def friendly_version(self) -> str:
        return friendly_version(self.provider or self.manufacturer, self.version)

    @property
    def best(self) -> DriverOffer | None:
        """The newest driver any source has for this device."""
        return max(self.offers, key=self.offer_rank) if self.offers else None

    def offer_rank(self, offer: DriverOffer):
        # NVIDIA counts in its own numbers (617.14), and Windows in its own
        # (32.0.16.1714), so a GeForce card compares both in NVIDIA's.
        if self.is_nvidia:
            text = offer.version if offer.source == "NVIDIA" else friendly_version("NVIDIA", offer.version)
            return (version_key(text) or (0,), offer.date)
        return (version_key(offer.version) or (0,), offer.date, offer.source == "Windows Update")

    def shown_version(self, offer: DriverOffer) -> str:
        if self.is_nvidia and offer.source != "NVIDIA":
            return friendly_version("NVIDIA", offer.version) or offer.version
        return offer.version


@dataclass
class DriverPackage:
    inf: str
    original: str
    provider: str
    class_name: str
    version: str
    date: str
    folder: str
    size: int
    newest_inf: str
    newest_version: str
    newest_date: str
    checked: bool = True


@dataclass
class DriverReport:
    devices: list[DeviceDriver]
    updates: list[DriverUpdate]
    packages: list[DriverPackage]
    checked_online: bool
    online_note: str
    scanned_at: float
    notes: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)

    @property
    def problems(self) -> list[DeviceDriver]:
        return [item for item in self.devices if item.problem_code and item.problem_code not in {22, 45}]

    @property
    def outdated(self) -> list[DeviceDriver]:
        return [item for item in self.devices if item.offers]

    def source_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for device in self.devices:
            best = device.best
            if best is not None:
                counts[best.source] = counts.get(best.source, 0) + 1
        return counts

    @property
    def unmatched_updates(self) -> list[DriverUpdate]:
        matched = {item.update.update_id for item in self.devices if item.update is not None}
        return [item for item in self.updates if item.update_id not in matched]


def scan_drivers(cancel, progress, online: bool = True) -> DriverReport:
    notes: list[str] = []
    progress("Reading devices and their drivers…")
    devices = list_devices()
    if not devices:
        notes.append("Windows did not list any devices. pnputil may be missing or blocked on this PC.")
    if cancel.is_set():
        return DriverReport(devices, [], [], False, "Stopped before checking Windows Update.", time.time(), notes)
    progress("Looking for old driver packages…")
    try:
        packages = superseded_packages()
    except (OSError, ET.ParseError, subprocess.SubprocessError) as exc:
        packages = []
        notes.append(f"Old driver packages could not be read. {exc}")
    sort_devices(devices)
    report = DriverReport(devices, [], packages, False, "Newer drivers were not looked up.", time.time(), notes)
    if online and not cancel.is_set():
        find_updates(report, cancel, progress)
    return report


SOURCES = ("Windows Update", "Update Catalog", "NVIDIA")


def find_updates(report: DriverReport, cancel, progress, on_partial=None) -> None:
    """Ask all three sources at once and add what they find to the report.

    on_partial is called with the report each time a source answers, from a
    worker thread, so the page can show results as they arrive.
    """
    report.pending = list(SOURCES)
    report.online_note = ""
    notes: list[str] = []
    # The lookups get their own list, because this thread re-sorts the report's
    # list each time a source answers.
    devices = list(report.devices)
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {
            pool.submit(search_windows_update, cancel): "Windows Update",
            pool.submit(lookup_catalog, devices, cancel, progress): "Update Catalog",
            pool.submit(lookup_nvidia, devices, cancel): "NVIDIA",
        }
        for future in as_completed(futures):
            source = futures[future]
            try:
                result = future.result()
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
                notes.append(f"{source} could not be checked. {_first_line(str(exc)) or exc}")
                result = None
            if source == "Windows Update" and result is not None:
                report.updates = result
                match_updates(report.devices, result)
            elif result is not None:
                offers, note = result
                if note:
                    notes.append(note)
                _add_offers(report.devices, offers)
            report.pending.remove(source)
            report.online_note = " ".join(notes)
            sort_devices(report.devices)
            if on_partial is not None and report.pending:
                on_partial(report)
    report.checked_online = not cancel.is_set()
    if cancel.is_set():
        report.online_note = "Stopped before every source answered."


def _add_offers(devices: list[DeviceDriver], offers: dict[str, DriverOffer]) -> None:
    for device in devices:
        offer = offers.get(device.instance_id)
        if offer is None:
            continue
        if offer.source == "NVIDIA":
            # NVIDIA always names its newest driver. It only counts when it is newer.
            installed = version_key(device.friendly_version)
            if installed is not None and (version_key(offer.version) or (0,)) <= installed:
                continue
        device.offers.append(offer)


def sort_devices(devices: list[DeviceDriver]) -> None:
    """Problems first, then devices with an update, then graphics, then by type and name."""
    devices.sort(key=_device_order)


def _device_order(item: DeviceDriver):
    problem = bool(item.problem_code) and item.problem_code not in {22, 45}
    return (not problem, not item.offers, not item.is_gpu, item.class_name.lower(), item.device.lower())


# ---------------------------------------------------------------- devices


def list_devices() -> list[DeviceDriver]:
    completed = run_hidden(["pnputil", "/enum-devices", "/connected", "/drivers", "/ids", "/format", "xml"], timeout=90)
    root = _xml(completed.stdout)
    if root is None:
        # Windows 10 and early Windows 11 cannot answer in XML.
        return list_devices_wmi()
    problems = _problem_codes()
    today = date.today()
    devices: list[DeviceDriver] = []
    for node in root.findall("Device"):
        instance = node.get("InstanceId") or ""
        description = _text(node, "DeviceDescription") or instance
        class_name = _text(node, "ClassName")
        installed = _text(node, "DriverName")
        status = _text(node, "Status")
        driver = _installed_driver(node, installed)
        version_text = _text(driver, "DriverVersion") if driver is not None else ""
        stamp, version = parse_driver_version(version_text)
        provider = _text(driver, "ProviderName") if driver is not None else ""
        code = problems.get(instance.lower(), 0) or _problem_code(node)
        ids = [item.text.strip() for item in node.iter() if item.tag in {"HardwareId", "CompatibleId"} and item.text]
        devices.append(
            DeviceDriver(
                instance_id=instance,
                device=description,
                device_class=class_name,
                class_name=CLASS_NAMES.get(class_name.lower(), class_name or "Other devices"),
                manufacturer=_text(node, "ManufacturerName"),
                status=status,
                problem_code=code,
                problem=PROBLEMS.get(code, f"Windows reports problem code {code}.") if code else "",
                inf=installed,
                original_inf=(_text(driver, "OriginalName") if driver is not None else "") or installed,
                provider=provider,
                version=version,
                date=stamp.isoformat() if stamp else "",
                signer=_text(driver, "SignerName") if driver is not None else "",
                hardware_ids=ids,
                age_days=(today - stamp).days if stamp else -1,
                inbox=bool(installed) and not installed.lower().startswith("oem"),
                matching_id=_text(driver, "MatchingDeviceId") if driver is not None else "",
            )
        )
    return devices


_WMI_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$signed = @{}
foreach ($d in Get-CimInstance Win32_PnPSignedDriver) { if ($d.DeviceID) { $signed[$d.DeviceID] = $d } }
$rows = @(foreach ($e in Get-CimInstance Win32_PnPEntity) {
  if ($e.ConfigManagerErrorCode -eq 45 -or $e.Present -eq $false) { continue }
  $d = $signed[$e.PNPDeviceID]
  $stamp = ''
  if ($d -and $d.DriverDate) { $stamp = ([datetime]$d.DriverDate).ToString('yyyy-MM-dd') }
  [pscustomobject]@{
    Id = [string]$e.PNPDeviceID; Name = [string]$e.Name; Class = [string]$e.PNPClass; Maker = [string]$e.Manufacturer
    Code = [int]$e.ConfigManagerErrorCode; Ids = @($e.HardwareID) + @($e.CompatibleID)
    Inf = [string]$d.InfName; Provider = [string]$d.DriverProviderName; Version = [string]$d.DriverVersion
    Date = $stamp; Signer = [string]$d.Signer; Matching = [string]$d.HardWareID
  }
})
ConvertTo-Json -InputObject $rows -Compress -Depth 3
"""


def list_devices_wmi() -> list[DeviceDriver]:
    """The same device list from WMI, which every Windows 10 and 11 has. Slower than pnputil."""
    completed = run_hidden(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", _WMI_SCRIPT], timeout=180)
    try:
        rows = json.loads(completed.stdout.lstrip("\ufeff").strip() or "[]")
    except json.JSONDecodeError:
        return []
    if isinstance(rows, dict):
        rows = [rows]
    today = date.today()
    devices = []
    dates: dict[str, date | None] = {}
    for row in rows:
        if not isinstance(row, dict) or not row.get("Id"):
            continue
        code = int(row.get("Code") or 0)
        inf = str(row.get("Inf") or "")
        # WMI swaps day and month on PCs set to a day-first date format, so the
        # date comes from the driver's own .inf file, which is always month first.
        if inf not in dates:
            dates[inf] = inf_date(inf)
        stamp = dates[inf]
        if stamp is None:
            try:
                stamp = date.fromisoformat(row.get("Date") or "")
            except ValueError:
                stamp = None
        class_name = str(row.get("Class") or "")
        devices.append(
            DeviceDriver(
                instance_id=str(row["Id"]),
                device=str(row.get("Name") or row["Id"]),
                device_class=class_name,
                class_name=CLASS_NAMES.get(class_name.lower(), class_name or "Other devices"),
                manufacturer=str(row.get("Maker") or ""),
                status="Problem" if code else "Started",
                problem_code=code,
                problem=PROBLEMS.get(code, f"Windows reports problem code {code}.") if code else "",
                inf=inf,
                original_inf=inf,
                provider=str(row.get("Provider") or ""),
                version=str(row.get("Version") or ""),
                date=stamp.isoformat() if stamp else "",
                signer=str(row.get("Signer") or ""),
                hardware_ids=[str(item) for item in row.get("Ids") or [] if item],
                age_days=(today - stamp).days if stamp else -1,
                inbox=bool(inf) and not inf.lower().startswith("oem"),
                matching_id=str(row.get("Matching") or ""),
            )
        )
    return devices


def inf_date(inf: str) -> date | None:
    """The DriverVer date in an installed .inf file. The format is always month/day/year."""
    if not inf or os.path.basename(inf) != inf:
        return None
    try:
        with open(os.path.join(INF_DIR, inf), "rb") as handle:
            raw = handle.read(256 * 1024)
    except OSError:
        return None
    text = raw.decode("utf-16", errors="replace") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("latin-1")
    match = re.search(r"^\s*DriverVer\s*=\s*(\d{1,2})/(\d{1,2})/(\d{4})", text, re.MULTILINE | re.IGNORECASE)
    if not match:
        return None
    try:
        return date(int(match.group(3)), int(match.group(1)), int(match.group(2)))
    except ValueError:
        return None


def _installed_driver(node, installed: str):
    matching = node.find("MatchingDrivers")
    if matching is None:
        return None
    for driver in matching.findall("DriverName"):
        if installed and (driver.get("DriverName") or "").lower() == installed.lower():
            return driver
    for driver in matching.findall("DriverName"):
        if "installed" in _text(driver, "Status").lower():
            return driver
    return None


def _problem_codes() -> dict[str, int]:
    """Instance ID -> problem code, from pnputil's own list of devices with problems."""
    try:
        completed = run_hidden(["pnputil", "/enum-devices", "/problem", "/format", "xml"], timeout=60)
    except (OSError, subprocess.SubprocessError):
        return {}
    root = _xml(completed.stdout)
    if root is None:
        return {}
    found = {}
    for node in root.findall("Device"):
        instance = (node.get("InstanceId") or "").lower()
        if instance:
            found[instance] = _problem_code(node) or 10
    return found


def _problem_code(node) -> int:
    text = _text(node, "ProblemCode") or _text(node, "Problem")
    numbers = re.findall(r"\d+", text)
    if numbers:
        try:
            return int(numbers[-1])
        except ValueError:
            return 0
    return 0


def parse_driver_version(text: str) -> tuple[date | None, str]:
    """pnputil writes '05/05/2026 32.0.15.9649' (month first) in its XML."""
    match = re.match(r"\s*(\d{1,2})/(\d{1,2})/(\d{4})\s+(\S+)", text or "")
    if not match:
        return None, (text or "").strip()
    first, second, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
    month, day = (first, second) if first <= 12 else (second, first)
    try:
        return date(year, month, day), match.group(4)
    except ValueError:
        return None, match.group(4)


def friendly_version(maker: str, version: str) -> str:
    """NVIDIA's own number, such as 596.49, from the Windows version 32.0.15.9649."""
    if "nvidia" not in (maker or "").lower():
        return ""
    parts = (version or "").split(".")
    if len(parts) < 4:
        return ""
    digits = (parts[-2] + parts[-1].zfill(4))[-5:]
    if len(digits) != 5 or not digits.isdigit():
        return ""
    return f"{int(digits[:3])}.{digits[3:]}"


# Names Windows gives a device when it does not know what the device is. A web
# search for these words finds nothing useful, so the hardware ID is searched instead.
GENERIC_NAMES = (
    "unknown",
    "generic",
    "composite",
    "hid-compliant",
    "usb input device",
    "base system device",
    "pci device",
    "pci simple communications",
    "sm bus controller",
    "multimedia audio controller",
    "multimedia controller",
    "standard ",
)


MAKER_SHORT = {"advanced micro devices": "AMD", "intel corporation": "Intel", "realtek semiconductor": "Realtek"}


def web_search_query(device: DeviceDriver) -> str:
    """What to type into a search engine to find this device's driver."""
    name = (device.device or "").strip()
    generic = not device.inf or not name or any(word in name.lower() for word in GENERIC_NAMES)
    hardware = device_query(device)
    if generic and hardware:
        # The vendor and product codes identify the exact product. Suffixes such as
        # &MI_02&Col01 only describe one part of it, and make searches miss.
        pair = re.search(r"(?:VEN|VID)_[0-9A-F]{4}&(?:DEV|PID)_[0-9A-F]{4}", hardware, re.IGNORECASE)
        return f'"{pair.group(0).upper() if pair else hardware}" driver'
    maker = _short_maker(device.manufacturer or device.provider or "")
    if not maker or maker.lower().startswith(("(standard", "microsoft")) or maker.split()[0].lower() in name.lower():
        maker = ""
    windows = "Windows 11" if sys.getwindowsversion().build >= 22000 else "Windows 10"
    return " ".join(part for part in (name, "driver", maker, windows) if part)


def _short_maker(maker: str) -> str:
    """'Advanced Micro Devices, Inc.' -> 'AMD', 'SteelSeries ApS' -> 'SteelSeries'."""
    text = re.sub(r"[,.]?\s+(inc|corp|corporation|co|ltd|limited|aps|gmbh|llc|s\.a)\b\.?.*$", "", maker.strip(), flags=re.IGNORECASE)
    text = text.strip(" ,.")
    return MAKER_SHORT.get(text.lower(), text)


def web_search_url(device: DeviceDriver) -> str:
    return "https://www.google.com/search?q=" + urllib.parse.quote(web_search_query(device), safe="")


def vendor_page(device: DeviceDriver) -> tuple[str, str] | None:
    blob = f"{device.manufacturer} {device.provider} {device.device}".lower()
    for needle, name, url in VENDOR_PAGES:
        if needle in blob:
            return name, url
    return None


# ---------------------------------------------------------------- Windows Update

_SEARCH_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$session = New-Object -ComObject Microsoft.Update.Session
$session.ClientApplicationID = 'PC Cleanup'
$searcher = $session.CreateUpdateSearcher()
$searcher.Online = $true
$result = $searcher.Search("IsInstalled=0 and Type='Driver' and IsHidden=0")
$rows = @(foreach ($u in $result.Updates) {
  $stamp = ''
  try { $stamp = ([datetime]$u.DriverVerDate).ToString('yyyy-MM-dd') } catch {}
  [pscustomobject]@{
    Id = [string]$u.Identity.UpdateID
    Title = [string]$u.Title
    Provider = [string]$u.DriverProvider
    Model = [string]$u.DriverModel
    Class = [string]$u.DriverClass
    Date = $stamp
    HardwareId = [string]$u.DriverHardwareID
    Size = [int64]$u.MaxDownloadSize
  }
})
ConvertTo-Json -InputObject $rows -Compress -Depth 3
"""


def search_windows_update(cancel) -> list[DriverUpdate]:
    output = _powershell(_SEARCH_SCRIPT, cancel, SEARCH_TIMEOUT)
    if cancel.is_set():
        return []
    raw = output.lstrip("\ufeff").strip()
    if not raw:
        return []
    data = json.loads(raw)
    if isinstance(data, dict):
        data = [data]
    updates = []
    for item in data or []:
        if not isinstance(item, dict) or not item.get("Id"):
            continue
        title = str(item.get("Title") or "")
        version = re.search(r"\(([\d.]+)\)\s*$", title)
        updates.append(
            DriverUpdate(
                update_id=str(item["Id"]),
                title=title,
                provider=str(item.get("Provider") or ""),
                model=str(item.get("Model") or ""),
                driver_class=str(item.get("Class") or ""),
                date=str(item.get("Date") or ""),
                version=version.group(1) if version else "",
                hardware_id=str(item.get("HardwareId") or ""),
                size=int(item.get("Size") or 0),
            )
        )
    return updates


def match_updates(devices: list[DeviceDriver], updates: list[DriverUpdate]) -> None:
    by_id: dict[str, list[DeviceDriver]] = defaultdict(list)
    for device in devices:
        for ident in device.hardware_ids:
            by_id[ident.lower()].append(device)
    for update in updates:
        for device in by_id.get(update.hardware_id.lower(), []):
            if device.update is None or update.date > device.update.date:
                device.update = update
    for device in devices:
        if device.update is not None:
            update = device.update
            device.offers = [offer for offer in device.offers if offer.source != "Windows Update"]
            device.offers.append(
                DriverOffer("Windows Update", update.version, update.date, update.title, update.size, update.update_id, hardware_id=update.hardware_id, update=update)
            )


def _powershell(script: str, cancel, timeout: int, input_text: str | None = None) -> str:
    """Run a PowerShell script that the Stop button can end. input_text arrives on stdin."""
    process = subprocess.Popen(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", POWERSHELL_UTF8 + script],
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=NO_WINDOW,
    )
    started = time.monotonic()
    pending = input_text.encode("utf-8") if input_text is not None else None
    while True:
        try:
            output, errors = process.communicate(pending, timeout=0.25)
            break
        except subprocess.TimeoutExpired:
            pending = None
            if cancel.is_set():
                process.kill()
                process.communicate()
                return ""
            if time.monotonic() - started > timeout:
                process.kill()
                process.communicate()
                raise RuntimeError("Windows Update took too long to answer.")
    output, errors = decode_output(output), decode_output(errors)
    if process.returncode != 0:
        raise RuntimeError(_first_line(errors) or f"PowerShell stopped with code {process.returncode}.")
    return output


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if line and not line.startswith(("At line", "+", "CategoryInfo", "FullyQualifiedErrorId")):
            return line
    return ""


_INSTALL_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$wanted = @(ConvertFrom-Json -InputObject ([Console]::In.ReadToEnd()))
$session = New-Object -ComObject Microsoft.Update.Session
$session.ClientApplicationID = 'PC Cleanup'
$searcher = $session.CreateUpdateSearcher()
$searcher.Online = $true
$found = $searcher.Search("IsInstalled=0 and Type='Driver' and IsHidden=0")
$batch = New-Object -ComObject Microsoft.Update.UpdateColl
foreach ($u in $found.Updates) {
  if ($wanted -contains [string]$u.Identity.UpdateID) {
    if (-not $u.EulaAccepted) { $u.AcceptEula() }
    [void]$batch.Add($u)
  }
}
$rows = @()
if ($batch.Count -gt 0) {
  $downloader = $session.CreateUpdateDownloader()
  $downloader.Updates = $batch
  [void]$downloader.Download()
  $installer = $session.CreateUpdateInstaller()
  $installer.Updates = $batch
  $outcome = $installer.Install()
  for ($i = 0; $i -lt $batch.Count; $i++) {
    $one = $outcome.GetUpdateResult($i)
    $rows += [pscustomobject]@{
      Id = [string]$batch.Item($i).Identity.UpdateID
      Title = [string]$batch.Item($i).Title
      Code = [int]$one.ResultCode
      Reboot = [bool]$one.RebootRequired
      HResult = [int]$one.HResult
    }
  }
}
ConvertTo-Json -InputObject @($rows) -Compress -Depth 3
"""

RESULT_TEXT = {2: "Installed", 3: "Installed with errors", 4: "Failed", 5: "Cancelled"}


def install_updates(updates: list[DriverUpdate], cancel, progress) -> tuple[list[str], bool]:
    """Download and install chosen Windows Update drivers. Needs administrator."""
    if not updates:
        return [], False
    progress(f"Downloading and installing {len(updates)} driver update{'s' if len(updates) != 1 else ''}. This can take several minutes…")
    output = _powershell(_INSTALL_SCRIPT, _NeverCancel(), 3600, json.dumps([item.update_id for item in updates]))
    raw = output.lstrip("\ufeff").strip()
    data = json.loads(raw) if raw else []
    if isinstance(data, dict):
        data = [data]
    lines: list[str] = []
    reboot = False
    done = {str(item.get("Id")) for item in data if isinstance(item, dict)}
    for item in data:
        if not isinstance(item, dict):
            continue
        reboot = reboot or bool(item.get("Reboot"))
        lines.append(f"{RESULT_TEXT.get(int(item.get('Code') or 0), 'Unknown result')}: {item.get('Title')}")
    for update in updates:
        if update.update_id not in done:
            lines.append(f"Not offered any more: {update.title}")
    return lines, reboot


class _NeverCancel:
    """Installing a driver must not be cut off halfway, so Stop is ignored here."""

    def is_set(self) -> bool:
        return False

    def wait(self, seconds: float) -> bool:
        time.sleep(seconds)
        return False


def create_restore_point(description: str = "PC Cleanup driver update") -> str:
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"Checkpoint-Computer -Description '{description}' -RestorePointType MODIFY_SETTINGS -WarningVariable w -WarningAction SilentlyContinue; "
        "if ($w) { 'WARN ' + ($w -join ' ') } else { 'OK' }"
    )
    completed = run_hidden(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], timeout=300)
    text = (completed.stdout or "").strip()
    if completed.returncode != 0:
        detail = _first_line(completed.stderr) or "System Protection may be turned off for this drive."
        return f"No restore point was made. {detail}"
    if text.startswith("WARN"):
        return "Windows skipped the restore point because one was made in the last 24 hours."
    return "A restore point was made first."


# ---------------------------------------------------------------- driver store


def superseded_packages() -> list[DriverPackage]:
    """Older copies of a driver that no device uses, where a newer copy is installed.

    Windows keeps every driver version it has installed. A newer copy wins
    whenever a device is connected, so the older unused ones only take space.
    """
    completed = run_hidden(["pnputil", "/enum-drivers", "/devices", "/format", "xml"], timeout=90)
    root = _xml(completed.stdout)
    if root is None:
        raise OSError("Finding old driver packages needs a newer Windows 11.")
    groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for node in root.findall("Driver"):
        stamp, version = parse_driver_version(_text(node, "DriverVersion"))
        devices = node.find("Devices")
        record = {
            "inf": node.get("DriverName") or "",
            "original": _text(node, "OriginalName"),
            "provider": _text(node, "ProviderName"),
            "class": _text(node, "ClassName"),
            "date": stamp,
            "version": version,
            "in_use": devices is not None and len(devices) > 0,
        }
        if record["inf"].lower().startswith("oem") and record["original"]:
            groups[(record["original"].lower(), record["provider"].lower(), record["class"].lower())].append(record)
    packages: list[DriverPackage] = []
    for items in groups.values():
        if len(items) < 2:
            continue
        items.sort(key=_package_rank, reverse=True)
        newest = items[0]
        for old in items[1:]:
            if old["in_use"]:
                continue
            folder = package_folder(old["inf"], old["original"])
            packages.append(
                DriverPackage(
                    inf=old["inf"],
                    original=old["original"],
                    provider=old["provider"],
                    class_name=CLASS_NAMES.get(old["class"].lower(), old["class"]),
                    version=old["version"],
                    date=old["date"].isoformat() if old["date"] else "",
                    folder=folder,
                    size=_folder_size(folder) if folder else 0,
                    newest_inf=newest["inf"],
                    newest_version=newest["version"],
                    newest_date=newest["date"].isoformat() if newest["date"] else "",
                )
            )
    packages.sort(key=lambda item: item.size, reverse=True)
    return packages


def _package_rank(record: dict):
    """Windows prefers the newer date, then the higher version, among equal matches."""
    parts = []
    for piece in (record["version"] or "").split("."):
        parts.append(int(piece) if piece.isdigit() else 0)
    return (record["date"] or date.min, tuple(parts))


def package_folder(inf: str, original: str) -> str:
    """The FileRepository folder for a published oemNN.inf.

    Windows keeps a copy of each published inf in the INF folder that is
    byte-for-byte the inf inside its package folder, so the match is exact.
    """
    try:
        with open(os.path.join(INF_DIR, inf), "rb") as handle:
            published = handle.read()
        names = os.listdir(DRIVER_STORE)
    except OSError:
        return ""
    prefix = original.lower() + "_"
    found = []
    for name in names:
        if not name.lower().startswith(prefix):
            continue
        candidate = os.path.join(DRIVER_STORE, name, original)
        try:
            with open(candidate, "rb") as handle:
                if handle.read() == published:
                    found.append(os.path.join(DRIVER_STORE, name))
        except OSError:
            continue
    return found[0] if len(found) == 1 else ""


def _folder_size(path: str) -> int:
    total = 0
    pending = [path]
    while pending:
        try:
            with os.scandir(pending.pop()) as entries:
                for entry in entries:
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if info.st_file_attributes & 0x400:
                        continue
                    if info.st_file_attributes & 0x10:
                        pending.append(entry.path)
                    else:
                        total += info.st_size
        except OSError:
            continue
    return total


def remove_packages(packages: list[DriverPackage], cancel, progress) -> tuple[int, int, list[str]]:
    """Delete unused driver packages. pnputil refuses any package a device still uses."""
    removed = 0
    freed = 0
    errors: list[str] = []
    for package in packages:
        if cancel.is_set():
            errors.append("Stopped early. The rest were left in place.")
            break
        if not re.fullmatch(r"oem\d+\.inf", package.inf, flags=re.IGNORECASE):
            errors.append(f"{package.inf}: only published oemNN.inf packages can be removed.")
            continue
        progress(f"Removing {package.original} {package.version}…")
        completed = run_hidden(["pnputil", "/delete-driver", package.inf], timeout=120)
        if completed.returncode == 0:
            removed += 1
            freed += package.size
        else:
            message = _first_line(completed.stdout) or _first_line(completed.stderr) or f"pnputil returned {completed.returncode}."
            errors.append(f"{package.original} ({package.inf}): {message}")
    return removed, freed, errors


# ---------------------------------------------------------------- helpers


def _xml(text: str | None):
    raw = (text or "").lstrip("\ufeff")
    start = raw.find("<")
    if start < 0:
        return None
    try:
        return ET.fromstring(raw[start:])
    except ET.ParseError:
        return None


def _text(node, tag: str) -> str:
    if node is None:
        return ""
    found = node.find(tag)
    return (found.text or "").strip() if found is not None and found.text else ""
