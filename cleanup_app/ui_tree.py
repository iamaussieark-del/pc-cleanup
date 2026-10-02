"""Native list and tree views for big data, styled to match the dark theme.

Every CustomTkinter widget draws itself on its own canvas, which costs a few
milliseconds each, so a list of a few hundred rows takes seconds to build.
A ttk.Treeview draws thousands of rows in one widget. The folder breakdown
also only reads a folder's children when that folder is opened.
"""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import ttk

import customtkinter as ctk

from cleanup_app.theme import ACCENT, BG, CARD, CARD_2, DANGER, FAINT, INFO, LINE, MUTED, OK, SIDEBAR, TEXT, WARN
from cleanup_app.util import format_size, reveal_in_explorer

STYLE = "Cleanup.Treeview"
SYNTHETIC = {"Files here", "Files in the drive root", "Smaller folders"}
TAG_COLORS = {"muted": FAINT, "warn": WARN, "danger": DANGER, "ok": OK, "info": INFO, "accent": ACCENT}
BAR_STEPS = 40


def scale_of(widget) -> float:
    try:
        return float(ctk.ScalingTracker.get_widget_scaling(widget))
    except Exception:
        return 1.0


def font(widget, size: int, weight: str = "normal") -> tuple:
    """A font the same size as CustomTkinter's. Tk does not scale point sizes for high-DPI
    displays here, so this uses pixels, scaled the way CustomTkinter scales its own."""
    return ("Segoe UI", -round(size * scale_of(widget)), weight)


def ensure_style(widget) -> None:
    """Set up the dark Treeview style once per Tk interpreter."""
    root = widget._root()
    if getattr(root, "_cleanup_tree_style", False):
        return
    scale = scale_of(widget)
    style = ttk.Style(root)
    if style.theme_use() != "clam":
        style.theme_use("clam")
    style.configure(
        STYLE,
        background=CARD,
        fieldbackground=CARD,
        foreground=TEXT,
        bordercolor=CARD,
        lightcolor=CARD,
        darkcolor=CARD,
        borderwidth=0,
        rowheight=int(27 * scale),
        font=font(widget, 13),
        indent=int(20 * scale),
    )
    style.map(STYLE, background=[("selected", CARD_2)], foreground=[("selected", TEXT)])
    style.configure(
        f"{STYLE}.Heading",
        background=SIDEBAR,
        foreground=MUTED,
        bordercolor=CARD,
        lightcolor=SIDEBAR,
        darkcolor=SIDEBAR,
        relief="flat",
        font=font(widget, 12, "bold"),
        padding=(int(8 * scale), int(6 * scale)),
    )
    style.map(f"{STYLE}.Heading", background=[("active", CARD_2)], foreground=[("active", TEXT)])
    style.configure(f"{STYLE}.Item", foreground=MUTED, indicatorsize=int(12 * scale))
    style.layout(STYLE, [("Treeview.treearea", {"sticky": "nswe"})])
    root._cleanup_tree_style = True


class TreeTable:
    """A styled Treeview in a rounded card, with a scrollbar and click-to-sort headings.

    columns: (id, heading, width, anchor, stretch). Each row can carry sort keys,
    so sizes sort as numbers rather than as text.
    """

    def __init__(self, parent, columns, *, tree_heading: str | None = None, tree_width: int = 320, height: int = 12, sortable: bool = True):
        ensure_style(parent)
        scale = scale_of(parent)
        self.frame = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=14)
        self.frame.grid_rowconfigure(0, weight=1)
        self.frame.grid_columnconfigure(0, weight=1)
        self.columns = [column[0] for column in columns]
        self.headings = {column[0]: column[1] for column in columns}
        show = "tree headings" if tree_heading is not None else "headings"
        self.tree = ttk.Treeview(self.frame, columns=self.columns, show=show, style=STYLE, height=height, selectmode="browse")
        if tree_heading is not None:
            self.headings["#0"] = tree_heading
            self.tree.heading("#0", text=tree_heading, anchor="w", command=(lambda: self.sort_by("#0")) if sortable else "")
            self.tree.column("#0", width=int(tree_width * scale), minwidth=int(160 * scale), stretch=True, anchor="w")
        for column_id, heading, width, anchor, stretch in columns:
            self.tree.heading(column_id, text=heading, anchor=anchor, command=(lambda name=column_id: self.sort_by(name)) if sortable else "")
            self.tree.column(column_id, width=int(width * scale), minwidth=int(48 * scale), anchor=anchor, stretch=stretch)
        for tag, color in TAG_COLORS.items():
            self.tree.tag_configure(tag, foreground=color)
        self.tree.tag_configure("bold", font=font(parent, 13, "bold"))
        scrollbar = ctk.CTkScrollbar(self.frame, command=self.tree.yview, fg_color=CARD, button_color=LINE, button_hover_color=CARD_2)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.grid(row=0, column=0, sticky="nsew", padx=(10, 0), pady=10)
        scrollbar.grid(row=0, column=1, sticky="ns", padx=(2, 6), pady=10)
        self.tree.bind("<MouseWheel>", self._wheel)
        self.keys: dict[str, dict] = {}
        self.sorted_by: tuple[str, bool] | None = None
        self._counter = 0

    def _wheel(self, event):
        # Inside a scrolling page, scroll only this list while it has more to show.
        first, last = self.tree.yview()
        if first <= 0.0 and last >= 1.0:
            return None
        self.tree.yview_scroll(-1 if event.delta > 0 else 1, "units")
        return "break"

    def clear(self) -> None:
        self.tree.delete(*self.tree.get_children(""))
        self.keys.clear()

    def insert(self, values, *, parent: str = "", text: str = "", tags=(), keys: dict | None = None, image=None, open: bool = False) -> str:
        self._counter += 1
        iid = f"r{self._counter}"
        options = {"text": text, "values": values, "tags": tags, "open": open}
        if image is not None:
            options["image"] = image
        self.tree.insert(parent, "end", iid=iid, **options)
        if keys:
            self.keys[iid] = keys
        return iid

    def selected(self) -> str:
        selection = self.tree.selection()
        return selection[0] if selection else ""

    def sort_by(self, column: str, descending: bool | None = None) -> None:
        if descending is None:
            descending = not (self.sorted_by == (column, True)) if self.sorted_by and self.sorted_by[0] == column else column != "#0"
        self.sorted_by = (column, descending)
        self._sort_level("", column, descending)
        for name, heading in self.headings.items():
            arrow = (" ▼" if descending else " ▲") if name == column else ""
            self.tree.heading(name, text=heading + arrow)

    def _sort_level(self, parent: str, column: str, descending: bool) -> None:
        children = list(self.tree.get_children(parent))

        def key(iid: str):
            row = self.keys.get(iid, {})
            if column in row:
                value = row[column]
            elif column == "#0":
                value = self.tree.item(iid, "text").lower()
            else:
                value = str(self.tree.set(iid, column)).lower()
            # Rows pinned with "last" stay at the bottom either way.
            return (row.get("last", False) != descending, value)

        children.sort(key=key, reverse=descending)
        for index, iid in enumerate(children):
            self.tree.move(iid, parent, index)
            if self.tree.item(iid, "open"):
                self._sort_level(iid, column, descending)


def share_text(share: float) -> str:
    if share >= 0.995:
        return "100%"
    if share >= 0.1:
        return f"{share:.0%}"
    if share >= 0.001:
        return f"{share:.1%}"
    return "<0.1%" if share > 0 else "0%"


class FolderBreakdown:
    """The folder breakdown window: drives, then folders, opened in place."""

    def __init__(self, app):
        self.app = app
        self.report = None
        self.window = None
        self.table: TreeTable | None = None
        self.nodes: dict[str, object] = {}
        self.iid_of: dict[int, str] = {}
        self.drive_size: dict[str, int] = {}
        self.pending: set[str] = set()
        self.bars: dict[tuple[int, bool], tk.PhotoImage] = {}

    # ------------------------------------------------------------ window

    def exists(self) -> bool:
        try:
            return self.window is not None and bool(self.window.winfo_exists())
        except tk.TclError:
            return False

    def close(self) -> None:
        window = self.window
        self.window = None
        self.table = None
        if window is not None:
            try:
                if window.winfo_exists():
                    window.destroy()
            except tk.TclError:
                pass

    def show(self, report, path: list | None = None) -> None:
        if report is not self.report or not self.exists():
            self.close()
            self.report = report
            self._build()
        self._reveal(path or [])
        try:
            self.window.deiconify()
            self.window.lift()
            self.window.focus_force()
            self.table.tree.focus_set()
        except tk.TclError:
            pass

    def _build(self) -> None:
        app = self.app
        window = ctk.CTkToplevel(app)
        self.window = window
        window.title("Folder breakdown")
        window.configure(fg_color=BG)
        window.minsize(760, 520)
        window.transient(app)
        window.protocol("WM_DELETE_WINDOW", self.close)
        window.bind("<Escape>", lambda _event: self.close())
        app._place_window(window, 1040, 740)
        app._style_title_soon(window)

        # The frame fills the whole window and pads inside, so no bare window background shows.
        wrap = ctk.CTkFrame(window, fg_color=BG, corner_radius=0)
        wrap.pack(fill="both", expand=True)
        wrap.grid_columnconfigure(0, weight=1)
        wrap.grid_rowconfigure(2, weight=1)
        ctk.CTkLabel(wrap, text="Folder breakdown", font=app.font_h, text_color=TEXT, anchor="w").grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 0))
        ctk.CTkLabel(
            wrap,
            text="Click the arrow, double-click, or press the right arrow key to open a folder. Sizes include everything inside. Folders under 1 MB are grouped as Smaller folders. Right-click for more.",
            font=app.font_s,
            text_color=MUTED,
            anchor="w",
            justify="left",
            wraplength=960,
        ).grid(row=1, column=0, sticky="ew", padx=18, pady=(4, 10))

        self.table = TreeTable(
            wrap,
            [
                ("size", "Size", 110, "e", False),
                ("share", "Of folder", 90, "e", False),
                ("drive", "Of drive", 90, "e", False),
            ],
            tree_heading="Folder",
            tree_width=600,
            height=18,
        )
        self.table.frame.grid(row=2, column=0, sticky="nsew", padx=18)
        tree = self.table.tree
        tree.bind("<<TreeviewOpen>>", self._on_open)
        tree.bind("<<TreeviewSelect>>", self._on_select)
        tree.bind("<Return>", lambda _event: self.open_selected())
        tree.bind("<Button-3>", self._context_menu)

        bar = ctk.CTkFrame(wrap, fg_color="transparent")
        bar.grid(row=3, column=0, sticky="ew", padx=18, pady=(10, 16))
        bar.grid_columnconfigure(0, weight=1)
        self.detail = ctk.CTkLabel(bar, text="Select a folder to see its full path.", font=app.font_s, text_color=MUTED, anchor="w")
        self.detail.grid(row=0, column=0, sticky="ew")
        app._button(bar, "Collapse all", self.collapse_all, "ghost").grid(row=0, column=1, padx=(8, 0))
        app._button(bar, "Copy path", self.copy_selected, "ghost").grid(row=0, column=2, padx=(8, 0))
        app._button(bar, "Open in Explorer", self.open_selected).grid(row=0, column=3, padx=(8, 0))

        self.menu = tk.Menu(window, tearoff=0, bg=CARD, fg=TEXT, activebackground=CARD_2, activeforeground=TEXT, bd=0, relief="flat")
        self.menu.add_command(label="Open in Explorer", command=self.open_selected)
        self.menu.add_command(label="Copy path", command=self.copy_selected)

        self.nodes.clear()
        self.iid_of.clear()
        self.pending.clear()
        drives = [drive for drive in self.report.drives if drive.tree is not None]
        for drive in drives:
            root = drive.tree
            self.drive_size[drive.root] = root.size or 1
            label = drive.root.rstrip("\\") or root.name
            iid = self._insert_node("", root, root.size or 1, drive.root, label=f"{label}  drive")
            if len(drives) == 1:
                self._fill(iid)
                tree.item(iid, open=True)
        try:
            window.attributes("-topmost", True)
            window.after(250, lambda: window.attributes("-topmost", False) if window.winfo_exists() else None)
        except tk.TclError:
            pass

    # ------------------------------------------------------------ rows

    def _bar(self, share: float, muted: bool) -> tk.PhotoImage:
        """A small share-of-parent bar drawn beside the folder name. Images are shared between rows."""
        step = max(0, min(BAR_STEPS, round(share * BAR_STEPS)))
        cached = self.bars.get((step, muted))
        if cached is not None:
            return cached
        scale = scale_of(self.window)
        width, height, gap = int(56 * scale), int(10 * scale), int(10 * scale)
        image = tk.PhotoImage(master=self.window, width=width + gap, height=height)
        image.put(LINE, to=(0, 0, width, height))
        filled = int(round(width * step / BAR_STEPS))
        if filled:
            image.put(FAINT if muted else ACCENT, to=(0, 0, filled, height))
        self.bars[(step, muted)] = image
        return image

    def _insert_node(self, parent: str, node, parent_size: int, drive_root: str, label: str | None = None) -> str:
        share = node.size / parent_size if parent_size else 0
        synthetic = node.name in SYNTHETIC
        drive_share = node.size / self.drive_size.get(drive_root, node.size or 1)
        iid = self.table.insert(
            (format_size(node.size), share_text(share), share_text(drive_share)),
            parent=parent,
            text="  " + (label or node.name or "Folder"),
            tags=("muted",) if synthetic else (),
            keys={"#0": (label or node.name or "").lower(), "size": node.size, "share": node.size, "drive": node.size, "last": synthetic},
            image=self._bar(share, synthetic),
        )
        self.nodes[iid] = (node, drive_root)
        self.iid_of[id(node)] = iid
        if _expandable(node):
            # A placeholder child makes the arrow appear. The real rows are read on open.
            self.table.tree.insert(iid, "end", iid=iid + ".more", text="  …", tags=("muted",))
            self.pending.add(iid)
        return iid

    def _fill(self, iid: str) -> None:
        if iid not in self.pending:
            return
        self.pending.discard(iid)
        tree = self.table.tree
        if tree.exists(iid + ".more"):
            tree.delete(iid + ".more")
        node, drive_root = self.nodes[iid]
        for child in node.children:
            self._insert_node(iid, child, node.size or 1, drive_root)
        if self.table.sorted_by:
            column, descending = self.table.sorted_by
            self.table._sort_level(iid, column, descending)

    def _on_open(self, _event=None) -> None:
        iid = self.table.tree.focus()
        if iid:
            self._fill(iid)

    def _on_select(self, _event=None) -> None:
        iid = self.table.selected()
        entry = self.nodes.get(iid)
        if entry is None:
            return
        node, drive_root = entry
        where = node.path or "Several small folders, grouped together"
        drive_share = node.size / self.drive_size.get(drive_root, node.size or 1)
        self.detail.configure(text=f"{where}   ·   {format_size(node.size)}   ·   {share_text(drive_share)} of what was measured on {drive_root[:2]}")

    def _reveal(self, path: list) -> None:
        """Open each folder along the path and select the last one."""
        tree = self.table.tree
        target = ""
        for node in path:
            iid = self.iid_of.get(id(node))
            if iid is None:
                break
            self._fill(iid)
            tree.item(iid, open=True)
            target = iid
        if target:
            tree.selection_set(target)
            tree.focus(target)
            tree.see(target)

    # ------------------------------------------------------------ actions

    def _selected_path(self) -> str:
        entry = self.nodes.get(self.table.selected()) if self.table else None
        if entry is None:
            return ""
        node = entry[0]
        return node.path if node.path and node.name != "Smaller folders" else ""

    def open_selected(self) -> None:
        path = self._selected_path()
        if path and os.path.exists(path):
            reveal_in_explorer(path)
        elif self.table is not None:
            self.detail.configure(text="Choose a folder first. Smaller folders is a group, not one folder.")

    def copy_selected(self) -> None:
        path = self._selected_path()
        if not path:
            return
        self.window.clipboard_clear()
        self.window.clipboard_append(path)
        self.detail.configure(text=f"Copied {path}")

    def collapse_all(self) -> None:
        tree = self.table.tree
        pending = list(tree.get_children(""))
        while pending:
            iid = pending.pop()
            tree.item(iid, open=False)
            pending.extend(tree.get_children(iid))

    def _context_menu(self, event) -> None:
        iid = self.table.tree.identify_row(event.y)
        if not iid or iid.endswith(".more"):
            return
        self.table.tree.selection_set(iid)
        self.table.tree.focus(iid)
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()


def _expandable(node) -> bool:
    children = node.children
    if not children:
        return False
    # Older saved scans list a lone "Files here" under folders that only hold files.
    return not (len(children) == 1 and children[0].name in SYNTHETIC)
