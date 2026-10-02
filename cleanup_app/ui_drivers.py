"""The Drivers page: every device, its driver, problems, and the newest driver three sources know of."""

from __future__ import annotations

import copy
import os
import tkinter as tk
import webbrowser
from datetime import date

import customtkinter as ctk

from cleanup_app.driver_lookup import install_from_catalog, open_download
from cleanup_app.drivers import (
    GPU_OLD_DAYS,
    OLD_DAYS,
    QUIET_CLASSES,
    create_restore_point,
    find_updates,
    install_updates,
    remove_packages,
    scan_drivers,
    vendor_page,
    web_search_query,
    web_search_url,
)
from cleanup_app.theme import CARD, CARD_2, DANGER, FAINT, INFO, LINE, MUTED, OK, TEXT, WARN
from cleanup_app.ui_tree import TreeTable
from cleanup_app.util import format_size, is_admin, log

FILTERS = ["Needs attention", "Third-party drivers", "All devices"]
SHORT_SOURCE = {"Windows Update": "Windows Update", "Update Catalog": "Catalog", "NVIDIA": "NVIDIA"}


def _when(stamp: str) -> str:
    try:
        return date.fromisoformat(stamp).strftime("%d %b %Y")
    except (TypeError, ValueError):
        return stamp or "Unknown"


def _age(days: int) -> str:
    if days < 0:
        return ""
    if days < 60:
        return f"{days} days old"
    if days < 730:
        return f"{days // 30} months old"
    return f"{days / 365:.1f} years old"


def _status(device) -> tuple[str, str]:
    if device.problem_code and device.problem_code not in {22, 45}:
        best = device.best
        found = f" A driver was found in {best.source}." if best is not None else ""
        return f"Problem: {device.problem}{found}", "danger"
    best = device.best
    if best is not None:
        if device.inbox:
            return f"Maker's driver, {_when(best.date)}", "info"
        return f"Newer, {_when(best.date)}", "info"
    if device.problem_code == 22:
        return "Turned off", "muted"
    if device.is_gpu and device.age_days > GPU_OLD_DAYS:
        return f"Check maker's site ({_age(device.age_days)})", "warn"
    if not device.inbox and device.age_days > OLD_DAYS:
        return f"Old: {_age(device.age_days)}, nothing newer found", "warn"
    if not device.inf:
        return "No driver", "danger"
    return "Up to date" if not device.inbox else "Windows driver", "muted" if device.inbox else ""


def _install_choice(device):
    """The newest offer this app can install itself, or None when the best is a download page."""
    best = device.best
    if best is None or not best.installable:
        return None
    return best


class DriversPage:
    """Mixed into CleanupApp. Uses its page, button, dialog, and job helpers."""

    def show_drivers(self) -> None:
        frame = self._page()
        frame.grid_rowconfigure(5, weight=1)
        self._heading(
            frame,
            "Drivers",
            "Checks every device against three sources at once: Windows Update, the Microsoft Update Catalog (every published version, kept only when the package lists this device's exact hardware ID), and NVIDIA's own driver service for GeForce cards, which is usually first.",
        )
        tools = ctk.CTkFrame(frame, fg_color="transparent")
        tools.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        report = self.driver_report
        installable = report is not None and any(_install_choice(device) for device in report.devices)
        self._button(tools, "Check drivers", self.start_driver_scan, "ghost" if installable else "primary").pack(side="left")
        self._button(tools, "Install updates", self.start_driver_install, "primary" if installable else "ghost").pack(side="left", padx=8)
        self._button(tools, "Device Manager", lambda: os.startfile("devmgmt.msc"), "ghost").pack(side="left")
        self._button(tools, "Windows Update", lambda: os.startfile("ms-settings:windowsupdate-optionalupdates"), "ghost").pack(side="left", padx=8)
        if report is None:
            text = (
                "Reading devices…"
                if self.busy and self.job_page == "drivers"
                else "Press Check drivers. Reading the devices takes a second or two. The online sources answer within about a minute, and much faster on a second check."
            )
            ctk.CTkLabel(frame, text=text, font=self.font, text_color=MUTED, anchor="w").grid(row=3, column=0, sticky="ew", pady=12)
            return
        self._driver_summary(frame, report)
        self._driver_table(frame, report)
        self._driver_packages(frame, report)

    def _driver_summary(self, frame, report) -> None:
        strip = ctk.CTkFrame(frame, fg_color="transparent")
        strip.grid(row=3, column=0, sticky="ew", pady=(0, 6))
        for index in range(4):
            strip.grid_columnconfigure(index, weight=1, uniform="tiles")
        counts = report.source_counts()
        found = len(report.outdated)
        if report.pending and self.busy and self.job_page == "drivers":
            update_value = f"{found}…" if found else "…"
            update_note = "still asking " + " and ".join(report.pending)
        elif report.checked_online or report.outdated:
            update_value = str(found)
            update_note = "  ·  ".join(f"{count} {source}" for source, count in counts.items()) or "everything is current"
        else:
            update_value, update_note = "?", report.online_note or "not looked up yet"
        third_party = sum(1 for device in report.devices if not device.inbox and device.inf)
        tiles = (
            ("Devices", str(len(report.devices)), f"{third_party} use a maker's driver", TEXT),
            ("Problems", str(len(report.problems)), "devices Windows flags", DANGER if report.problems else OK),
            ("Newer drivers", update_value, update_note, INFO if found else TEXT),
            ("Old packages", format_size(sum(item.size for item in report.packages)), f"{len(report.packages)} unused older copies", WARN if report.packages else TEXT),
        )
        for index, (title, value, note, color) in enumerate(tiles):
            tile = ctk.CTkFrame(strip, fg_color=CARD, corner_radius=14)
            tile.grid(row=0, column=index, sticky="nsew", padx=(0 if index == 0 else 4, 0 if index == 3 else 4))
            ctk.CTkLabel(tile, text=title, font=self.font_s, text_color=MUTED, anchor="w").pack(fill="x", padx=14, pady=(10, 0))
            ctk.CTkLabel(tile, text=value, font=self.font_h, text_color=color, anchor="w").pack(fill="x", padx=14)
            ctk.CTkLabel(tile, text=note, font=self.font_s, text_color=FAINT, anchor="w", wraplength=210, justify="left").pack(fill="x", padx=14, pady=(0, 10))
        gpus = [device for device in report.devices if device.is_gpu]
        if gpus:
            card = ctk.CTkFrame(frame, fg_color=CARD, corner_radius=14)
            card.grid(row=4, column=0, sticky="ew", pady=(0, 6))
            for gpu in gpus:
                self._gpu_row(card, gpu)

    def _gpu_row(self, card, gpu) -> None:
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=8)
        text = ctk.CTkFrame(row, fg_color="transparent")
        text.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(text, text=gpu.device, font=self.font_b, text_color=TEXT, anchor="w").pack(fill="x")
        version = f"{gpu.friendly_version}  ({gpu.version})" if gpu.friendly_version else gpu.version
        line = f"Driver {version}  ·  {_when(gpu.date)}  ·  {_age(gpu.age_days)}"
        color = MUTED
        best = gpu.best
        if best is not None:
            line += f"  ·  {best.source} has {gpu.shown_version(best)} from {_when(best.date)}"
            others = [offer for offer in gpu.offers if offer is not best]
            if others:
                line += " (" + ", ".join(f"{offer.source} {gpu.shown_version(offer)}" for offer in others) + ")"
            color = INFO
        elif gpu.age_days > GPU_OLD_DAYS:
            line += "  ·  Graphics drivers get fixes for new games every few months. Check the maker's site."
            color = WARN
        ctk.CTkLabel(text, text=line, font=self.font_s, text_color=color, anchor="w", justify="left", wraplength=660).pack(fill="x")
        if best is not None and best.source == "NVIDIA":
            self._button(row, f"Download {best.version}", lambda offer=best: open_download(offer)).pack(side="right")
            return
        page = vendor_page(gpu)
        if page is not None:
            name, url = page
            self._button(row, f"Latest from {name}", lambda link=url: webbrowser.open(link), "ghost").pack(side="right")

    def _driver_table(self, frame, report) -> None:
        holder = ctk.CTkFrame(frame, fg_color="transparent")
        holder.grid(row=5, column=0, sticky="nsew")
        holder.grid_columnconfigure(0, weight=1)
        holder.grid_rowconfigure(1, weight=1)
        top = ctk.CTkFrame(holder, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ctk.CTkOptionMenu(
            top,
            values=FILTERS,
            variable=self.driver_filter,
            command=lambda _value: self._fill_driver_table(),
            font=self.font,
            dropdown_font=self.font,
            fg_color=CARD,
            button_color=CARD_2,
            button_hover_color=LINE,
            width=200,
        ).pack(side="left")
        self._driver_count = ctk.CTkLabel(top, text="", font=self.font_s, text_color=MUTED, anchor="w")
        self._driver_count.pack(side="left", padx=12)
        self.driver_table = TreeTable(
            holder,
            [
                ("device", "Device", 250, "w", True),
                ("class", "Type", 150, "w", False),
                ("version", "Installed", 140, "w", False),
                ("date", "Date", 100, "w", False),
                ("newest", "Newest found", 200, "w", False),
                ("status", "Status", 200, "w", True),
            ],
            height=10,
        )
        self.driver_table.tree.configure(selectmode="extended")
        self.driver_table.frame.grid(row=1, column=0, sticky="nsew")
        self._driver_detail = ctk.CTkLabel(
            holder,
            text="Select a device to see what each source has. Select several to install only theirs. Right-click one to search the web for its driver.",
            font=self.font_s,
            text_color=FAINT,
            anchor="w",
            justify="left",
            wraplength=900,
        )
        self._driver_detail.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        self.driver_table.tree.bind("<<TreeviewSelect>>", self._on_driver_select)
        self.driver_table.tree.bind("<Button-3>", self._driver_menu)
        self._driver_menu_widget = tk.Menu(holder, tearoff=0, bg=CARD, fg=TEXT, activebackground=CARD_2, activeforeground=TEXT, bd=0, relief="flat")
        self._fill_driver_table()

    def _fill_driver_table(self) -> None:
        table = getattr(self, "driver_table", None)
        report = self.driver_report
        if table is None or report is None or not table.tree.winfo_exists():
            return
        table.clear()
        self._driver_rows = {}
        mode = self.driver_filter.get()
        shown = 0
        for device in report.devices:
            status, tag = _status(device)
            if mode == "Needs attention" and tag not in {"danger", "info", "warn"} and not device.is_gpu:
                continue
            if mode == "Third-party drivers" and (device.inbox or not device.inf):
                continue
            if mode != "All devices" and device.device_class.lower() in QUIET_CLASSES:
                continue
            version = f"{device.friendly_version} ({device.version})" if device.friendly_version else device.version
            best = device.best
            iid = table.insert(
                (
                    device.device,
                    device.class_name,
                    version or "—",
                    _when(device.date) if device.date else "—",
                    f"{device.shown_version(best)}  ·  {SHORT_SOURCE.get(best.source, best.source)}" if best is not None else "",
                    status,
                ),
                tags=(tag,) if tag else (),
                keys={
                    "device": device.device.lower(),
                    "class": device.class_name.lower(),
                    "date": device.date,
                    "newest": best is None,
                    "status": (tag != "danger", tag != "info", tag != "warn", status),
                },
            )
            self._driver_rows[iid] = device
            shown += 1
        if self._driver_count.winfo_exists():
            hidden = len(report.devices) - shown
            self._driver_count.configure(text=f"{shown} shown" + (f", {hidden} more under All devices" if hidden and mode != "All devices" else ""))
        if not shown:
            table.insert(("Nothing needs attention. Every source agrees these drivers are current.", "", "", "", "", ""), tags=("muted",))

    def _on_driver_select(self, _event=None) -> None:
        devices = [self._driver_rows[iid] for iid in self.driver_table.tree.selection() if iid in getattr(self, "_driver_rows", {})]
        if not devices:
            return
        if len(devices) > 1:
            ready = sum(1 for device in devices if _install_choice(device))
            self._driver_detail.configure(text=f"{len(devices)} devices selected, {ready} with a driver this app can install. Install updates will install only those.")
            return
        device = devices[0]
        parts = [device.problem] if device.problem else []
        parts.append(f"Driver {device.inf or 'none'}" + (f" ({device.original_inf})" if device.original_inf and device.original_inf != device.inf else ""))
        if device.provider:
            parts.append(f"from {device.provider}")
        for offer in sorted(device.offers, key=device.offer_rank, reverse=True):
            size = f", {format_size(offer.size)}" if offer.size else ""
            parts.append(f"{offer.source}: {device.shown_version(offer)} ({_when(offer.date)}{size})")
        if not device.offers and device.age_days > OLD_DAYS and not device.inbox:
            parts.append("No source has anything newer. The maker may have stopped updating it, which is normal for simple devices.")
        parts.append(device.instance_id)
        self._driver_detail.configure(text="   ·   ".join(parts))

    def _driver_menu(self, event) -> None:
        """Right-click a device to search the web for its driver."""
        tree = self.driver_table.tree
        iid = tree.identify_row(event.y)
        device = getattr(self, "_driver_rows", {}).get(iid)
        if device is None:
            return
        if iid not in tree.selection():
            tree.selection_set(iid)
        tree.focus(iid)
        menu = self._driver_menu_widget
        menu.delete(0, "end")
        menu.add_command(label="Search the web for a driver", command=lambda: self.search_driver_web(device))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def search_driver_web(self, device) -> None:
        webbrowser.open(web_search_url(device))
        self.set_status(f"Searched the web for: {web_search_query(device)}")

    def _driver_packages(self, frame, report) -> None:
        lines = []
        if report.packages:
            lines.append(
                f"{len(report.packages)} older driver packages are unused and take {format_size(sum(item.size for item in report.packages))}. A newer copy of each is installed, so Windows will not use them again."
            )
        lines.extend(report.notes)
        if report.online_note and not report.pending:
            lines.append(report.online_note)
        if not lines:
            return
        bar = ctk.CTkFrame(frame, fg_color=CARD, corner_radius=14)
        bar.grid(row=6, column=0, sticky="ew", pady=(8, 0))
        ctk.CTkLabel(bar, text="\n".join(lines), font=self.font_s, text_color=MUTED, anchor="w", justify="left", wraplength=760).pack(side="left", fill="x", expand=True, padx=14, pady=10)
        if report.packages:
            self._button(bar, "Remove old packages", self.start_package_cleanup, "ghost").pack(side="right", padx=12, pady=10)

    # ------------------------------------------------------------ jobs

    def start_driver_scan(self) -> None:
        token = self.begin_job("drivers", "Reading devices and drivers…")
        if token is None:
            return

        def publish(report) -> None:
            # Called from the lookup thread as each source answers.
            snapshot = copy.deepcopy(report)
            self._post(lambda: self._driver_partial(token, snapshot))

        def work(cancel, progress):
            report = scan_drivers(cancel, progress, online=False)
            report.pending = ["Windows Update", "Update Catalog", "NVIDIA"]
            publish(report)
            if cancel.is_set():
                return report
            progress("Asking Windows Update, the Microsoft Update Catalog, and NVIDIA…")
            find_updates(report, cancel, progress, on_partial=publish)
            return report

        def done(result, error) -> None:
            if error is None:
                self.driver_report = result
                counts = result.source_counts()
                log(
                    f"Driver check: {len(result.devices)} devices, {len(result.problems)} problems, "
                    f"{len(result.outdated)} with newer drivers ({', '.join(f'{count} {source}' for source, count in counts.items()) or 'none'}), "
                    f"{len(result.packages)} old packages."
                )
            elif self.current == "drivers":
                self.show_info("Driver check did not finish", str(error))
            if self.current == "drivers":
                self.show_drivers()

        self.run_bg(token, work, done)

    def _driver_partial(self, token: int, report) -> None:
        if token != self.job_token:
            return
        self.driver_report = report
        self.invalidate()
        if self.current == "drivers":
            self.show_drivers()

    def start_driver_install(self) -> None:
        report = self.driver_report
        if report is None or report.pending:
            self.show_info("Check first", "Press Check drivers and wait for every source to answer.")
            return
        rows = getattr(self, "_driver_rows", {})
        table = getattr(self, "driver_table", None)
        selected = [rows[iid] for iid in table.tree.selection() if iid in rows] if table is not None and table.tree.winfo_exists() else []
        pool = selected if any(_install_choice(device) for device in selected) else report.devices
        windows_update, catalog, downloads = [], [], []
        for device in pool:
            best = device.best
            if best is None:
                continue
            if not best.installable:
                downloads.append((device, best))
                continue
            if best.source == "Windows Update":
                if best.update not in windows_update:
                    windows_update.append(best.update)
            elif all(offer.update_id != best.update_id for _device, offer in catalog):
                catalog.append((device, best))
        if pool is report.devices:
            windows_update += [update for update in report.unmatched_updates if update not in windows_update]
        if not windows_update and not catalog:
            extra = ""
            if downloads:
                extra = " " + " ".join(f"{device.device}: download {offer.version} from {offer.source} with the button above." for device, offer in downloads)
            self.show_info("Nothing to install here", "No source has a newer driver this app can install." + extra)
            return
        if not is_admin():
            self.show_info("Administrator needed", "Restart as administrator to install drivers.")
            return
        listed = [f"•  {update.title}  ({format_size(update.size)}, Windows Update)" for update in windows_update]
        listed += [f"•  {device.device}: {offer.version}  ({format_size(offer.size)}, Update Catalog)" for device, offer in catalog]
        note = ""
        if downloads:
            note = "\n\nNot included: " + ", ".join(f"{device.device} ({offer.source} {offer.version}, use its Download button)" for device, offer in downloads) + "."
        more = f"\n…and {len(listed) - 14} more" if len(listed) > 14 else ""
        if not self.confirm(
            "Install driver updates",
            "These drivers will be downloaded and installed:\n\n"
            + "\n".join(listed[:14])
            + more
            + "\n\nA restore point is made first when System Protection is on. Windows only installs a driver it ranks as a better match than the one in use. A restart may be needed afterwards."
            + note,
            "Install",
        ):
            return
        token = self.begin_job("drivers", "Making a restore point…")
        if token is None:
            return

        def work(cancel, progress):
            progress("Making a restore point…")
            lines = [create_restore_point()]
            reboot = False
            if windows_update:
                done_lines, needs = install_updates(windows_update, cancel, progress)
                lines += done_lines
                reboot = reboot or needs
            for device, offer in catalog:
                if cancel.is_set():
                    lines.append("Stopped. The remaining drivers were not installed.")
                    break
                try:
                    message, needs = install_from_catalog(offer, cancel, progress)
                    lines.append(f"{device.device}: {message}")
                    reboot = reboot or needs
                except (OSError, RuntimeError) as exc:
                    lines.append(f"{device.device}: not installed. {exc}")
            return lines, reboot

        def done(result, error) -> None:
            if error is not None:
                self.show_info("Driver install did not finish", str(error))
                return
            lines, reboot = result
            log("Driver install: " + " ".join(lines))
            message = "\n".join(lines)
            if reboot:
                message += "\n\nRestart the PC to finish."
            self.show_info("Driver updates", message)
            if self.current == "drivers":
                self.start_driver_scan()

        self.run_bg(token, work, done)

    def start_package_cleanup(self) -> None:
        report = self.driver_report
        packages = list(report.packages) if report else []
        if not packages:
            return
        if not is_admin():
            self.show_info("Administrator needed", "Restart as administrator to remove driver packages.")
            return
        listed = "\n".join(f"•  {item.original} {item.version} ({_when(item.date)}), {format_size(item.size)}. Newer: {item.newest_version}" for item in packages[:12])
        if not self.confirm(
            "Remove old driver packages",
            f"These older copies are not used by any device:\n\n{listed}\n\nWindows refuses to remove any package that a device still uses.",
            "Remove",
            danger=True,
        ):
            return
        token = self.begin_job("drivers", "Removing old driver packages…")
        if token is None:
            return

        def done(result, error) -> None:
            if error is not None:
                self.show_info("Driver packages", str(error))
                return
            removed, freed, errors = result
            log(f"Removed {removed} old driver packages, {format_size(freed)}.")
            message = f"Removed {removed} package{'s' if removed != 1 else ''} and freed about {format_size(freed)}."
            if errors:
                message += "\n\n" + "\n".join(errors[:6])
            self.show_info("Driver packages", message)
            if self.current == "drivers":
                self.start_driver_scan()

        self.run_bg(token, lambda cancel, progress: remove_packages(packages, cancel, progress), done)
