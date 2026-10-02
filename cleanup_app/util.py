"""Small helpers used across the scanners."""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
from datetime import datetime

KB = 1024
MB = 1024**2
GB = 1024**3

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def format_size(num: int | float) -> str:
    value = float(max(0, num))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def format_when(timestamp: float) -> str:
    try:
        return datetime.fromtimestamp(timestamp).strftime("%d %b %Y")
    except (OSError, OverflowError, ValueError):
        return "Unknown date"


def idle_days(modified: float, accessed: float, now: float | None = None) -> int:
    moment = time.time() if now is None else now
    return max(0, int((moment - max(modified, accessed)) / 86400))


def clip(text: str, limit: int = 96) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def is_frozen() -> bool:
    """True when running as the packaged PC Cleanup.exe rather than from Python."""
    return bool(getattr(sys, "frozen", False))


def resource_path(*parts: str) -> str:
    """A file shipped with the app. The exe unpacks them to a temporary folder."""
    bundle = getattr(sys, "_MEIPASS", None)
    root = os.path.join(bundle, "cleanup_app") if bundle else os.path.dirname(os.path.abspath(__file__))
    return os.path.join(root, *parts)


def app_dir() -> str:
    root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    path = os.path.join(root, "PcCleanup")
    os.makedirs(path, exist_ok=True)
    return path


def backup_dir() -> str:
    path = os.path.join(app_dir(), "backups")
    os.makedirs(path, exist_ok=True)
    return path


def log_path() -> str:
    return os.path.join(app_dir(), "cleanup.log")


def log(message: str) -> None:
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  {message}\n"
    with open(log_path(), "a", encoding="utf-8") as handle:
        handle.write(line)


def read_log() -> str:
    path = log_path()
    if not os.path.isfile(path):
        return ""
    with open(path, encoding="utf-8", errors="replace") as handle:
        lines = handle.readlines()
    return "".join(lines[-400:])


def load_state() -> dict:
    path = os.path.join(app_dir(), "state.json")
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            data.setdefault("disabled_startup", [])
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"disabled_startup": []}


def save_state(data: dict) -> None:
    path = os.path.join(app_dir(), "state.json")
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
    os.replace(temporary, path)


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except OSError:
        return False


def relaunch_as_admin() -> bool:
    """Show the Windows administrator prompt and start an elevated copy.

    Returns True when that copy was launched. The current process should exit.
    Returns False when this process is already elevated, or the prompt was declined.
    """
    if is_admin():
        return False
    rest = [arg for arg in sys.argv[1:] if arg != "--no-admin"]
    if is_frozen():
        # PC Cleanup.exe: the program is the exe itself, with no script to pass.
        folder = os.path.dirname(sys.executable)
        args = rest
    else:
        script = os.path.abspath(sys.argv[0])
        folder = os.path.dirname(script)
        args = [script, *rest]
    ctypes.windll.shell32.ShellExecuteW.restype = ctypes.c_void_p
    launched = ctypes.windll.shell32.ShellExecuteW(
        None,
        "runas",
        sys.executable,
        subprocess.list2cmdline(args),
        folder,
        1,
    )
    return int(launched or 0) > 32


POWERSHELL_UTF8 = "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "


def run_hidden(args: list[str], timeout: int = 60, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run a console program with no window and return its output as text.

    Windows programs write their output in the console code page, which is
    different on a German, French, or Japanese PC, so the bytes are decoded
    here rather than with Python's default. PowerShell is told to answer in
    UTF-8, so names in any language come back intact. input_text should be
    ASCII, such as json.dumps output.
    """
    args = list(args)
    if args and os.path.basename(args[0]).lower() in {"powershell", "powershell.exe"} and "-Command" in args:
        index = args.index("-Command") + 1
        if index < len(args) and args[index] != "-":
            args[index] = POWERSHELL_UTF8 + args[index]
    # stdin is always given. A windowed app (pythonw, or the packaged exe) has no
    # console handles to pass on, and Windows rejects the invalid ones.
    completed = subprocess.run(
        args,
        capture_output=True,
        timeout=timeout,
        input=input_text.encode("utf-8") if input_text is not None else None,
        stdin=subprocess.DEVNULL if input_text is None else None,
        creationflags=NO_WINDOW,
        check=False,
    )
    return subprocess.CompletedProcess(args, completed.returncode, decode_output(completed.stdout), decode_output(completed.stderr))


def decode_output(data: bytes | None) -> str:
    """UTF-8 when it is valid UTF-8, otherwise this PC's OEM code page.

    A program started with no window gets a fresh console, and a fresh console
    writes in the OEM code page (437 in English, 850 in much of Europe, 932 in Japan).
    """
    if not data:
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return data.decode(f"cp{ctypes.windll.kernel32.GetOEMCP()}", errors="replace")
    except (LookupError, OSError, AttributeError):
        return data.decode("mbcs", errors="replace")


def ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def extract_exe(command: str) -> str:
    if not command:
        return ""
    text = os.path.expandvars(command.strip())
    if text.lower().startswith("\\??\\"):
        text = text[4:]
    if text.startswith('"'):
        end = text.find('"', 1)
        if end > 1:
            return text[1:end]
    lowered = text.lower()
    for marker in (".exe", ".bat", ".cmd", ".com"):
        index = lowered.find(marker)
        if index != -1:
            return text[: index + len(marker)].strip().strip('"')
    return text.split(" ")[0].strip('"')


def file_description(path: str) -> str:
    if not path or not os.path.isfile(path):
        return ""
    version = ctypes.windll.version
    size = version.GetFileVersionInfoSizeW(path, None)
    if not size:
        return ""
    buffer = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(path, 0, size, buffer):
        return ""
    pointer = ctypes.c_void_p()
    length = ctypes.c_uint()
    if not version.VerQueryValueW(
        buffer, "\\VarFileInfo\\Translation", ctypes.byref(pointer), ctypes.byref(length)
    ):
        return ""
    if not pointer.value or length.value < 4:
        return ""
    pair = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32)).contents.value
    lang = pair & 0xFFFF
    codepage = (pair >> 16) & 0xFFFF
    for field in ("FileDescription", "ProductName"):
        query = f"\\StringFileInfo\\{lang:04x}{codepage:04x}\\{field}"
        if version.VerQueryValueW(buffer, query, ctypes.byref(pointer), ctypes.byref(length)) and pointer.value:
            value = ctypes.cast(pointer, ctypes.c_wchar_p).value
            if value:
                return value
    return ""


def reveal_in_explorer(path: str) -> None:
    subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])


def shorten_path(path: str, limit: int = 88) -> str:
    if len(path) <= limit:
        return path
    return "…" + path[-(limit - 1) :]
