# Library migration results

Run date: 2026-11-02/03. Root:
`$SWITCH_LIBRARY`

## Input

- 547 `.xci`/`.nsp` files
- ~1.86 TiB
- 78 subdirectories
- 70 title families with separate update/DLC files

## Output

- **73 merged files created**
- **344.41 GiB** of merged output
- **435 container files** in the library root
- **1 remaining subdirectory** (`Dusk Diver 2`, see below)
- No `.sak-*` temporary files left behind

Verification:

- All **73** newly created files pass `sak-modern verify`.
- Independent content checks with the Rust NSCB backend confirmed base/update/DLC
  counts for Dusk Diver, Spirit Hunter, SD Gundam, Bayonetta 3, Gundam Breaker 4,
  Pokémon Legends: Z-A, Smelter, and Tears of the Kingdom.
- 5 pre-existing base-only files failed structural checks and were left
  untouched; they were not produced by this migration.


## Remaining folder

A `Dusk Diver 2/` folder remains with an update NSP whose internal title ID
does not match the installed base. It was deliberately left intact. Metadata:


`01003980174BC800` vs installed base `01008AF016240000`.

Merging it would create a mismatched update. The folder was left intact for
manual review.

## Validation bug found during this run

Ryujinx rejected an early rebuilt XCI with
`ResultFsNonRealDataVerificationFailed ... Hash error!`. The cause was the
writer emitting each NCA header and then copying from the start of the source
NCA instead of skipping the source header. That shifted all section data by
`0xC00`. Fixed in `_write_entry`; a regression test now asserts
`output == header + source[0xC00:]`. Rebuilt NCAs compare byte-for-byte with
their source NCAs, and the Ryujinx CNMT hash error is gone.
