"""Checks the scanners without deleting anything you own."""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import threading
import time
import traceback

import base64
import json
from datetime import date

from cleanup_app.ai_analyst import _startup_notes, validate_endpoint
from cleanup_app.space import collapse_slices, fixed_drive_roots, scan_space
from cleanup_app.chatgpt_login import (
    _usable_jwks_cache,
    build_authorize_url,
    code_challenge,
    completed_text,
    endpoint_is_openai,
    verify_id_token,
)
from cleanup_app.filescan import analyze_file, scan_files
from cleanup_app.junk import clean_categories
from cleanup_app.models import DriveMap, FileHit, FolderNode, JunkCategory, RegistryIssue, ServiceInfo, SpaceReport, SpaceSlice, StartupItem
from cleanup_app.actions import delete_personal_files
from cleanup_app.diskinfo import get_disks
from cleanup_app.recycle import empty_recycle_bin, query_recycle_bin, send_to_recycle
from cleanup_app.ai_analyst import build_report
from cleanup_app.registry_scan import issue_is_contained, missing_local_file, scan_registry
from cleanup_app.safety import approved_junk_path
from cleanup_app.services_scan import scan_services
from cleanup_app.startup_scan import scan_startup
from cleanup_app.scan_store import TREE_FILE, format_scan_time, pack_snapshot, read_snapshot, write_snapshot, write_trees
from cleanup_app import procsnap
from cleanup_app.drivers import DeviceDriver, DriverUpdate, friendly_version, match_updates, parse_driver_version, _add_offers, web_search_query, web_search_url
from cleanup_app.driver_lookup import DriverOffer, _parse_search as parse_catalog_search, device_query as lookup_query, os_supported
from cleanup_app.performance import GUID_PATTERN, PROTECTED, read_animations
from cleanup_app.util import format_size


def self_test() -> int:
    try:
        _run()
    except Exception:
        traceback.print_exc()
        return 1
    print("Self-test passed.")
    return 0


def _recovery_checks() -> None:
    """Deleted-file recovery against a small NTFS image, so it runs without administrator."""
    import ctypes
    import struct

    from cleanup_app import ntfs_fixture, recover

    folder = tempfile.mkdtemp(prefix="pc-cleanup-recover-")
    image = os.path.join(folder, "volume.img")
    ntfs_fixture.build(image)

    class Strict:
        """A raw volume refuses reads that are not whole sectors. A plain file does not, so check here."""

        def __init__(self, real, check):
            self.real, self.check, self.argtypes = real, check, None

        def __call__(self, *args):
            self.check(*args)
            self.real.argtypes = self.argtypes
            return self.real(*args)

    def seek_check(_handle, offset, *_rest):
        assert offset.value % 512 == 0, f"unaligned seek {offset.value}"

    def read_check(_handle, _buffer, size, *_rest):
        assert size % 512 == 0, f"unaligned read of {size} bytes"

    class Volume:
        def __init__(self):
            real = ctypes.WinDLL("kernel32", use_last_error=True)
            self.SetFilePointerEx = Strict(real.SetFilePointerEx, seek_check)
            self.ReadFile = Strict(real.ReadFile, read_check)
            self.CloseHandle = real.CloseHandle
            real.CreateFileW.restype = ctypes.c_void_p
            real.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
            self.handle = real.CreateFileW(image, 0x80000000, 7, None, 3, 0x80, None)

    saved = (recover._open_volume, recover._filesystem, recover.is_admin)
    try:
        def open_image(_letter):
            volume = Volume()
            return volume, volume.handle

        recover._open_volume = open_image
        recover._filesystem = lambda _letter: "NTFS"
        recover.is_admin = lambda: True
        scan = recover.scan_deleted(["X"], "", "", "", None, None, False, True, threading.Event(), lambda _message: None)
        found = {item.name: item for item in scan.files}
        assert set(found) == {"report.bin", "note.txt", "reused.bin"}, (set(found), scan.notes)
        assert found["report.bin"].original_path == "X:\\Docs\\report.bin", found["report.bin"].original_path
        assert found["report.bin"].chance == "Good" and found["note.txt"].chance == "Good"
        assert found["reused.bin"].chance == "Poor", "a deleted file whose space was reused must be flagged"
        out = os.path.join(folder, "restored")
        for item in scan.files:
            item.checked = item.name in {"report.bin", "note.txt"}
        restored, skipped, errors = recover.restore_files(scan.files, out, False, threading.Event(), lambda _message: None)
        assert restored == 2 and not errors, errors
        with open(os.path.join(out, "Docs", "report.bin"), "rb") as handle:
            assert handle.read() == ntfs_fixture.REPORT, "the restored copy must match the deleted file byte for byte"
        with open(os.path.join(out, "Docs", "note.txt"), "rb") as handle:
            assert handle.read() == ntfs_fixture.NOTE
        found["report.bin"].drive = os.path.splitdrive(folder)[0][:1]
        try:
            recover.restore_files(scan.files, out, False, threading.Event(), lambda _message: None)
        except PermissionError:
            pass
        else:
            raise AssertionError("restoring onto the drive being recovered from must be refused")
    finally:
        recover._open_volume, recover._filesystem, recover.is_admin = saved

    # A Recycle Bin holding one deleted file and one deleted folder.
    bin_root = os.path.join(folder, "$Recycle.Bin", "S-1-5-21-test")
    os.makedirs(os.path.join(bin_root, "$RABC123"))
    with open(os.path.join(bin_root, "$RABC123", "inside.txt"), "w", encoding="utf-8") as handle:
        handle.write("in a deleted folder")
    with open(os.path.join(bin_root, "$RDEF456.txt"), "w", encoding="utf-8") as handle:
        handle.write("deleted file")

    def info(path: str) -> bytes:
        return struct.pack("<QQQI", 2, 12, 133_000_000_000_000_000, len(path) + 1) + (path + "\0").encode("utf-16le")

    for name, original in (("$IABC123", r"C:\Users\Someone\Old folder"), ("$IDEF456.txt", r"C:\Users\Someone\notes.txt")):
        with open(os.path.join(bin_root, name), "wb") as handle:
            handle.write(info(original))
    listed = {os.path.basename(original) for _data, _info, _size, _when, original in recover.list_recycle_bin(os.path.join(folder, "$Recycle.Bin"))}
    assert listed == {"Old folder", "notes.txt"}, listed
    shutil.rmtree(folder, ignore_errors=True)


def _run() -> None:
    _recovery_checks()
    assert validate_endpoint("https://api.openai.com/v1") == "https://api.openai.com/v1"
    assert validate_endpoint("http://127.0.0.1:1234/v1/") == "http://127.0.0.1:1234/v1"
    try:
        validate_endpoint("http://example.com/v1")
    except ValueError:
        pass
    else:
        raise AssertionError("plain http endpoints must be refused")
    verifier = "the-verifier"
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert code_challenge(verifier) == expected
    assert not endpoint_is_openai("http://auth.openai.com/api/accounts/authorize")
    assert not endpoint_is_openai("https://evil.example/api/accounts/authorize")
    authorize = build_authorize_url(
        "https://auth.openai.com/api/accounts/authorize",
        "http://127.0.0.1:1455/auth/callback",
        "state",
        "nonce",
        verifier,
        "urn:uuid:11111111-1111-1111-1111-111111111111",
        "dynamic_agent_client",
    )
    assert authorize.startswith("https://auth.openai.com/api/accounts/authorize?")
    assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A1455%2Fauth%2Fcallback" in authorize
    assert "localhost" not in authorize
    try:
        build_authorize_url("https://evil.example/authorize", "http://127.0.0.1:1455/auth/callback", "s", "n", verifier, "urn:uuid:x", "dynamic_agent_client")
    except ValueError:
        pass
    else:
        raise AssertionError("sign-in must stay on OpenAI")
    sample = (
        b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"Hi"}\n\n'
        b'data: {"type":"response.completed","response":{}}\n\n'
    )
    assert completed_text(sample) == "Hi"
    assert _usable_jwks_cache("https://auth.openai.com/.well-known/jwks.json", time.time()) is None
    assert _usable_jwks_cache({"expires": time.time() + 60, "keys": [{"kid": "a"}]}, time.time())[0]["kid"] == "a"
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps({}).encode()).rstrip(b"=").decode()
    try:
        verify_id_token(f"{header}.{payload}.x", "client", "nonce")
    except ValueError:
        pass
    else:
        raise AssertionError("unsigned ChatGPT tokens must be refused")
    assert format_size(1536).endswith("KB")
    windir = os.environ.get("WINDIR", r"C:\Windows")
    assert not approved_junk_path(windir, "tree", [])
    assert not approved_junk_path(os.path.join(windir, "System32"), "tree", [])
    temp = os.environ.get("TEMP") or ""
    if temp:
        assert approved_junk_path(temp, "tree", [])

    now = time.time()
    old = now - 400 * 86400
    installer = analyze_file(r"C:\Users\A\Downloads\setup_game.exe", 800 * 1024 * 1024, old, old, now)
    assert installer.precheck, installer
    photo = analyze_file(r"C:\Users\A\Pictures\holiday.jpg", 80 * 1024 * 1024, old, old, now)
    assert not photo.precheck
    assert photo.score <= 50

    contained = RegistryIssue(
        id="1",
        kind="startup",
        hive_name="HKCU",
        key=r"Software\Microsoft\Windows\CurrentVersion\Run",
        value_name="OldApp",
        problem="",
        detail="",
    )
    assert issue_is_contained(contained)
    outside = RegistryIssue(
        id="2",
        kind="startup",
        hive_name="HKCU",
        key=r"Software\Microsoft\Windows\CurrentVersion\Policies",
        value_name="x",
        problem="",
        detail="",
    )
    assert not issue_is_contained(outside)
    apppath = RegistryIssue(
        id="3",
        kind="apppath",
        hive_name="HKLM",
        key=r"Software\Microsoft\Windows\CurrentVersion\App Paths\gone.exe",
        value_name="",
        problem="",
        detail="",
    )
    assert issue_is_contained(apppath)
    parent_list = RegistryIssue(
        id="4",
        kind="apppath",
        hive_name="HKLM",
        key=r"Software\Microsoft\Windows\CurrentVersion\App Paths",
        value_name="",
        problem="",
        detail="",
    )
    assert not issue_is_contained(parent_list)
    assert missing_local_file(r"C:\no-such-pc-cleanup-file.exe").lower().endswith("no-such-pc-cleanup-file.exe")
    assert missing_local_file(r"\\server\share\file.exe") == ""

    notepad = os.path.join(windir, "System32", "notepad.exe")
    if os.path.isfile(notepad):
        decoy = FileHit(notepad, 1, old, old, 0, "Likely keep", [], False, r"C:\\", checked=True)
        report, removed = delete_personal_files([decoy], [r"C:\\"], False, threading.Event(), lambda _message: None)
        assert removed == []
        assert report.skipped == 1
        assert os.path.isfile(notepad)

    folder = os.path.join(os.path.expanduser("~"), "pc-cleanup-selftest-downloads")
    os.makedirs(folder, exist_ok=True)
    try:
        setup = os.path.join(folder, "setup_tool.exe")
        recent = os.path.join(folder, "notes.pdf")
        picture = os.path.join(folder, "holiday.jpg")
        for path, stamp, payload in (
            (setup, old, b"x" * 1200),
            (picture, old, b"x" * 1200),
            (recent, now, b"x" * 1200),
        ):
            with open(path, "wb") as handle:
                handle.write(payload)
            os.utime(path, (stamp, stamp))
        result = scan_files([folder], 100, 180, False, threading.Event(), lambda _message: None)
        names = {os.path.basename(hit.path): hit for hit in result.hits}
        assert "setup_tool.exe" in names, names
        assert names["setup_tool.exe"].precheck
        assert "holiday.jpg" in names and not names["holiday.jpg"].precheck
        assert "notes.pdf" not in names

        cache = os.path.join(folder, "cache")
        os.makedirs(cache)
        leftover = os.path.join(cache, "leftover.tmp")
        with open(leftover, "wb") as handle:
            handle.write(b"temp")
        os.utime(leftover, (old, old))
        category = JunkCategory(
            id="test",
            name="Test cache",
            group="Safe",
            safety="safe",
            description="",
            paths=[cache],
            mode="tree",
            checked=True,
        )
        cleaned = clean_categories([category], threading.Event(), lambda _message: None)
        assert not os.path.exists(leftover), cleaned
        assert cleaned.deleted_files == 1

        precious = os.path.join(folder, "documents")
        os.makedirs(precious)
        keep = os.path.join(precious, "thesis.docx")
        with open(keep, "wb") as handle:
            handle.write(b"keep me")
        os.utime(keep, (old, old))
        tampered = JunkCategory(id="x", name="Tampered", group="Safe", safety="safe", description="", paths=[precious], checked=True)
        refused = clean_categories([tampered], threading.Event(), lambda _message: None)
        assert os.path.exists(keep), "an unapproved folder was cleaned"
        assert refused.deleted_files == 0 and refused.errors
    finally:
        shutil.rmtree(folder, ignore_errors=True)

    _size, _count = query_recycle_bin()
    sample = os.path.join(tempfile.gettempdir(), "pc-cleanup-self-test.txt")
    before_items = _count
    with open(sample, "w", encoding="utf-8") as handle:
        handle.write("safe to delete")
    try:
        send_to_recycle([sample])
        assert not os.path.exists(sample)
    finally:
        if os.path.exists(sample):
            os.remove(sample)
        if before_items == 0:
            empty_recycle_bin()

    disks = get_disks()
    assert disks, "expected at least one drive"
    print("Drives:", ", ".join(f"{disk.letter}: {disk.media} {format_size(disk.free)} free" for disk in disks))

    services = scan_services(threading.Event(), lambda _message: None, sample_cpu=False)
    assert len(services) > 20, len(services)
    locked = [service for service in services if service.critical]
    assert locked and locked[0].actions == []
    print(f"Services: {len(services)}")

    startup = scan_startup(threading.Event(), lambda _message: None)
    print(f"Startup items: {len(startup)}")
    registry = scan_registry(threading.Event(), lambda _message: None)
    print(f"Registry leftovers: {len(registry)}")
    for issue in registry:
        assert issue_is_contained(issue), issue.key
        assert issue.recommendation in {"Safe to remove", "Review first"}
        assert issue.fix

    impersonated, _siblings = _startup_notes(
        r"C:\Users\Public\helper.exe",
        "Windows System Cache Manager",
        "Helper",
    )
    assert any("outside the Windows folder" in note for note in impersonated)
    ordinary, _ignored = _startup_notes(r"C:\Users\Public\Discord.exe", "Discord", "Discord")
    assert not any("outside the Windows folder" in note for note in ordinary)
    collapsed = collapse_slices(
        [SpaceSlice(f"Folder {index}", index) for index in range(1, 12)] + [SpaceSlice("Free space", 50, "", "free")]
    )
    assert sum(item.size for item in collapsed if item.kind == "folder") == sum(range(1, 12))
    assert any(item.kind == "free" for item in collapsed)
    roots = fixed_drive_roots()
    assert roots and all(len(item) == 3 and item.endswith("\\") for item in roots)
    sample_root = os.path.join(os.environ.get("USERPROFILE") or tempfile.gettempdir(), "pc-cleanup-selftest-space")
    os.makedirs(os.path.join(sample_root, "Windows"), exist_ok=True)
    os.makedirs(os.path.join(sample_root, "Users"), exist_ok=True)
    try:
        with open(os.path.join(sample_root, "Windows", "a.bin"), "wb") as handle:
            handle.write(b"x" * 1000)
        with open(os.path.join(sample_root, "Users", "b.bin"), "wb") as handle:
            handle.write(b"y" * 4000)
        mapped = scan_space(threading.Event(), lambda _message: None, sample_root)
        sizes = {item.name: item.size for drive in mapped.drives for item in drive.slices}
        assert sizes.get("Windows") == 1000, sizes
        assert sizes.get("Users") == 4000, sizes
        tree_sizes = {child.name: child.size for drive in mapped.drives if drive.tree for child in drive.tree.children}
        assert tree_sizes.get("Windows") == 1000, tree_sizes
        assert tree_sizes.get("Users") == 4000, tree_sizes
        assert mapped.measured == 5000
        assert len(mapped.drives) == 1
        text = build_report(
            threading.Event(),
            lambda _message: None,
            {"space": mapped, "registry": [], "startup": [], "services": [], "junk": [], "files": None},
        )
        assert "Where the space is" in text
        assert "Users" in text and "3.9 KB" in text
        assert "Nothing is sent" not in text or "Caches" in text
    finally:
        shutil.rmtree(sample_root, ignore_errors=True)

    node = FolderNode("Windows", os.path.join(sample_root, "Windows"), 1000, [FolderNode("a.bin", os.path.join(sample_root, "Windows", "a.bin"), 1000)])
    drive = DriveMap(sample_root, [SpaceSlice("Windows", 1000)], 1000, 1, node)
    stored = SpaceReport(
        [drive],
        [],
        [],
        [],
        ["note"],
        1000,
        1,
        0,
        False,
        [("Hidden on C:", "About 1 GB could not be opened.", "info")],
    )
    startup = StartupItem("id", "Old", "C:\\gone.exe", "HKCU", "HKCU", "registry", True, True, "", {"name": "Old"})
    service = ServiceInfo("Svc", "Service", "", "running", "automatic", "", None, 10, 0.0, False, "windows", "Low", "", False, False, False)
    saved_path = os.path.join(tempfile.mkdtemp(), "last_scan.json")
    write_trees(stored, os.path.join(os.path.dirname(saved_path), TREE_FILE))
    assert stored.tree_id
    payload = pack_snapshot(1_700_000_000.0, None, stored, [], [parent_list], None, [startup], [service], "local report", "ai write-up")
    assert payload["space"]["drives"][0]["tree"] is None, "the tree must not be written into the small snapshot"
    try:
        write_snapshot(payload, saved_path)
        loaded = read_snapshot(saved_path)
        assert loaded["ai_report"] == "local report"
        assert loaded["ai_answer"] == "ai write-up"
        assert loaded["space"].drives[0].tree.children[0].name == "a.bin"
        assert loaded["space"].drives[0].tree.children[0].size == 1000
        assert loaded["space"].explanations[0][0] == "Hidden on C:"
        assert loaded["registry"][0].recommendation == ""
        assert loaded["startup"][0].payload["name"] == "Old"
        assert loaded["services"][0].pid is None
        assert loaded["services"][0].actions == []
        assert format_scan_time(loaded["saved_at"])
        old_format = dict(payload, version=1)
        old_format["space"] = dict(payload["space"], tree_id="")
        old_format["space"]["drives"] = [dict(old_format["space"]["drives"][0], tree={"name": "Windows", "path": "x", "size": 1000, "children": [{"name": "a.bin", "path": "y", "size": 1000, "children": []}]})]
        write_snapshot(old_format, saved_path)
        upgraded = read_snapshot(saved_path)
        assert upgraded["space"].drives[0].tree.children[0].name == "a.bin", "scans saved by older versions must still load"
        with open(saved_path, "w", encoding="utf-8") as handle:
            handle.write("{")
        assert read_snapshot(saved_path) is None
    finally:
        shutil.rmtree(os.path.dirname(saved_path), ignore_errors=True)

    assert parse_driver_version("05/05/2026 32.0.15.9649") == (date(2026, 5, 5), "32.0.15.9649")
    assert parse_driver_version("16/12/2022 1.0.0.7")[0] == date(2022, 12, 16)
    assert friendly_version("NVIDIA", "32.0.15.9649") == "596.49"
    assert friendly_version("NVIDIA", "32.0.16.1088") == "610.88"
    assert friendly_version("Intel Corporation", "32.0.101.6556") == ""
    gpu = DeviceDriver("PCI\\X", "GPU", "Display", "Display adapters", "NVIDIA", "Started", 0, "", "oem1.inf", "nv.inf", "NVIDIA", "1.0", "2026-01-01", "", ["PCI\\VEN_10DE&DEV_2C05&SUBSYS_1", "PCI\\VEN_10DE&DEV_2C05"])
    offer = DriverUpdate("id", "NVIDIA Display Driver Update (32.0.16.1088)", "NVIDIA", "GPU", "Video", "2026-07-22", "32.0.16.1088", "pci\\ven_10de&dev_2c05", 1)
    match_updates([gpu], [offer])
    assert gpu.update is offer, "Windows Update matches hardware IDs without regard to case"
    assert gpu.best.source == "Windows Update"

    # The newest offer wins across sources. A GeForce card compares in NVIDIA's numbers.
    gpu.manufacturer = gpu.provider = "NVIDIA"
    _add_offers([gpu], {gpu.instance_id: DriverOffer("NVIDIA", "617.14", "2026-09-22", "Game Ready", url="https://x")})
    assert gpu.best.source == "NVIDIA" and gpu.shown_version(gpu.offers[0]) == "610.88"
    current = DeviceDriver(r"PCI\Y", "GPU", "Display", "Display adapters", "NVIDIA", "Started", 0, "", "oem1.inf", "nv.inf", "NVIDIA", "32.0.16.1714", "2026-09-22", "")
    _add_offers([current], {current.instance_id: DriverOffer("NVIDIA", "617.14", "2026-09-22", "Game Ready", url="https://x")})
    assert not current.offers, "NVIDIA's newest only counts when it is newer than what is installed"
    assert lookup_query(gpu) == r"PCI\VEN_10DE&DEV_2C05", lookup_query(gpu)
    assert web_search_query(gpu).startswith("GPU driver"), web_search_query(gpu)
    unknown = DeviceDriver(r"USB\X", "USB Input Device", "HIDClass", "Human interface devices", "(Standard system devices)", "Started", 0, "", "input.inf", "input.inf", "Microsoft", "10.0.1", "2006-06-21", "", [r"USB\VID_046D&PID_C547&REV_0100", r"USB\VID_046D&PID_C547"], inbox=True)
    assert web_search_query(unknown) == '"VID_046D&PID_C547" driver', web_search_query(unknown)
    assert web_search_url(unknown).startswith("https://www.google.com/search?q=%22VID_046D%26PID_C547")

    # Catalogue results only count for the Windows this PC runs.
    assert os_supported("Windows 11 Client, version 24H2 and later, Servicing Drivers", build=26200)
    assert not os_supported("Windows 11 Client, version 24H2 and later, Servicing Drivers", build=22631)
    assert not os_supported("Windows 11 Client, version 26H1 and later, Servicing Drivers", build=26200)
    assert os_supported("Windows 10, version 1903 and later, Servicing Drivers", build=26200)
    assert not os_supported("Windows Server 2022 and later, Servicing Drivers", build=26200)
    sample_row = (
        '<tr id="9dcb99c6-961a-4f79-a02b-6d040f3bdff5_R0" style="">'
        '<td id="9dcb99c6-961a-4f79-a02b-6d040f3bdff5_C1_R0"><a>NVIDIA Display Driver Update (32.0.16.1088)</a></td>'
        '<td id="9dcb99c6-961a-4f79-a02b-6d040f3bdff5_C2_R0">Windows 11 Client,  version 24H2 and later</td>'
        '<td id="9dcb99c6-961a-4f79-a02b-6d040f3bdff5_C3_R0">Drivers (Video)</td>'
        '<td id="9dcb99c6-961a-4f79-a02b-6d040f3bdff5_C4_R0">7/21/2026</td>'
        '<td id="9dcb99c6-961a-4f79-a02b-6d040f3bdff5_C5_R0">32.0.16.1088</td>'
        '<td id="9dcb99c6-961a-4f79-a02b-6d040f3bdff5_C6_R0"><span id="x_originalSize">1235887641</span></td></tr>'
    )
    parsed = parse_catalog_search(sample_row)
    assert len(parsed) == 1 and parsed[0].version == "32.0.16.1088" and parsed[0].date == "2026-07-21" and parsed[0].size == 1235887641, parsed

    # Plan names can contain brackets. A missed "*" made a custom plan look unknown.
    plan = GUID_PATTERN.search("Power Scheme GUID: cab200f4-3141-4c3b-ae83-5f80cae4c8d9  (Ultimate Performance (ExitLag)) *")
    assert plan.group(2) == "Ultimate Performance (ExitLag)" and plan.group(3) == "*"
    assert "explorer.exe" in PROTECTED and "lsass.exe" in PROTECTED
    assert isinstance(read_animations(), dict)
    snap = procsnap.snapshot()
    if snap is not None:
        me = snap.get(os.getpid())
        assert me is not None and me.name.lower() == os.path.basename(sys.executable).lower(), me
        assert me.working_set > 0 and me.private_working_set <= me.working_set

    import customtkinter as ctk

    ctk.set_appearance_mode("dark")
    from cleanup_app.ui import CleanupApp

    app = CleanupApp(autoload=False)
    app.update()
    for page in ("files", "junk", "recover", "services", "startup", "registry", "ai", "activity", "performance", "drivers", "dashboard"):
        app.select_nav(page)
        app.update()
    # Pages are cached, so a second visit shows the same frame without rebuilding it.
    built = app._pages["junk"]
    app.select_nav("junk")
    app.update()
    assert app._pages["junk"] is built
    app.invalidate()
    app.select_nav("junk")
    app.update()
    assert app._pages["junk"] is not built, "a page must rebuild after its data changes"
    app.on_close()
