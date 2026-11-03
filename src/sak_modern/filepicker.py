from __future__ import annotations

import fnmatch
import os
import stat
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk
import tkinter as tk
import tkinter.font as tkfont

from . import gui as ui


def autosize_tree(tree: ttk.Treeview, max_widths: dict[str, int], min_width: int = 60) -> None:
    """Size table columns to their content without requiring manual dragging."""
    try:
        font = tkfont.nametofont("TkDefaultFont")
        bold = tkfont.Font(font=font)
        bold.configure(weight="bold")
    except Exception:
        font = bold = None
    for col in tree["columns"]:
        limit = max_widths.get(col, 400)
        width = min_width
        try:
            width = max(width, bold.measure(str(tree.heading(col, "text"))) + 28)
        except Exception:
            pass
        for iid in tree.get_children():
            try:
                text = str(tree.set(iid, col))
            except Exception:
                continue
            if font is not None:
                width = max(width, font.measure(text) + 26)
            else:
                width = max(width, len(text) * 8 + 20)
        tree.column(col, width=min(limit, width), stretch=False)


class FilePickerDialog(tk.Toplevel):
    """A dark, Dolphin-style file/folder picker.

    Returns a Path for single selection, a list[Path] for multiple files, or
    None after Cancel.
    """

    def __init__(
        self,
        parent: tk.Misc,
        *,
        title: str = "Select file",
        initialdir: str | Path | None = None,
        filetypes: list[tuple[str, str]] | None = None,
        multiple: bool = False,
        mode: str = "open",
    ):
        super().__init__(parent)
        self.parent = parent
        self.mode = mode
        self.multiple = multiple
        self.filetypes = filetypes or [("All files", "*")]
        self.result = None
        self._paths: dict[str, Path] = {}
        self._history: list[Path] = []
        self._history_index = -1
        self._sort_col = "name"
        self._sort_reverse = False
        self._iid = 0

        u = ui
        self.bg = u.BG
        self.panel = u.PANEL
        self.panel2 = u.PANEL_2
        self.border = u.BORDER
        self.fg = u.FG
        self.muted = u.MUTED
        self.accent = u.ACCENT

        self.title(title)
        self.configure(bg=self.bg)
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        width = min(1180, max(860, int(sw * 0.86)))
        height = min(760, max(560, int(sh * 0.78)))
        self.geometry(f"{width}x{height}")
        self.minsize(min(860, width), min(560, height))
        self.transient(parent)
        self._build(title)
        self._bind_keys()
        start = Path(initialdir).expanduser() if initialdir else Path.home()
        if start.is_file():
            start = start.parent
        if not start.is_dir():
            start = Path.home()
        self.navigate(start, record=False)
        self.after(50, self._center)

    def _center(self) -> None:
        try:
            self.update_idletasks()
            px = self.parent.winfo_rootx()
            py = self.parent.winfo_rooty()
            pw = self.parent.winfo_width()
            ph = self.parent.winfo_height()
            w, h = self.winfo_width(), self.winfo_height()
            self.geometry(f"+{px + max(0, (pw - w) // 2)}+{py + max(0, (ph - h) // 2)}")
        except Exception:
            pass

    # ---------------------------------------------------------------- UI ----

    def _build(self, title: str) -> None:
        root = tk.Frame(self, bg=self.bg)
        root.pack(fill="both", expand=True, padx=12, pady=10)

        head = tk.Frame(root, bg=self.bg)
        head.pack(fill="x", pady=(0, 8))
        tk.Label(head, text=title, bg=self.bg, fg=self.fg,
                 font=(ui.FONT, 16, "bold")).pack(side="left")
        tk.Label(head, text="  Name / Size / Modified / Type / Permissions", bg=self.bg,
                 fg=self.muted, font=(ui.FONT, 9)).pack(side="left")

        bar = tk.Frame(root, bg=self.panel)
        bar.pack(fill="x", pady=(0, 6))
        for text, cmd in (("←", self.go_back), ("→", self.go_forward),
                          ("↑", self.go_up), ("⌂", self.go_home), ("⟳", self.refresh)):
            b = tk.Button(bar, text=text, command=cmd, bg=self.panel2, fg=self.fg,
                          activebackground=self.border, activeforeground=self.fg,
                          relief="flat", bd=0, padx=10, pady=5, font=(ui.FONT, 12),
                          highlightthickness=0, takefocus=0)
            b.pack(side="left", padx=(4, 0), pady=4)
        self.path_var = tk.StringVar()
        path_entry = tk.Entry(bar, textvariable=self.path_var, bg=self.panel2, fg=self.fg,
                              insertbackground=self.fg, relief="flat", font=(ui.FONT, 10))
        path_entry.pack(side="left", fill="x", expand=True, padx=8, pady=5, ipady=4)
        path_entry.bind("<Return>", lambda _e: self.navigate(self.path_var.get()))
        tk.Button(bar, text="Go", command=lambda: self.navigate(self.path_var.get()),
                  bg=self.accent, fg="#ffffff", relief="flat", bd=0, padx=14, pady=5,
                  font=(ui.FONT, 10, "bold")).pack(side="left", padx=(0, 4), pady=4)

        body = tk.Frame(root, bg=self.bg)
        body.pack(fill="both", expand=True)

        places = tk.Frame(body, bg=self.panel, width=170)
        places.pack(side="left", fill="y", padx=(0, 8))
        places.pack_propagate(False)
        tk.Label(places, text="Places", bg=self.panel, fg=self.muted,
                 font=(ui.FONT, 9, "bold"), anchor="w").pack(fill="x", padx=10, pady=(10, 6))
        for label, path in self._places():
            b = tk.Button(places, text=label, anchor="w", bg=self.panel, fg=self.fg,
                          activebackground=self.panel2, activeforeground=self.fg,
                          relief="flat", bd=0, padx=10, pady=6, font=(ui.FONT, 10),
                          highlightthickness=0, takefocus=0,
                          command=lambda p=path: self.navigate(p))
            b.pack(fill="x", padx=4, pady=1)

        main = tk.Frame(body, bg=self.bg)
        main.pack(side="left", fill="both", expand=True)
        table_wrap = tk.Frame(main, bg=self.border)
        table_wrap.pack(fill="both", expand=True)
        cols = ("name", "size", "modified", "type", "perms")
        self.tree = ttk.Treeview(table_wrap, columns=cols, show="headings", selectmode="extended")
        headings = {"name": "Name", "size": "Size", "modified": "Modified", "type": "Type", "perms": "Permissions"}
        widths = {"name": 360, "size": 100, "modified": 150, "type": 130, "perms": 120}
        for c in cols:
            self.tree.heading(c, text=headings[c], command=lambda cc=c: self.sort_by(cc))
            self.tree.column(c, width=widths[c], anchor="w", stretch=(c == "name"))
        self.tree.tag_configure("dir", foreground=self.accent)
        self.tree.tag_configure("hidden", foreground=self.muted)
        ysb = ttk.Scrollbar(table_wrap, orient="vertical", command=self.tree.yview)
        xsb = ttk.Scrollbar(table_wrap, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ysb.grid(row=0, column=1, sticky="ns")
        xsb.grid(row=1, column=0, sticky="ew")
        table_wrap.rowconfigure(0, weight=1)
        table_wrap.columnconfigure(0, weight=1)
        self.tree.bind("<Double-1>", self._double_click)
        self.tree.bind("<<TreeviewSelect>>", self._selection_changed)

        self.detail = tk.Label(main, text="Select an item to see its full path and details.", bg=self.panel, fg=self.muted, anchor="w",
                               justify="left", font=(ui.FONT, 9), padx=10, pady=6)
        self.detail.pack(fill="x", pady=(6, 0))

        bottom = tk.Frame(root, bg=self.bg)
        bottom.pack(fill="x", pady=(8, 0))
        filter_row = tk.Frame(bottom, bg=self.bg)
        filter_row.pack(fill="x", pady=(0, 6))
        tk.Label(filter_row, text="Filter", bg=self.bg, fg=self.muted,
                 font=(ui.FONT, 9)).pack(side="left")
        self.filter_var = tk.StringVar()
        filt = tk.Entry(filter_row, textvariable=self.filter_var, bg=self.panel2, fg=self.fg,
                        insertbackground=self.fg, relief="flat", font=(ui.FONT, 9))
        filt.pack(side="left", fill="x", expand=True, padx=8, ipady=3)
        self.filter_var.trace_add("write", lambda *_: self.refresh())
        self.hidden_var = tk.BooleanVar(value=False)
        tk.Checkbutton(filter_row, text="Show hidden", variable=self.hidden_var,
                       command=self.refresh, bg=self.bg, fg=self.muted,
                       activebackground=self.bg, activeforeground=self.fg,
                       selectcolor=self.panel2, font=(ui.FONT, 9)).pack(side="left", padx=(8, 0))

        choose_row = tk.Frame(bottom, bg=self.bg)
        choose_row.pack(fill="x")
        tk.Label(choose_row, text="File name", bg=self.bg, fg=self.muted,
                 font=(ui.FONT, 9)).pack(side="left")
        self.name_var = tk.StringVar()
        name_entry = tk.Entry(choose_row, textvariable=self.name_var, bg=self.panel2, fg=self.fg,
                              insertbackground=self.fg, relief="flat", font=(ui.FONT, 10))
        name_entry.pack(side="left", fill="x", expand=True, padx=8, ipady=4)
        name_entry.bind("<Return>", lambda _e: self.accept())
        self.type_var = tk.StringVar(value=self.filetypes[0][0])
        self.type_combo = ttk.Combobox(choose_row, textvariable=self.type_var, state="readonly",
                                       values=[x[0] for x in self.filetypes], width=28)
        if self.mode != "open":
            self.type_combo.configure(state="disabled")
        self.type_combo.pack(side="left", padx=(0, 8))
        self.type_combo.bind("<<ComboboxSelected>>", lambda _e: self.refresh())
        cancel = tk.Button(choose_row, text="Cancel", command=self.cancel, bg=self.panel2,
                           fg=self.fg, relief="flat", bd=0, padx=18, pady=6,
                           font=(ui.FONT, 10))
        cancel.pack(side="left", padx=(0, 6))
        self.open_btn = tk.Button(choose_row, text="Select folder" if self.mode == "directory" else "Open",
                                  command=self.accept, bg=self.accent, fg="#ffffff",
                                  relief="flat", bd=0, padx=20, pady=6,
                                  font=(ui.FONT, 10, "bold"))
        self.open_btn.pack(side="left")

        self.status = tk.Label(root, text="", bg=self.bg, fg=self.muted, anchor="w",
                               font=(ui.FONT, 9))
        self.status.pack(fill="x", pady=(6, 0))

    def _places(self) -> list[tuple[str, Path]]:
        home = Path.home()
        candidates = [
            ("⌂  Home", home),
            ("↓  Downloads", home / "Downloads"),
            ("▦  Switch ROMs", home / "Mounts/Emulation/Emulation/ROMM Library/roms/switch"),
            ("▦  Emulation", home / "Mounts/Emulation/Emulation"),
            ("⌂  /home", Path("/home")),
            ("⌂  /mnt", Path("/mnt")),
            ("⌂  /media", Path("/media")),
            ("⌂  Root", Path("/")),
        ]
        return [(label, path) for label, path in candidates if path.is_dir()]

    def _bind_keys(self) -> None:
        self.bind("<Escape>", lambda _e: self.cancel())
        self.bind("<Control-l>", lambda _e: self.path_var.set(str(self.cwd)))
        self.bind("<BackSpace>", lambda _e: self.go_up())
        self.bind("<Return>", lambda _e: self.accept())
        self.grab_set()
        self.focus_set()

    # ---------------------------------------------------------- navigation ----

    def navigate(self, path: str | Path, record: bool = True) -> None:
        try:
            p = Path(path).expanduser().resolve()
        except Exception:
            return
        if p.is_file():
            p = p.parent
        if not p.is_dir():
            self.status.configure(text=f"Not a folder: {path}", fg=ui.RED)
            return
        if record and (not self._history or self._history[self._history_index] != p):
            self._history = self._history[: self._history_index + 1]
            self._history.append(p)
            self._history_index = len(self._history) - 1
        self.cwd = p
        self.path_var.set(str(p))
        self.name_var.set("")
        self.refresh()

    def go_back(self) -> None:
        if self._history_index > 0:
            self._history_index -= 1
            self.navigate(self._history[self._history_index], record=False)

    def go_forward(self) -> None:
        if self._history_index < len(self._history) - 1:
            self._history_index += 1
            self.navigate(self._history[self._history_index], record=False)

    def go_up(self) -> None:
        if self.cwd.parent != self.cwd:
            self.navigate(self.cwd.parent)

    def go_home(self) -> None:
        self.navigate(Path.home())

    # ------------------------------------------------------------- contents ----

    def _patterns(self) -> list[str] | None:
        if self.mode != "open":
            return None
        selected = self.type_var.get()
        for label, patterns in self.filetypes:
            if label == selected:
                return patterns.split()
        return ["*"]

    def _matches(self, name: str, patterns: list[str] | None) -> bool:
        if patterns is None:
            return True
        low = name.lower()
        return any(fnmatch.fnmatch(low, p.lower()) for p in patterns)

    def refresh(self) -> None:
        try:
            entries = list(os.scandir(self.cwd))
        except PermissionError:
            self.status.configure(text=f"Permission denied: {self.cwd}", fg=ui.RED)
            return
        except OSError as exc:
            self.status.configure(text=str(exc), fg=ui.RED)
            return
        show_hidden = self.hidden_var.get()
        text_filter = self.filter_var.get().strip().lower()
        patterns = self._patterns()
        rows = []
        for entry in entries:
            name = entry.name
            if not show_hidden and name.startswith("."):
                continue
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
                is_file = entry.is_file(follow_symlinks=False)
            except OSError:
                continue
            if not is_dir and not is_file:
                continue
            if text_filter and text_filter not in name.lower():
                continue
            if is_file and not self._matches(name, patterns):
                continue
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            rows.append((name, is_dir, st))
        reverse = self._sort_reverse
        key = self._sort_col

        def sort_key(row):
            name, is_dir, st = row
            if key == "size":
                value = st.st_size
            elif key == "modified":
                value = st.st_mtime
            elif key == "type":
                value = "Folder" if is_dir else (Path(name).suffix.lower() or "File")
            elif key == "perms":
                value = stat.filemode(st.st_mode)[1:]
            else:
                value = name.lower()
            return (not is_dir, value)

        rows.sort(key=sort_key, reverse=reverse)
        self.tree.delete(*self.tree.get_children())
        self._paths.clear()
        self._iid = 0
        for name, is_dir, st in rows:
            iid = str(self._iid)
            self._iid += 1
            path = self.cwd / name
            self._paths[iid] = path
            kind = "Folder" if is_dir else self._kind(name)
            size = "" if is_dir else ui.human_size(st.st_size)
            modified = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
            perms = stat.filemode(st.st_mode)[1:]
            tags = ("dir",) if is_dir else (("hidden",) if name.startswith(".") else ())
            self.tree.insert("", "end", iid=iid, values=(
                name + ("/" if is_dir else ""), size, modified, kind, perms), tags=tags)
        self.status.configure(
            text=f"{len(rows)} item(s) · {self.cwd}" + ("" if show_hidden else " · hidden files hidden"),
            fg=self.muted)
        self.detail.configure(text="Select an item to see its full path and details.")
        self.after_idle(lambda: autosize_tree(
            self.tree,
            {"name": 420, "size": 100, "modified": 155, "type": 135, "perms": 120},
            min_width=70))

    @staticmethod
    def _kind(name: str) -> str:
        ext = Path(name).suffix.lower()
        if ext in (".xci", ".nsp"):
            return "Switch container"
        if ext in (".nsz", ".xcz"):
            return "Compressed Switch container"
        if ext in (".keys", ".txt", ".dat"):
            return "Keys / text"
        if ext:
            return ext[1:].upper() + " file"
        return "File"

    def sort_by(self, col: str) -> None:
        if self._sort_col == col:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_col = col
            self._sort_reverse = False
        self.refresh()

    def _selection_changed(self, _event=None) -> None:
        selected = self.tree.selection()
        if not selected:
            self.detail.configure(text="")
            return
        path = self._paths.get(selected[0])
        if path is None:
            return
        try:
            st = path.stat()
            if path.is_dir():
                info = f"{path.name}/  ·  folder\n{path}"
            else:
                info = (f"{path.name}  ·  {ui.human_size(st.st_size)}  ·  "
                        f"{datetime.fromtimestamp(st.st_mtime):%Y-%m-%d %H:%M}\n{path}")
            if self.mode == "open" and not path.is_dir():
                self.name_var.set(path.name)
        except OSError:
            info = str(path)
        self.detail.configure(text=info)

    def _double_click(self, _event=None) -> None:
        selected = self.tree.selection()
        if not selected:
            return
        path = self._paths.get(selected[0])
        if path is None:
            return
        if path.is_dir():
            self.navigate(path)
        else:
            self.accept()

    # --------------------------------------------------------------- result ----

    def accept(self) -> None:
        typed = self.name_var.get().strip()
        selected = [self._paths[i] for i in self.tree.selection() if i in self._paths]
        if self.multiple:
            files = [p for p in selected if p.is_file()]
            if len(files) > 1:
                self.result = files
                self.destroy()
                return
        if typed:
            p = Path(typed).expanduser()
            if not p.is_absolute():
                p = self.cwd / p
            if p.is_dir():
                if self.mode == "directory":
                    self.result = p
                    self.destroy()
                    return
                self.navigate(p)
                return
            if p.is_file():
                self.result = p
                self.destroy()
                return
        if self.mode == "directory":
            target = selected[0] if selected and selected[0].is_dir() else self.cwd
            self.result = target
            self.destroy()
            return
        files = [p for p in selected if p.is_file()]
        if self.multiple:
            if files:
                self.result = files
                self.destroy()
            elif selected and selected[0].is_dir():
                self.navigate(selected[0])
            else:
                messagebox.showinfo("Select file", "Select one or more files.", parent=self)
            return
        if files:
            self.result = files[0]
            self.destroy()
        elif selected and selected[0].is_dir():
            self.navigate(selected[0])
        else:
            messagebox.showinfo("Select file", "Select a file.", parent=self)

    def cancel(self) -> None:
        self.result = None
        self.destroy()

    def show(self):
        self.wait_window(self)
        return self.result
