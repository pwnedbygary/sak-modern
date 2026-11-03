# Verification log

Date: 2026-11-02. Host: CachyOS, Python 3.12, 64-bit.

## Automated tests

```text
$ pytest -q
7 passed
```

Coverage: PFS0 layout/alignment, HFS0 hash layout, gamecard size boundaries,
NCA header rewrite, ticket offsets, update replacement, DLC replacement.

## Real-content remux checks

Keys were supplied locally and are not part of the repository.

### Dusk Diver (base XCI + update + 4 DLC)

| check | result |
|---|---|
| inputs | base `0100B2B00E7AA000`, update `v393216`, DLC `005/006/007/008` |
| output XCI | 3,614,426,624 bytes, 18 NCAs |
| output NSP | 3,614,378,752 bytes, 28 PFS0 entries (18 NCAs + 5 tickets + 5 certs) |
| card size | `0xF0` (4 GiB) |
| verify | OK |
| update replacement | re-bundled merged XCI + newer update; 20 entries, no duplicate update |

The XCI's DLC NCA key-area slot matches the old SAK output byte-for-byte
(`c07b7c28b7e318792a79f915e6be1a43` for `7edf1a4d...nca`).

### Bayonetta 3 (base XCI + update)

| check | result |
|---|---|
| inputs | base `01004A4010FEA000`, update `v131072` |
| output XCI | 17,085,348,864 bytes, 10 NCAs |
| card size | `0xE1` (16 GiB) |
| free headroom | ~94 MiB before the next card class |
| verify | OK |

### Package extraction

A merged Transiruby XCI was split through the GUI and the CLI. The three
outputs were independently verified:

```text
Transiruby [0100A86014332000] [BASE].xci          OK (4 NCAs)
Transiruby [0100A86014332800] [UPD v131072].nsp   OK (4 entries)
Transiruby [0100A86014333001] [DLC 1].nsp         OK (2 entries)
```

### Ryujinx observation

A merged Dusk Diver XCI was loaded as the base application. Ryujinx then loaded
the update and all four DLCs from the separate NSP files in the configured game
directory. It did not use the update/DLC NCAs embedded in the XCI. This is
normal emulator behavior and is why the app recommends NSP output for emulators.

## File picker and columns

- Replaced the native Tk file dialog with the custom Dolphin-style picker.
- Verified places navigation, path entry, filtering, hidden-file toggle, selection
  details, and auto-sized Name/Size/Modified/Type/Permissions columns.
- Add-on and Inspect tables auto-size after population; wide tables have
  horizontal and vertical scrollbars rather than silently clipping text.
- The app window was checked at 100% on 1920x1080 and 150% on 1920x1080; it is
  capped and centered to the desktop rather than opening at a fixed size.

## Desktop appearance

- Text size 100/115/130/150% and Dark/Light/High-contrast themes were exercised
  in source and in the packaged AppImage.
- The AppImage was rebuilt from PyInstaller using the host Tk 8.6 stack because
  the previous portable-Python Tk 9 build fell back to a fixed bitmap font and
  ignored font scaling.
- 150% high-contrast screenshots were checked for sidebar clipping and a visible
  bottom action bar; the sidebar and window width now scale with text size.
- The Windows executable was rebuilt from the updated GUI and smoke-tested under
  Wine.

## Android APK

- Built with Chaquopy 16.1 + Python 3.12 + `cryptography==42.0.8`.
- Installed and launched on a connected Android 13 / arm64 device.
- Imported a merged Transiruby XCI, showed base/update/DLC in the partition map,
  extracted all three packages, and copied them to `Downloads/SAK-Output`.
- Pulled those Android-produced files back to Linux; all three pass
  `sak-modern verify`.
- Release APK is signed with the local development key
  `CN=SAK Modern` (SHA-256
  `23a7ad798b42a0ff77b012f2ce649d8738b1f0dc4e8b83d4c93761b13d90ac78`).

## Desktop artifacts

- `SAK-Modern-x86_64.AppImage` (66 MiB) launches under Xvfb; self-contained
  Python 3.12 + Tk + cryptography.
- `SAK-Modern.exe` (16 MiB) launches under Wine/Xvfb; built with PyInstaller
  from the same source.
