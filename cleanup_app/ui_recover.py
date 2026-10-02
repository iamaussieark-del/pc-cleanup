"""The Recover page: find deleted files in the Recycle Bin and on NTFS drives, and copy them back."""

from __future__ import annotations

import os
from datetime import datetime
from tkinter import filedialog

import customtkinter as ctk

from cleanup_app.recover import TYPE_PRESETS, _filesystem, _ready_drives, parse_day, restore_files, same_drive, scan_deleted
from cleanup_app.theme import CARD, CARD_2, FAINT, LINE, MUTED, WARN
from cleanup_app.ui_tree import TreeTable
from cleanup_app.util import format_size, is_admin, log

CHANCE_TAG = {"Good": "ok", "In Recycle Bin": "info", "Poor": "warn", "Unknown": "warn", "Encrypted": "muted", "Compressed": "muted", "Name only": "muted"}
CHANCE_ORDER = {"In Recycle Bin": 0, "Good": 1, "Unknown": 2, "Poor": 3, "Compressed": 4, "Encrypted": 5, "Name only": 6}
RESTORABLE = {"In Recycle Bin", "Good", "Unknown", "Poor"}


def _when(stamp: float) -> str:
    try:
        return datetime.fromtimestamp(stamp).strftime("%d %b %Y") if stamp > 0 else "Unknown"
    except (OSError, OverflowError, ValueError):
        return "Unknown"


class RecoverPage:
    """Mixed into CleanupApp. Uses its page, button, dialog, and job helpers."""

    def _recover_vars(self) -> None:
        if getattr(self, "recover_query", None) is not None:
            return
        self.recover_scan = None
        self.recover_query = ctk.StringVar(master=self, value="")
        self.recover_type = ctk.StringVar(master=self, value="All files")
        self.recover_from = ctk.StringVar(master=self, value="")
        self.recover_to = ctk.StringVar(master=self, value="")
        self.recover_folder = ctk.StringVar(master=self, value="")
        self.recover_bin = ctk.BooleanVar(master=self, value=True)
        self.recover_disk = ctk.BooleanVar(master=self, value=True)
        self.recover_drives: dict[str, ctk.BooleanVar] = {}
        for letter in _ready_drives():
            self.recover_drives[letter] = ctk.BooleanVar(master=self, value=True)

    def show_recover(self) -> None:
        self._recover_vars()
        frame = self._page()
        frame.grid_rowconfigure(4, weight=1)
        self._heading(
            frame,
            "Recover deleted files",
            "Finds files you deleted: in the Recycle Bin, and on NTFS drives after the bin was emptied (that part needs administrator). Nothing changes until you restore. For the best chance, stop saving to that drive now, and restore to a different drive such as a USB stick.",
        )
        form = ctk.CTkFrame(frame, fg_color=CARD, corner_radius=14)
        form.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        top = ctk.CTkFrame(form, fg_color="transparent")
        top.pack(fill="x", padx=12, pady=(12, 6))
        ctk.CTkCheckBox(top, text="Recycle Bin", variable=self.recover_bin, font=self.font_s).pack(side="left")
        ctk.CTkCheckBox(top, text="Deleted from drives", variable=self.recover_disk, font=self.font_s).pack(side="left", padx=(12, 18))
        ctk.CTkLabel(top, text="Drives", font=self.font_s, text_color=MUTED).pack(side="left", padx=(0, 6))
        for letter, var in self.recover_drives.items():
            ctk.CTkCheckBox(top, text=f"{letter}:", variable=var, font=self.font_s, width=60).pack(side="left")
        if not is_admin():
            ctk.CTkLabel(top, text="Drives need administrator", font=self.font_s, text_color=WARN).pack(side="right")
        middle = ctk.CTkFrame(form, fg_color="transparent")
        middle.pack(fill="x", padx=12, pady=6)
        # CustomTkinter hides placeholder text on entries bound to a variable, so these get labels.
        ctk.CTkLabel(middle, text="Name has", font=self.font_s, text_color=MUTED).pack(side="left", padx=(0, 4))
        ctk.CTkEntry(middle, textvariable=self.recover_query, font=self.font, width=180).pack(side="left")
        ctk.CTkOptionMenu(
            middle,
            values=list(TYPE_PRESETS),
            variable=self.recover_type,
            font=self.font,
            dropdown_font=self.font,
            fg_color=CARD_2,
            button_color=CARD_2,
            button_hover_color=LINE,
            width=140,
        ).pack(side="left", padx=8)
        ctk.CTkLabel(middle, text="Deleted from", font=self.font_s, text_color=MUTED).pack(side="left", padx=(8, 4))
        ctk.CTkEntry(middle, textvariable=self.recover_from, font=self.font, width=110).pack(side="left")
        ctk.CTkLabel(middle, text="to", font=self.font_s, text_color=MUTED).pack(side="left", padx=4)
        ctk.CTkEntry(middle, textvariable=self.recover_to, font=self.font, width=110).pack(side="left")
        ctk.CTkLabel(middle, text="YYYY-MM-DD, or blank", font=self.font_s, text_color=FAINT).pack(side="left", padx=(6, 0))
        self._button(middle, "Scan", self.start_recover_scan).pack(side="right")
        bottom = ctk.CTkFrame(form, fg_color="transparent")
        bottom.pack(fill="x", padx=12, pady=(6, 12))
        ctk.CTkLabel(bottom, text="Only from folder", font=self.font_s, text_color=MUTED).pack(side="left", padx=(0, 6))
        ctk.CTkEntry(bottom, textvariable=self.recover_folder, font=self.font).pack(side="left", fill="x", expand=True)
        self._button(bottom, "Browse", self._pick_recover_folder, "ghost").pack(side="left", padx=(8, 0))

        scan = self.recover_scan
        summary = ctk.CTkLabel(frame, text="", font=self.font_s, text_color=MUTED, anchor="w", justify="left", wraplength=900)
        summary.grid(row=3, column=0, sticky="ew", pady=(0, 6))
        if scan is None:
            busy = self.busy and self.job_page == "recover"
            summary.configure(text="Scanning…" if busy else "Choose where to look, then press Scan. The Recycle Bin is quick. A whole drive takes a minute or two.")
            return
        restorable = sum(1 for item in scan.files if item.chance in RESTORABLE)
        found = f"{len(scan.files):,} deleted file{'s' if len(scan.files) != 1 else ''} found"
        lines = [f"{found}, {restorable:,} with contents that can be copied back. Newest first." if scan.files else "No deleted files matched."]
        lines += scan.notes
        summary.configure(text="  ".join(lines))

        self.recover_table = TreeTable(
            frame,
            [
                ("name", "Name", 210, "w", True),
                ("where", "Was in", 300, "w", True),
                ("size", "Size", 90, "e", False),
                ("when", "Deleted", 110, "w", False),
                ("chance", "Chance", 120, "w", False),
                ("source", "Found in", 130, "w", False),
            ],
            height=12,
        )
        self.recover_table.tree.configure(selectmode="extended")
        self.recover_table.frame.grid(row=4, column=0, sticky="nsew")
        self._recover_rows = {}
        for item in scan.files:
            iid = self.recover_table.insert(
                (item.name, os.path.dirname(item.original_path), format_size(item.size), _when(item.when), item.chance, "Recycle Bin" if item.source == "recycle" else f"{item.drive}: drive"),
                tags=(CHANCE_TAG.get(item.chance, ""),),
                keys={"name": item.name.lower(), "where": item.original_path.lower(), "size": item.size, "when": item.when, "chance": CHANCE_ORDER.get(item.chance, 9), "source": item.source},
            )
            self._recover_rows[iid] = item
        bar = ctk.CTkFrame(frame, fg_color="transparent")
        bar.grid(row=5, column=0, sticky="ew", pady=(8, 0))
        bar.grid_columnconfigure(0, weight=1)
        self._recover_detail = ctk.CTkLabel(bar, text="Select files to restore. Ctrl-click or Shift-click selects several.", font=self.font_s, text_color=FAINT, anchor="w", justify="left", wraplength=640)
        self._recover_detail.grid(row=0, column=0, sticky="ew")
        self._button(bar, "Put back", lambda: self.start_restore(put_back=True), "ghost").grid(row=0, column=1, padx=(8, 0))
        self._button(bar, "Restore to folder…", lambda: self.start_restore(put_back=False)).grid(row=0, column=2, padx=(8, 0))
        self.recover_table.tree.bind("<<TreeviewSelect>>", self._on_recover_select)

    def _pick_recover_folder(self) -> None:
        path = filedialog.askdirectory(title="Only files that were in this folder")
        if path:
            self.recover_folder.set(os.path.normpath(path))

    def _selected_recover(self) -> list:
        table = getattr(self, "recover_table", None)
        if table is None or not table.tree.winfo_exists():
            return []
        rows = getattr(self, "_recover_rows", {})
        return [rows[iid] for iid in table.tree.selection() if iid in rows]

    def _on_recover_select(self, _event=None) -> None:
        items = self._selected_recover()
        if not items:
            return
        if len(items) > 1:
            total = sum(item.size for item in items)
            self._recover_detail.configure(text=f"{len(items)} files selected, {format_size(total)}.")
            return
        item = items[0]
        self._recover_detail.configure(text=f"{item.original_path}   ·   {item.note}")

    # ------------------------------------------------------------ jobs

    def start_recover_scan(self) -> None:
        self._recover_vars()
        try:
            start = parse_day(self.recover_from.get())
            end = parse_day(self.recover_to.get(), end=True)
        except ValueError as exc:
            self.show_info("Dates", str(exc))
            return
        drives = [letter for letter, var in self.recover_drives.items() if var.get()]
        include_bin = bool(self.recover_bin.get())
        include_disk = bool(self.recover_disk.get())
        if not include_bin and not include_disk:
            self.show_info("Choose where to look", "Tick the Recycle Bin, deleted files on drives, or both.")
            return
        if include_disk and not drives:
            self.show_info("Choose a drive", "Tick at least one drive to search.")
            return
        not_ntfs = [letter for letter in drives if include_disk and is_admin() and _filesystem(letter) not in {"NTFS", ""}]
        folder = self.recover_folder.get().strip()
        query = self.recover_query.get().strip()
        extensions = TYPE_PRESETS.get(self.recover_type.get(), "")
        token = self.begin_job("recover", "Looking for deleted files…")
        if token is None:
            return

        def work(cancel, progress):
            return scan_deleted(drives, folder, query, extensions, start, end, include_bin, include_disk, cancel, progress)

        def done(result, error) -> None:
            if error is None:
                if not_ntfs:
                    result.notes.append(f"{', '.join(letter + ':' for letter in not_ntfs)} not NTFS, so only the Recycle Bin was searched there.")
                self.recover_scan = result
                log(f"Recover scan: {len(result.files)} deleted files found. Nothing was changed.")
            elif self.current == "recover":
                self.show_info("Scan did not finish", str(error))
            if self.current == "recover":
                self.show_recover()

        self.run_bg(token, work, done)

    def start_restore(self, put_back: bool) -> None:
        items = self._selected_recover()
        if not items:
            self.show_info("Choose files", "Select the files to restore in the list first.")
            return
        usable = [item for item in items if item.chance in RESTORABLE]
        if not usable:
            self.show_info("Nothing to restore", "The selected files have no contents left to copy back. " + items[0].note)
            return
        if put_back:
            usable = [item for item in usable if item.source == "recycle"]
            if not usable:
                self.show_info("Put back", "Put back only works for files still in the Recycle Bin. Use Restore to folder for the others.")
                return
            destination = ""
            message = f"Put {len(usable)} file{'s' if len(usable) != 1 else ''} back where {'they were' if len(usable) != 1 else 'it was'}?"
        else:
            destination = filedialog.askdirectory(title="Restore to this folder")
            if not destination:
                return
            destination = os.path.normpath(destination)
            clash = sorted({item.drive for item in usable if item.source == "disk" and same_drive(destination, item.drive)})
            if clash:
                self.show_info(
                    "Use a different drive",
                    f"These files were deleted from {', '.join(letter + ':' for letter in clash)}. Saving them to the same drive can overwrite what is left of them before it is copied. Choose a folder on another drive, such as a USB stick.",
                )
                return
            poor = sum(1 for item in usable if item.chance in {"Poor", "Unknown"})
            warning = f" {poor} of them may come back incomplete, because some of their old disk space was reused." if poor else ""
            message = f"Copy {len(usable)} file{'s' if len(usable) != 1 else ''} ({format_size(sum(item.size for item in usable))}) to {destination}? The folder layout they had is kept.{warning}"
        if not self.confirm("Restore", message, "Restore"):
            return
        chosen = {id(item) for item in usable}
        for item in self.recover_scan.files:
            item.checked = id(item) in chosen
        token = self.begin_job("recover", "Restoring files…")
        if token is None:
            return
        files = list(self.recover_scan.files)

        def work(cancel, progress):
            return restore_files(files, destination, put_back, cancel, progress)

        def done(result, error) -> None:
            if error is not None:
                self.show_info("Restore did not finish", str(error))
                return
            restored, skipped, errors = result
            log(f"Restored {restored} deleted files" + (f" to {destination}" if destination else " to where they were") + f". Skipped {skipped}.")
            if put_back:
                # Drop only the files that really left the Recycle Bin.
                self.recover_scan.files = [item for item in self.recover_scan.files if not (id(item) in chosen and not os.path.exists(item.recycle_data))]
            text = f"Restored {restored} file{'s' if restored != 1 else ''}."
            if errors:
                text += "\n\n" + "\n".join(errors[:8])
            if destination and restored:
                if self.ask("Files restored", text, [("Close", False, "ghost"), ("Open folder", True, "primary")]):
                    os.startfile(destination)
            else:
                self.show_info("Files restored", text)
            if self.current == "recover":
                self.show_recover()

        self.run_bg(token, work, done)
