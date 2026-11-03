# Partition workspace

The **Partitions** tab is a disk-editor-style view of an already-merged file.

1. Click **Import file…** and choose an XCI or NSP. The file can contain a base,
   one or more updates, and many DLC packages.
2. The header shows container type, package count, and total size.
3. The partition map draws each package proportionally:
   - blue = base
   - amber = update
   - green/purple/teal = DLC
4. Click a block to select it. Drag a block down into the dashed **Extraction
   tray** (or double-click it) to add that package.
5. Choose an output folder and press **Extract selected** or **Extract all
   packages**.

Base output keeps the source format by default (XCI → XCI, NSP → NSP). Updates
and DLC are written as standalone NSPs so their tickets/rights IDs are preserved.
The **Base format** selector can force the base to XCI or NSP.

The same operation is available without the GUI:

```bash
sak-modern extract "Game (1G+1U+4D).xci" \
  -o ~/SAK-Output --keys ~/.switch/prod.keys
```

List packages without writing:

```bash
sak-modern extract "Game (1G+1U+4D).xci" --list --keys ~/.switch/prod.keys
```
