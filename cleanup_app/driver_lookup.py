"""Find the newest driver for each device, from more than one place.

Windows Update only offers the drivers Microsoft chooses to push to this PC.
Two more sources are checked here, side by side with it:

* The Microsoft Update Catalog, the public catalogue behind Windows Update.
  It lists every published version. Each device is searched by its own
  hardware IDs, and a result only counts when its package lists one of this
  device's exact hardware IDs. Card makers publish their own variants, so the
  newest version number is often a package for somebody else's card.
* NVIDIA's driver lookup, the service its download page uses. GeForce drivers
  reach it weeks before they reach Microsoft.

Answers are cached on this PC, so a second check is quick and polite.
"""

from __future__ import annotations

import base64
import hashlib
import html
import http.client
import json
import os
import platform
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from cleanup_app.util import app_dir, run_hidden

CATALOG = "https://www.catalog.update.microsoft.com"
NVIDIA_PRODUCTS = "https://www.nvidia.com/Download/API/lookupValueSearch.aspx?TypeID=3"
NVIDIA_LOOKUP = (
    "https://gfwsl.geforce.com/services_toolkit/services/com/nvidia/services/AjaxDriverService.php"
    "?func=DriverManualLookup&psid={psid}&pfid={pfid}&osID=57&languageCode=1033&beta=0&isWHQL=1"
    "&dltype=-1&dch=1&upCRD=0&qnf=0&sort1=0&numberOfResults=1"
)
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) PC-Cleanup/1.1"
WORKERS = 6
# Catalogue dates are when a package was published, a day or so either side of the
# driver's own date, so a little slack keeps a same-week package in play.
DATE_SLACK_DAYS = 3
SEARCH_TTL = 12 * 3600
NVIDIA_TTL = 12 * 3600
PRODUCTS_TTL = 7 * 86400
MAX_VERIFY = 8
# Inbox drivers in these classes are often generic stand-ins for a maker's driver,
# such as Microsoft's High Definition Audio driver on a Realtek sound chip.
LOOKUP_INBOX_CLASSES = {"display", "net", "media", "bluetooth"}
# Windows 11 release names and the build each one starts at.
WIN11_RELEASES = {"21H2": 22000, "22H2": 22621, "23H2": 22631, "24H2": 26100, "25H2": 26200}


@dataclass
class DriverOffer:
    source: str
    version: str
    date: str
    title: str
    size: int = 0
    update_id: str = ""
    url: str = ""
    hardware_id: str = ""
    update: object = None

    @property
    def installable(self) -> bool:
        return self.source in {"Windows Update", "Update Catalog"}


@dataclass
class CatalogEntry:
    update_id: str
    title: str
    products: str
    classification: str
    date: str
    version: str
    size: int


# ---------------------------------------------------------------- cache


class _Cache:
    """A small JSON cache shared by the lookup threads."""

    def __init__(self) -> None:
        self.path = os.path.join(app_dir(), "driver_lookup_cache.json")
        self.lock = threading.Lock()
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
            self.data = data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            self.data = {}
        self.dirty = False

    def get(self, key: str, ttl: float | None):
        with self.lock:
            item = self.data.get(key)
        if not isinstance(item, dict):
            return None
        if ttl is not None and time.time() - float(item.get("at", 0)) > ttl:
            return None
        return item.get("value")

    def put(self, key: str, value) -> None:
        with self.lock:
            self.data[key] = {"at": time.time(), "value": value}
            self.dirty = True

    def save(self) -> None:
        with self.lock:
            if not self.dirty:
                return
            # Keep it from growing forever: drop anything older than a month.
            cutoff = time.time() - 30 * 86400
            self.data = {key: item for key, item in self.data.items() if isinstance(item, dict) and float(item.get("at", 0)) >= cutoff}
            text = json.dumps(self.data, separators=(",", ":"))
            self.dirty = False
        temporary = self.path + ".tmp"
        try:
            with open(temporary, "w", encoding="utf-8") as handle:
                handle.write(text)
            os.replace(temporary, self.path)
        except OSError:
            pass


def clear_cache() -> None:
    try:
        os.remove(os.path.join(app_dir(), "driver_lookup_cache.json"))
    except OSError:
        pass


# ---------------------------------------------------------------- http


_local = threading.local()


class SourceError(OSError):
    """A source answered with an error page."""


def _connection(host: str, timeout: float) -> http.client.HTTPSConnection:
    # One kept-alive connection per worker thread and host. Opening a fresh TLS
    # connection for each of a hundred requests costs more than the requests.
    connections = getattr(_local, "connections", None)
    if connections is None:
        connections = _local.connections = {}
    connection = connections.get(host)
    if connection is None:
        proxy = _proxy_for(host)
        if proxy is None:
            connection = http.client.HTTPSConnection(host, timeout=timeout)
        else:
            # Offices and schools often only allow the web through a proxy.
            # The HTTPS connection is tunnelled through it, as a browser does.
            proxy_host, proxy_port, headers = proxy
            connection = http.client.HTTPSConnection(proxy_host, proxy_port, timeout=timeout)
            connection.set_tunnel(host, 443, headers=headers)
        connections[host] = connection
    return connection


def _proxy_for(host: str) -> tuple[str, int, dict] | None:
    """The proxy Windows or the environment sets for HTTPS, unless this host bypasses it."""
    try:
        address = urllib.request.getproxies().get("https") or ""
        if not address or urllib.request.proxy_bypass(host):
            return None
    except (OSError, ValueError):
        return None
    parts = urllib.parse.urlsplit(address if "://" in address else f"http://{address}")
    if not parts.hostname:
        return None
    headers = {}
    if parts.username:
        token = base64.b64encode(f"{urllib.parse.unquote(parts.username)}:{urllib.parse.unquote(parts.password or '')}".encode()).decode()
        headers["Proxy-Authorization"] = f"Basic {token}"
    return parts.hostname, parts.port or 8080, headers


def _drop(host: str) -> None:
    connection = getattr(_local, "connections", {}).pop(host, None)
    if connection is not None:
        connection.close()


def _fetch(url: str, data: bytes | None = None, timeout: float = 10, attempts: int = 3) -> str:
    """GET, or POST when data is given. A redirect means there is no such page, so the answer is empty.

    The timeout is short on purpose: one of the catalogue's addresses sometimes
    never answers, and Windows would otherwise wait 21 seconds before giving up
    on it. A dropped or refused request is tried again on a new connection.
    """
    parts = urllib.parse.urlsplit(url)
    path = parts.path + (f"?{parts.query}" if parts.query else "")
    headers = {"User-Agent": USER_AGENT, "Connection": "keep-alive"}
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    for attempt in range(attempts):
        last = attempt == attempts - 1
        connection = _connection(parts.netloc, timeout)
        try:
            connection.request("POST" if data is not None else "GET", path, body=data, headers=headers)
            response = connection.getresponse()
            body = response.read()
        except (OSError, http.client.HTTPException):
            _drop(parts.netloc)
            if last:
                raise
            time.sleep(0.5 * (attempt + 1))
            continue
        if response.getheader("Connection", "").lower() == "close":
            _drop(parts.netloc)
        if 300 <= response.status < 400:
            return ""
        if response.status in {429, 500, 502, 503, 504} and not last:
            time.sleep(1.5 * (attempt + 1))
            continue
        if response.status >= 400:
            raise SourceError(f"{parts.netloc} answered {response.status}.")
        return body.decode("utf-8", "replace")
    return ""


# ---------------------------------------------------------------- Update Catalog


def catalog_search(query: str, cache: _Cache) -> list[CatalogEntry]:
    key = "search:" + query.lower()
    cached = cache.get(key, SEARCH_TTL)
    if cached is not None:
        return [CatalogEntry(**item) for item in cached]
    page = _fetch(f"{CATALOG}/Search.aspx?q={urllib.parse.quote(query)}")
    entries = _parse_search(page)
    cache.put(key, [entry.__dict__ for entry in entries])
    return entries


def _parse_search(page: str) -> list[CatalogEntry]:
    entries = []
    for match in re.finditer(r'<tr id="([0-9a-f-]{36})_R(\d+)".*?</tr>', page, re.S):
        update_id, row = match.group(1), match.group(0)

        def cell(column: int) -> str:
            found = re.search(rf'_C{column}_R\d+">(.*?)</td>', row, re.S)
            text = re.sub(r"<[^>]+>", " ", found.group(1)) if found else ""
            return re.sub(r"\s+", " ", html.unescape(text)).strip()

        size = re.search(r'_originalSize">(\d+)<', row)
        entries.append(
            CatalogEntry(
                update_id=update_id,
                title=cell(1),
                products=cell(2),
                classification=cell(3),
                date=_us_date(cell(4)),
                version=cell(5),
                size=int(size.group(1)) if size else 0,
            )
        )
    return entries


def catalog_details(update_id: str, cache: _Cache) -> dict | None:
    """The hardware IDs and processor type a catalogue package is for. These never change."""
    key = "details:" + update_id
    cached = cache.get(key, None)
    if cached is not None:
        return cached
    page = _fetch(f"{CATALOG}/ScopedViewInline.aspx?updateid={update_id}")
    if not page or "driverhwIDs" not in page:
        return None
    block = page[page.find('id="driverhwIDs"') :]
    block = block[: block.find('id="ScopedViewHandler_labelSupportedUpdateIDs"')]
    ids = [html.unescape(item).strip().lower() for item in re.findall(r'<div style="padding-bottom: 0.3em;">(.*?)</div>', block, re.S)]
    arch = re.search(r'id="archDiv"[^>]*>(.*?)</div>', page, re.S)
    arch_text = re.sub(r"<[^>]+>|Architecture:", " ", arch.group(1)) if arch else ""
    details = {"ids": [item for item in ids if item], "arch": re.sub(r"\s+", " ", html.unescape(arch_text)).strip()}
    cache.put(key, details)
    return details


def catalog_download_urls(update_id: str) -> list[tuple[str, str]]:
    """Download links and their SHA-1 digests, from the catalogue's download dialog."""
    form = urllib.parse.urlencode(
        {"updateIDs": json.dumps([{"size": 0, "languages": "", "uidInfo": update_id, "updateID": update_id}])}
    ).encode()
    page = _fetch(f"{CATALOG}/DownloadDialog.aspx", data=form)
    urls = re.findall(r"downloadInformation\[\d+\]\.files\[(\d+)\]\.url\s*=\s*'([^']+)'", page)
    digests = dict(re.findall(r"downloadInformation\[\d+\]\.files\[(\d+)\]\.digest\s*=\s*'([^']+)'", page))
    return [(url, digests.get(index, "")) for index, url in urls if url.startswith("https://")]


def _us_date(text: str) -> str:
    match = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", text or "")
    if not match:
        return ""
    return f"{int(match.group(3)):04d}-{int(match.group(1)):02d}-{int(match.group(2)):02d}"


def _days_before(stamp: str, days: int) -> str:
    try:
        moment = time.mktime(time.strptime(stamp, "%Y-%m-%d")) - days * 86400
    except (TypeError, ValueError, OverflowError):
        return ""
    return time.strftime("%Y-%m-%d", time.localtime(moment))


def version_key(text: str) -> tuple[int, ...] | None:
    parts = (text or "").strip().split(".")
    if not parts or not all(part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def os_supported(products: str, build: int | None = None, win11: bool | None = None) -> bool:
    """True when one of the catalogue's product names covers the Windows on this PC."""
    build = build or sys.getwindowsversion().build
    win11 = build >= 22000 if win11 is None else win11
    for product in re.split(r",\s*(?=Windows)", products or ""):
        lowered = product.lower()
        if "server" in lowered or "windows" not in lowered:
            continue
        release = re.search(r"version\s+(\w+)\s+and\s+later", product, re.IGNORECASE)
        is_win11_product = "windows 11" in lowered or "windows - client" in lowered
        if is_win11_product:
            if not win11:
                continue
            if release is None:
                return True
            needed = WIN11_RELEASES.get(release.group(1).upper())
            if needed is None:
                # A release this app does not know yet is newer than every known one.
                if build > max(WIN11_RELEASES.values()):
                    return True
                continue
            if build >= needed:
                return True
        elif "windows 10" in lowered:
            return True
    return False


def _arch_ok(arch: str) -> bool:
    machine = platform.machine().upper()
    text = (arch or "").upper()
    if not text or text in {"N/A", "NA"}:
        return True
    if machine in {"AMD64", "X86_64"}:
        return "AMD64" in text or "X64" in text
    if machine == "ARM64":
        return "ARM64" in text
    return machine in text


def device_query(device) -> str:
    r"""One search per device, at the vendor-and-device level.

    The catalogue search matches part of an ID, so searching PCI\VEN_10DE&DEV_2C05
    also finds every card maker's package for that chip. Each result is then
    checked against this device's own IDs before it counts.
    """
    ids = [item for item in device.hardware_ids if "\\" in item]
    paired = [item for item in ids if re.search(r"(VEN|VID)_[0-9A-F]{4}.*(DEV|PID)_[0-9A-F]{4}", item, re.IGNORECASE)]
    if paired:
        return min(paired, key=len)
    matched = (getattr(device, "matching_id", "") or "").strip()
    if matched and len(matched) >= 6:
        return matched
    return ids[0] if ids else ""


def lookup_catalog(devices, cancel, progress) -> tuple[dict[str, DriverOffer], str]:
    """The newest catalogue driver that applies to each device. Returns offers and a note."""
    cache = _Cache()
    groups: dict[tuple, list] = {}
    for device in devices:
        if not wants_lookup(device):
            continue
        groups.setdefault(tuple(sorted(item.lower() for item in device.hardware_ids)), []).append(device)
    offers: dict[str, DriverOffer] = {}
    failures = 0
    done = 0
    total = len(groups)
    if not total:
        return offers, ""
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(_best_catalog_offer, members[0], cache, cancel): members for members in groups.values()}
        for future in as_completed(futures):
            done += 1
            if done % 3 == 0 or done == total:
                progress(f"Searching the Microsoft Update Catalog… {done} of {total} devices")
            try:
                offer = future.result()
            except (OSError, http.client.HTTPException, ValueError):
                failures += 1
                continue
            if offer is not None:
                for device in futures[future]:
                    offers[device.instance_id] = offer
    cache.save()
    note = ""
    if failures and not offers:
        note = "The Microsoft Update Catalog could not be reached. Check the internet connection."
    elif failures:
        note = f"The Microsoft Update Catalog did not answer for {failures} devices."
    return offers, note


def wants_lookup(device) -> bool:
    if device.problem_code in {22, 45}:
        return False
    if not device.inf or device.problem_code:
        return True
    if not device.inbox:
        return True
    return device.device_class.lower() in LOOKUP_INBOX_CLASSES


def _best_catalog_offer(device, cache: _Cache, cancel) -> DriverOffer | None:
    if cancel.is_set():
        return None
    # A maker's driver replacing a generic Windows one is worth showing whatever its
    # version. Otherwise a package must be newer by date as well as by number:
    # Windows ranks equal matches by date first, and makers sometimes restart
    # their numbering, so a bigger number alone can be an older driver.
    installed = version_key(device.version) if not device.inbox else None
    oldest = _days_before(device.date, DATE_SLACK_DAYS) if installed is not None else ""
    query = device_query(device)
    if not query:
        return None
    candidates: dict[str, CatalogEntry] = {}
    for entry in catalog_search(query, cache):
        if "driver" not in entry.classification.lower() or not os_supported(entry.products):
            continue
        key = version_key(entry.version)
        if key is None or (installed is not None and key <= installed):
            continue
        if oldest and entry.date and entry.date < oldest:
            continue
        candidates.setdefault(entry.update_id, entry)
    ordered = sorted(candidates.values(), key=lambda item: (version_key(item.version), item.date), reverse=True)
    for entry in ordered[:MAX_VERIFY]:
        if cancel.is_set():
            return None
        details = catalog_details(entry.update_id, cache)
        if not details or not _arch_ok(details.get("arch", "")):
            continue
        listed = set(details.get("ids") or [])
        # Windows only installs a driver whose package lists one of the device's own IDs.
        match = next((item for item in device.hardware_ids if item.lower() in listed), "")
        if match:
            return DriverOffer(
                source="Update Catalog",
                version=entry.version,
                date=entry.date,
                title=entry.title,
                size=entry.size,
                update_id=entry.update_id,
                hardware_id=match,
            )
    return None


# ---------------------------------------------------------------- NVIDIA


def lookup_nvidia(devices, cancel) -> tuple[dict[str, DriverOffer], str]:
    gpus = [device for device in devices if device.is_gpu and "nvidia" in f"{device.manufacturer} {device.provider}".lower()]
    if not gpus or cancel.is_set():
        return {}, ""
    cache = _Cache()
    try:
        products = cache.get("nvidia:products", PRODUCTS_TTL)
        if products is None:
            root = ET.fromstring(_fetch(NVIDIA_PRODUCTS))
            products = [
                [value.findtext("Name") or "", value.findtext("Value") or "", value.get("ParentID") or ""]
                for value in root.iter("LookupValue")
            ]
            cache.put("nvidia:products", products)
        offers = {}
        for gpu in gpus:
            wanted = _nvidia_name(gpu.device)
            match = next((item for item in products if _nvidia_name(item[0]) == wanted), None)
            if match is None:
                continue
            key = f"nvidia:driver:{match[2]}:{match[1]}"
            info = cache.get(key, NVIDIA_TTL)
            if info is None:
                data = json.loads(_fetch(NVIDIA_LOOKUP.format(psid=match[2], pfid=match[1])))
                found = (data.get("IDS") or [{}])[0].get("downloadInfo") or {}
                info = {
                    "version": str(found.get("Version") or ""),
                    "date": _nvidia_date(str(found.get("ReleaseDateTime") or "")),
                    "name": urllib.parse.unquote(str(found.get("Name") or "GeForce driver")),
                    "url": str(found.get("DownloadURL") or ""),
                }
                cache.put(key, info)
            if info.get("version") and info.get("url", "").startswith("https://"):
                offers[gpu.instance_id] = DriverOffer("NVIDIA", info["version"], info["date"], info["name"], url=info["url"])
        cache.save()
        return offers, ""
    except (OSError, http.client.HTTPException, ValueError, ET.ParseError) as exc:
        return {}, f"NVIDIA's driver lookup could not be reached. {exc}"


def _nvidia_name(name: str) -> str:
    lowered = re.sub(r"\(r\)|\(tm\)|®|™", "", (name or "").lower())
    lowered = lowered.replace("nvidia", "").replace("geforce", "")
    return re.sub(r"\s+", " ", lowered).strip()


def _nvidia_date(text: str) -> str:
    try:
        return time.strftime("%Y-%m-%d", time.strptime(text, "%a %b %d, %Y"))
    except ValueError:
        return ""


# ---------------------------------------------------------------- install from the catalogue


def install_from_catalog(offer: DriverOffer, cancel, progress) -> tuple[str, bool]:
    """Download a catalogue package, check its digest, unpack it, and hand it to pnputil.

    pnputil only installs a driver that Windows ranks as a better match than
    the one in use, and it checks the driver's signature. Needs administrator.
    """
    files = catalog_download_urls(offer.update_id)
    if not files:
        raise RuntimeError("The catalogue did not give a download link for this driver.")
    folder = os.path.join(app_dir(), "driver_downloads", offer.update_id)
    shutil.rmtree(folder, ignore_errors=True)
    unpacked = os.path.join(folder, "unpacked")
    os.makedirs(unpacked, exist_ok=True)
    try:
        for index, (url, digest) in enumerate(files):
            target = os.path.join(folder, f"package{index}.cab")
            _download(url, target, digest, cancel, progress, offer.title)
            if cancel.is_set():
                return "Stopped before installing. Nothing was changed.", False
            progress(f"Unpacking {offer.title}…")
            expanded = run_hidden(["expand", "-F:*", target, unpacked], timeout=1800)
            if expanded.returncode != 0:
                raise RuntimeError(f"Windows could not unpack the download. {expanded.stdout.strip()[-200:]}")
            os.remove(target)
        if not any(name.lower().endswith(".inf") for _root, _dirs, names in os.walk(unpacked) for name in names):
            raise RuntimeError("The download did not contain a driver.")
        progress(f"Installing {offer.title}…")
        completed = run_hidden(["pnputil", "/add-driver", os.path.join(unpacked, "*.inf"), "/subdirs", "/install"], timeout=1800)
        text = (completed.stdout or "") + (completed.stderr or "")
        reboot = completed.returncode == 3010 or "reboot" in text.lower()
        if completed.returncode not in (0, 3010):
            last = [line.strip() for line in text.splitlines() if line.strip()]
            raise RuntimeError(last[-1] if last else f"pnputil returned {completed.returncode}.")
        return f"Installed: {offer.title}", reboot
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _download(url: str, target: str, digest: str, cancel, progress, title: str) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    sha1 = hashlib.sha1()
    with urllib.request.urlopen(request, timeout=60) as response, open(target, "wb") as handle:
        total = int(response.headers.get("Content-Length") or 0)
        received = 0
        last = 0.0
        while True:
            if cancel.is_set():
                return
            chunk = response.read(1 << 20)
            if not chunk:
                break
            handle.write(chunk)
            sha1.update(chunk)
            received += len(chunk)
            moment = time.monotonic()
            if moment - last > 0.5:
                last = moment
                share = f"{received / total:.0%} of " if total else ""
                progress(f"Downloading {title}… {share}{received / 1048576:.0f} MB")
    if digest and base64.b64encode(sha1.digest()).decode() != digest:
        raise RuntimeError("The download did not match the catalogue's checksum, so it was not installed.")



def open_download(offer: DriverOffer) -> None:
    webbrowser.open(offer.url)
