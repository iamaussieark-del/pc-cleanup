"""The Performance page: a quick health check, a few reversible speed-ups, and the heaviest apps."""

from __future__ import annotations

import tkinter as tk

import customtkinter as ctk

from cleanup_app.performance import (
    boost,
    check_performance,
    end_process_group,
    has_backup,
    live_usage,
    optimize_drives,
    undo_all,
    undo_tweak,
    apply_tweak,
)
from cleanup_app.space import fixed_drive_roots
from cleanup_app.theme import ACCENT, CARD, DANGER, FAINT, INFO, LINE, MUTED, OK, TEXT, WARN
from cleanup_app.ui_tree import TreeTable
from cleanup_app.util import format_size, is_admin, log

LIVE_MS = 1500


class PerformancePage:
    """Mixed into CleanupApp. Uses its page, button, dialog, and job helpers."""

    def show_performance(self) -> None:
        frame = self._page()
        frame.grid_rowconfigure(3, weight=1)
        self._heading(
            frame,
            "Performance",
            "Finds what is slowing this PC down, then offers a few safe speed-ups. Every change is an ordinary Windows setting and Undo boost puts it back. There is no memory cleaner here: emptying memory only makes Windows load it all again.",
        )
        tools = ctk.CTkFrame(frame, fg_color="transparent")
        tools.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        report = self.perf_report
        can_boost = report is not None and any(tweak.in_boost and tweak.available for tweak in report.tweaks)
        self._button(tools, "Check now", self.start_perf_check, "ghost" if can_boost else "primary").pack(side="left")
        self._button(tools, "Boost", self.start_boost, "primary" if can_boost else "ghost").pack(side="left", padx=8)
        if has_backup():
            self._button(tools, "Undo boost", self.start_undo_boost, "ghost").pack(side="left")
        self._button(tools, "Optimize drives", self.start_optimize_drives, "ghost").pack(side="left", padx=8)
        body = ctk.CTkScrollableFrame(frame, fg_color="transparent")
        body.grid(row=3, column=0, sticky="nsew")
        self._live_card(body)
        if report is None:
            text = (
                "Checking this PC…"
                if self.busy and self.job_page == "performance"
                else "Press Check now. It takes a couple of seconds and changes nothing."
            )
            ctk.CTkLabel(body, text=text, font=self.font, text_color=MUTED, anchor="w").pack(fill="x", pady=12)
            return
        self._perf_findings(body, report)
        self._perf_tweaks(body, report)
        self._perf_processes(body, report)

    # ------------------------------------------------------------ live meters

    def _live_card(self, body) -> None:
        card = ctk.CTkFrame(body, fg_color=CARD, corner_radius=14)
        card.pack(fill="x", pady=6)
        card.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(card, text="Right now", font=self.font_h, text_color=TEXT, anchor="w").grid(row=0, column=0, columnspan=3, sticky="ew", padx=16, pady=(14, 6))
        self._live = {}
        for row, (key, title) in enumerate((("cpu", "Processor"), ("ram", "Memory")), start=1):
            ctk.CTkLabel(card, text=title, font=self.font_s, text_color=MUTED, anchor="w", width=90).grid(row=row, column=0, sticky="w", padx=(16, 8), pady=4)
            bar = ctk.CTkProgressBar(card, height=10, progress_color=ACCENT, fg_color=LINE)
            bar.grid(row=row, column=1, sticky="ew", pady=4)
            bar.set(0)
            label = ctk.CTkLabel(card, text="…", font=self.font_s, text_color=TEXT, anchor="e", width=190)
            label.grid(row=row, column=2, sticky="e", padx=(8, 16), pady=4)
            self._live[key] = (bar, label)
        report = self.perf_report
        if report is not None:
            days = report.uptime_hours / 24
            since = f"{days:.0f} days" if days >= 2 else f"{report.uptime_hours:.0f} hours"
            facts = f"On for {since}   ·   Power plan: {report.power_plan}   ·   {report.cpu_threads} processor threads   ·   {format_size(report.ram_total)} memory"
            ctk.CTkLabel(card, text=facts, font=self.font_s, text_color=FAINT, anchor="w").grid(row=3, column=0, columnspan=3, sticky="ew", padx=16, pady=(4, 0))
        ctk.CTkFrame(card, fg_color="transparent", height=10).grid(row=4, column=0)

    def _start_live_meters(self) -> None:
        if getattr(self, "_live_after", None) is not None:
            return
        live_usage()  # The first processor reading only sets the starting point.
        self._live_after = self.after(400, self._tick_live)

    def _tick_live(self) -> None:
        self._live_after = None
        meters = getattr(self, "_live", {})
        if self.current != "performance" or not meters:
            return
        try:
            cpu, ram, used, total = live_usage()
            bar, label = meters["cpu"]
            if not bar.winfo_exists():
                return
            bar.set(cpu / 100)
            bar.configure(progress_color=DANGER if cpu >= 90 else WARN if cpu >= 70 else ACCENT)
            label.configure(text=f"{cpu:.0f}%")
            bar, label = meters["ram"]
            bar.set(ram / 100)
            bar.configure(progress_color=DANGER if ram >= 90 else WARN if ram >= 80 else ACCENT)
            label.configure(text=f"{format_size(used)} of {format_size(total)}  ({ram:.0f}%)")
        except (tk.TclError, KeyError):
            return
        self._live_after = self.after(LIVE_MS, self._tick_live)

    # ------------------------------------------------------------ check results

    def _perf_findings(self, body, report) -> None:
        card = ctk.CTkFrame(body, fg_color=CARD, corner_radius=14)
        card.pack(fill="x", pady=6)
        head = ctk.CTkFrame(card, fg_color="transparent")
        head.pack(fill="x", padx=16, pady=(14, 4))
        if report.score >= 85:
            verdict, color = "Good", OK
        elif report.score >= 65:
            verdict, color = "Could be quicker", WARN
        else:
            verdict, color = "Needs attention", DANGER
        ctk.CTkLabel(head, text=f"Health {report.score} / 100", font=self.font_h, text_color=TEXT, anchor="w").pack(side="left")
        ctk.CTkLabel(head, text=f"   {verdict}", font=self.font_b, text_color=color, anchor="w").pack(side="left")
        for finding in report.findings:
            row = ctk.CTkFrame(card, fg_color="transparent")
            row.pack(fill="x", padx=16, pady=(6, 2))
            text = ctk.CTkFrame(row, fg_color="transparent")
            text.pack(side="left", fill="x", expand=True)
            ctk.CTkLabel(text, text=finding.title, font=self.font_b, text_color=WARN if finding.level == "warn" else INFO, anchor="w").pack(fill="x")
            ctk.CTkLabel(text, text=finding.detail, font=self.font_s, text_color=MUTED, anchor="w", justify="left", wraplength=700).pack(fill="x")
            if finding.page and finding.page != "performance":
                self._button(row, "Open", lambda page=finding.page: self.select_nav(page), "ghost", width=72).pack(side="right", padx=(8, 0))
        ctk.CTkFrame(card, fg_color="transparent", height=10).pack()

    def _perf_tweaks(self, body, report) -> None:
        card = ctk.CTkFrame(body, fg_color=CARD, corner_radius=14)
        card.pack(fill="x", pady=6)
        ctk.CTkLabel(card, text="Speed-ups", font=self.font_h, text_color=TEXT, anchor="w").pack(fill="x", padx=16, pady=(14, 2))
        ctk.CTkLabel(
            card,
            text="Boost changes the ones marked Boost. Each can also be changed on its own, and put back.",
            font=self.font_s,
            text_color=MUTED,
            anchor="w",
        ).pack(fill="x", padx=16, pady=(0, 6))
        for tweak in report.tweaks:
            row = ctk.CTkFrame(card, fg_color="transparent")
            row.pack(fill="x", padx=16, pady=6)
            text = ctk.CTkFrame(row, fg_color="transparent")
            text.pack(side="left", fill="x", expand=True)
            title = ctk.CTkFrame(text, fg_color="transparent")
            title.pack(fill="x")
            ctk.CTkLabel(title, text=tweak.name, font=self.font_b, text_color=TEXT, anchor="w").pack(side="left")
            ctk.CTkLabel(title, text=f"   {tweak.state}", font=self.font_s, text_color=OK if not tweak.available else MUTED, anchor="w").pack(side="left")
            if tweak.recommended:
                ctk.CTkLabel(title, text="   Recommended", font=self.font_s, text_color=WARN, anchor="w").pack(side="left")
            elif tweak.in_boost:
                ctk.CTkLabel(title, text="   Boost", font=self.font_s, text_color=INFO, anchor="w").pack(side="left")
            ctk.CTkLabel(text, text=tweak.detail, font=self.font_s, text_color=MUTED, anchor="w", justify="left", wraplength=640).pack(fill="x")
            if tweak.can_undo:
                self._button(row, "Put back", lambda name=tweak.id: self.start_tweak(name, undo=True), "ghost", width=92).pack(side="right", padx=(8, 0))
            if tweak.available:
                self._button(row, tweak.action, lambda name=tweak.id: self.start_tweak(name), "ghost").pack(side="right")
        ctk.CTkFrame(card, fg_color="transparent", height=8).pack()

    def _perf_processes(self, body, report) -> None:
        card = ctk.CTkFrame(body, fg_color=CARD, corner_radius=14)
        card.pack(fill="x", pady=6)
        ctk.CTkLabel(card, text="Apps using the most", font=self.font_h, text_color=TEXT, anchor="w").pack(fill="x", padx=16, pady=(14, 2))
        ctk.CTkLabel(
            card,
            text="Grouped by program, measured over one second. Memory is what Task Manager shows. End task closes every window of that app without saving. Parts of Windows are greyed out and cannot be ended here.",
            font=self.font_s,
            text_color=MUTED,
            anchor="w",
            justify="left",
            wraplength=820,
        ).pack(fill="x", padx=16, pady=(0, 6))
        table = TreeTable(
            card,
            [
                ("name", "App", 300, "w", True),
                ("count", "Processes", 90, "e", False),
                ("memory", "Memory", 110, "e", False),
                ("cpu", "Processor", 100, "e", False),
            ],
            height=12,
        )
        table.frame.configure(fg_color=CARD)
        table.frame.pack(fill="x", padx=6)
        groups = {}
        for group in report.processes[:60]:
            iid = table.insert(
                (group.name, group.count, format_size(group.memory), f"{group.cpu:.1f}%"),
                tags=() if group.can_end else ("muted",),
                keys={"name": group.name.lower(), "count": group.count, "memory": group.memory, "cpu": group.cpu},
            )
            groups[iid] = group
        bar = ctk.CTkFrame(card, fg_color="transparent")
        bar.pack(fill="x", padx=16, pady=(6, 14))
        note = ctk.CTkLabel(bar, text="Select an app to end it.", font=self.font_s, text_color=FAINT, anchor="w")
        note.pack(side="left")

        def chosen():
            return groups.get(table.selected())

        def on_select(_event=None) -> None:
            group = chosen()
            if group is not None:
                note.configure(text=group.note or f"{group.name}: {group.count} process{'es' if group.count != 1 else ''}, {format_size(group.memory)}.")

        table.tree.bind("<<TreeviewSelect>>", on_select)
        self._button(bar, "End task", lambda: self.start_end_task(chosen()), "danger").pack(side="right")

    # ------------------------------------------------------------ jobs

    def _perf_job(self, label: str, action, done_title: str | None = None) -> None:
        """Run action, then check again so the page shows the new state."""
        token = self.begin_job("performance", label)
        if token is None:
            return
        startup = self.startup_items

        def work(cancel, progress):
            lines = action(cancel, progress) if action else []
            report = check_performance(cancel, progress, startup)
            return lines, report

        def done(result, error) -> None:
            if error is not None:
                self.show_info("Performance", str(error))
                if self.current == "performance":
                    self.show_performance()
                return
            lines, report = result
            self.perf_report = report
            if self.current == "performance":
                self.show_performance()
            if done_title and lines:
                log(f"{done_title}: " + " ".join(lines))
                self.show_info(done_title, "\n".join(lines))

        self.run_bg(token, work, done)

    def start_perf_check(self) -> None:
        self._perf_job("Checking performance…", None)

    def start_tweak(self, tweak_id: str, undo: bool = False) -> None:
        def action(_cancel, progress):
            progress("Putting the setting back…" if undo else "Changing the setting…")
            return [undo_tweak(tweak_id) if undo else apply_tweak(tweak_id)]

        self._perf_job("Updating a setting…", action, "Setting updated")

    def start_boost(self) -> None:
        report = self.perf_report
        if report is None:
            self.show_info("Check first", "Press Check now first, so Boost knows what is already set.")
            return
        chosen = [tweak for tweak in report.tweaks if tweak.in_boost and tweak.available]
        if not chosen:
            others = [finding.title for finding in report.findings if finding.level == "warn"]
            extra = f" What would help more: {', '.join(others)}." if others else ""
            self.show_info("Already boosted", f"The speed-up settings are already where they should be on this PC.{extra}")
            return
        listed = "\n".join(f"•  {tweak.name}: {tweak.state} → {tweak.action.lower()}" for tweak in chosen)
        if not self.confirm("Boost", f"These settings will change:\n\n{listed}\n\nUndo boost puts every one back.", "Boost now"):
            return
        ids = [tweak.id for tweak in chosen]
        self._perf_job("Boosting…", lambda _cancel, progress: boost(ids, progress), "Boost finished")

    def start_undo_boost(self) -> None:
        if not self.confirm("Undo boost", "Put every setting Boost changed back the way it was?", "Put back"):
            return
        self._perf_job("Putting settings back…", lambda _cancel, progress: undo_all(progress), "Settings put back")

    def start_end_task(self, group) -> None:
        if group is None:
            self.show_info("End task", "Select an app in the list first.")
            return
        if not group.can_end:
            self.show_info("End task", group.note or f"{group.name} is part of Windows, so it is left running.")
            return
        plural = f"all {group.count} of its processes" if group.count > 1 else "it"
        if not self.confirm("End task", f"Close {group.name} and {plural}? Anything not saved in it will be lost.", "End task", danger=True):
            return

        def action(_cancel, progress):
            progress(f"Closing {group.name}…")
            ended, errors = end_process_group(group)
            return [f"Closed {ended} process{'es' if ended != 1 else ''} of {group.name}."] + errors[:6]

        self._perf_job(f"Closing {group.name}…", action, "End task")

    def start_optimize_drives(self) -> None:
        if not is_admin():
            self.show_info("Administrator needed", "Restart as administrator to optimize drives.")
            return
        letters = [root[0] for root in fixed_drive_roots()]
        media = {disk.letter: disk.media for disk in getattr(self.dashboard_data, "disks", []) or []}
        listed = ", ".join(f"{letter}: ({media.get(letter, 'drive')})" for letter in letters)
        slow = any(media.get(letter) == "HDD" for letter in letters)
        warning = " Hard drives are defragmented, which can take an hour or more. Stop is safe at any time." if slow else " On an SSD this sends TRIM and takes a minute or two."
        if not self.confirm("Optimize drives", f"Run Windows' Optimize Drives on {listed}?{warning}", "Optimize"):
            return
        self._perf_job("Optimizing drives…", lambda cancel, progress: optimize_drives(letters, cancel, progress), "Optimize drives")
