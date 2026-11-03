# File picker

SAK Modern uses its own dark, Dolphin-style picker instead of the small Tk
`askopenfilename` dialog.

- Places sidebar: Home, Downloads, Switch ROMs, Emulation, `/home`, `/mnt`,
  `/media`, and `/`.
- Back / forward / up / home / refresh buttons, plus an editable path bar.
- Columns: **Name, Size, Modified, Type, Permissions**.
- Sort by clicking a column heading.
- Hidden dot-files are hidden by default; **Show hidden** reveals them.
- **Filter** narrows the current folder by name.
- **File type** limits the list to Switch containers, keys, or all files.
- Selecting a row shows its full path and details in the info pane.
- `Ctrl+L` focuses the path bar, `Backspace` goes up, `Enter` opens, `Esc`
  cancels.
- Multiple selection is supported for update/DLC import.

The dialog sizes itself from the desktop resolution and only opens at readable
sizes.

![File picker](file-picker.png)
