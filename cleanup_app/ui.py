"""Desktop window for PC Cleanup."""

from __future__ import annotations

import ctypes
import math
import os
import queue
import sys
import threading
import time
import traceback
import tkinter as tk
from tkinter import Canvas, filedialog

import customtkinter as ctk

from cleanup_app import __version__
from cleanup_app.actions import collect_snapshot, delete_personal_files
from cleanup_app.ai_analyst import PROVIDERS, ask_model, build_report, load_ai_settings, save_ai_settings
from cleanup_app.chatgpt_login import (
    account_summary,
    ask_with_plan,
    open_usage_settings,
    remember_model_label,
    sign_in,
    sign_out,
)
from cleanup_app.diskinfo import data_drive_letters, known_folders
from cleanup_app.junk import clean_categories, scan_junk
from cleanup_app.filescan import scan_files
from cleanup_app.models import FileHit, JunkCategory, ServiceInfo, StartupItem
from cleanup_app.registry_scan import KIND_GUIDE, remove_issues, scan_registry
from cleanup_app.safety import is_system_location
from cleanup_app.scan_store import format_scan_time, pack_snapshot, read_snapshot, write_snapshot, write_trees
from cleanup_app.space import scan_space
from cleanup_app.services_scan import scan_services, set_start_type, stop_service
from cleanup_app.startup_scan import disable_startup, enable_startup, scan_startup
from cleanup_app.theme import ACCENT, ACCENT_2, BG, CARD, CARD_2, DANGER, DANGER_2, FAINT, INFO, LINE, MUTED, OK, SIDEBAR, TEXT, WARN
from cleanup_app.util import (
    backup_dir,
    clip,
    format_size,
    idle_days,
    is_admin,
    log,
    read_log,
    relaunch_as_admin,
    resource_path,
    reveal_in_explorer,
    shorten_path,
)
from cleanup_app.ui_drivers import DriversPage
from cleanup_app.ui_performance import PerformancePage
from cleanup_app.ui_recover import RecoverPage
from cleanup_app.ui_tree import FolderBreakdown

FILE_PAGE = 18
SERVICE_PAGE = 30
IMPACT_COLOR = {
    "High CPU": DANGER,
    "High memory": DANGER,
    "Medium memory": WARN,
    "Boots with Windows": WARN,
    "Shared host": WARN,
    "Security": INFO,
    "System": FAINT,
    "Low": OK,
    "Not running": FAINT,
}
VERDICT_COLOR = {
    "Likely unused": WARN,
    "Probably unused": INFO,
    "Review": MUTED,
    "Likely keep": OK,
}
GROUP_ORDER = ["Safe", "App caches", "GPU caches", "Developer caches", "System leftovers"]
PIE_COLORS = ["#4c8dff", "#3dce8f", "#f0b429", "#ff5d73", "#8ebcff", "#c084fc", "#22d3ee", "#fb923c", "#94a3b8"]
PIE_SPECIAL = {"free": "#2c384c", "unknown": "#6d7c92"}
ICON = resource_path("assets", "pc_cleanup.ico")
# Pages that read something live every time they are opened, so they are never cached.
ALWAYS_FRESH = {"activity"}


class CleanupApp(PerformancePage, DriversPage, RecoverPage, ctk.CTk):
    def __init__(self, autoload: bool = True):
        super().__init__()
        ctk.set_appearance_mode("dark")
        self.title("PC Cleanup")
        self._apply_icon(self)
        self.configure(fg_color=BG)
        self.minsize(1080, 700)
        self.geometry("1220x800")
        self._center()
        self.font_title = ctk.CTkFont(family="Segoe UI", size=26, weight="bold")
        self.font_h = ctk.CTkFont(family="Segoe UI", size=16, weight="bold")
        self.font_b = ctk.CTkFont(family="Segoe UI", size=13, weight="bold")
        self.font = ctk.CTkFont(family="Segoe UI", size=13)
        self.font_s = ctk.CTkFont(family="Segoe UI", size=12)
        self.font_m = ctk.CTkFont(family="Consolas", size=12)

        self.current = "dashboard"
        self.busy = False
        self.job_page = ""
        self.job_token = 0
        self._cancel = threading.Event()
        self.nav_buttons: dict[str, ctk.CTkButton] = {}

        self.dashboard_data = None
        self.space_report = None
        self.breakdown = FolderBreakdown(self)
        self.reader_windows: dict = {}
        self.scan_ready_for_ai = False
        self.last_scan_at = None
        self._persist_scans = bool(autoload)
        self.junk_items: list[JunkCategory] | None = None
        self.file_result = None
        self.services: list[ServiceInfo] | None = None
        self.startup_items: list[StartupItem] | None = None
        self.registry_items = None
        self.ai_report = ""
        self.ai_answer = ""

        self.location_mode = ctk.StringVar(master=self, value="Personal folders")
        self.custom_folder = ctk.StringVar(master=self, value="")
        self.min_size = ctk.StringVar(master=self, value="50")
        self.min_days = ctk.StringVar(master=self, value="180")
        self.only_idle = ctk.BooleanVar(master=self, value=True)
        self.include_appdata = ctk.BooleanVar(master=self, value=False)
        self.file_query = ctk.StringVar(master=self, value="")
        self.file_sort = ctk.StringVar(master=self, value="Smart score")
        self.file_page_index = 0
        self.service_query = ctk.StringVar(master=self, value="")
        self.service_filter = ctk.StringVar(master=self, value="Needs a look")
        self.service_page_index = 0
        self.ai_provider = ctk.StringVar(master=self, value="ChatGPT (OpenAI)")
        self.ai_base = ctk.StringVar(master=self, value="https://api.openai.com/v1")
        self.ai_model = ctk.StringVar(master=self, value="gpt-4o-mini")
        self.ai_key = ctk.StringVar(master=self, value="")
        self.ai_chatgpt_model = ctk.StringVar(master=self, value="")
        self._ai_loaded = False
        self._building = False
        self.drive_vars: dict[str, ctk.BooleanVar] = {}
        self.junk_total = None
        self.file_total = None
        self.file_list = None
        self.perf_report = None
        self.driver_report = None
        self.driver_filter = ctk.StringVar(master=self, value="Needs attention")
        # Built pages are kept and shown again instead of being rebuilt on every click.
        # Any change to the data or to a running job bumps the version, and a page
        # older than that is rebuilt the next time it is opened.
        self._pages: dict[str, ctk.CTkFrame] = {}
        self._page_versions: dict[str, int] = {}
        self._data_version = 0
        # Worker threads never touch Tk. They drop callbacks and the latest
        # progress text here, and the window picks them up on its own thread.
        self._inbox: queue.SimpleQueue = queue.SimpleQueue()
        self._status_note: tuple[int, str] | None = None
        self._pump_after = None

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._build_sidebar()
        self.content = ctk.CTkFrame(self, fg_color=BG, corner_radius=0)
        self.content.grid(row=0, column=1, sticky="nsew")
        self.content.grid_rowconfigure(0, weight=1)
        self.content.grid_columnconfigure(0, weight=1)
        self._content_size: tuple[int, int] | None = None
        self.content.bind("<Configure>", self._on_content_resize, add="+")
        self._build_status()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.report_callback_exception = self._report_error
        self._pump()
        self.file_query.trace_add("write", lambda *_args: self._reset_file_page())
        self.service_query.trace_add("write", lambda *_args: self._reset_service_page())
        if autoload:
            self._load_saved_scan()
        self.select_nav("dashboard")
        self.after(150, self._style_title)
        if autoload:
            self.after(400, self._offer_fresh_scan)

    def _center(self) -> None:
        # A 1920x1080 laptop at 150% scaling has about 1280x690 to work with, so the
        # smallest allowed size shrinks to fit the screen the window is on.
        scale = self._get_window_scaling()
        left, top, right, bottom = work_area(self)
        room_w = int((right - left) / scale) - 16
        room_h = int((bottom - top) / scale) - 48
        self.minsize(max(640, min(1080, room_w)), max(400, min(700, room_h)))
        self._place_window(self, 1220, 800)

    def _place_window(self, window, width: int, height: int) -> None:
        """Size a window and centre it, kept inside the monitor's work area.

        width and height are CustomTkinter sizes, which it scales for the
        display. Window positions, winfo sizes, and the work area are physical
        pixels, so the sums are done in pixels and the size converted back.
        The main window centres on its monitor; others centre over it.
        """
        scale = self._get_window_scaling()
        self.update_idletasks()
        left, top, right, bottom = work_area(self)
        caption = round(32 * scale)
        edge = round(8 * scale)
        w = min(round(width * scale), right - left - 2 * edge)
        h = min(round(height * scale), bottom - top - caption - 2 * edge)
        if window is self:
            x = left + (right - left - w) // 2
            y = top + (bottom - top - h - caption) // 2
        else:
            x = self.winfo_rootx() + (self.winfo_width() - w) // 2
            y = self.winfo_rooty() + (self.winfo_height() - h - caption) // 2
        x = max(left, min(x, right - w - edge))
        y = max(top, min(y, bottom - h - caption - edge))
        window.geometry(f"{math.floor(w / scale)}x{math.floor(h / scale)}+{x}+{y}")

    def _style_title(self, widget=None) -> None:
        """Match a window's title bar to the dark background."""
        widget = self if widget is None else widget
        try:
            if not widget.winfo_exists():
                return
            widget.update_idletasks()
            user32 = ctypes.windll.user32
            child = widget.winfo_id()
            outer = user32.GetParent(child) or child
            main_child = self.winfo_id()
            main_outer = user32.GetParent(main_child) or main_child
            handles = []
            for handle in (outer, child):
                if not handle:
                    continue
                if widget is not self and handle in {main_child, main_outer}:
                    continue
                if handle not in handles:
                    handles.append(handle)
            dark = ctypes.c_int(1)
            caption = ctypes.c_int(0x0018120E)
            lettering = ctypes.c_int(0x00F7EEE8)
            border = ctypes.c_int(0x004C382C)
            dwm = ctypes.windll.dwmapi.DwmSetWindowAttribute
            for handle in handles:
                dwm(handle, 20, ctypes.byref(dark), ctypes.sizeof(dark))
                dwm(handle, 19, ctypes.byref(dark), ctypes.sizeof(dark))
                dwm(handle, 35, ctypes.byref(caption), ctypes.sizeof(caption))
                dwm(handle, 36, ctypes.byref(lettering), ctypes.sizeof(lettering))
                dwm(handle, 34, ctypes.byref(border), ctypes.sizeof(border))
        except Exception:
            return

    def _apply_icon(self, window) -> None:
        # Setting it here also stops CustomTkinter swapping in its own icon.
        if os.path.isfile(ICON):
            try:
                window.iconbitmap(ICON)
            except tk.TclError:
                pass

    def _style_title_soon(self, widget) -> None:
        self._apply_icon(widget)

        def paint() -> None:
            self._style_title(widget)

        try:
            widget.after(30, paint)
            widget.after(250, paint)
        except Exception:
            return

    def _report_error(self, exc_type, exc, tb) -> None:
        log("".join(traceback.format_exception(exc_type, exc, tb)))
        self.show_info("Something went wrong", str(exc))

    def on_close(self) -> None:
        self._cancel.set()
        self.job_token += 1
        self._remember_scan()
        self._close_space_popout()
        for pending in (self._pump_after, getattr(self, "_live_after", None)):
            if pending is not None:
                try:
                    self.after_cancel(pending)
                except tk.TclError:
                    pass
        self._pump_after = None
        self._live_after = None
        for window in list(self.reader_windows.values()):
            try:
                if window.winfo_exists():
                    window.destroy()
            except Exception:
                pass
        self.reader_windows.clear()
        self.destroy()

    def _build_sidebar(self) -> None:
        side = ctk.CTkFrame(self, fg_color=SIDEBAR, corner_radius=0, width=228)
        side.grid(row=0, column=0, sticky="nsew")
        side.grid_propagate(False)
        side.pack_propagate(False)
        brand = ctk.CTkFrame(side, fg_color="transparent")
        brand.pack(side="top", fill="x", padx=18, pady=(22, 12))
        ctk.CTkLabel(brand, text="PC Cleanup", font=self.font_h, text_color=TEXT, anchor="w").pack(fill="x")
        ctk.CTkLabel(brand, text="Scans stay local. AI sends a report only when you ask.", font=self.font_s, text_color=MUTED, anchor="w", wraplength=190, justify="left").pack(fill="x", pady=(2, 0))
        items = [
            ("dashboard", "Overview"),
            ("performance", "Performance"),
            ("junk", "Caches"),
            ("files", "Old files"),
            ("recover", "Recover"),
            ("services", "Services"),
            ("startup", "Startup"),
            ("drivers", "Drivers"),
            ("registry", "Registry"),
            ("ai", "AI analyst"),
            ("activity", "Activity"),
        ]
        for key, label in items:
            button = ctk.CTkButton(
                side,
                text=label,
                anchor="w",
                height=40,
                corner_radius=10,
                fg_color="transparent",
                hover_color=CARD,
                text_color=MUTED,
                font=self.font,
                command=lambda name=key: self.select_nav(name),
            )
            button.pack(fill="x", padx=12, pady=2)
            self.nav_buttons[key] = button
        footer = ctk.CTkFrame(side, fg_color="transparent")
        footer.pack(side="bottom", fill="x", padx=16, pady=16)
        admin = is_admin()
        ctk.CTkLabel(
            footer,
            text="Administrator" if admin else "Standard user",
            font=self.font_s,
            text_color=OK if admin else WARN,
            anchor="w",
        ).pack(fill="x")
        if not admin:
            self._button(footer, "Restart as admin", self.relaunch_admin, "ghost").pack(fill="x", pady=(8, 8))
        ctk.CTkLabel(
            footer,
            text="Ticks first. Caches are deleted.\nPersonal files go to the Recycle Bin.",
            font=self.font_s,
            text_color=FAINT,
            justify="left",
            anchor="w",
        ).pack(fill="x", pady=(8, 0))
        ctk.CTkLabel(footer, text=f"Version {__version__}", font=self.font_s, text_color=FAINT, anchor="w").pack(fill="x", pady=(8, 0))

    def _build_status(self) -> None:
        bar = ctk.CTkFrame(self, fg_color=SIDEBAR, corner_radius=0, height=46)
        bar.grid(row=1, column=0, columnspan=2, sticky="ew")
        bar.grid_columnconfigure(0, weight=1)
        self.status_label = ctk.CTkLabel(bar, text="Ready", font=self.font_s, text_color=MUTED, anchor="w")
        self.status_label.grid(row=0, column=0, sticky="ew", padx=16, pady=10)
        self.progress = ctk.CTkProgressBar(bar, width=160, height=8, mode="indeterminate", progress_color=ACCENT, fg_color=LINE)
        self.cancel_btn = self._button(bar, "Stop", self.cancel_job, "ghost", width=72)

    def select_nav(self, name: str) -> None:
        self.current = name
        for key, button in self.nav_buttons.items():
            selected = key == name
            button.configure(fg_color=CARD if selected else "transparent", text_color=TEXT if selected else MUTED)
        cached = self._pages.get(name)
        if cached is not None and name not in ALWAYS_FRESH and self._page_versions.get(name) == self._data_version:
            self._show_page(name)
        else:
            {
                "dashboard": self.show_dashboard,
                "performance": self.show_performance,
                "junk": self.show_junk,
                "files": self.show_files,
                "recover": self.show_recover,
                "services": self.show_services,
                "startup": self.show_startup,
                "drivers": self.show_drivers,
                "registry": self.show_registry,
                "ai": self.show_ai,
                "activity": self.show_activity,
            }[name]()
        if name == "performance":
            self._start_live_meters()

    def invalidate(self) -> None:
        """Data or the running job changed, so cached pages are rebuilt when next opened."""
        self._data_version += 1

    def _page(self) -> ctk.CTkFrame:
        """A fresh frame for the current page. Other pages stay built, just hidden."""
        name = self.current
        old = self._pages.pop(name, None)
        if old is not None:
            old.destroy()
        frame = ctk.CTkFrame(self.content, fg_color="transparent")
        frame.grid_columnconfigure(0, weight=1)
        self._pages[name] = frame
        self._page_versions[name] = self._data_version
        self._show_page(name)
        return frame

    def _show_page(self, name: str) -> None:
        # Built pages sit on top of each other and the current one is raised.
        # Hiding a page instead would unmap hundreds of native windows, and
        # mapping them again costs a few hundred milliseconds per visit.
        frame = self._pages[name]
        if not frame.winfo_manager():
            frame.grid(row=0, column=0, sticky="nsew", padx=22, pady=16)
        frame.tkraise()

    def _on_content_resize(self, event) -> None:
        # Every stacked page would reflow on a resize, so hidden pages step out
        # of the layout until they are opened again.
        size = (event.width, event.height)
        if size == self._content_size:
            return
        first = self._content_size is None
        self._content_size = size
        if first:
            return
        for key, frame in self._pages.items():
            if key != self.current and frame.winfo_manager():
                frame.grid_remove()

    def _button(self, parent, text, command, style="primary", width=0):
        colors = {
            "primary": (ACCENT, ACCENT_2, TEXT),
            "danger": (DANGER, DANGER_2, TEXT),
            "ghost": (CARD, CARD_2, TEXT),
        }[style]
        pixel_width = width or max(96, int(len(text) * 7.2) + 32)
        return ctk.CTkButton(
            parent,
            text=text,
            command=command,
            width=pixel_width,
            height=34,
            corner_radius=10,
            fg_color=colors[0],
            hover_color=colors[1],
            text_color=colors[2],
            font=self.font_b,
            border_width=1 if style == "ghost" else 0,
            border_color=LINE,
        )

    def _heading(self, parent, title: str, subtitle: str, row: int = 0) -> None:
        ctk.CTkLabel(parent, text=title, font=self.font_title, text_color=TEXT, anchor="w").grid(row=row, column=0, sticky="ew")
        ctk.CTkLabel(parent, text=subtitle, font=self.font, text_color=MUTED, anchor="w", justify="left", wraplength=820).grid(
            row=row + 1, column=0, sticky="ew", pady=(4, 12)
        )

    def set_status(self, text: str) -> None:
        self.status_label.configure(text=text)

    def _status_async(self, token: int, text: str) -> None:
        # Called from worker threads many times a second. Only the latest text
        # matters, and the window shows it on its next pump.
        self._status_note = (token, text)

    def _post(self, callback) -> None:
        """Run callback on the window's thread. Safe to call from any thread."""
        self._inbox.put(callback)

    def _pump(self) -> None:
        # Schedule the next pump first, so a dialog opened by a callback below
        # does not stall the queue while it waits for the user.
        self._pump_after = self.after(60, self._pump)
        note = self._status_note
        if note is not None:
            self._status_note = None
            token, text = note
            if self.busy and token == self.job_token:
                self.status_label.configure(text=clip(text, 150))
        while True:
            try:
                callback = self._inbox.get_nowait()
            except queue.Empty:
                break
            try:
                callback()
            except Exception:
                self._report_error(*sys.exc_info())

    def set_busy(self, busy: bool, label: str) -> None:
        self.invalidate()
        self.status_label.configure(text=label)
        if busy:
            self.progress.grid(row=0, column=1, padx=(0, 8), pady=16)
            self.cancel_btn.grid(row=0, column=2, padx=(0, 12), pady=8)
            self.progress.start()
        else:
            self.progress.stop()
            self.progress.grid_remove()
            self.cancel_btn.grid_remove()

    def begin_job(self, page: str, label: str) -> int | None:
        if self.busy:
            self.set_status("A check is still running. Press Stop, then try again.")
            return None
        self.busy = True
        self.job_page = page
        self.job_token += 1
        self._cancel.clear()
        self.set_busy(True, label)
        return self.job_token

    def cancel_job(self) -> None:
        self._cancel.set()
        self.set_status("Stopping after the current step…")

    def run_bg(self, token: int, fn, done) -> None:
        def run() -> None:
            try:
                result = fn(self._cancel, lambda message: self._status_async(token, message))
                error = None
            except Exception as exc:
                log(traceback.format_exc())
                result = None
                error = exc
            self._post(lambda: self._job_done(token, result, error, done))

        threading.Thread(target=run, daemon=True).start()

    def _job_done(self, token: int, result, error, done) -> None:
        if token != self.job_token:
            return
        self.busy = False
        self.set_busy(False, "Ready" if error is None else "Something went wrong. See Activity.")
        done(result, error)

    def ask(self, title: str, message: str, choices: list[tuple[str, object, str]]):
        result = {"value": None}
        dialog = ctk.CTkToplevel(self)
        dialog.title(title)
        dialog.configure(fg_color=BG)
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.grab_set()
        self._style_title_soon(dialog)
        wrap = ctk.CTkFrame(dialog, fg_color=BG)
        wrap.pack(fill="both", expand=True, padx=22, pady=18)
        ctk.CTkLabel(wrap, text=title, font=self.font_h, text_color=TEXT, anchor="w").pack(fill="x")
        ctk.CTkLabel(
            wrap,
            text=message,
            font=self.font,
            text_color=MUTED,
            anchor="w",
            justify="left",
            wraplength=480,
        ).pack(fill="x", pady=(8, 16))
        buttons = ctk.CTkFrame(wrap, fg_color="transparent")
        buttons.pack(fill="x")

        def choose(value) -> None:
            result["value"] = value
            try:
                dialog.grab_release()
            except Exception:
                pass
            dialog.destroy()

        for label, value, style in choices:
            self._button(buttons, label, lambda chosen=value: choose(chosen), style).pack(side="right", padx=(8, 0))
        dialog.protocol("WM_DELETE_WINDOW", lambda: choose(None))
        dialog.bind("<Escape>", lambda _event: choose(None))
        dialog.update_idletasks()
        # winfo_req* are physical pixels. The placement helper wants CustomTkinter sizes.
        scale = self._get_window_scaling()
        width = max(540, math.ceil(dialog.winfo_reqwidth() / scale))
        height = max(200, math.ceil(dialog.winfo_reqheight() / scale))
        self._place_window(dialog, width, height)
        try:
            dialog.attributes("-topmost", True)
            dialog.after(200, lambda: dialog.attributes("-topmost", False) if dialog.winfo_exists() else None)
        except Exception:
            pass
        dialog.wait_window()
        return result["value"]

    def show_info(self, title: str, message: str) -> None:
        self.ask(title, message, [("OK", True, "primary")])

    def confirm(self, title: str, message: str, confirm_label: str = "Continue", danger: bool = False) -> bool:
        choice = self.ask(
            title,
            message,
            [("Cancel", False, "ghost"), (confirm_label, True, "danger" if danger else "primary")],
        )
        return choice is True

    def relaunch_admin(self) -> None:
        if relaunch_as_admin():
            self.on_close()
            return
        self.show_info("Administrator", "Windows did not grant administrator. The parts that work without it are still available.")

    def refresh_dashboard(self) -> None:
        token = self.begin_job("dashboard", "Checking drives, startup, and temporary files…")
        if token is None:
            return

        def done(result, error) -> None:
            if error is None:
                self.dashboard_data = result
                self.startup_items = list(result.startup_items)
                self.services = list(result.service_items)
                self._remember_scan(stamp=True)
            elif self.current == "dashboard":
                self.show_info("Overview did not finish", str(error))
            if self.current == "dashboard":
                self.show_dashboard()

        self.run_bg(token, collect_snapshot, done)

    def show_dashboard(self) -> None:
        frame = self._page()
        frame.grid_rowconfigure(3, weight=1)
        when = format_scan_time(self.last_scan_at)
        overview = "Map every internal drive, then open the folder breakdown in its own window. USB drives are skipped. Nothing on this page is deleted."
        if when:
            overview = f"Showing the scan from {when}. " + overview
        self._heading(frame, "Overview", overview)
        actions = ctk.CTkFrame(frame, fg_color="transparent")
        actions.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        self._button(actions, "Full scan", self.start_full_scan).pack(side="left")
        self._button(actions, "Map disk space", self.start_space_map, "ghost").pack(side="left", padx=8)
        self._button(actions, "Refresh", self.refresh_dashboard, "ghost").pack(side="left")
        self._button(actions, "Scan caches", lambda: self.select_nav("junk"), "ghost").pack(side="left")
        self._button(actions, "Find old files", lambda: self.select_nav("files"), "ghost").pack(side="left", padx=8)
        body = ctk.CTkScrollableFrame(frame, fg_color="transparent")
        body.grid(row=3, column=0, sticky="nsew", pady=(8, 0))
        frame.grid_rowconfigure(3, weight=1)
        data = self.dashboard_data
        if data is None:
            text = "Checking this PC…" if self.busy and self.job_page == "dashboard" else "No saved scan on this PC yet."
            ctk.CTkLabel(body, text=text, font=self.font, text_color=MUTED, anchor="w").pack(fill="x", pady=12)
            return
        for warning in data.warnings:
            ctk.CTkLabel(body, text=warning, font=self.font_s, text_color=WARN, anchor="w", wraplength=860, justify="left").pack(fill="x")
        self._render_space(body)
        disks = ctk.CTkFrame(body, fg_color=CARD, corner_radius=14)
        disks.pack(fill="x", pady=6)
        ctk.CTkLabel(disks, text="Drives", font=self.font_h, text_color=TEXT, anchor="w").pack(fill="x", padx=16, pady=(14, 6))
        if not data.disks:
            ctk.CTkLabel(disks, text="No drives could be read.", font=self.font, text_color=MUTED, anchor="w").pack(fill="x", padx=16, pady=(0, 14))
        for disk in data.disks:
            self._disk_row(disks, disk)
        stats = ctk.CTkFrame(body, fg_color="transparent")
        stats.pack(fill="x", pady=6)
        stats.grid_columnconfigure((0, 1), weight=1)
        self._stat(stats, 0, "Recycle Bin", format_size(data.recycle_bytes), f"{data.recycle_items} items", "junk")
        self._stat(stats, 1, "Temporary files", format_size(data.temp_bytes), f"{data.temp_files} files in your temp folder", "junk")
        self._stat(stats, 2, "Startup items", str(data.startup_count), f"{data.startup_broken} point at a missing file", "startup")
        self._stat(stats, 3, "Services to review", str(len(data.service_notes)), "Third-party or optional services that start with Windows", "services")
        ctk.CTkLabel(body, text="Worth a look", font=self.font_h, text_color=TEXT, anchor="w").pack(fill="x", pady=(16, 6))
        for finding in data.findings:
            self._finding(body, finding)

    def start_space_map(self) -> None:
        token = self.begin_job("dashboard", "Mapping disk space…")
        if token is None:
            return

        def done(result, error) -> None:
            if error is None:
                self.space_report = result
                self._close_space_popout()
                self._remember_scan(stamp=True)
                log(f"Disk map measured {format_size(result.measured)} across {result.files_seen} files.")
            elif self.current == "dashboard":
                self.show_info("Disk map did not finish", str(error))
            if self.current == "dashboard":
                self.show_dashboard()

        self.run_bg(token, self._map_space, done)

    def _map_space(self, cancel, progress):
        report = scan_space(cancel, progress)
        self._save_trees(report)
        return report

    def _save_trees(self, report) -> None:
        """Write the folder trees from the worker thread, so the window never waits on them."""
        if not self._persist_scans or report is None:
            return
        try:
            write_trees(report)
        except (OSError, ValueError, RecursionError) as exc:
            log(f"Could not save the folder breakdown. {exc}")

    def _render_space(self, body) -> None:
        report = self.space_report
        card = ctk.CTkFrame(body, fg_color=CARD, corner_radius=14)
        card.pack(fill="x", pady=6)
        ctk.CTkLabel(card, text="Where the space is", font=self.font_h, text_color=TEXT, anchor="w").pack(fill="x", padx=16, pady=(14, 2))
        if report is None or not report.drives:
            message = "Use Full scan to check the whole PC, or Map disk space for folders only. After that, open the folder breakdown. Nothing is deleted. A large drive can take a few minutes, and several folders are read at once."
            ctk.CTkLabel(card, text=message, font=self.font, text_color=MUTED, anchor="w", justify="left", wraplength=860).pack(fill="x", padx=16, pady=(4, 14))
            return
        caption_row = ctk.CTkFrame(card, fg_color="transparent")
        caption_row.pack(fill="x", padx=16, pady=(0, 8))
        caption = f"{format_size(report.measured)} measured  ·  {report.files_seen:,} files  ·  {len(report.drives)} internal drive{'s' if len(report.drives) != 1 else ''}"
        ctk.CTkLabel(caption_row, text=caption, font=self.font_s, text_color=MUTED, anchor="w").pack(side="left")
        self._button(caption_row, "Open folder breakdown", lambda: self._open_space_popout([]), "ghost").pack(side="right")
        for drive in report.drives:
            self._render_drive_chart(card, drive)
        ctk.CTkFrame(card, fg_color="transparent", height=8).pack()
        self._render_scan_report(body, report)
        if report.user_folders:
            self._space_list(body, "Your folders", [(item.name, format_size(item.size), item.path) for item in report.user_folders])
        if report.apps:
            self._space_list(
                body,
                "Biggest installed apps",
                [(item.name, format_size(item.size), item.publisher or item.location) for item in report.apps[:12]],
                paths=[item.location for item in report.apps[:12]],
            )
        if report.files:
            self._space_list(
                body,
                "Biggest files",
                [(os.path.basename(item.path), format_size(item.size), shorten_path(item.path, 96)) for item in report.files[:12]],
                paths=[item.path for item in report.files[:12]],
            )

    def _slice_color(self, item, index: int) -> str:
        return PIE_SPECIAL.get(item.kind, PIE_COLORS[index % len(PIE_COLORS)])

    def _render_drive_chart(self, card, drive) -> None:
        chart = ctk.CTkFrame(card, fg_color="transparent")
        chart.pack(fill="x", padx=16, pady=(0, 8))
        canvas = Canvas(chart, width=240, height=240, bg=CARD, highlightthickness=0)
        canvas.pack(side="left", padx=(0, 16))
        self._draw_pie(canvas, drive)
        legend = ctk.CTkFrame(chart, fg_color="transparent")
        legend.pack(side="left", fill="both", expand=True)
        total = sum(item.size for item in drive.slices) or 1
        children = {child.name: child for child in (drive.tree.children if drive.tree else [])}
        for index, item in enumerate(drive.slices):
            child = children.get(item.name)
            command = None
            if drive.tree is not None and child is not None and child.children:
                command = lambda node=drive.tree, target=child: self._open_space_popout([node, target])
            elif drive.tree is not None and item.kind == "folder":
                command = lambda node=drive.tree: self._open_space_popout([node])
            self._legend_row(legend, item.name, item.size, item.size / total, self._slice_color(item, index), command)

    def _draw_pie(self, canvas: Canvas, drive) -> None:
        slices = [item for item in drive.slices if item.size > 0]
        total = sum(item.size for item in slices) or 1
        angle = 90.0
        for index, item in enumerate(slices):
            extent = -360.0 * item.size / total
            canvas.create_arc(18, 18, 222, 222, start=angle, extent=extent, fill=self._slice_color(item, index), outline=CARD, width=2)
            angle += extent
        canvas.create_oval(74, 74, 166, 166, fill=CARD, outline=CARD)
        used = sum(item.size for item in slices if item.kind != "free")
        canvas.create_text(120, 112, text=f"{used / total:.0%}", fill=TEXT, font=("Segoe UI", 18, "bold"))
        letter = drive.root[:2] if len(drive.root) >= 2 else "PC"
        canvas.create_text(120, 138, text=letter, fill=MUTED, font=("Segoe UI", 11))

    def _legend_row(self, parent, name: str, size: int, share: float, color: str, command=None) -> None:
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=2)
        swatch = ctk.CTkFrame(row, width=12, height=12, fg_color=color, corner_radius=3)
        swatch.pack(side="left", padx=(0, 8), pady=4)
        swatch.pack_propagate(False)
        label = ctk.CTkLabel(row, text=name, font=self.font, text_color=TEXT if command is None else ACCENT, anchor="w")
        label.pack(side="left")
        ctk.CTkLabel(row, text=f"{format_size(size)}   {share:.0%}", font=self.font_s, text_color=MUTED, anchor="e").pack(side="right")
        if command is not None:
            for widget in (row, label, swatch):
                widget.bind("<Button-1>", lambda _event, action=command: action())

    def _render_scan_report(self, body, report) -> None:
        card = ctk.CTkFrame(body, fg_color=CARD, corner_radius=14)
        card.pack(fill="x", pady=6)
        ctk.CTkLabel(card, text="Scan report", font=self.font_h, text_color=TEXT, anchor="w").pack(fill="x", padx=16, pady=(14, 4))
        summary = self._space_summary(report)
        if summary:
            ctk.CTkLabel(card, text=summary, font=self.font, text_color=MUTED, anchor="w", justify="left", wraplength=860).pack(fill="x", padx=16, pady=(0, 8))
        explanations = list(getattr(report, "explanations", []) or [])
        if not explanations and not summary:
            ctk.CTkLabel(card, text="The folder sizes are in the chart above.", font=self.font, text_color=MUTED, anchor="w").pack(fill="x", padx=16, pady=(0, 14))
            return
        for item in explanations:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            title, detail = item[0], item[1]
            level = item[2] if len(item) > 2 else "info"
            color = WARN if level == "warn" else TEXT
            ctk.CTkLabel(card, text=title, font=self.font_b, text_color=color, anchor="w").pack(fill="x", padx=16, pady=(6, 0))
            ctk.CTkLabel(card, text=detail, font=self.font, text_color=MUTED, anchor="w", justify="left", wraplength=860).pack(fill="x", padx=16, pady=(2, 4))
        ctk.CTkFrame(card, fg_color="transparent", height=8).pack()

    def _space_summary(self, report) -> str:
        parts = []
        for drive in report.drives:
            letter = drive.root[:2] if len(drive.root) >= 2 else drive.root
            free = next((item for item in drive.slices if item.kind == "free"), None)
            folders = [item for item in drive.slices if item.kind == "folder"][:4]
            sentence = letter
            if free is not None:
                sentence += f" has {format_size(free.size)} free."
            if folders:
                listed = ", ".join(f"{item.name} ({format_size(item.size)})" for item in folders)
                sentence += f" Largest areas this scan could open: {listed}."
            elif free is None:
                sentence += f" measured {format_size(drive.measured)}."
            parts.append(sentence.strip())
        if report.apps:
            top = report.apps[0]
            parts.append(f"Largest listed program: {top.name} at {format_size(top.size)}.")
        if report.files:
            top = report.files[0]
            parts.append(f"Largest file: {os.path.basename(top.path)} at {format_size(top.size)}.")
        return " ".join(parts)

    def _close_space_popout(self) -> None:
        self.breakdown.close()

    def _open_space_popout(self, nodes: list) -> None:
        report = self.space_report
        if report is None or not any(drive.tree for drive in report.drives):
            self.show_info("No folder breakdown yet", "Run Full scan or Map disk space first.")
            return
        self.breakdown.show(report, nodes)

    def _scan_question(self) -> bool:
        when = format_scan_time(self.last_scan_at)
        if when:
            title = "Last scan"
            message = (
                f"Your last scan was {when}. Do you want to do another scan? "
                "It checks drives, where the space is, caches, startup programs, services, registry leftovers, and old personal files. "
                "Nothing is deleted. No keeps the results already on screen."
            )
        else:
            title = "Full fresh scan"
            message = (
                "Do you want a full fresh scan of this PC? "
                "It checks drives, where the space is, caches, startup programs, services, registry leftovers, and old personal files. "
                "It can take several minutes. Nothing is deleted."
            )
        return self.ask(title, message, [("No", False, "ghost"), ("Scan now", True, "primary")]) is True

    def _offer_fresh_scan(self) -> None:
        if self._scan_question():
            self.start_full_scan()

    def _load_saved_scan(self) -> None:
        saved = read_snapshot()
        if not saved:
            return
        self.last_scan_at = saved.get("saved_at")
        self.dashboard_data = saved.get("dashboard")
        self.space_report = saved.get("space")
        self.junk_items = saved.get("junk")
        self.registry_items = saved.get("registry")
        self.file_result = saved.get("files")
        startup = saved.get("startup")
        services = saved.get("services")
        if startup is None and self.dashboard_data is not None:
            startup = list(self.dashboard_data.startup_items)
        if services is None and self.dashboard_data is not None:
            services = list(self.dashboard_data.service_items)
        self.startup_items = startup
        self.services = services
        self.ai_report = saved.get("ai_report") or ""
        self.ai_answer = saved.get("ai_answer") or ""
        space = self.space_report
        if space is not None and not space.tree_id and any(drive.tree for drive in space.drives):
            # A scan saved by an older version keeps its tree inside last_scan.json.
            # Move it to its own file once, so later saves stay small and quick.
            self._save_trees(space)
            self._remember_scan()

    def _remember_scan(self, stamp: bool = False) -> None:
        if not self._persist_scans:
            return
        has_scan = any(
            item is not None
            for item in (self.dashboard_data, self.space_report, self.junk_items, self.registry_items, self.file_result, self.startup_items, self.services)
        )
        if not has_scan and not self.ai_report and not self.ai_answer:
            return
        if stamp or not self.last_scan_at:
            self.last_scan_at = time.time()
        try:
            write_snapshot(
                pack_snapshot(
                    self.last_scan_at,
                    self.dashboard_data,
                    self.space_report,
                    self.junk_items,
                    self.registry_items,
                    self.file_result,
                    self.startup_items,
                    self.services,
                    self.ai_report,
                    self.ai_answer,
                )
            )
        except OSError as exc:
            log(f"Could not save the last scan. {exc}")

    def start_full_scan(self) -> None:
        token = self.begin_job("dashboard", "Starting a full scan…")
        if token is None:
            return

        def work(cancel, progress):
            progress("Checking drives, startup, and services…")
            dashboard = collect_snapshot(cancel, progress)
            self._publish_scan(token, "dashboard", dashboard)
            if cancel.is_set():
                return None
            progress("Scanning caches…")
            junk = scan_junk(cancel, progress)
            self._publish_scan(token, "junk", junk)
            if cancel.is_set():
                return None
            progress("Checking registry leftovers…")
            registry = scan_registry(cancel, progress)
            self._publish_scan(token, "registry", registry)
            if cancel.is_set():
                return None
            progress("Mapping where the space is…")
            space = scan_space(cancel, progress)
            self._save_trees(space)
            self._publish_scan(token, "space", space)
            if cancel.is_set():
                return None
            progress("Looking for old personal files…")
            roots = [path for _label, path in known_folders()]
            files = scan_files(roots, 50 * 1024 * 1024, 180, False, cancel, progress)
            self._publish_scan(token, "files", files)
            return True

        def done(result, error) -> None:
            if error is not None:
                self.show_info("Full scan did not finish", str(error))
                return
            if not result:
                if self.current == "dashboard":
                    self.show_dashboard()
                return
            log("Full scan finished. Nothing was deleted.")
            self.ai_report = ""
            self.ai_answer = ""
            self.scan_ready_for_ai = True
            self._remember_scan(stamp=True)
            if self.current == "dashboard":
                self.show_dashboard()
            else:
                self.select_nav("dashboard")
            if self.ask(
                "AI analysis",
                "The scan finished. Do you want an AI analysis of it?\n\nYes opens AI analyst. Sign in with ChatGPT there if you have not already, then press Build report. Nothing is sent until you press Analyze with AI.",
                [("No", False, "ghost"), ("Yes", True, "primary")],
            ):
                self.select_nav("ai")

        self.run_bg(token, work, done)

    def _publish_scan(self, token: int, kind: str, value) -> None:
        def apply() -> None:
            if token != self.job_token:
                return
            page = ""
            if kind == "dashboard":
                self.dashboard_data = value
                self.startup_items = list(value.startup_items)
                self.services = list(value.service_items)
                page = self.current if self.current in {"dashboard", "startup", "services"} else ""
            elif kind == "junk":
                self.junk_items = value
                page = "junk"
            elif kind == "registry":
                self.registry_items = value
                page = "registry"
            elif kind == "space":
                self.space_report = value
                self._close_space_popout()
                page = "dashboard"
            elif kind == "files":
                self.file_result = value
                self.file_page_index = 0
                page = "files"
            self.invalidate()
            if page and self.current == page:
                self.select_nav(self.current)

        self._post(apply)

    def _space_list(self, parent, title: str, rows: list[tuple[str, str, str]], paths: list[str] | None = None) -> None:
        card = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=14)
        card.pack(fill="x", pady=6)
        ctk.CTkLabel(card, text=title, font=self.font_h, text_color=TEXT, anchor="w").pack(fill="x", padx=16, pady=(14, 6))
        for index, (name, size, detail) in enumerate(rows):
            row = ctk.CTkFrame(card, fg_color="transparent")
            row.pack(fill="x", padx=12, pady=2)
            text = ctk.CTkFrame(row, fg_color="transparent")
            text.pack(side="left", fill="x", expand=True)
            ctk.CTkLabel(text, text=f"{name}   {size}", font=self.font_b, text_color=TEXT, anchor="w").pack(fill="x")
            if detail:
                ctk.CTkLabel(text, text=detail, font=self.font_s, text_color=MUTED, anchor="w").pack(fill="x")
            path = "" if paths is None else paths[index]
            if path and os.path.exists(path):
                self._button(row, "Show", lambda item=path: reveal_in_explorer(item), "ghost").pack(side="right", padx=4, pady=4)
        ctk.CTkFrame(card, fg_color="transparent", height=8).pack()

    def _disk_row(self, parent, disk) -> None:
        ratio = (disk.used / disk.total) if disk.total else 0
        free_ratio = (disk.free / disk.total) if disk.total else 1
        color = DANGER if free_ratio < 0.10 else WARN if disk.media == "HDD" and free_ratio < 0.20 else ACCENT
        block = ctk.CTkFrame(parent, fg_color="transparent")
        block.pack(fill="x", padx=16, pady=6)
        title = f"{disk.letter}:  {disk.label + '  ' if disk.label else ''}{disk.media}"
        if disk.health:
            title += f"  ·  {disk.health}"
        ctk.CTkLabel(block, text=title, font=self.font_b, text_color=TEXT, anchor="w").pack(fill="x")
        detail = f"{format_size(disk.free)} free of {format_size(disk.total)}"
        if disk.model:
            detail += f"  ·  {clip(disk.model, 48)}"
        ctk.CTkLabel(block, text=detail, font=self.font_s, text_color=MUTED, anchor="w").pack(fill="x", pady=(2, 4))
        bar = ctk.CTkProgressBar(block, height=8, progress_color=color, fg_color=LINE)
        bar.pack(fill="x", pady=(0, 8))
        bar.set(max(0.0, min(1.0, ratio)))

    def _stat(self, parent, index: int, title: str, value: str, detail: str, page: str) -> None:
        card = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=14)
        card.grid(row=index // 2, column=index % 2, sticky="nsew", padx=4, pady=4)
        ctk.CTkLabel(card, text=title, font=self.font_s, text_color=MUTED, anchor="w").pack(fill="x", padx=14, pady=(12, 0))
        ctk.CTkLabel(card, text=value, font=self.font_h, text_color=TEXT, anchor="w").pack(fill="x", padx=14)
        ctk.CTkLabel(card, text=detail, font=self.font_s, text_color=FAINT, anchor="w", wraplength=360, justify="left").pack(fill="x", padx=14, pady=(2, 8))
        self._button(card, "Open", lambda name=page: self.select_nav(name), "ghost").pack(anchor="w", padx=14, pady=(0, 12))

    def _finding(self, parent, finding) -> None:
        card = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=14)
        card.pack(fill="x", pady=4)
        color = WARN if finding.level == "warn" else INFO
        ctk.CTkLabel(card, text=finding.title, font=self.font_b, text_color=color, anchor="w").pack(fill="x", padx=14, pady=(12, 0))
        ctk.CTkLabel(card, text=finding.detail, font=self.font, text_color=MUTED, anchor="w", justify="left", wraplength=760).pack(
            fill="x", padx=14, pady=(4, 12)
        )

    def show_junk(self) -> None:
        frame = self._page()
        frame.grid_rowconfigure(3, weight=1)
        self._heading(
            frame,
            "Caches",
            "Safe items are ticked after a scan. App caches are listed for you to choose, because programs rebuild them. Passwords, history, and bookmarks are left alone.",
        )
        tools = ctk.CTkFrame(frame, fg_color="transparent")
        tools.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        self._button(tools, "Scan", self.start_junk_scan).pack(side="left")
        self._button(tools, "Tick safe items", self._tick_safe_junk, "ghost").pack(side="left", padx=8)
        self._button(tools, "Clear ticks", self._clear_junk, "ghost").pack(side="left")
        self.junk_list = ctk.CTkScrollableFrame(frame, fg_color="transparent")
        self.junk_list.grid(row=3, column=0, sticky="nsew")
        bar = ctk.CTkFrame(frame, fg_color="transparent")
        bar.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        self.junk_total = ctk.CTkLabel(bar, text=self._junk_total_text(), font=self.font_b, text_color=TEXT)
        self.junk_total.pack(side="left")
        self._button(bar, "Clean ticked items", self.clean_junk, "danger").pack(side="right")
        self._render_junk()

    def _junk_total_text(self) -> str:
        chosen = [item for item in self.junk_items or [] if item.checked and item.size > 0]
        return f"{len(chosen)} ticked  ·  {format_size(sum(item.size for item in chosen))}"

    def _render_junk(self) -> None:
        if not hasattr(self, "junk_list") or not self.junk_list.winfo_exists():
            return
        for child in self.junk_list.winfo_children():
            child.destroy()
        items = self.junk_items
        if items is None:
            message = "Scanning caches…" if self.busy and self.job_page == "junk" else "Press Scan to measure temp files, browser caches, and the Recycle Bin."
            ctk.CTkLabel(self.junk_list, text=message, font=self.font, text_color=MUTED, anchor="w").pack(fill="x", pady=8)
            return
        visible = [item for item in items if item.size > 0 or item.note]
        hidden = [item.name for item in items if item.size <= 0 and not item.note]
        if not visible:
            ctk.CTkLabel(self.junk_list, text="Nothing to remove. The usual cache folders are already empty.", font=self.font, text_color=MUTED, anchor="w").pack(fill="x", pady=8)
        last_group = ""
        for item in sorted(visible, key=lambda entry: (GROUP_ORDER.index(entry.group) if entry.group in GROUP_ORDER else 99, -entry.size)):
            if item.group != last_group:
                last_group = item.group
                ctk.CTkLabel(self.junk_list, text=item.group, font=self.font_b, text_color=FAINT, anchor="w").pack(fill="x", pady=(12, 4))
            self._junk_row(item)
        if hidden:
            ctk.CTkLabel(
                self.junk_list,
                text="Also checked, and empty: " + ", ".join(hidden),
                font=self.font_s,
                text_color=FAINT,
                anchor="w",
                justify="left",
                wraplength=820,
            ).pack(fill="x", pady=(12, 4))
        if self.junk_total and self.junk_total.winfo_exists():
            self.junk_total.configure(text=self._junk_total_text())

    def _junk_row(self, item: JunkCategory) -> None:
        row = ctk.CTkFrame(self.junk_list, fg_color=CARD, corner_radius=12)
        row.pack(fill="x", pady=4)
        var = ctk.BooleanVar(master=self, value=item.checked)

        def toggle(category=item, variable=var) -> None:
            category.checked = bool(variable.get())
            if self.junk_total and self.junk_total.winfo_exists():
                self.junk_total.configure(text=self._junk_total_text())

        ctk.CTkCheckBox(row, text="", variable=var, command=toggle, width=28, checkbox_width=20, checkbox_height=20).pack(side="left", padx=(12, 4), pady=12)
        text = ctk.CTkFrame(row, fg_color="transparent")
        text.pack(side="left", fill="x", expand=True, pady=10)
        ctk.CTkLabel(text, text=item.name, font=self.font_b, text_color=TEXT, anchor="w").pack(fill="x")
        detail = item.note or item.description
        if item.count and not item.note:
            detail = f"{item.count} files  ·  {item.description}"
        ctk.CTkLabel(text, text=detail, font=self.font_s, text_color=MUTED, anchor="w", justify="left", wraplength=640).pack(fill="x")
        side = ctk.CTkFrame(row, fg_color="transparent")
        side.pack(side="right", padx=12)
        ctk.CTkLabel(side, text=format_size(item.size), font=self.font_b, text_color=TEXT).pack(anchor="e")
        badge = "Safe" if item.safety == "safe" else "Review"
        ctk.CTkLabel(side, text=badge, font=self.font_s, text_color=OK if item.safety == "safe" else WARN).pack(anchor="e")

    def _tick_safe_junk(self) -> None:
        for item in self.junk_items or []:
            item.checked = item.safety == "safe" and item.size > 0
        self._render_junk()

    def _clear_junk(self) -> None:
        for item in self.junk_items or []:
            item.checked = False
        self._render_junk()

    def start_junk_scan(self) -> None:
        token = self.begin_job("junk", "Scanning caches…")
        if token is None:
            return

        def done(result, error) -> None:
            if error is None:
                self.junk_items = result
                self._remember_scan(stamp=True)
                log(f"Cache scan finished. {sum(item.size for item in result)} bytes across {len(result)} categories.")
            elif self.current == "junk":
                self.show_info("Cache scan did not finish", str(error))
            if self.current == "junk":
                self.show_junk()

        self.run_bg(token, scan_junk, done)

    def clean_junk(self) -> None:
        chosen = [item for item in self.junk_items or [] if item.checked and item.size > 0]
        if not chosen:
            self.show_info("Nothing ticked", "Scan first, then tick the caches you want to remove.")
            return
        total = sum(item.size for item in chosen)
        names = ", ".join(item.name for item in chosen)
        if not self.confirm(
            "Clean ticked caches",
            f"This permanently removes about {format_size(total)} from: {names}. The Recycle Bin, if ticked, is emptied for good. Locked files are left in place.",
            "Clean now",
            danger=True,
        ):
            return
        token = self.begin_job("junk", "Cleaning caches…")
        if token is None:
            return

        def work(cancel, progress):
            return clean_categories(chosen, cancel, progress)

        def done(result, error) -> None:
            if error is not None:
                self.show_info("Clean did not finish", str(error))
                return
            log(
                f"Cache clean removed {result.deleted_files} files ({format_size(result.deleted_bytes)}). "
                f"Skipped {result.skipped}. " + " ".join(result.notes[:12])
            )
            self.show_info("Caches cleaned", _report_text(result, permanent=True))
            if self.current == "junk":
                self.start_junk_scan()

        self.run_bg(token, work, done)

    def show_files(self) -> None:
        self._building = True
        try:
            self._build_files_page()
        finally:
            self._building = False

    def _build_files_page(self) -> None:
        frame = self._page()
        frame.grid_rowconfigure(4, weight=1)
        self._heading(
            frame,
            "Old files",
            "Smart analysis scores age, size, name, and folder. Likely leftovers can be ticked for you. Personal files stay unticked. Windows often does not record every time a file is opened, so the date is the newest of last opened and last modified.",
        )
        tools = ctk.CTkFrame(frame, fg_color=CARD, corner_radius=14)
        tools.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        top = ctk.CTkFrame(tools, fg_color="transparent")
        top.pack(fill="x", padx=12, pady=(12, 6))
        ctk.CTkOptionMenu(
            top,
            values=["Personal folders", "A folder I choose", "Whole drives"],
            variable=self.location_mode,
            command=lambda _value: self.after(1, self._rebuild_files),
            font=self.font,
            dropdown_font=self.font,
            fg_color=CARD_2,
            button_color=CARD_2,
            button_hover_color=LINE,
        ).pack(side="left")
        ctk.CTkLabel(top, text="Min MB", font=self.font_s, text_color=MUTED).pack(side="left", padx=(12, 4))
        ctk.CTkEntry(top, textvariable=self.min_size, width=70, font=self.font).pack(side="left")
        ctk.CTkLabel(top, text="Idle days", font=self.font_s, text_color=MUTED).pack(side="left", padx=(12, 4))
        ctk.CTkEntry(top, textvariable=self.min_days, width=70, font=self.font).pack(side="left")
        ctk.CTkCheckBox(top, text="Only idle files", variable=self.only_idle, font=self.font_s).pack(side="left", padx=12)
        self._button(top, "Scan", self.start_file_scan).pack(side="right")
        extra = ctk.CTkFrame(tools, fg_color="transparent")
        extra.pack(fill="x", padx=12, pady=(0, 8))
        mode = self.location_mode.get()
        if mode == "A folder I choose":
            ctk.CTkEntry(extra, textvariable=self.custom_folder, font=self.font, placeholder_text="Choose a folder").pack(side="left", fill="x", expand=True)
            self._button(extra, "Browse", self._pick_folder, "ghost").pack(side="left", padx=(8, 0))
        elif mode == "Whole drives":
            letters = self._ensure_drive_vars()
            for letter in letters:
                ctk.CTkCheckBox(extra, text=f"{letter}:", variable=self.drive_vars[letter], font=self.font_s).pack(side="left", padx=(0, 10))
            ctk.CTkLabel(extra, text="Network drives are not listed. Use a folder for those.", font=self.font_s, text_color=FAINT).pack(side="left", padx=8)
        else:
            names = ", ".join(label for label, _path in known_folders()) or "No personal folders found"
            ctk.CTkLabel(extra, text=names, font=self.font_s, text_color=MUTED, anchor="w").pack(side="left")
        ctk.CTkCheckBox(tools, text="Include AppData (easy to break apps, so nothing in there is auto-ticked)", variable=self.include_appdata, font=self.font_s).pack(
            anchor="w", padx=12, pady=(0, 12)
        )
        refine = ctk.CTkFrame(frame, fg_color="transparent")
        refine.grid(row=3, column=0, sticky="ew", pady=(0, 6))
        ctk.CTkEntry(refine, textvariable=self.file_query, placeholder_text="Filter by name or folder", font=self.font).pack(side="left", fill="x", expand=True)
        ctk.CTkOptionMenu(
            refine,
            values=["Smart score", "Size", "Idle time"],
            variable=self.file_sort,
            command=lambda _value: self._reset_file_page(),
            font=self.font,
            dropdown_font=self.font,
            fg_color=CARD,
            button_color=CARD_2,
            button_hover_color=LINE,
            width=150,
        ).pack(side="left", padx=8)
        self._button(refine, "Tick leftovers", self._tick_leftovers, "ghost").pack(side="left")
        self._button(refine, "Clear ticks", self._clear_files, "ghost").pack(side="left", padx=(8, 0))
        self.file_list = ctk.CTkScrollableFrame(frame, fg_color="transparent")
        self.file_list.grid(row=4, column=0, sticky="nsew")
        bar = ctk.CTkFrame(frame, fg_color="transparent")
        bar.grid(row=5, column=0, sticky="ew", pady=(8, 0))
        self.file_total = ctk.CTkLabel(bar, text="", font=self.font_b, text_color=TEXT)
        self.file_total.pack(side="left")
        self._button(bar, "Delete permanently", self.purge_files, "danger").pack(side="right")
        self._button(bar, "Move to Recycle Bin", self.recycle_files).pack(side="right", padx=(0, 8))
        self._render_file_list()

    def _rebuild_files(self) -> None:
        if self.current == "files":
            self.show_files()

    def _reset_file_page(self) -> None:
        if self._building or self.current != "files":
            return
        self.file_page_index = 0
        if self.file_list is not None:
            try:
                if self.file_list.winfo_exists():
                    self._render_file_list()
            except Exception:
                return

    def _ensure_drive_vars(self) -> list[str]:
        letters = []
        if self.dashboard_data and self.dashboard_data.disks:
            letters = [disk.letter for disk in self.dashboard_data.disks if disk.drive_type != "Network"]
        if not letters:
            letters = data_drive_letters()
        for letter in letters:
            if letter not in self.drive_vars:
                self.drive_vars[letter] = ctk.BooleanVar(master=self, value=False)
        return letters

    def _pick_folder(self) -> None:
        path = filedialog.askdirectory(title="Folder to scan")
        if path:
            self.custom_folder.set(path)

    def _file_roots(self) -> list[str]:
        mode = self.location_mode.get()
        if mode == "Personal folders":
            return [path for _label, path in known_folders()]
        if mode == "A folder I choose":
            folder = self.custom_folder.get().strip()
            return [folder] if folder and os.path.isdir(folder) else []
        return [f"{letter}:\\" for letter, var in self.drive_vars.items() if var.get()]

    def _filtered_hits(self) -> list[FileHit]:
        hits = list(self.file_result.hits) if self.file_result else []
        query = self.file_query.get().strip().lower()
        if query:
            hits = [hit for hit in hits if query in hit.path.lower()]
        sort = self.file_sort.get()
        if sort == "Size":
            hits.sort(key=lambda hit: hit.size, reverse=True)
        elif sort == "Idle time":
            hits.sort(key=lambda hit: max(hit.modified, hit.accessed))
        else:
            hits.sort(key=lambda hit: (hit.score, hit.size), reverse=True)
        return hits

    def _render_file_list(self) -> None:
        if self.file_list is None or not self.file_list.winfo_exists():
            return
        for child in self.file_list.winfo_children():
            child.destroy()
        if self.file_result is None:
            message = "Scanning files…" if self.busy and self.job_page == "files" else "Choose where to look, then press Scan. Personal folders are the fastest place to start."
            ctk.CTkLabel(self.file_list, text=message, font=self.font, text_color=MUTED, anchor="w", wraplength=820, justify="left").pack(fill="x", pady=8)
            self._set_file_total()
            return
        hits = self._filtered_hits()
        if self.file_result.capped:
            ctk.CTkLabel(
                self.file_list,
                text=f"Showing the {len(self.file_result.hits)} largest matches out of {self.file_result.matched}. Raise the minimum size to narrow it.",
                font=self.font_s,
                text_color=WARN,
                anchor="w",
            ).pack(fill="x", pady=(0, 6))
        if not hits:
            ctk.CTkLabel(self.file_list, text="No files matched. Lower the minimum size or the idle days.", font=self.font, text_color=MUTED, anchor="w").pack(fill="x", pady=8)
            self._set_file_total()
            return
        pages = max(1, math.ceil(len(hits) / FILE_PAGE))
        self.file_page_index = min(self.file_page_index, pages - 1)
        start = self.file_page_index * FILE_PAGE
        for hit in hits[start : start + FILE_PAGE]:
            self._file_row(hit)
        self._pager(self.file_list, self.file_page_index, pages, lambda page: self._move_file_page(page))
        self._set_file_total()

    def _move_file_page(self, page: int) -> None:
        self.file_page_index = page
        self._render_file_list()

    def _file_row(self, hit: FileHit) -> None:
        row = ctk.CTkFrame(self.file_list, fg_color=CARD, corner_radius=12)
        row.pack(fill="x", pady=4)
        var = ctk.BooleanVar(master=self, value=hit.checked)

        def toggle(item=hit, variable=var) -> None:
            item.checked = bool(variable.get())
            self._set_file_total()

        ctk.CTkCheckBox(row, text="", variable=var, command=toggle, width=28, checkbox_width=20, checkbox_height=20).pack(side="left", padx=(12, 4), pady=12)
        side = ctk.CTkFrame(row, fg_color="transparent")
        side.pack(side="right", padx=10, pady=8)
        ctk.CTkLabel(side, text=format_size(hit.size), font=self.font_b, text_color=TEXT).pack(anchor="e")
        ctk.CTkLabel(side, text=f"Idle {idle_days(hit.modified, hit.accessed)} days", font=self.font_s, text_color=MUTED).pack(anchor="e")
        ctk.CTkLabel(side, text=f"{hit.score}  {hit.verdict}", font=self.font_s, text_color=VERDICT_COLOR.get(hit.verdict, MUTED)).pack(anchor="e")
        self._button(side, "Show", lambda path=hit.path: reveal_in_explorer(path), "ghost", width=64).pack(anchor="e", pady=(4, 0))
        text = ctk.CTkFrame(row, fg_color="transparent")
        text.pack(side="left", fill="x", expand=True, pady=8)
        ctk.CTkLabel(text, text=clip(os.path.basename(hit.path), 80), font=self.font_b, text_color=TEXT, anchor="w").pack(fill="x")
        ctk.CTkLabel(text, text=shorten_path(os.path.dirname(hit.path), 100), font=self.font_s, text_color=FAINT, anchor="w").pack(fill="x")
        ctk.CTkLabel(text, text=" ".join(hit.reasons[:3]), font=self.font_s, text_color=MUTED, anchor="w", justify="left", wraplength=620).pack(fill="x")

    def _set_file_total(self) -> None:
        if not self.file_total or not self.file_total.winfo_exists():
            return
        hits = self.file_result.hits if self.file_result else []
        chosen = [hit for hit in hits if hit.checked]
        likely = [hit for hit in hits if hit.precheck]
        self.file_total.configure(
            text=f"{len(chosen)} ticked  ·  {format_size(sum(hit.size for hit in chosen))}    {len(likely)} look like leftovers"
        )

    def _tick_leftovers(self) -> None:
        for hit in self.file_result.hits if self.file_result else []:
            hit.checked = hit.precheck
        self._render_file_list()

    def _clear_files(self) -> None:
        for hit in self.file_result.hits if self.file_result else []:
            hit.checked = False
        self._render_file_list()

    def start_file_scan(self) -> None:
        roots = self._file_roots()
        if not roots:
            self.show_info("Choose a location", "Pick personal folders, a folder, or at least one drive.")
            return
        for root in roots:
            if len(os.path.abspath(root).rstrip("\\")) > 3 and is_system_location(root):
                self.show_info("That folder is protected", "Windows and Program Files are left alone. Choose a personal folder, or scan a whole drive and those folders will be skipped.")
                return
        try:
            min_bytes = int(float(self.min_size.get().strip()) * 1024 * 1024)
        except ValueError:
            min_bytes = 50 * 1024 * 1024
        min_bytes = max(1, min_bytes)
        idle = None
        if self.only_idle.get():
            try:
                idle = max(0, int(self.min_days.get().strip()))
            except ValueError:
                idle = 180
        token = self.begin_job("files", "Scanning for old and large files…")
        if token is None:
            return
        include_appdata = bool(self.include_appdata.get())

        def work(cancel, progress):
            return scan_files(roots, min_bytes, idle, include_appdata, cancel, progress)

        def done(result, error) -> None:
            if error is None:
                self.file_result = result
                self.file_page_index = 0
                self._remember_scan(stamp=True)
                log(f"File scan: {result.matched} matches in {len(result.roots)} locations, kept {len(result.hits)}.")
            elif self.current == "files":
                self.show_info("File scan did not finish", str(error))
            if self.current == "files":
                self.show_files()

        self.run_bg(token, work, done)

    def recycle_files(self) -> None:
        self._delete_files(recycle=True)

    def purge_files(self) -> None:
        self._delete_files(recycle=False)

    def _delete_files(self, recycle: bool) -> None:
        hits = self.file_result.hits if self.file_result else []
        chosen = [hit for hit in hits if hit.checked]
        if not chosen:
            self.show_info("Nothing ticked", "Tick the files you want to remove. Leftovers can be ticked from the button above the list.")
            return
        total = sum(hit.size for hit in chosen)
        if recycle:
            title = "Move to the Recycle Bin"
            message = f"{len(chosen)} files, about {format_size(total)}, will go to the Recycle Bin. You can restore them from there until you empty it."
            label = "Move files"
            danger = False
        else:
            title = "Delete permanently"
            message = f"{len(chosen)} files, about {format_size(total)}, will be deleted and will not go to the Recycle Bin."
            label = "Delete permanently"
            danger = True
        if not self.confirm(title, message, label, danger=danger):
            return
        roots = self.file_result.roots if self.file_result else []
        token = self.begin_job("files", "Moving files…" if recycle else "Deleting files…")
        if token is None:
            return

        def work(cancel, progress):
            return delete_personal_files(hits, roots, recycle, cancel, progress)

        def done(result, error) -> None:
            if error is not None:
                self.show_info("Could not remove those files", str(error))
                return
            report, removed = result
            removed_keys = {os.path.normcase(path) for path in removed}
            if self.file_result:
                self.file_result.hits = [hit for hit in self.file_result.hits if os.path.normcase(hit.path) not in removed_keys]
                self._remember_scan()
            where = "Recycle Bin" if recycle else "permanent delete"
            log(f"{where}: {report.deleted_files} files, {format_size(report.deleted_bytes)}. Skipped {report.skipped}.")
            for path in removed[:80]:
                log(f"  {path}")
            self.show_info("Files updated", _report_text(report, permanent=not recycle))
            if self.current == "files":
                self.show_files()

        self.run_bg(token, work, done)

    def show_services(self) -> None:
        self._building = True
        try:
            self._build_services_page()
        finally:
            self._building = False

    def _build_services_page(self) -> None:
        frame = self._page()
        frame.grid_rowconfigure(3, weight=1)
        self._heading(
            frame,
            "Services",
            "A service is a program Windows keeps running in the background. Security services stay locked. Optional Windows services can be set to Manual, which means they start only when something asks for them.",
        )
        tools = ctk.CTkFrame(frame, fg_color="transparent")
        tools.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        self._button(tools, "Scan", self.start_service_scan).pack(side="left")
        ctk.CTkOptionMenu(
            tools,
            values=["Needs a look", "Third-party", "Optional Windows", "All"],
            variable=self.service_filter,
            command=lambda _value: self._reset_service_page(),
            font=self.font,
            dropdown_font=self.font,
            fg_color=CARD,
            button_color=CARD_2,
            button_hover_color=LINE,
            width=180,
        ).pack(side="left", padx=8)
        ctk.CTkEntry(tools, textvariable=self.service_query, placeholder_text="Search name or description", font=self.font).pack(side="left", fill="x", expand=True)
        self._button(tools, "Windows Services", self._open_services, "ghost").pack(side="left", padx=(8, 0))
        if not is_admin():
            ctk.CTkLabel(tools, text="Admin needed to change services", font=self.font_s, text_color=WARN).pack(side="right")
        self.service_list = ctk.CTkScrollableFrame(frame, fg_color="transparent")
        self.service_list.grid(row=3, column=0, sticky="nsew", pady=(8, 0))
        self._render_services()

    def _reset_service_page(self) -> None:
        if self._building or self.current != "services":
            return
        self.service_page_index = 0
        if self.current == "services" and hasattr(self, "service_list"):
            try:
                if self.service_list.winfo_exists():
                    self._render_services()
            except Exception:
                return

    def _service_matches(self, service: ServiceInfo) -> bool:
        mode = self.service_filter.get()
        if mode == "Third-party" and service.publisher_kind != "third-party":
            return False
        if mode == "Optional Windows" and not service.optional_windows:
            return False
        if mode == "Needs a look" and service.impact in {"System", "Low", "Not running", "Security"}:
            return False
        query = self.service_query.get().strip().lower()
        if not query:
            return True
        blob = f"{service.name} {service.display_name} {service.description} {service.advice}".lower()
        return query in blob

    def _render_services(self) -> None:
        if not hasattr(self, "service_list") or not self.service_list.winfo_exists():
            return
        for child in self.service_list.winfo_children():
            child.destroy()
        if self.services is None:
            message = "Reading services…" if self.busy and self.job_page == "services" else "Press Scan to see what is running and which services start with Windows."
            ctk.CTkLabel(self.service_list, text=message, font=self.font, text_color=MUTED, anchor="w").pack(fill="x", pady=8)
            return
        rows = [service for service in self.services if self._service_matches(service)]
        if not rows:
            ctk.CTkLabel(self.service_list, text="Nothing in this filter.", font=self.font, text_color=MUTED, anchor="w").pack(fill="x", pady=8)
            return
        pages = max(1, math.ceil(len(rows) / SERVICE_PAGE))
        self.service_page_index = min(self.service_page_index, pages - 1)
        start = self.service_page_index * SERVICE_PAGE
        for service in rows[start : start + SERVICE_PAGE]:
            self._service_row(service)
        self._pager(self.service_list, self.service_page_index, pages, lambda page: self._move_service_page(page))

    def _move_service_page(self, page: int) -> None:
        self.service_page_index = page
        self._render_services()

    def _service_row(self, service: ServiceInfo) -> None:
        row = ctk.CTkFrame(self.service_list, fg_color=CARD, corner_radius=12, cursor="hand2")
        row.pack(fill="x", pady=4)
        text = ctk.CTkFrame(row, fg_color="transparent")
        text.pack(side="left", fill="x", expand=True, padx=14, pady=10)
        memory = format_size(service.memory) if service.memory else "—"
        memory_note = "shared" if service.shared_process and service.memory else memory
        ctk.CTkLabel(text, text=service.display_name, font=self.font_b, text_color=TEXT, anchor="w").pack(fill="x")
        ctk.CTkLabel(
            text,
            text=clip(service.advice or service.description or "No description published.", 180),
            font=self.font_s,
            text_color=MUTED,
            anchor="w",
            justify="left",
            wraplength=700,
        ).pack(fill="x")
        side = ctk.CTkFrame(row, fg_color="transparent")
        side.pack(side="right", padx=12)
        ctk.CTkLabel(side, text=service.impact, font=self.font_s, text_color=IMPACT_COLOR.get(service.impact, MUTED)).pack(anchor="e")
        ctk.CTkLabel(side, text=f"{service.status}  ·  {service.start_type}", font=self.font_s, text_color=FAINT).pack(anchor="e")
        ctk.CTkLabel(side, text=memory_note, font=self.font_s, text_color=MUTED).pack(anchor="e")

        def open_service(_event=None, item=service) -> None:
            self._open_service(item)

        _bind_click(row, open_service)

    def _open_service(self, service: ServiceInfo) -> None:
        lines = [
            service.advice or "No description is published for this service.",
            "",
            f"Name: {service.name}",
            f"Status: {service.status}",
            f"Start: {service.start_type}",
            f"Memory: {format_size(service.memory) if service.memory else 'not running'}",
        ]
        if service.cpu:
            lines.append(f"CPU while scanning: {service.cpu:.1f}%")
        if service.shared_process:
            lines.append("Memory and CPU are for the shared host process, not this service alone.")
        if service.binpath:
            lines.append(f"Program: {service.binpath}")
        if not service.actions:
            lines.append("")
            lines.append("This one stays locked so Windows, printing, or security is not switched off by accident.")
        action = self.ask(
            service.display_name,
            "\n".join(lines),
            _service_choices(service),
        )
        if action in {"stop", "manual", "automatic"}:
            self._change_service(service, action)

    def _change_service(self, service: ServiceInfo, action: str) -> None:
        if not is_admin():
            self.show_info("Administrator needed", "Restart as administrator to change services.")
            return
        token = self.begin_job("services", f"Updating {service.display_name}…")
        if token is None:
            return

        def work(_cancel, _progress):
            if action == "stop":
                return stop_service(service.name)
            return set_start_type(service.name, action)

        def done(result, error) -> None:
            if error is not None:
                self.show_info("Service was not changed", str(error))
                return
            if action == "stop":
                service.status = "stop pending"
            else:
                service.start_type = "automatic" if action == "automatic" else "manual"
            log(f"Service {service.name} set to {action}. {result}")
            if self.current == "services":
                self.show_services()
            self.show_info("Service updated", f"{service.display_name} is now {action}. Manual means it can still start when an app asks for it.")

        self.run_bg(token, work, done)

    def _open_services(self) -> None:
        os.startfile("services.msc")

    def start_service_scan(self) -> None:
        token = self.begin_job("services", "Reading services…")
        if token is None:
            return

        def work(cancel, progress):
            return scan_services(cancel, progress, sample_cpu=True)

        def done(result, error) -> None:
            if error is None:
                self.services = result
                self.service_page_index = 0
                self._remember_scan(stamp=True)
            elif self.current == "services":
                self.show_info("Service scan did not finish", str(error))
            if self.current == "services":
                self.show_services()

        self.run_bg(token, work, done)

    def show_startup(self) -> None:
        frame = self._page()
        frame.grid_rowconfigure(3, weight=1)
        self._heading(
            frame,
            "Startup",
            "These programs launch when you sign in. Turning one off keeps it installed. You can turn it back on here. A missing file means the entry is leftover.",
        )
        tools = ctk.CTkFrame(frame, fg_color="transparent")
        tools.grid(row=2, column=0, sticky="ew")
        self._button(tools, "Scan", self.start_startup_scan).pack(side="left")
        listing = ctk.CTkScrollableFrame(frame, fg_color="transparent")
        listing.grid(row=3, column=0, sticky="nsew", pady=(8, 0))
        if self.startup_items is None:
            message = "Reading startup programs…" if self.busy and self.job_page == "startup" else "Press Scan to list startup programs."
            ctk.CTkLabel(listing, text=message, font=self.font, text_color=MUTED, anchor="w").pack(fill="x", pady=8)
            return
        if not self.startup_items:
            ctk.CTkLabel(listing, text="No startup programs were found in the usual places.", font=self.font, text_color=MUTED, anchor="w").pack(fill="x", pady=8)
            return
        for item in self.startup_items:
            self._startup_row(listing, item)

    def _startup_row(self, parent, item: StartupItem) -> None:
        row = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=12)
        row.pack(fill="x", pady=4)
        text = ctk.CTkFrame(row, fg_color="transparent")
        text.pack(side="left", fill="x", expand=True, padx=14, pady=10)
        title = item.name + ("  ·  off" if not item.enabled else "")
        ctk.CTkLabel(text, text=title, font=self.font_b, text_color=TEXT, anchor="w").pack(fill="x")
        detail = item.description or clip(item.command, 120)
        if item.missing:
            detail = "The file this entry launches is missing. " + detail
        ctk.CTkLabel(text, text=f"{item.location}  ·  {detail}", font=self.font_s, text_color=WARN if item.missing else MUTED, anchor="w", justify="left", wraplength=680).pack(fill="x")
        label = "Turn on" if not item.enabled else "Turn off"
        self._button(row, label, lambda current=item: self._toggle_startup(current), "ghost" if item.enabled else "primary").pack(side="right", padx=12)

    def _toggle_startup(self, item: StartupItem) -> None:
        if item.scope == "HKLM" and not is_admin():
            self.show_info("Administrator needed", "This startup item is for every user on the PC. Restart as administrator to change it.")
            return
        turning_off = item.enabled
        message = (
            f"Turn off {item.name} at startup? It stays installed, and you can turn it back on here."
            if turning_off
            else f"Turn {item.name} back on at startup?"
        )
        if not self.confirm("Startup", message, "Turn off" if turning_off else "Turn on", danger=False):
            return
        token = self.begin_job("startup", "Updating startup…")
        if token is None:
            return

        def work(_cancel, _progress):
            if turning_off:
                disable_startup(item)
            else:
                enable_startup(item)
            return True

        def done(_result, error) -> None:
            if error is not None:
                self.show_info("Startup was not changed", str(error))
                return
            log(f"{'Disabled' if turning_off else 'Enabled'} startup item {item.name}")
            self.start_startup_scan()

        self.run_bg(token, work, done)

    def start_startup_scan(self) -> None:
        token = self.begin_job("startup", "Reading startup programs…")
        if token is None:
            return

        def done(result, error) -> None:
            if error is None:
                self.startup_items = result
                self._remember_scan(stamp=True)
            elif self.current == "startup":
                self.show_info("Startup scan did not finish", str(error))
            if self.current == "startup":
                self.show_startup()

        self.run_bg(token, scan_startup, done)

    def show_registry(self) -> None:
        frame = self._page()
        frame.grid_rowconfigure(3, weight=1)
        self._heading(
            frame,
            "Registry",
            "Looks for leftover registry entries that point at files already gone: startup, uninstall records, application paths, shared files, fonts, help files, installer folders, and icon names. Safe items are ticked. Review items stay unticked. This tidies the registry. It will not make a healthy PC faster. A .reg backup is saved before each change.",
        )
        tools = ctk.CTkFrame(frame, fg_color="transparent")
        tools.grid(row=2, column=0, sticky="ew")
        self._button(tools, "Scan", self.start_registry_scan).pack(side="left")
        self._button(tools, "Tick recommended", self._tick_recommended, "ghost").pack(side="left", padx=8)
        self._button(tools, "Tick all", self._tick_registry, "ghost").pack(side="left")
        self._button(tools, "Clear ticks", self._clear_registry, "ghost").pack(side="left", padx=8)
        self._button(tools, "Open backups", lambda: os.startfile(backup_dir()), "ghost").pack(side="left")
        self.registry_list = ctk.CTkScrollableFrame(frame, fg_color="transparent")
        self.registry_list.grid(row=3, column=0, sticky="nsew", pady=(8, 0))
        bar = ctk.CTkFrame(frame, fg_color="transparent")
        bar.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        self._button(bar, "Back up and remove ticked items", self.clean_registry, "danger").pack(side="right")
        self._render_registry()

    def _render_registry(self) -> None:
        if not hasattr(self, "registry_list") or not self.registry_list.winfo_exists():
            return
        for child in self.registry_list.winfo_children():
            child.destroy()
        if self.registry_items is None:
            message = "Checking the registry…" if self.busy and self.job_page == "registry" else "Press Scan to look for broken leftovers."
            ctk.CTkLabel(self.registry_list, text=message, font=self.font, text_color=MUTED, anchor="w").pack(fill="x", pady=8)
            return
        if not self.registry_items:
            ctk.CTkLabel(self.registry_list, text="No broken leftovers were found in the places this tool checks.", font=self.font, text_color=MUTED, anchor="w").pack(fill="x", pady=8)
            return
        safe = sum(1 for item in self.registry_items if item.recommendation == "Safe to remove")
        review = len(self.registry_items) - safe
        summary = ctk.CTkFrame(self.registry_list, fg_color=CARD, corner_radius=12)
        summary.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(
            summary,
            text=f"{len(self.registry_items)} leftovers  ·  {safe} safe to remove  ·  {review} to review first",
            font=self.font_b,
            text_color=TEXT,
            anchor="w",
        ).pack(fill="x", padx=14, pady=(12, 4))
        ctk.CTkLabel(
            summary,
            text="Recommended fixes are under each group. Ticked items are the ones that only refer to a missing file. Nothing is removed until you confirm.",
            font=self.font_s,
            text_color=MUTED,
            anchor="w",
            justify="left",
            wraplength=820,
        ).pack(fill="x", padx=14, pady=(0, 12))
        order = list(KIND_GUIDE)
        grouped: dict[str, list] = {kind: [] for kind in order}
        extra: list = []
        for item in self.registry_items:
            if item.kind in grouped:
                grouped[item.kind].append(item)
            else:
                extra.append(item)
        for kind in order:
            batch = grouped[kind]
            if not batch:
                continue
            if kind == "mui":
                self._icon_cache_row(batch)
            else:
                self._registry_group(kind, batch)
        if extra:
            self._registry_group("", extra)

    def _registry_group(self, kind: str, items: list) -> None:
        guide = KIND_GUIDE.get(kind)
        title, recommendation, fix = guide if guide else ("Other leftovers", "Review first", "Check each one before removing it.")
        head = ctk.CTkFrame(self.registry_list, fg_color="transparent")
        head.pack(fill="x", pady=(10, 2))
        color = OK if recommendation == "Safe to remove" else WARN
        ctk.CTkLabel(head, text=f"{title}  ·  {len(items)}", font=self.font_b, text_color=TEXT, anchor="w").pack(fill="x")
        ctk.CTkLabel(head, text=recommendation, font=self.font_s, text_color=color, anchor="w").pack(fill="x")
        ctk.CTkLabel(head, text=fix, font=self.font_s, text_color=MUTED, anchor="w", justify="left", wraplength=860).pack(fill="x", pady=(2, 4))
        for item in items:
            self._registry_row(item)

    def _registry_row(self, item) -> None:
        row = ctk.CTkFrame(self.registry_list, fg_color=CARD, corner_radius=12)
        row.pack(fill="x", pady=4)
        var = ctk.BooleanVar(master=self, value=item.checked)

        def toggle(issue=item, variable=var) -> None:
            issue.checked = bool(variable.get())

        ctk.CTkCheckBox(row, text="", variable=var, command=toggle, width=28, checkbox_width=20, checkbox_height=20).pack(side="left", padx=(12, 4), pady=12)
        text = ctk.CTkFrame(row, fg_color="transparent")
        text.pack(side="left", fill="x", expand=True, pady=8)
        ctk.CTkLabel(text, text=item.problem, font=self.font_b, text_color=TEXT, anchor="w").pack(fill="x")
        detail = item.detail
        if item.recommendation:
            detail = f"{item.recommendation}. {detail}"
        ctk.CTkLabel(
            text,
            text=f"{detail}  ·  {item.hive_name}\\{item.key}",
            font=self.font_s,
            text_color=MUTED,
            anchor="w",
            justify="left",
            wraplength=760,
        ).pack(fill="x")

    def _icon_cache_row(self, items) -> None:
        row = ctk.CTkFrame(self.registry_list, fg_color=CARD, corner_radius=12)
        row.pack(fill="x", pady=4)
        var = ctk.BooleanVar(master=self, value=bool(items) and all(item.checked for item in items))

        def toggle(variable=var) -> None:
            checked = bool(variable.get())
            for issue in items:
                issue.checked = checked

        ctk.CTkCheckBox(row, text="", variable=var, command=toggle, width=28, checkbox_width=20, checkbox_height=20).pack(side="left", padx=(12, 4), pady=12)
        text = ctk.CTkFrame(row, fg_color="transparent")
        text.pack(side="left", fill="x", expand=True, pady=8)
        _title, recommendation, fix = KIND_GUIDE["mui"]
        ctk.CTkLabel(text, text=f"{len(items)} stale icon names", font=self.font_b, text_color=TEXT, anchor="w").pack(fill="x")
        ctk.CTkLabel(text, text=recommendation, font=self.font_s, text_color=OK, anchor="w").pack(fill="x")
        ctk.CTkLabel(
            text,
            text=fix,
            font=self.font_s,
            text_color=MUTED,
            anchor="w",
            justify="left",
            wraplength=760,
        ).pack(fill="x")

    def _tick_registry(self) -> None:
        for item in self.registry_items or []:
            item.checked = True
        self._render_registry()

    def _tick_recommended(self) -> None:
        for item in self.registry_items or []:
            item.checked = item.recommendation == "Safe to remove"
        self._render_registry()

    def _clear_registry(self) -> None:
        for item in self.registry_items or []:
            item.checked = False
        self._render_registry()

    def start_registry_scan(self) -> None:
        token = self.begin_job("registry", "Checking the registry…")
        if token is None:
            return

        def done(result, error) -> None:
            if error is None:
                self.registry_items = result
                self._remember_scan(stamp=True)
            elif self.current == "registry":
                self.show_info("Registry scan did not finish", str(error))
            if self.current == "registry":
                self.show_registry()

        self.run_bg(token, scan_registry, done)

    def clean_registry(self) -> None:
        chosen = [item for item in self.registry_items or [] if item.checked]
        if not chosen:
            self.show_info("Nothing ticked", "Scan first, then tick the leftovers you want removed.")
            return
        if not self.confirm(
            "Back up and remove",
            f"{len(chosen)} registry leftovers will be removed. A .reg backup of each key is saved in {backup_dir()} first. Entries under HKLM need administrator.",
            "Remove",
            danger=True,
        ):
            return
        token = self.begin_job("registry", "Backing up and removing registry leftovers…")
        if token is None:
            return

        def work(cancel, progress):
            return remove_issues(chosen, cancel, progress)

        def done(result, error) -> None:
            if error is not None:
                self.show_info("Registry clean did not finish", str(error))
                return
            log(f"Registry clean removed {result.deleted_files} entries. Skipped {result.skipped}.")
            for note in result.notes:
                log(f"  backup {note}")
            self.show_info("Registry updated", _report_text(result, permanent=True))
            if self.current == "registry":
                self.start_registry_scan()

        self.run_bg(token, work, done)

    def show_activity(self) -> None:
        frame = self._page()
        frame.grid_rowconfigure(3, weight=1)
        self._heading(frame, "Activity", "A local log of scans and deletes. It stays on this PC.")
        tools = ctk.CTkFrame(frame, fg_color="transparent")
        tools.grid(row=2, column=0, sticky="ew")
        self._button(tools, "Refresh", self.show_activity, "ghost").pack(side="left")
        self._button(tools, "Open log folder", lambda: os.startfile(os.path.dirname(backup_dir())), "ghost").pack(side="left", padx=8)
        box = ctk.CTkTextbox(frame, font=self.font_m, fg_color=CARD, text_color=TEXT, wrap="word")
        box.grid(row=3, column=0, sticky="nsew", pady=(8, 0))
        text = read_log() or "No activity yet."
        box.insert("1.0", text)
        box.configure(state="disabled")

    def _load_ai_fields(self) -> None:
        if self._ai_loaded:
            return
        self._ai_loaded = True
        saved = load_ai_settings()
        if not saved:
            return
        self.ai_provider.set(saved.get("provider") or "ChatGPT (OpenAI)")
        self.ai_base.set(saved.get("base_url") or self.ai_base.get())
        self.ai_model.set(saved.get("model") or self.ai_model.get())
        if saved.get("api_key"):
            self.ai_key.set(saved["api_key"])

    def _apply_ai_provider(self, name: str) -> None:
        if self._building or name not in PROVIDERS:
            return
        url, model = PROVIDERS[name]
        self.ai_base.set(url)
        self.ai_model.set(model)

    def _save_ai_connection(self, quiet: bool = False) -> None:
        save_ai_settings(self.ai_provider.get(), self.ai_base.get(), self.ai_model.get(), self.ai_key.get())
        if not quiet:
            self.set_status("AI connection saved on this PC.")

    def show_ai(self) -> None:
        self._load_ai_fields()
        self._building = True
        try:
            self._build_ai_page()
        finally:
            self._building = False

    def _remember_chatgpt_model(self, label: str) -> None:
        if self._building:
            return
        remember_model_label(label)

    def _build_ai_page(self) -> None:
        summary = account_summary()
        frame = self._page()
        shift = 1 if self.scan_ready_for_ai else 0
        self._heading(
            frame,
            "AI analyst",
            "Sign in with the ChatGPT account you already pay for, or use an API key. Build the report first. The report and the analysis each open in their own window. Nothing is sent until you click Analyze, and it does not read your ChatGPT chats.",
        )
        if self.scan_ready_for_ai:
            banner = ctk.CTkFrame(frame, fg_color=CARD, corner_radius=14)
            banner.grid(row=2, column=0, sticky="ew", pady=(0, 8))
            ctk.CTkLabel(banner, text="A scan is ready for a report", font=self.font_b, text_color=TEXT, anchor="w").pack(fill="x", padx=14, pady=(12, 2))
            signed = "You are signed in." if summary["plan_enabled"] else "Sign in with ChatGPT below if you have not already. An API key works too."
            ctk.CTkLabel(
                banner,
                text=f"{signed} Press Build report. It includes the scan just run: drives, space, caches, startup, services, registry, and old files. Analyze with AI is the step that sends it.",
                font=self.font,
                text_color=MUTED,
                anchor="w",
                justify="left",
                wraplength=860,
            ).pack(fill="x", padx=14, pady=(0, 12))
        self._build_chatgpt_card(frame, summary, row=2 + shift)
        form = ctk.CTkFrame(frame, fg_color=CARD, corner_radius=14)
        form.grid(row=3 + shift, column=0, sticky="ew", pady=(0, 8))
        ctk.CTkLabel(form, text="Other providers, with an API key", font=self.font_s, text_color=MUTED, anchor="w").pack(fill="x", padx=12, pady=(10, 0))
        top = ctk.CTkFrame(form, fg_color="transparent")
        top.pack(fill="x", padx=12, pady=(8, 6))
        ctk.CTkOptionMenu(
            top,
            values=list(PROVIDERS) + ["Custom"],
            variable=self.ai_provider,
            command=self._apply_ai_provider,
            font=self.font,
            dropdown_font=self.font,
            fg_color=CARD_2,
            button_color=CARD_2,
            button_hover_color=LINE,
            width=220,
        ).pack(side="left")
        ctk.CTkEntry(top, textvariable=self.ai_model, width=220, font=self.font, placeholder_text="Model name").pack(side="left", padx=8)
        ctk.CTkEntry(top, textvariable=self.ai_key, font=self.font, show="*", placeholder_text="API key").pack(side="left", fill="x", expand=True)
        self._button(top, "Save", lambda: self._save_ai_connection(False), "ghost").pack(side="left", padx=(8, 0))
        ctk.CTkEntry(form, textvariable=self.ai_base, font=self.font, placeholder_text="https://api.openai.com/v1").pack(fill="x", padx=12, pady=(0, 12))
        tools = ctk.CTkFrame(frame, fg_color="transparent")
        tools.grid(row=4 + shift, column=0, sticky="ew", pady=(0, 8))
        report_style = "primary" if self.scan_ready_for_ai and not self.ai_report else "ghost"
        answer_style = "ghost" if report_style == "primary" else "primary"
        self._button(tools, "Build report", self.start_ai_report, report_style).pack(side="left")
        self._button(tools, "Analyze with AI", self.start_ai_answer, answer_style).pack(side="left", padx=8)
        hint = "While you are signed in, Analyze uses your ChatGPT plan." if summary["plan_enabled"] else "Sign in, or use an API key. Nothing is sent until Analyze with AI."
        ctk.CTkLabel(tools, text=hint, font=self.font_s, text_color=FAINT).pack(side="left", padx=12)
        library = ctk.CTkFrame(frame, fg_color=CARD, corner_radius=14)
        library.grid(row=5 + shift, column=0, sticky="ew")
        self._text_actions(library, "Report", "The local report that can be sent. It is not sent until you analyze.", self.ai_report, "report", "PC Cleanup report.txt")
        self._text_actions(library, "Analysis", "The write-up that comes back. It opens in its own window when it finishes.", self.ai_answer, "analysis", "PC Cleanup analysis.txt")

    def _build_chatgpt_card(self, frame, summary: dict, row: int = 2) -> None:
        card = ctk.CTkFrame(frame, fg_color=CARD, corner_radius=14)
        card.grid(row=row, column=0, sticky="ew", pady=(0, 8))
        if summary["plan_enabled"]:
            who = summary["email"] or "your ChatGPT account"
            text = f"Signed in as {who}. Analysis uses your ChatGPT plan."
        elif summary["signed_in"]:
            who = summary["email"] or "your ChatGPT account"
            text = f"Signed in as {who}. ChatGPT plan usage is off."
        else:
            text = "Use the ChatGPT subscription you already pay for. Plus and Pro can run this analysis."
        ctk.CTkLabel(card, text=text, font=self.font, text_color=TEXT, anchor="w", justify="left", wraplength=860).pack(fill="x", padx=14, pady=(12, 8))
        buttons = ctk.CTkFrame(card, fg_color="transparent")
        buttons.pack(fill="x", padx=14, pady=(0, 12))
        if summary["plan_enabled"]:
            labels = [item["label"] for item in summary["models"]]
            if labels:
                if self.ai_chatgpt_model.get() not in labels:
                    preferred = summary["model_label"] or labels[0]
                    self.ai_chatgpt_model.set(preferred if preferred in labels else labels[0])
                ctk.CTkOptionMenu(
                    buttons,
                    values=labels,
                    variable=self.ai_chatgpt_model,
                    command=self._remember_chatgpt_model,
                    font=self.font,
                    dropdown_font=self.font,
                    fg_color=CARD_2,
                    button_color=CARD_2,
                    button_hover_color=LINE,
                    width=240,
                ).pack(side="left", padx=(0, 8))
            self._button(buttons, "Manage usage", open_usage_settings, "ghost").pack(side="left", padx=(0, 8))
            self._button(buttons, "Sign out", self.start_chatgpt_sign_out, "ghost").pack(side="left")
            return
        if not summary["signed_in"]:
            self._button(buttons, "Continue with ChatGPT", lambda: self.start_chatgpt_sign_in(False, False)).pack(side="left", padx=(0, 8))
        if summary["signed_in"] or summary["has_registration"]:
            self._button(buttons, "Allow ChatGPT plan usage", lambda: self.start_chatgpt_sign_in(False, True)).pack(side="left", padx=(0, 8))
        if summary["has_registration"]:
            self._button(buttons, "Use another account", lambda: self.start_chatgpt_sign_in(True, False), "ghost").pack(side="left", padx=(0, 8))
        if summary["signed_in"]:
            self._button(buttons, "Sign out", self.start_chatgpt_sign_out, "ghost").pack(side="left")

    def _collected_scan(self) -> dict:
        dashboard = self.dashboard_data
        return {
            "dashboard": dashboard,
            "space": self.space_report,
            "junk": self.junk_items,
            "registry": self.registry_items,
            "files": self.file_result,
            "startup": self.startup_items if self.startup_items is not None else getattr(dashboard, "startup_items", None),
            "services": self.services if self.services is not None else getattr(dashboard, "service_items", None),
            "performance": self.perf_report,
            "drivers": self.driver_report,
        }

    def start_ai_report(self) -> None:
        token = self.begin_job("ai", "Building a local report…")
        if token is None:
            return
        collected = self._collected_scan()

        def work(cancel, progress):
            return build_report(cancel, progress, collected)

        def done(result, error) -> None:
            if error is not None:
                self.show_info("Report did not finish", str(error))
                return
            self.ai_report = result
            self._remember_scan()
            log("Built a local AI report. It was not sent.")
            if self.current == "ai":
                self.show_ai()
            if (result or "").strip():
                self._open_text_popout("report", "Report", result, "PC Cleanup report.txt")

        self.run_bg(token, work, done)

    def start_chatgpt_sign_in(self, new_account: bool = False, consent: bool = False) -> None:
        token = self.begin_job("ai", "Waiting for ChatGPT in your browser…")
        if token is None:
            return

        def work(cancel, progress):
            return sign_in(cancel, progress, new_account=new_account, consent=consent)

        def done(result, error) -> None:
            if error is not None:
                self.show_info("ChatGPT sign-in did not finish", str(error))
                return
            log("Signed in with ChatGPT. Tokens were not written to the log.")
            if result.get("first_plan"):
                self.show_info(
                    "Using your ChatGPT plan",
                    "Eligible analysis in this app uses your ChatGPT plan. Manage usage opens ChatGPT settings if you want to review the limit.",
                )
            elif result.get("signed_in") and not result.get("plan_enabled"):
                self.show_info(
                    "Plan usage is off",
                    "You are signed in, but ChatGPT plan usage was not allowed. Choose Allow ChatGPT plan usage and approve it in the browser.",
                )
            if self.current == "ai":
                self.show_ai()

        self.run_bg(token, work, done)

    def start_chatgpt_sign_out(self) -> None:
        token = self.begin_job("ai", "Signing out of ChatGPT…")
        if token is None:
            return

        def work(cancel, progress):
            progress("Signing out of ChatGPT…")
            return sign_out()

        def done(result, error) -> None:
            if error is not None:
                self.show_info("Sign-out did not finish", str(error))
                return
            log("Signed out of ChatGPT on this PC.")
            if not result:
                self.show_info(
                    "Signed out on this PC",
                    "ChatGPT did not confirm the remote sign-out. You can disconnect PC Cleanup from ChatGPT settings.",
                )
            if self.current == "ai":
                self.show_ai()

        self.run_bg(token, work, done)

    def start_ai_answer(self) -> None:
        self._save_ai_connection(quiet=True)
        base = self.ai_base.get()
        key = self.ai_key.get()
        model = self.ai_model.get()
        chatgpt_model = self.ai_chatgpt_model.get()
        use_plan = account_summary()["plan_enabled"]
        existing = self.ai_report
        collected = self._collected_scan()
        token = self.begin_job("ai", "Preparing the report for the AI…")
        if token is None:
            return

        def work(cancel, progress):
            report = existing.strip() or build_report(cancel, progress, collected)
            if cancel.is_set():
                return report, ""
            if use_plan:
                answer = ask_with_plan(chatgpt_model, report, progress, cancel)
            else:
                answer = ask_model(base, key, model, report, progress)
            return report, answer

        def done(result, error) -> None:
            if error is not None:
                self.show_info("AI analysis did not finish", str(error))
                return
            report, answer = result
            self.ai_report = report
            self.ai_answer = answer
            self._remember_scan()
            via = "ChatGPT plan" if use_plan else model
            log(f"AI analysis finished with {via}. Secrets were not written to the log.")
            if self.current == "ai":
                self.show_ai()
            if (answer or "").strip():
                self._open_text_popout("analysis", "AI analysis", answer, "PC Cleanup analysis.txt")

        self.run_bg(token, work, done)

    def _text_actions(self, parent, title: str, detail: str, body: str, kind: str, filename: str) -> None:
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=10)
        text = ctk.CTkFrame(row, fg_color="transparent")
        text.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(text, text=title, font=self.font_b, text_color=TEXT, anchor="w").pack(fill="x")
        status = "Ready to open." if (body or "").strip() else "Not created yet."
        ctk.CTkLabel(text, text=f"{detail} {status}", font=self.font_s, text_color=MUTED, anchor="w", justify="left", wraplength=620).pack(fill="x")
        self._button(row, "Export", lambda: self._export_text(title, body, filename), "ghost").pack(side="right")
        self._button(row, "Open", lambda: self._open_text_popout(kind, title, body, filename), "ghost").pack(side="right", padx=(0, 8))

    def _open_text_popout(self, kind: str, title: str, body: str, filename: str) -> None:
        text = body or ""
        if not text.strip():
            self.show_info(title, "Nothing to open yet.")
            return
        previous = self.reader_windows.get(kind)
        if previous is not None:
            try:
                if previous.winfo_exists():
                    previous.destroy()
            except Exception:
                pass
        window = ctk.CTkToplevel(self)
        self.reader_windows[kind] = window
        window.title(title)
        window.configure(fg_color=BG)
        window.minsize(720, 520)
        window.geometry("920x700")
        window.transient(self)
        self._style_title_soon(window)
        wrap = ctk.CTkFrame(window, fg_color=BG)
        wrap.pack(fill="both", expand=True, padx=18, pady=16)
        header = ctk.CTkFrame(wrap, fg_color="transparent")
        header.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(header, text=title, font=self.font_h, text_color=TEXT, anchor="w").pack(side="left")
        self._button(header, "Export", lambda: self._export_text(title, text, filename, window), "ghost").pack(side="right")
        box = ctk.CTkTextbox(wrap, font=self.font, fg_color=CARD, text_color=TEXT, wrap="word")
        box.pack(fill="both", expand=True)
        box.insert("1.0", text)
        box.configure(state="disabled")
        self._place_reader(window)
        try:
            window.attributes("-topmost", True)
            window.after(250, lambda: window.attributes("-topmost", False) if window.winfo_exists() else None)
            window.lift()
            window.focus_force()
        except Exception:
            pass

    def _place_reader(self, window) -> None:
        self._place_window(window, 920, 700)

    def _export_text(self, title: str, body: str, filename: str, parent=None) -> None:
        text = body or ""
        if not text.strip():
            self.show_info(title, "Nothing to export yet.")
            return
        path = filedialog.asksaveasfilename(
            parent=parent or self,
            title=f"Export {title}",
            defaultextension=".txt",
            initialfile=filename,
            filetypes=[("Text", "*.txt"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)
        except OSError as exc:
            self.show_info("Export did not finish", str(exc))
            return
        log(f"Exported {title} to {path}")
        self.set_status(f"Saved {os.path.basename(path)}")

    def _pager(self, parent, index: int, pages: int, on_move) -> None:
        bar = ctk.CTkFrame(parent, fg_color="transparent")
        bar.pack(fill="x", pady=(8, 0))
        self._button(bar, "Previous", lambda: on_move(index - 1), "ghost", width=100).pack(side="left")
        if index == 0:
            bar.winfo_children()[-1].configure(state="disabled")
        ctk.CTkLabel(bar, text=f"{index + 1} / {pages}", font=self.font_s, text_color=MUTED).pack(side="left", padx=12)
        self._button(bar, "Next", lambda: on_move(index + 1), "ghost", width=100).pack(side="left")
        if index >= pages - 1:
            bar.winfo_children()[-1].configure(state="disabled")


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("rcMonitor", ctypes.c_long * 4),
        ("rcWork", ctypes.c_long * 4),
        ("dwFlags", ctypes.c_ulong),
    ]


def work_area(widget) -> tuple[int, int, int, int]:
    """The monitor a window is on, minus the taskbar, in physical pixels.

    Tk's winfo_screenwidth reports scaled-down sizes on a high-DPI display,
    so it cannot be mixed with window positions.
    """
    user32 = ctypes.windll.user32
    user32.GetParent.restype = ctypes.c_void_p
    user32.GetParent.argtypes = [ctypes.c_void_p]
    user32.MonitorFromWindow.restype = ctypes.c_void_p
    user32.MonitorFromWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    try:
        handle = user32.GetParent(widget.winfo_id()) or widget.winfo_id()
        monitor = user32.MonitorFromWindow(handle, 2)
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        if monitor and user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return tuple(info.rcWork)
    except (OSError, tk.TclError):
        pass
    return 0, 0, widget.winfo_screenwidth(), widget.winfo_screenheight()


def _bind_click(widget, callback) -> None:
    widget.bind("<Button-1>", callback)
    for child in widget.winfo_children():
        _bind_click(child, callback)


def _service_choices(service: ServiceInfo) -> list[tuple[str, object, str]]:
    choices: list[tuple[str, object, str]] = [("Close", None, "ghost")]
    labels = {"stop": "Stop until restart", "manual": "Set to Manual", "automatic": "Set to Automatic"}
    for action in service.actions:
        style = "danger" if action == "stop" else "primary"
        choices.append((labels[action], action, style))
    return choices


def _report_text(report, permanent: bool) -> str:
    verb = "Removed" if permanent else "Moved to the Recycle Bin"
    lines = [f"{verb} {report.deleted_files} files ({format_size(report.deleted_bytes)})."]
    if report.skipped:
        lines.append(f"{report.skipped} files were locked, protected, or already gone.")
    backups = [note for note in report.notes if isinstance(note, str) and note.lower().endswith(".reg")]
    if backups:
        lines.append("")
        lines.append("Backups:")
        lines.extend(backups[:8])
    if report.errors:
        lines.append("")
        lines.extend(report.errors[:6])
    return "\n".join(lines)
