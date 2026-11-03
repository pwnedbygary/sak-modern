from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from . import __version__
from .core.bundle import build_nsp, build_xci, make_plan
from .core.errors import SakError
from .core.extract import extract_packages, package_label
from .core.keys import KeyStore
from .core.model import group_contents, load_container
from .core.verify import verify_container
from .filepicker import FilePickerDialog, autosize_tree

BG = "#0b0f14"
PANEL = "#111820"
PANEL_2 = "#17212b"
BORDER = "#243241"
FG = "#e6edf3"
MUTED = "#8b98a9"
ACCENT = "#3b82f6"
ACCENT_HOVER = "#2563eb"
GREEN = "#3fb950"
YELLOW = "#d29922"
RED = "#f85149"
BLUE = "#3b82f6"
AMBER = "#f59e0b"
PURPLE = "#a855f7"
TEAL = "#14b8a6"
GREEN_2 = "#22c55e"
PALETTES = {
    "dark": {
        "BG": "#0b0f14", "PANEL": "#111820", "PANEL_2": "#17212b",
        "BORDER": "#243241", "FG": "#e6edf3", "MUTED": "#8b98a9",
        "ACCENT": "#3b82f6", "ACCENT_HOVER": "#2563eb",
        "GREEN": "#3fb950", "YELLOW": "#d29922", "RED": "#f85149",
        "BLUE": "#3b82f6", "AMBER": "#f59e0b", "PURPLE": "#a855f7",
        "TEAL": "#14b8a6", "GREEN_2": "#22c55e",
    },
    "light": {
        "BG": "#f4f7fb", "PANEL": "#ffffff", "PANEL_2": "#e8eef6",
        "BORDER": "#c7d2e0", "FG": "#111827", "MUTED": "#4b5563",
        "ACCENT": "#2563eb", "ACCENT_HOVER": "#1d4ed8",
        "GREEN": "#15803d", "YELLOW": "#a16207", "RED": "#b91c1c",
        "BLUE": "#2563eb", "AMBER": "#d97706", "PURPLE": "#7c3aed",
        "TEAL": "#0f766e", "GREEN_2": "#16a34a",
    },
    "contrast": {
        "BG": "#000000", "PANEL": "#090909", "PANEL_2": "#1d1d1d",
        "BORDER": "#777777", "FG": "#ffffff", "MUTED": "#d0d0d0",
        "ACCENT": "#00a8ff", "ACCENT_HOVER": "#0084cc",
        "GREEN": "#00ff88", "YELLOW": "#ffd400", "RED": "#ff5555",
        "BLUE": "#00a8ff", "AMBER": "#ffb000", "PURPLE": "#d58aff",
        "TEAL": "#00e5d0", "GREEN_2": "#00ff88",
    },
}
PALETTE_KEYS = ("BG", "PANEL", "PANEL_2", "BORDER", "FG", "MUTED", "ACCENT",
                "ACCENT_HOVER", "GREEN", "YELLOW", "RED", "BLUE", "AMBER",
                "PURPLE", "TEAL", "GREEN_2")


def set_palette(name: str) -> None:
    global BG, PANEL, PANEL_2, BORDER, FG, MUTED, ACCENT, ACCENT_HOVER
    global GREEN, YELLOW, RED, BLUE, AMBER, PURPLE, TEAL, GREEN_2
    p = PALETTES.get(name, PALETTES["dark"])
    BG = p["BG"]
    PANEL = p["PANEL"]
    PANEL_2 = p["PANEL_2"]
    BORDER = p["BORDER"]
    FG = p["FG"]
    MUTED = p["MUTED"]
    ACCENT = p["ACCENT"]
    ACCENT_HOVER = p["ACCENT_HOVER"]
    GREEN = p["GREEN"]
    YELLOW = p["YELLOW"]
    RED = p["RED"]
    BLUE = p["BLUE"]
    AMBER = p["AMBER"]
    PURPLE = p["PURPLE"]
    TEAL = p["TEAL"]
    GREEN_2 = p["GREEN_2"]


FONT = "DejaVu Sans"
MONO = "DejaVu Sans Mono"


def human_size(n: int) -> str:
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.2f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{n} B"


class Card(tk.Frame):
    def __init__(self, master, **kw):
        super().__init__(master, bg=PANEL, highlightbackground=BORDER, highlightthickness=1, **kw)



def make_scrollable_tree(parent, columns, widths, height=6):
    wrap = tk.Frame(parent, bg=PANEL)
    wrap.pack(fill="both", expand=True)
    tree = ttk.Treeview(wrap, columns=columns, show="headings", height=height)
    for c in columns:
        tree.heading(c, text=c.title())
        tree.column(c, width=widths.get(c, 100), anchor="w", stretch=False)
    ysb = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
    xsb = ttk.Scrollbar(wrap, orient="horizontal", command=tree.xview)
    tree.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
    tree.grid(row=0, column=0, sticky="nsew")
    ysb.grid(row=0, column=1, sticky="ns")
    xsb.grid(row=1, column=0, sticky="ew")
    wrap.rowconfigure(0, weight=1)
    wrap.columnconfigure(0, weight=1)
    return tree

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("SAK Modern")
        self.geometry("1240x820")
        self.minsize(1080, 720)
        self.configure(bg=BG)
        self.events: queue.Queue = queue.Queue()
        self.base_tk_scaling = float(self.tk.call("tk", "scaling"))
        self.palette_name = "dark"
        self.current_page = "bundle"
        self.ui_scale = 1.0
        self.keys_path: Path | None = None
        self.keystore: KeyStore | None = None
        self.base_path: Path | None = None
        self.add_paths: list[Path] = []
        self.last_output: Path | None = None
        self.log_lines: list[str] = []
        self.log_window: tk.Toplevel | None = None
        self._progress_value = tk.DoubleVar(value=0)
        self.pkg_source: Path | None = None
        self.pkg_container = None
        self.pkg_groups: list = []
        self.pkg_selected: int | None = None
        self.pkg_tray: list = []
        self.pkg_hitboxes: list[tuple[int, int, int, int, int]] = []
        self.pkg_drag_start: tuple[int, int] | None = None
        self.pkg_drag_index: int | None = None
        self.pkg_ghost: tk.Toplevel | None = None
        self._pkg_progress = tk.DoubleVar(value=0)
        self._last_dir: Path | None = None
        self.scale_var = tk.StringVar(value="100%")
        self.theme_var = tk.StringVar(value="Dark")
        self._build_widgets()
        self._apply_style()
        self._load_saved_settings()
        self.after(100, self._poll_events)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Control-plus>", lambda _e: self._nudge_scale(1))
        self.bind("<Control-equal>", lambda _e: self._nudge_scale(1))
        self.bind("<Control-minus>", lambda _e: self._nudge_scale(-1))
        self.bind("<Control-0>", lambda _e: self._set_scale_index(0))

    # ---------- styling ----------
    def _apply_style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", background=BG, foreground=FG, fieldbackground=PANEL_2,
                        bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                        focuscolor=ACCENT, font=(FONT, 10))
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=PANEL)
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("Card.TLabel", background=PANEL, foreground=FG)
        style.configure("Muted.TLabel", background=PANEL, foreground=MUTED, font=(FONT, 9))
        style.configure("Title.TLabel", background=BG, foreground=FG, font=(FONT, 22, "bold"))
        style.configure("Sub.TLabel", background=BG, foreground=MUTED, font=(FONT, 10))
        style.configure("H2.TLabel", background=PANEL, foreground=FG, font=(FONT, 12, "bold"))
        style.configure("Accent.TButton", background=ACCENT, foreground="#ffffff",
                        font=(FONT, 10, "bold"), padding=(16, 9), borderwidth=0)
        style.map("Accent.TButton", background=[("active", ACCENT_HOVER), ("disabled", "#1f3a5f")])
        style.configure("TButton", background=PANEL_2, foreground=FG, padding=(10, 6),
                        borderwidth=0, font=(FONT, 10))
        style.map("TButton", background=[("active", "#223041")])
        style.configure("Ghost.TButton", background=PANEL, foreground=MUTED, padding=(8, 4))
        style.map("Ghost.TButton", background=[("active", PANEL_2)], foreground=[("active", FG)])
        style.configure("Nav.TButton", background=BG, foreground=MUTED, anchor="w", padding=(18, 12))
        style.map("Nav.TButton", background=[("active", PANEL_2)], foreground=[("active", FG)])
        style.configure("NavActive.TButton", background=PANEL_2, foreground=FG, anchor="w", padding=(18, 12))
        style.configure("TEntry", fieldbackground=PANEL_2, foreground=FG, insertcolor=FG, padding=8)
        style.configure("TCombobox", fieldbackground=PANEL_2, foreground=FG, background=PANEL_2,
                        arrowcolor=FG, padding=6)
        style.map("TCombobox",
                  fieldbackground=[("readonly", PANEL_2)],
                  foreground=[("readonly", FG)],
                  selectbackground=[("readonly", PANEL_2)],
                  selectforeground=[("readonly", FG)])
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=FG,
                        rowheight=int(28 * max(self.ui_scale, 1.0)), borderwidth=0)
        style.configure("Treeview.Heading", background=PANEL_2, foreground=MUTED,
                        font=(FONT, 9, "bold"), borderwidth=0)
        style.map("Treeview", background=[("selected", "#1d4ed8")], foreground=[("selected", "#ffffff")])
        style.configure("Horizontal.TProgressbar", troughcolor=PANEL_2, background=ACCENT,
                        bordercolor=PANEL_2, lightcolor=ACCENT, darkcolor=ACCENT)

    # ---------- layout ----------
    def _build_widgets(self):
        for child in list(self.winfo_children()):
            child.destroy()
        self.log_window = None
        self.pkg_ghost = None
        sidebar = tk.Frame(self, bg=BG, width=int(210 * max(self.ui_scale, 1.0)))
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        brand = tk.Frame(sidebar, bg=BG)
        brand.pack(fill="x", padx=18, pady=(22, 18))
        tk.Label(brand, text="SAK", bg=BG, fg=ACCENT, font=(FONT, 24, "bold")).pack(side="left")
        tk.Label(brand, text="modern", bg=BG, fg=FG, font=(FONT, 13)).pack(side="left", padx=(6, 0), pady=(8, 0))

        self.nav_buttons = {}
        for name, label in (("bundle", "◈  Bundle"), ("partitions", "▦  Partitions"),
                            ("inspect", "⌕  Inspect"), ("settings", "⚙  Settings")):
            b = ttk.Button(sidebar, text=label, style="Nav.TButton", command=lambda n=name: self.show_page(n))
            b.pack(fill="x", padx=8, pady=2)
            self.nav_buttons[name] = b

        foot = tk.Frame(sidebar, bg=BG)
        foot.pack(side="bottom", fill="x", padx=18, pady=18)
        tk.Label(foot, text=f"v{__version__}", bg=BG, fg=MUTED, font=(FONT, 9)).pack(anchor="w")
        tk.Label(foot, text="Clean-room remux engine", bg=BG, fg=MUTED, font=(FONT, 8)).pack(anchor="w")

        self.main = tk.Frame(self, bg=BG)
        self.main.pack(side="left", fill="both", expand=True, padx=(0, 18), pady=18)

        self.pages = {}
        self.pages["bundle"] = self._build_bundle_page()
        self.pages["partitions"] = self._build_partitions_page()
        self.pages["inspect"] = self._build_inspect_page()
        self.pages["settings"] = self._build_settings_page()
        self.show_page("bundle")

    def _build_bundle_page(self):
        page = tk.Frame(self.main, bg=BG)
        header = tk.Frame(page, bg=BG)
        header.pack(fill="x", pady=(0, 14))
        ttk.Label(header, text="Build a bundle", style="Title.TLabel").pack(anchor="w")
        ttk.Label(header, text="Base game + update + DLC in one NSP or XCI. New updates replace old ones.",
                  style="Sub.TLabel").pack(anchor="w", pady=(4, 0))

        base_card = Card(page)
        base_card.pack(fill="x", pady=(0, 10))
        inner = tk.Frame(base_card, bg=PANEL)
        inner.pack(fill="x", padx=16, pady=14)
        ttk.Label(inner, text="1  Base game", style="H2.TLabel").pack(anchor="w")
        ttk.Label(inner, text="The original XCI or NSP. Already-merged files are accepted.",
                  style="Muted.TLabel").pack(anchor="w", pady=(2, 8))
        row = tk.Frame(inner, bg=PANEL)
        row.pack(fill="x")
        self.base_var = tk.StringVar()
        ttk.Entry(row, textvariable=self.base_var, state="readonly").pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Choose base…", command=self.choose_base).pack(side="left", padx=(8, 0))
        self.base_info = ttk.Label(inner, text="No base selected", style="Muted.TLabel")
        self.base_info.pack(anchor="w", pady=(8, 0))

        add_card = Card(page)
        add_card.pack(fill="both", expand=True, pady=(0, 10))
        inner = tk.Frame(add_card, bg=PANEL)
        inner.pack(fill="both", expand=True, padx=16, pady=14)
        head = tk.Frame(inner, bg=PANEL)
        head.pack(fill="x")
        ttk.Label(head, text="2  Updates and DLC", style="H2.TLabel").pack(side="left")
        ttk.Button(head, text="Clear", style="Ghost.TButton", command=self.clear_addons).pack(side="right")
        ttk.Button(head, text="Add files…", style="Ghost.TButton", command=self.choose_addons).pack(side="right", padx=(0, 6))
        ttk.Label(inner, text="Select one or more update/DLC NSPs. Higher versions win; matching DLC is replaced.",
                  style="Muted.TLabel").pack(anchor="w", pady=(2, 8))
        cols = ("kind", "title", "version", "size", "file")
        self.addon_tree = make_scrollable_tree(
            inner, cols, {"kind": 75, "title": 190, "version": 90, "size": 100, "file": 520}, height=4)

        out_card = Card(page)
        out_card.pack(fill="x")
        inner = tk.Frame(out_card, bg=PANEL)
        inner.pack(fill="x", padx=16, pady=14)
        ttk.Label(inner, text="3  Output", style="H2.TLabel").grid(row=0, column=0, columnspan=4, sticky="w")
        self.format_var = tk.StringVar(value="xci")
        fmt = tk.Frame(inner, bg=PANEL)
        fmt.grid(row=1, column=0, columnspan=4, sticky="w", pady=(8, 10))
        for value, label, tip in (("xci", "XCI", "hardware/USB · ticketless embedded content"),
                                  ("nsp", "NSP", "installers/emulators · tickets preserved")):
            rb = ttk.Radiobutton(fmt, text=label, value=value, variable=self.format_var)
            rb.pack(side="left", padx=(0, 16))
            tk.Label(fmt, text=tip, bg=PANEL, fg=MUTED, font=(FONT, 9)).pack(side="left", padx=(0, 20))

        ttk.Label(inner, text="Folder", style="Card.TLabel").grid(row=2, column=0, sticky="w")
        self.out_var = tk.StringVar(value=str(Path.home() / "SAK-Output"))
        ttk.Entry(inner, textvariable=self.out_var).grid(row=2, column=1, columnspan=2, sticky="ew", padx=(10, 8))
        ttk.Button(inner, text="Browse…", command=self.choose_out).grid(row=2, column=3, sticky="e")
        ttk.Label(inner, text="Name (optional)", style="Card.TLabel").grid(row=3, column=0, sticky="w", pady=(8, 0))
        self.name_var = tk.StringVar()
        ttk.Entry(inner, textvariable=self.name_var).grid(row=3, column=1, columnspan=3, sticky="ew", padx=(10, 0), pady=(8, 0))
        inner.columnconfigure(1, weight=1)

        actions = tk.Frame(page, bg=BG)
        actions.pack(fill="x", pady=(12, 0))
        self.build_btn = ttk.Button(actions, text="Build bundle", style="Accent.TButton", command=self.start_build)
        self.build_btn.pack(side="right")
        self.log_btn = ttk.Button(actions, text="Log", style="Ghost.TButton", command=self.show_log)
        self.log_btn.pack(side="right", padx=(0, 8))
        self.open_btn = ttk.Button(actions, text="Open output folder", style="Ghost.TButton", command=self.open_output)
        self.open_btn.pack(side="right", padx=(0, 8))
        self.status = ttk.Label(actions, text="Ready", style="Sub.TLabel")
        self.status.pack(side="left")
        self.progress = ttk.Progressbar(actions, variable=self._progress_value, maximum=100, length=260)
        self.progress.pack(side="right", padx=(0, 14))

        return page

    def _build_partitions_page(self):
        page = tk.Frame(self.main, bg=BG)
        header = tk.Frame(page, bg=BG)
        header.pack(fill="x", pady=(0, 10))
        ttk.Label(header, text="Partition workspace", style="Title.TLabel").pack(anchor="w")
        ttk.Label(header, text="Open a merged XCI/NSP, inspect base/update/DLC as a disk map, then drag packages to extract.",
                  style="Sub.TLabel").pack(anchor="w", pady=(3, 0))

        import_card = Card(page)
        import_card.pack(fill="x", pady=(0, 8))
        inner = tk.Frame(import_card, bg=PANEL)
        inner.pack(fill="x", padx=14, pady=10)
        ttk.Label(inner, text="Merged file", style="H2.TLabel").pack(side="left")
        self.pkg_path_var = tk.StringVar()
        ttk.Entry(inner, textvariable=self.pkg_path_var, state="readonly").pack(
            side="left", fill="x", expand=True, padx=(12, 8))
        ttk.Button(inner, text="Import file…", command=self.choose_partition_file).pack(side="left")
        self.pkg_meta = ttk.Label(inner, text="No file loaded", style="Muted.TLabel")
        self.pkg_meta.pack(side="left", padx=(12, 0))

        map_card = Card(page)
        map_card.pack(fill="x", pady=(0, 8))
        inner = tk.Frame(map_card, bg=PANEL)
        inner.pack(fill="x", padx=14, pady=10)
        head = tk.Frame(inner, bg=PANEL)
        head.pack(fill="x")
        ttk.Label(head, text="Partition map", style="H2.TLabel").pack(side="left")
        ttk.Label(head, text="click to select · drag a block into the extraction tray",
                  style="Muted.TLabel").pack(side="left", padx=(12, 0))
        self.pkg_canvas = tk.Canvas(inner, bg=PANEL, highlightthickness=0, height=106, bd=0)
        self.pkg_canvas.pack(fill="x", pady=(8, 4))
        self.pkg_canvas.bind("<Configure>", lambda _e: self._draw_partition_map())
        self.pkg_canvas.bind("<ButtonPress-1>", self._pkg_press)
        self.pkg_canvas.bind("<B1-Motion>", self._pkg_motion)
        self.pkg_canvas.bind("<ButtonRelease-1>", self._pkg_release)
        self.pkg_canvas.bind("<Double-Button-1>", self._pkg_double)
        self.pkg_legend = tk.Frame(inner, bg=PANEL)
        self.pkg_legend.pack(fill="x")

        lower = tk.Frame(page, bg=BG)
        lower.pack(fill="both", expand=True)
        left = Card(lower)
        left.pack(side="left", fill="both", expand=True, padx=(0, 8))
        left_inner = tk.Frame(left, bg=PANEL)
        left_inner.pack(fill="both", expand=True, padx=14, pady=10)
        head = tk.Frame(left_inner, bg=PANEL)
        head.pack(fill="x")
        ttk.Label(head, text="Extraction tray", style="H2.TLabel").pack(side="left")
        ttk.Button(head, text="Add selected", style="Ghost.TButton", command=self._pkg_add_selected).pack(side="right")
        ttk.Button(head, text="Clear", style="Ghost.TButton", command=self._pkg_clear).pack(side="right", padx=(0, 6))
        ttk.Button(head, text="Remove", style="Ghost.TButton", command=self._pkg_remove).pack(side="right", padx=(0, 6))
        cols = ("package", "title", "size", "format")
        self.pkg_tree = make_scrollable_tree(
            left_inner, cols, {"package": 130, "title": 230, "size": 100, "format": 85}, height=5)
        self.pkg_tree.bind("<Double-1>", lambda _e: self._pkg_remove())
        self.drop_canvas = tk.Canvas(left_inner, bg=PANEL_2, highlightthickness=0, height=58, bd=0)
        self.drop_canvas.pack(fill="x")
        self.drop_canvas.bind("<Configure>", lambda _e: self._draw_drop_zone())
        self.drop_canvas.bind("<ButtonRelease-1>", self._pkg_release)

        right = Card(lower, width=330)
        right.pack(side="left", fill="y")
        right.pack_propagate(False)
        right_inner = tk.Frame(right, bg=PANEL)
        right_inner.pack(fill="both", expand=True, padx=14, pady=10)
        ttk.Label(right_inner, text="Extract", style="H2.TLabel").pack(anchor="w")
        ttk.Label(right_inner, text="Base keeps source format. Updates and DLC are written as NSP.",
                  style="Muted.TLabel", wraplength=290).pack(anchor="w", pady=(2, 8))
        ttk.Label(right_inner, text="Output folder", style="Card.TLabel").pack(anchor="w")
        row = tk.Frame(right_inner, bg=PANEL)
        row.pack(fill="x", pady=(4, 8))
        self.pkg_out_var = tk.StringVar(value=str(Path.home() / "SAK-Output"))
        ttk.Entry(row, textvariable=self.pkg_out_var).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="…", width=3, command=self.choose_pkg_out).pack(side="left", padx=(6, 0))
        ttk.Label(right_inner, text="Base format", style="Card.TLabel").pack(anchor="w")
        self.pkg_base_fmt = tk.StringVar(value="Match source")
        ttk.Combobox(right_inner, textvariable=self.pkg_base_fmt, state="readonly",
                     values=("Match source", "XCI", "NSP")).pack(fill="x", pady=(4, 10))
        self.pkg_extract_btn = ttk.Button(right_inner, text="Extract selected", style="Accent.TButton",
                                         command=lambda: self.start_extract(False))
        self.pkg_extract_btn.pack(fill="x")
        self.pkg_extract_all_btn = ttk.Button(right_inner, text="Extract all packages", style="Ghost.TButton",
                                             command=lambda: self.start_extract(True))
        self.pkg_extract_all_btn.pack(fill="x", pady=(6, 0))
        self.pkg_open_btn = ttk.Button(right_inner, text="Open output folder", style="Ghost.TButton",
                                      command=self.open_pkg_output)
        self.pkg_open_btn.pack(fill="x", pady=(6, 0))
        self.pkg_status = ttk.Label(right_inner, text="Ready", style="Muted.TLabel", wraplength=290)
        self.pkg_status.pack(anchor="w", pady=(12, 4))
        self.pkg_progress = ttk.Progressbar(right_inner, variable=self._pkg_progress, maximum=100)
        self.pkg_progress.pack(fill="x", pady=(0, 2))
        self.pkg_last_output: Path | None = None
        self._draw_partition_map()
        self._draw_drop_zone()
        return page

    def _build_inspect_page(self):
        page = tk.Frame(self.main, bg=BG)
        ttk.Label(page, text="Inspect a container", style="Title.TLabel").pack(anchor="w")
        ttk.Label(page, text="Read title IDs, versions, content types, and package boundaries.",
                  style="Sub.TLabel").pack(anchor="w", pady=(4, 12))
        card = Card(page)
        card.pack(fill="x", pady=(0, 8))
        inner = tk.Frame(card, bg=PANEL)
        inner.pack(fill="x", padx=14, pady=10)
        row = tk.Frame(inner, bg=PANEL)
        row.pack(fill="x")
        self.inspect_var = tk.StringVar()
        ttk.Entry(row, textvariable=self.inspect_var, state="readonly").pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Choose file…", command=self.choose_inspect).pack(side="left", padx=(8, 0))
        ttk.Button(row, text="Inspect", command=self.run_inspect).pack(side="left", padx=(8, 0))
        ttk.Button(row, text="Verify", command=self.run_verify).pack(side="left", padx=(8, 0))
        ttk.Button(row, text="Open in Partitions", style="Ghost.TButton",
                   command=self._open_inspect_in_partitions).pack(side="left", padx=(8, 0))
        self.inspect_summary = ttk.Label(inner, text="No file inspected", style="Muted.TLabel")
        self.inspect_summary.pack(anchor="w", pady=(8, 0))
        table_card = Card(page)
        table_card.pack(fill="both", expand=True)
        table_inner = tk.Frame(table_card, bg=PANEL)
        table_inner.pack(fill="both", expand=True, padx=14, pady=10)
        cols = ("package", "nca", "title", "type", "size", "keygen")
        self.inspect_tree = make_scrollable_tree(
            table_inner, cols,
            {"package": 110, "nca": 340, "title": 170, "type": 100, "size": 100, "keygen": 75},
            height=10)
        self.inspect_text = tk.Text(table_inner, height=4, bg=PANEL, fg=MUTED, insertbackground=FG,
                                    relief="flat", font=(MONO, 8), wrap="word")
        self.inspect_text.pack(fill="x", pady=(6, 0))
        return page

    def _build_settings_page(self):
        page = tk.Frame(self.main, bg=BG)
        ttk.Label(page, text="Settings", style="Title.TLabel").pack(anchor="w")
        ttk.Label(page, text="Readability, theme, and local keys.", style="Sub.TLabel").pack(anchor="w", pady=(4, 14))
        appearance = Card(page)
        appearance.pack(fill="x", pady=(0, 8))
        ai = tk.Frame(appearance, bg=PANEL)
        ai.pack(fill="x", padx=16, pady=14)
        ttk.Label(ai, text="Appearance", style="H2.TLabel").pack(anchor="w")
        ttk.Label(ai, text="Increase the text size if the default UI is hard to read on a 1440p/4K display.",
                  style="Muted.TLabel").pack(anchor="w", pady=(2, 10))
        controls = tk.Frame(ai, bg=PANEL)
        controls.pack(fill="x")
        ttk.Label(controls, text="Text size", style="Card.TLabel").pack(side="left")
        ttk.Combobox(controls, textvariable=self.scale_var, state="readonly", width=8,
                     values=("100%", "115%", "130%", "150%")).pack(side="left", padx=(10, 24))
        ttk.Label(controls, text="Theme", style="Card.TLabel").pack(side="left")
        ttk.Combobox(controls, textvariable=self.theme_var, state="readonly", width=14,
                     values=("Dark", "Light", "High contrast")).pack(side="left", padx=(10, 16))
        ttk.Button(controls, text="Apply", style="Ghost.TButton", command=self.apply_appearance).pack(side="left")
        self.appearance_status = ttk.Label(ai, text="Changes apply immediately and are remembered.",
                                           style="Muted.TLabel")
        self.appearance_status.pack(anchor="w", pady=(10, 0))
        preview = tk.Label(ai, text="Aa  The quick brown fox  ·  partition map  ·  README",
                           bg=PANEL, fg=FG, font=(FONT, 13))
        preview.pack(anchor="w", pady=(8, 0))
        card = Card(page)
        card.pack(fill="x")
        inner = tk.Frame(card, bg=PANEL)
        inner.pack(fill="x", padx=16, pady=16)
        ttk.Label(inner, text="prod.keys / keys.txt", style="H2.TLabel").pack(anchor="w")
        row = tk.Frame(inner, bg=PANEL)
        row.pack(fill="x", pady=(8, 0))
        self.keys_var = tk.StringVar()
        ttk.Entry(row, textvariable=self.keys_var).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Browse…", command=self.choose_keys).pack(side="left", padx=(8, 0))
        ttk.Button(row, text="Test", command=self.test_keys).pack(side="left", padx=(8, 0))
        self.keys_status = ttk.Label(inner, text="", style="Muted.TLabel")
        self.keys_status.pack(anchor="w", pady=(10, 0))
        ttk.Label(inner, text="Accepted names: prod.keys, keys.txt, keys.dat. Auto-detected from the app folder, "
                               "~/.switch, or Ryujinx's system folder.",
                  style="Muted.TLabel").pack(anchor="w", pady=(8, 0))
        return page

    # ---------- partition workspace ----------
    def _partition_color(self, index: int, group) -> str:
        if group.kind == "base":
            return BLUE
        if group.kind == "update":
            return AMBER
        palette = (GREEN_2, PURPLE, TEAL, "#ec4899", "#eab308", "#06b6d4")
        return palette[index % len(palette)]

    def _draw_partition_map(self):
        if not hasattr(self, "pkg_canvas"):
            return
        c = self.pkg_canvas
        c.delete("all")
        w = c.winfo_width()
        h = c.winfo_height()
        if w < 120:
            return
        if not self.pkg_groups:
            c.create_text(w // 2, h // 2, text="Import a merged XCI or NSP to see its partitions",
                          fill=MUTED, font=(FONT, 10))
            return
        total = sum(g.total_size for g in self.pkg_groups) or 1
        avail = max(w - 32, 100)
        weights = [max(g.total_size / total * avail, 9) for g in self.pkg_groups]
        scale = avail / sum(weights)
        widths = [max(6, int(x * scale)) for x in weights]
        widths[-1] += avail - sum(widths)
        x = 16
        y = 18
        bar_h = 54
        self.pkg_hitboxes = []
        for i, (g, seg_w) in enumerate(zip(self.pkg_groups, widths)):
            color = self._partition_color(i, g)
            x2 = x + seg_w
            c.create_rectangle(x, y, x2, y + bar_h, fill=color, outline=PANEL, width=2)
            if self.pkg_selected == i:
                c.create_rectangle(x + 1, y + 1, x2 - 1, y + bar_h - 1, outline="#ffffff", width=2)
            label = package_label(g)
            if seg_w >= 92:
                c.create_text(x + 8, y + 15, text=label, anchor="w", fill="#ffffff",
                              font=(FONT, 9, "bold"))
                c.create_text(x + 8, y + 34, text=human_size(g.total_size), anchor="w",
                              fill="#ffffff", font=(FONT, 8))
            elif seg_w >= 30:
                c.create_text((x + x2) // 2, y + bar_h // 2, text=label.split()[0],
                              fill="#ffffff", font=(FONT, 7, "bold"))
            self.pkg_hitboxes.append((x, y, x2, y + bar_h, i))
            x = x2
        c.create_text(16, y + bar_h + 16, text="0", anchor="w", fill=MUTED, font=(FONT, 8))
        c.create_text(w - 16, y + bar_h + 16, text=human_size(total), anchor="e", fill=MUTED, font=(FONT, 8))
        # Legend
        for child in self.pkg_legend.winfo_children():
            child.destroy()
        for i, g in enumerate(self.pkg_groups):
            item = tk.Frame(self.pkg_legend, bg=PANEL)
            item.grid(row=i // 6, column=i % 6, sticky="w", padx=(0, 16), pady=2)
            tk.Label(item, text="■", bg=PANEL, fg=self._partition_color(i, g),
                     font=(FONT, 9)).pack(side="left")
            tk.Label(item, text=f" {package_label(g)}", bg=PANEL, fg=MUTED,
                     font=(FONT, 8)).pack(side="left")

    def _draw_drop_zone(self):
        c = self.drop_canvas
        c.delete("all")
        w = max(c.winfo_width(), 100)
        h = max(c.winfo_height(), 40)
        c.create_rectangle(2, 2, w - 2, h - 2, outline="#3b82f6", dash=(5, 4), width=1)
        c.create_text(w // 2, h // 2, text="Drag a package here to add it to the tray  ·  double-click a block also works",
                      fill=MUTED, font=(FONT, 8))

    def _pkg_double(self, event):
        for x1, y1, x2, y2, index in self.pkg_hitboxes:
            if x1 <= event.x <= x2 and y1 <= event.y <= y2:
                self.pkg_selected = index
                self._pkg_add_index(index)
                return

    def _pkg_press(self, event):
        if not self.pkg_groups:
            return
        for x1, y1, x2, y2, index in self.pkg_hitboxes:
            if x1 <= event.x <= x2 and y1 <= event.y <= y2:
                self.pkg_selected = index
                self.pkg_drag_start = (event.x_root, event.y_root)
                self.pkg_drag_index = index
                self._draw_partition_map()
                return

    def _pkg_motion(self, event):
        if self.pkg_drag_index is None or self.pkg_drag_start is None:
            return
        dx = event.x_root - self.pkg_drag_start[0]
        dy = event.y_root - self.pkg_drag_start[1]
        if abs(dx) + abs(dy) < 8:
            return
        group = self.pkg_groups[self.pkg_drag_index]
        if self.pkg_ghost is None:
            self.pkg_ghost = tk.Label(self, text=f"  {package_label(group)}  ", bg=self._partition_color(self.pkg_drag_index, group),
                                      fg="#ffffff", padx=8, pady=5, bd=0, font=(FONT, 9, "bold"))
            self.pkg_ghost.lift()
            self.pkg_ghost.bind("<ButtonRelease-1>", self._pkg_release)
        self.pkg_ghost.place(x=event.x_root - self.winfo_rootx() + 12,
                             y=event.y_root - self.winfo_rooty() + 12)

    def _pkg_release(self, event):
        if self.pkg_ghost is not None:
            inside = self._point_in_drop_zone(event.x_root, event.y_root)
            self.pkg_ghost.destroy()
            self.pkg_ghost = None
            if inside and self.pkg_drag_index is not None:
                self._pkg_add_index(self.pkg_drag_index)
        self.pkg_drag_index = None
        self.pkg_drag_start = None
        self._draw_partition_map()

    def _point_in_drop_zone(self, x_root: int, y_root: int) -> bool:
        c = self.drop_canvas
        x1 = c.winfo_rootx()
        y1 = c.winfo_rooty()
        return x1 <= x_root <= x1 + c.winfo_width() and y1 <= y_root <= y1 + c.winfo_height()

    def _pkg_add_index(self, index: int):
        group = self.pkg_groups[index]
        if all(g.package_id != group.package_id for g in self.pkg_tray):
            self.pkg_tray.append(group)
        self._refresh_pkg_tray()

    def _pkg_add_selected(self):
        if self.pkg_selected is None:
            messagebox.showinfo("Packages", "Select a partition block first.")
            return
        self._pkg_add_index(self.pkg_selected)

    def _pkg_remove(self):
        selected = self.pkg_tree.selection()
        if not selected:
            return
        remove = {self.pkg_tree.item(i, "values")[0] for i in selected}
        self.pkg_tray = [g for g in self.pkg_tray if package_label(g) not in remove]
        self._refresh_pkg_tray()

    def _pkg_clear(self):
        self.pkg_tray = []
        self._refresh_pkg_tray()

    def _refresh_pkg_tray(self):
        for item in self.pkg_tree.get_children():
            self.pkg_tree.delete(item)
        for g in self.pkg_tray:
            if g.kind != "base":
                fmt = "NSP"
            else:
                fmt = "XCI" if self.pkg_source and self.pkg_source.suffix.lower() == ".xci" else "NSP"
            self.pkg_tree.insert("", "end", values=(package_label(g), f"{g.title_id:016X}",
                                                    human_size(g.total_size), fmt))
        self.after_idle(lambda: autosize_tree(
            self.pkg_tree,
            {"package": 150, "title": 260, "size": 120, "format": 90},
            min_width=70))

    def _rom_dir(self) -> Path:
        candidates = [
            Path.home() / "Mounts/Emulation/Emulation/ROMM Library/roms/switch",
            Path.home() / "SAK-Output",
            Path.home(),
        ]
        for p in candidates:
            if p.is_dir():
                return p
        return Path.home()

    def _pick_file(self, title: str, *, multiple: bool = False, mode: str = "open",
                   filetypes: list[tuple[str, str]] | None = None, initialdir: Path | None = None):
        start = initialdir or self._last_dir or self._rom_dir()
        if self._last_dir and self._last_dir.is_dir():
            start = self._last_dir
        dlg = FilePickerDialog(
            self, title=title, initialdir=start, multiple=multiple,
            mode=mode, filetypes=filetypes,
        )
        result = dlg.show()
        if result:
            first = result[0] if isinstance(result, list) else result
            try:
                self._last_dir = Path(first).parent if Path(first).is_file() else Path(first)
            except Exception:
                pass
        return result

    def choose_partition_file(self):
        p = self._pick_file(
            "Open merged XCI/NSP",
            filetypes=[("Switch containers (*.xci *.nsp)", "*.xci *.nsp"),
                       ("All files (*.*)", "*")],
            initialdir=self._rom_dir(),
        )
        if p:
            self.load_partition_file(Path(p))

    def load_partition_file(self, path: Path):
        if not self.require_keys():
            return
        try:
            c = load_container(path, self.keystore)
            bases, updates, dlcs = group_contents([c])
            groups = [*bases, *updates, *dlcs]
            if not groups:
                raise SakError("No title packages found in this file.")
        except Exception as exc:
            messagebox.showerror("Open container", str(exc))
            return
        self.pkg_source = path
        self.pkg_container = c
        self.pkg_groups = groups
        self.pkg_selected = 0
        self.pkg_tray = []
        self.pkg_last_output = None
        self.pkg_path_var.set(str(path))
        total = sum(g.total_size for g in groups)
        self.pkg_meta.configure(text=f"{c.kind.upper()} · {len(groups)} packages · {human_size(total)}")
        self.pkg_status.configure(text="Ready — drag a partition into the tray.", foreground=GREEN)
        self._refresh_pkg_tray()
        self.after_idle(self._draw_partition_map)
        self.after_idle(self._draw_drop_zone)

    def choose_pkg_out(self):
        p = self._pick_file(
            "Choose extraction folder", mode="directory",
            initialdir=Path(self.pkg_out_var.get()).expanduser() if self.pkg_out_var.get() else Path.home(),
        )
        if p:
            self.pkg_out_var.set(str(p))

    def _pkg_selected_groups(self, all_packages: bool):
        if all_packages:
            return list(self.pkg_groups)
        return list(self.pkg_tray)

    def start_extract(self, all_packages: bool):
        if not self.require_keys():
            return
        if not self.pkg_source or not self.pkg_groups:
            messagebox.showinfo("Extract", "Import a merged XCI/NSP first.")
            return
        groups = self._pkg_selected_groups(all_packages)
        if not groups:
            messagebox.showinfo("Extract", "Drag packages into the extraction tray first.")
            return
        out = Path(self.pkg_out_var.get()).expanduser()
        fmt = {"Match source": "source", "XCI": "xci", "NSP": "nsp"}.get(self.pkg_base_fmt.get(), "source")
        self.pkg_extract_btn.configure(state="disabled")
        self.pkg_extract_all_btn.configure(state="disabled")
        self._pkg_progress.set(0)
        self.pkg_status.configure(text=f"Extracting {len(groups)} package(s)…", foreground=FG)

        def worker():
            try:
                result = extract_packages(
                    self.pkg_source, out, self.keystore, base_format=fmt,
                    selected=[g.package_id for g in groups],
                    progress=lambda done, total, label: self.events.put(
                        ("pkg_progress", done * 100.0 / max(total, 1), label)),
                )
                self.events.put(("pkg_done", result))
            except Exception as exc:
                self.events.put(("pkg_error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def open_pkg_output(self):
        target = self.pkg_last_output.parent if self.pkg_last_output else Path(self.pkg_out_var.get())
        if target.exists():
            try:
                if sys.platform.startswith("win"):
                    os.startfile(target)  # type: ignore[attr-defined]
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", str(target)])
                else:
                    subprocess.Popen(["xdg-open", str(target)])
            except Exception as exc:
                messagebox.showerror("Open folder", str(exc))

    # ---------- navigation ----------
    def show_page(self, name: str):
        self.current_page = name
        for key, page in self.pages.items():
            page.pack_forget()
        self.pages[name].pack(fill="both", expand=True)
        for key, button in self.nav_buttons.items():
            button.configure(style="NavActive.TButton" if key == name else "Nav.TButton")

    # ---------- settings / keys ----------
    def _load_saved_settings(self):
        cfg = Path.home() / ".config" / "sak-modern" / "settings.ini"
        if cfg.is_file():
            for line in cfg.read_text().splitlines():
                if line.startswith("keys="):
                    self.keys_var.set(line[5:])
                if line.startswith("output="):
                    self.out_var.set(line[7:])
                if line.startswith("scale="):
                    value = line[6:].strip()
                    self.scale_var.set(value if value.endswith("%") else value + "%")
                if line.startswith("theme="):
                    self.theme_var.set(line[6:].strip() or "Dark")
        self.apply_appearance(save=False)
        candidate = self.keys_var.get().strip()
        if candidate:
            self._set_keys(Path(candidate), silent=True)
        else:
            try:
                self._set_keys(None, silent=True)
            except Exception:
                pass

    def _save_settings(self):
        cfg_dir = Path.home() / ".config" / "sak-modern"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "settings.ini").write_text(
            f"keys={self.keys_var.get().strip()}\n"
            f"output={self.out_var.get().strip()}\n"
            f"scale={self.scale_var.get().strip()}\n"
            f"theme={self.theme_var.get().strip()}\n"
        )

    # ---------- appearance ----------
    def _scale_percent(self) -> int:
        try:
            return int(self.scale_var.get().strip().replace("%", ""))
        except ValueError:
            return 100

    def _set_scale_index(self, index: int):
        values = (100, 115, 130, 150)
        self.scale_var.set(f"{values[max(0, min(index, len(values) - 1))]}%")
        self.apply_appearance()

    def _nudge_scale(self, direction: int):
        values = (100, 115, 130, 150)
        current = self._scale_percent()
        if current not in values:
            current = min(values, key=lambda v: abs(v - current))
        index = values.index(current) + direction
        self._set_scale_index(index)

    def _recolor_widgets(self, old: dict):
        mapping = {}
        for key in PALETTE_KEYS:
            if old.get(key) and old[key] != globals().get(key):
                mapping[old[key]] = globals()[key]
        if not mapping:
            return

        def walk(widget):
            for opt in ("background", "bg", "foreground", "fg", "highlightbackground",
                        "highlightcolor", "insertbackground", "selectbackground",
                        "activebackground", "activeforeground"):
                try:
                    value = str(widget.cget(opt))
                except Exception:
                    continue
                if value in mapping:
                    try:
                        widget.configure(**{opt: mapping[value]})
                    except Exception:
                        pass
            for child in widget.winfo_children():
                walk(child)

        for child in self.winfo_children():
            walk(child)
        self.configure(bg=BG)

    def _apply_scaling(self):
        self.tk.call("tk", "scaling", self.base_tk_scaling * self.ui_scale)
        desired_w = int(1240 + max(0.0, self.ui_scale - 1.0) * 720)
        desired_h = int(820 + max(0.0, self.ui_scale - 1.0) * 440)
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        # Leave room for window decorations and the taskbar, and never create
        # an initial window larger than the desktop.
        width = min(desired_w, max(900, int(screen_w * 0.94)))
        height = min(desired_h, max(620, int(screen_h * 0.90)))
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 2)
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _capture_ui_state(self) -> dict:
        state = {
            "keys": self.keys_var.get() if hasattr(self, "keys_var") else "",
            "out": self.out_var.get() if hasattr(self, "out_var") else "",
            "pkg_out": self.pkg_out_var.get() if hasattr(self, "pkg_out_var") else "",
            "name": self.name_var.get() if hasattr(self, "name_var") else "",
            "base": self.base_path,
            "addons": list(self.add_paths),
            "pkg_source": self.pkg_source,
            "pkg_container": self.pkg_container,
            "pkg_groups": list(self.pkg_groups),
            "pkg_selected": self.pkg_selected,
            "pkg_tray": list(self.pkg_tray),
            "page": self.current_page,
        }
        return state

    def _restore_ui_state(self, state: dict) -> None:
        if hasattr(self, "keys_var"):
            self.keys_var.set(state.get("keys", ""))
        if hasattr(self, "out_var"):
            self.out_var.set(state.get("out", ""))
        if hasattr(self, "pkg_out_var"):
            self.pkg_out_var.set(state.get("pkg_out", ""))
        if hasattr(self, "name_var"):
            self.name_var.set(state.get("name", ""))
        self.base_path = state.get("base")
        self.add_paths = list(state.get("addons", []))
        if self.base_path and hasattr(self, "base_var"):
            self.base_var.set(str(self.base_path))
            try:
                self.base_info.configure(text=f"{self.base_path.name} · selected", foreground=GREEN)
            except Exception:
                pass
        if self.keystore and hasattr(self, "keys_status"):
            self.keys_status.configure(text=f"OK — {len(self.keystore.values)} keys loaded", foreground=GREEN)
        if self.base_path:
            try:
                self._refresh_addons()
            except Exception:
                pass
        self.pkg_source = state.get("pkg_source")
        self.pkg_container = state.get("pkg_container")
        self.pkg_groups = list(state.get("pkg_groups", []))
        self.pkg_selected = state.get("pkg_selected")
        self.pkg_tray = list(state.get("pkg_tray", []))
        if self.pkg_source and self.pkg_groups:
            try:
                self.pkg_path_var.set(str(self.pkg_source))
                total = sum(g.total_size for g in self.pkg_groups)
                self.pkg_meta.configure(
                    text=f"{self.pkg_container.kind.upper()} · {len(self.pkg_groups)} packages · {human_size(total)}")
                self._refresh_pkg_tray()
                self.after_idle(self._draw_partition_map)
                self.after_idle(self._draw_drop_zone)
            except Exception:
                pass
        self.show_page(state.get("page", "bundle"))

    def _rebuild_ui(self):
        state = self._capture_ui_state()
        for child in list(self.winfo_children()):
            child.destroy()
        self.log_window = None
        self.pkg_ghost = None
        self._build_widgets()
        self._apply_style()
        self._restore_ui_state(state)

    def apply_appearance(self, save: bool = True):
        theme = {"Dark": "dark", "Light": "light", "High contrast": "contrast"}.get(
            self.theme_var.get(), "dark")
        set_palette(theme)
        self.palette_name = theme
        self.ui_scale = max(0.8, min(2.0, self._scale_percent() / 100.0))
        self._apply_scaling()
        self._rebuild_ui()
        try:
            self.appearance_status.configure(
                text=f"Applied {self.scale_var.get()} · {self.theme_var.get()} theme",
                foreground=GREEN)
        except Exception:
            pass
        if save:
            self._save_settings()

    def choose_keys(self):
        p = self._pick_file(
            "Select prod.keys",
            filetypes=[("Switch keys (*.keys *.txt *.dat)", "*.keys *.txt *.dat"),
                       ("All files (*.*)", "*")],
            initialdir=self._rom_dir(),
        )
        if p:
            self.keys_var.set(str(p))
            self._set_keys(Path(p))

    def _set_keys(self, path: Path | None, silent: bool = False):
        try:
            self.keystore = KeyStore.load(path)
            self.keys_path = self.keystore.source
            if self.keystore.source:
                self.keys_var.set(str(self.keystore.source))
            if not silent:
                self.keys_status.configure(text=f"OK — {len(self.keystore.values)} keys loaded", foreground=GREEN)
            self._save_settings()
            return True
        except SakError as exc:
            self.keystore = None
            if not silent:
                self.keys_status.configure(text=str(exc), foreground=RED)
                messagebox.showerror("Keys", str(exc))
            return False

    def test_keys(self):
        self._set_keys(Path(self.keys_var.get()).expanduser() if self.keys_var.get().strip() else None)

    def require_keys(self) -> bool:
        if self.keystore:
            return True
        if self.keys_path and self._set_keys(self.keys_path):
            return True
        messagebox.showinfo("Keys required", "Select a prod.keys/keys.txt file in Settings first.")
        self.show_page("settings")
        return False

    # ---------- file pickers ----------
    def choose_base(self):
        if not self.require_keys():
            return
        p = self._pick_file(
            "Select base game",
            filetypes=[("Switch containers (*.xci *.nsp)", "*.xci *.nsp"),
                       ("All files (*.*)", "*")],
            initialdir=self._rom_dir(),
        )
        if not p:
            return
        self.base_path = Path(p)
        self.base_var.set(str(self.base_path))
        try:
            c = load_container(self.base_path, self.keystore)
            bases, updates, dlcs = group_contents([c])
            if not bases:
                raise SakError("No base game content found in this file.")
            b = bases[0]
            existing = []
            if updates:
                existing.append(f"update v{updates[0].version}")
            if dlcs:
                existing.append(f"{len(dlcs)} DLC")
            suffix = f" — contains {', '.join(existing)}" if existing else ""
            self.base_info.configure(text=f"{b.title_id:016X} · {len(b.entries)} NCAs{suffix}", foreground=GREEN)
            self.add_paths = []
            self._refresh_addons()
        except Exception as exc:
            self.base_info.configure(text=f"Could not read base: {exc}", foreground=RED)
            messagebox.showerror("Base game", str(exc))

    def choose_addons(self):
        if not self.require_keys():
            return
        picked = self._pick_file(
            "Select updates / DLC",
            multiple=True,
            filetypes=[("Switch containers (*.xci *.nsp)", "*.xci *.nsp"),
                       ("All files (*.*)", "*")],
            initialdir=self._rom_dir(),
        )
        if not picked:
            return
        for p in picked if isinstance(picked, list) else [picked]:
            path = Path(p)
            if path not in self.add_paths:
                self.add_paths.append(path)
        self._refresh_addons()

    def clear_addons(self):
        self.add_paths = []
        self._refresh_addons()

    def _refresh_addons(self):
        for item in self.addon_tree.get_children():
            self.addon_tree.delete(item)
        if not self.base_path or not self.keystore:
            return
        try:
            loaded = [load_container(p, self.keystore) for p in self.add_paths]
            _, updates, dlcs = group_contents(loaded)
            for g in updates + dlcs:
                kind = "Update" if g.kind == "update" else "DLC"
                source = g.entries[0].path.name
                self.addon_tree.insert("", "end", values=(kind, f"{g.title_id:016X}", f"v{g.version}",
                                                          human_size(g.total_size), source))
        except Exception as exc:
            self.log_line(f"Could not read add-on: {exc}")
        self.after_idle(lambda: autosize_tree(
            self.addon_tree,
            {"kind": 110, "title": 300, "version": 110, "size": 120, "file": 900},
            min_width=60))

    def choose_out(self):
        p = self._pick_file(
            "Choose output folder", mode="directory",
            initialdir=Path(self.out_var.get()).expanduser() if self.out_var.get() else Path.home(),
        )
        if p:
            self.out_var.set(str(p))
            self._save_settings()

    def choose_inspect(self):
        p = self._pick_file(
            "Select container",
            filetypes=[("Switch containers (*.xci *.nsp)", "*.xci *.nsp"),
                       ("All files (*.*)", "*")],
            initialdir=self._rom_dir(),
        )
        if p:
            self.inspect_var.set(str(p))

    def open_output(self):
        target = self.last_output.parent if self.last_output else Path(self.out_var.get())
        if target.exists():
            try:
                if sys.platform.startswith("win"):
                    os.startfile(target)  # type: ignore[attr-defined]
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", str(target)])
                else:
                    subprocess.Popen(["xdg-open", str(target)])
            except Exception as exc:
                messagebox.showerror("Open folder", str(exc))

    # ---------- operations ----------
    def log_line(self, text: str):
        self.log_lines.append(text)
        if self.log_window is not None and self.log_window.winfo_exists():
            widget = getattr(self.log_window, "text", None)
            if widget is not None:
                widget.configure(state="normal")
                widget.insert("end", text + "\n")
                widget.see("end")
                widget.configure(state="disabled")

    def show_log(self):
        if self.log_window is not None and self.log_window.winfo_exists():
            self.log_window.lift()
            return
        win = tk.Toplevel(self)
        win.title("SAK Modern — activity log")
        win.geometry("820x420")
        win.configure(bg=BG)
        text = tk.Text(win, bg=PANEL, fg=FG, insertbackground=FG, relief="flat",
                       font=(MONO, 9), wrap="word")
        text.pack(fill="both", expand=True, padx=12, pady=12)
        text.insert("end", "\n".join(self.log_lines))
        text.configure(state="disabled")
        win.text = text  # type: ignore[attr-defined]
        self.log_window = win

    def _open_inspect_in_partitions(self):
        path = self.inspect_var.get().strip()
        if path:
            self.load_partition_file(Path(path))
            self.show_page("partitions")

    def run_inspect(self):
        path = self.inspect_var.get().strip()
        if not path:
            return
        if not self.require_keys():
            return
        try:
            c = load_container(path, self.keystore)
            bases, updates, dlcs = group_contents([c])
            groups = [*bases, *updates, *dlcs]
        except Exception as exc:
            self.inspect_text.delete("1.0", "end")
            self.inspect_text.insert("end", f"Error: {exc}\n")
            return
        by_id = {g.package_id: g for g in groups}
        for item in self.inspect_tree.get_children():
            self.inspect_tree.delete(item)
        for e in c.ncas:
            n = e.nca
            assert n is not None
            group = by_id.get(e.package_id)
            label = package_label(group) if group else e.kind.upper()
            self.inspect_tree.insert("", "end", values=(label, e.name, n.title_id_hex,
                                                       n.content_type_name, human_size(e.size), n.key_generation))
        total = sum(e.size for e in c.ncas)
        self.inspect_summary.configure(
            text=f"{c.kind.upper()} · {len(c.ncas)} NCAs · {human_size(total)}", foreground=GREEN)
        lines = [f"{c.path.name}"] + [f"! {w}" for w in c.warnings]
        self.inspect_text.delete("1.0", "end")
        self.inspect_text.insert("end", "\n".join(lines) + "\n")
        self.after_idle(lambda: autosize_tree(
            self.inspect_tree,
            {"package": 140, "nca": 600, "title": 260, "type": 150, "size": 120, "keygen": 100},
            min_width=60))

    def run_verify(self):
        path = self.inspect_var.get().strip()
        if not path:
            return
        if not self.require_keys():
            return
        try:
            rep = verify_container(path, self.keys_path)
        except Exception as exc:
            self.inspect_text.delete("1.0", "end")
            self.inspect_text.insert("end", f"Error: {exc}\n")
            return
        lines = [f"VERIFY  {rep.path.name}", ""]
        lines += [f"  {d}" for d in rep.details]
        lines += [f"  ! {w}" for w in rep.warnings]
        lines += [f"  ERROR: {e}" for e in rep.errors]
        lines.append("")
        lines.append("OK" if rep.ok else "FAILED")
        self.inspect_text.delete("1.0", "end")
        self.inspect_text.insert("end", "\n".join(lines) + "\n")
        self.after_idle(lambda: autosize_tree(
            self.inspect_tree,
            {"package": 140, "nca": 600, "title": 260, "type": 150, "size": 120, "keygen": 100},
            min_width=60))

    def start_build(self):
        if not self.require_keys():
            return
        if not self.base_path:
            messagebox.showinfo("Base game", "Choose a base game first.")
            return
        out_dir = Path(self.out_var.get()).expanduser()
        if not str(out_dir):
            messagebox.showinfo("Output", "Choose an output folder.")
            return
        self.build_btn.configure(state="disabled")
        self._progress_value.set(0)
        self.log_lines.clear()
        if self.log_window is not None and self.log_window.winfo_exists():
            widget = getattr(self.log_window, "text", None)
            if widget is not None:
                widget.configure(state="normal")
                widget.delete("1.0", "end")
                widget.configure(state="disabled")
        self.status.configure(text="Planning…")

        def worker():
            try:
                paths = [self.base_path, *self.add_paths]
                plan = make_plan(paths, self.keystore)
                for w in plan.warnings:
                    self.events.put(("log", f"! {w}"))
                self.events.put(("log", f"Base {plan.title_id:016X} · update v{plan.version} · {len(plan.dlcs)} DLC"))
                fmt = self.format_var.get()
                builder = build_xci if fmt == "xci" else build_nsp
                name = self.name_var.get().strip() or None

                def progress(done, total, label):
                    pct = done * 100.0 / max(total, 1)
                    self.events.put(("progress", pct, f"{label}: {human_size(done)} / {human_size(total)}"))

                out = builder(plan, self.keystore, out_dir, name=name, progress=progress)
                self.events.put(("done", out))
            except Exception as exc:
                self.events.put(("error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _poll_events(self):
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "log":
                    self.log_line(event[1])
                elif event[0] == "progress":
                    self._progress_value.set(event[1])
                    self.status.configure(text=event[2])
                elif event[0] == "done":
                    self.last_output = event[1]
                    self._progress_value.set(100)
                    self.status.configure(text=f"Done — {event[1].name}", foreground=GREEN)
                    self.log_line(f"Wrote {event[1]}")
                    self.build_btn.configure(state="normal")
                elif event[0] == "error":
                    self.status.configure(text="Failed", foreground=RED)
                    self.log_line(f"Error: {event[1]}")
                    self.build_btn.configure(state="normal")
                    messagebox.showerror("Bundle failed", event[1])
                elif event[0] == "pkg_progress":
                    self._pkg_progress.set(event[1])
                    self.pkg_status.configure(text=event[2], foreground=FG)
                elif event[0] == "pkg_done":
                    self.pkg_extract_btn.configure(state="normal")
                    self.pkg_extract_all_btn.configure(state="normal")
                    self._pkg_progress.set(100)
                    if event[1]:
                        self.pkg_last_output = event[1][0].path
                        self.pkg_status.configure(text=f"Extracted {len(event[1])} package(s)", foreground=GREEN)
                        for item in event[1]:
                            self.log_line(f"Extracted {item.label}: {item.path}")
                    else:
                        self.pkg_status.configure(text="Nothing selected", foreground=YELLOW)
                elif event[0] == "pkg_error":
                    self.pkg_extract_btn.configure(state="normal")
                    self.pkg_extract_all_btn.configure(state="normal")
                    self.pkg_status.configure(text="Extraction failed", foreground=RED)
                    self.log_line(f"Extraction error: {event[1]}")
                    messagebox.showerror("Extraction failed", event[1])
        except queue.Empty:
            pass
        self.after(100, self._poll_events)

    def _on_close(self):
        try:
            self._save_settings()
        except Exception:
            pass
        self.destroy()


def main() -> int:
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
