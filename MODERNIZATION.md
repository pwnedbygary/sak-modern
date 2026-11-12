# SAK Modernization Notes

This document records what was recovered from the shipped SAK 0.7.14 binary and
what the replacement does differently. It is intentionally explicit about what
was observed versus what is inferred.

## 1. Recovering the original behavior

`SAK.exe` is an AutoIt v3 compiled script, not a .NET application. The script was
recovered from the 64-bit binary with `autoit-ripper` (EA06). The recovered
`script.au3` is 894,698 bytes / 19,001 lines; application code starts after the
bundled AutoIt UDFs.

Relevant facts from the script:

- version `0.7.14.0`, product “Switch Army Knife by kempa”;
- external tools are expected under `bin/` (`4nxci`, `hacbrewpack`, `hactool`,
  `hactoolnet`, `renxpack`, `squirrel`);
- the update function writes `squirrel/list.txt` containing the base file followed
  by every selected NSP/NSZ, then runs:
  ```text
  squirrel.exe -b 65536 -pv false -kp false --RSVcap 268435656 \
    -fat exfat -fx files -ND true -t xci|nsp -o ...\squirrel \
    -tfile ...\squirrel\list.txt -roma TRUE -dmul calculate
  ```
- changelog entries show NSC_BUILDER was reverted to **0.98b** before release.

`squirrel.exe` is a PyInstaller bundle (Python 3.7). Its extracted `squirrel.pyc`
and `Fs/` modules confirm the PFS0/HFS0/NCA handling and the `get_xciheader`
builder. The old pipeline is therefore a Windows GUI around a 2019-era
NSC_BUILDER.

## 2. Reproduction

The old `squirrel.exe` was run under Wine with the user's local keys and the two
example sets. The same arguments as SAK were used.

### Dusk Diver

Inputs:

- base `0100B2B00E7AA000` XCI, 3,210,989,056 bytes;
- update `0100B2B00E7AA800` NSP, 368,674,240 bytes;
- four DLC NSPs `0100B2B00E7AB005/6/7/8`.

Result: 3,614,426,624-byte XCI, 18 NCAs. Decrypting the output NCA headers shows:

- every added NCA has `rights_id = 0`;
- the title key is rewrapped in all four key-area slots;
- there are no `.tik`/`.cert` entries in the secure partition.

That conversion math was reproduced independently. For the “Welcome Summer
Swimsuits” NCA, the old output key-area slot is:

```text
c07b7c28b7e318792a79f915e6be1a43
```

The new implementation produces the same value from the ticket and key
generation. This confirms the old container is not corrupt; it is a ticketless
“super XCI”.

Ryujinx loaded the XCI as the base, then loaded the update and all four DLCs from
the separate NSP files in the library. The embedded update/DLC was not used.
This is expected emulator behavior, and it explains a “DLC doesn’t work” result
when testing an XCI in Ryujinx.

### Bayonetta 3

Inputs:

- base `01004A4010FEA000` XCI, 15,971,909,632 bytes;
- update `01004A4010FEA800` NSP, 2,241,292,222 bytes.

The old flow produced a 17,085,348,864-byte XCI on card size `0xE1` (16 GiB).
It fits with about 94 MiB to spare. This rules out a simple “over 16 GiB” failure.

Both failing NSPs have PFS0 data starts that are not 512-byte aligned:

| file | PFS0 data start |
|---|---:|
| Bayonetta 3 update | `0x1FE` |
| Dusk Diver “Welcome Summer Swimsuits” | `0x110` |

The old reader uses absolute entry offsets, so it can still read these, but the
assumption is fragile. The new writer always aligns every output entry to 512
bytes.

## 3. What actually breaks

The evidence supports three separate issues, not one:

1. **Output-model mismatch.** A ticketless multi-content XCI is not the same
   thing as installed update/DLC. Emulators do not read add-on content from an
   XCI. If the user's test target is Ryujinx/Yuzu, no amount of XCI repacking will
   make the embedded DLC appear. The correct artifact is a merged NSP (tickets
   preserved), or separate installed content.
2. **Ticketless DLC licensing.** On hardware, a ticketless DLC NCA can be
   installed with sigpatches, but it is not “owned” in the same way as a
   ticketed DLC. The old SAK output has no tickets by construction. NSP output
   from SAK Modern keeps them.
3. **Legacy 0.98b multi-content handling.** The old `-dmul` path flattens all
   inputs into one list and relies on filename/title heuristics. It has known
   cases where later DLC entries are ignored, and it can retain stale ticket or
   cert bytes. The new planner keeps a package identity per CNMT, selects the
   highest version per title, and replaces rather than appends.

The 16 GiB boundary is real but Bayonetta 3 fits. The app still sizes cards
correctly for 8/16/32 GiB and refuses >32 GiB.

## 4. New pipeline

For each input:

1. Detect PFS0 (NSP) or XCI (`HEAD` at `0x100`).
2. Parse container headers and NCA headers (AES-XTS, header key, big-endian
   sector tweak).
3. Split NCAs into packages at each `.cnmt.nca`, using the CNMT's title ID to
   classify base/update/DLC. This works for both CNMT-last Nintendo layouts and
   CNMT-first layouts produced by some tools.
4. Select one base package; select the highest-version update; select one
   package per DLC title ID. Supplied files win ties over existing merged
   content.
5. For XCI output, convert rights-based NCAs to ticketless key-area form and
   write a new secure HFS0. For NSP output, preserve rights and copy tickets,
   certs, and XML.
6. Emit a 512-byte-aligned PFS0/HFS0, compute HFS0 SHA-256 entries and the XCI
   root hash, and choose the smallest valid gamecard size.

Applying a new update to an existing merged XCI/NSP is the same operation: the
merged file is the base, the new update is an add-on, and the old update package
is replaced. Matching DLC packages are replaced the same way.

## 5. Verification performed

- Synthetic tests: PFS0 alignment/round-trip, HFS0 hash layout, card-size
  boundaries, NCA header rewrite, ticket parsing, update/DLC replacement
  planning (7 tests).
- Dusk Diver: rebuilt XCI and NSP; `verify` reports OK; XCI has 18 NCAs and card
  `0xF0`.
- Bayonetta 3: rebuilt XCI; `verify` reports OK; 10 NCAs, card `0xE1`
  (16 GiB), 17,085,348,864 bytes.
- AppImage and Windows PE both launch under Xvfb/Wine.
- Emulator reconciliation: Ryujinx initially reported
  `ResultFsNonRealDataVerificationFailed ... Hash error!` for a rebuilt XCI.
  The cause was a writer bug: `_write_entry` emitted the new 0xC00 NCA header
  and then copied from the start of the source NCA instead of skipping the
  source header, shifting all section data by 0xC00. A regression test now
  compares `header + source[0xC00:]`; rebuilt NCAs compare byte-for-byte with
  their sources.

## 6. Library migration result

The batch tooling merged **73** title families into the library root and removed
their source files/folders after verification. All 73 new files pass
`sak-modern verify`; the five pre-existing structural failures and the one
unmergeable mismatched update are listed in
[docs/migration-results.md](docs/migration-results.md).

## 7. Remaining limits

- NSZ/XCZ compression is not implemented; those inputs are rejected with a
  clear message.
- Content signatures are not re-signed (impossible without Nintendo keys);
  structural hashes and CFW-compatible headers are produced.
- Full NCA section decryption and CNMT version parsing are not used; versions
  come from filenames and package structure. A future version can read CNMT
  versions directly.

## 8. Switch 2 / NUP (new capability, not recovered)

Nothing in this section comes from the shipped SAK. SAK 0.7.14 predates the
Switch 2 and knows only NSP/XCI. Switch 2 dumps are a different shape: a
decrypted game is a plain `exefs/` + `romfs/` folder, and the single-file form is
**NUP** (NX Package Format).

NUP is an external, published clean-room specification
([a55uka/nup `SPEC.md`](https://github.com/a55uka/nup), v1.0-draft), so
`src/sak_modern/core/nup.py` implements that document rather than any recovered
behaviour. The layout was cross-checked byte for byte against a real 7,283,578,874
byte container before any code was written:

- superblock `NUP\x01`, header 0x80, TOC at 0x80, `toc_size` 0x1180;
- the stored TOC SHA-256 matches `SHA-256` over the whole TOC region;
- `dir_count` 10, `file_count` 41, and the derived children-array length is
exactly 50 slots with a zero remainder against the string-pool offset;
- all 41 extents are 16-byte aligned, every file is raw codec, and
  `sum(sizes) + alignment padding + meta block == file_size`.

The validation loop in `open_nup` enforces the spec's reader rules: magic and
version, `toc_offset == header_size`, `blob_offset` alignment and
non-overlap, `file_size` equality, TOC hash, cross-checked package counts,
bounds-checked string pool and extent ranges, acyclic child graph, and rejection
of reserved `kind`/`codec` values and unknown superblock flag bits. Node names
are treated as labels, never as paths: `..`, drive-letter and NUL names are
refused at extraction time (SPEC.md 6.1.5).

Producer-side choices worth recording:

- The string pool is interned **before** the TOC size is known, because the pool
  length feeds `toc_size`, which feeds `blob_offset`, which feeds every absolute
  extent offset. Names are interned in emission order so the later record-write
  pass hits the cache.
- Extents are deduplicated by `(sha256, size, codec)` across all packages, so a
  base game and its update share unchanged files, and a re-bundle does not grow
  by the size of the old update.
- Files inherited from an existing `.nup` keep their stored extent and are copied
  verbatim, with the SHA-256 re-verified during the copy; they are never
  decompressed and recompressed.
- `zstd` (codec 1) is supported by the reader and available for the writer
  through an optional extra. Files above `memory_cap` stay raw, which is
  conformant because the codec is a per-file field (SPEC.md 4.1).
- Switch 2 NPDMs moved the program ID from `ACI0 + 0x00` to `ACI0 + 0x10`; both
  offsets are tried and validated against the title-ID shape (top byte
  0x01/0x04/0x05 and low three nibbles zero), so a bad read yields 0 rather than
  a plausible wrong title ID.

A folder round trip is byte-identical: `folder -> NUP -> folder -> NUP` produces
the same container bytes, which is asserted in the test suite.

### 8.1 Hardening from independent review

An independent adversarial review of the first snapshot returned
`NEEDS CHANGES`; every finding was reproduced and fixed:

- **Add-on content was lost silently.** Packages were keyed by title ID alone,
  so `dlc/<title-id>/0` and `dlc/<title-id>/1` collapsed into one and a user's
  files disappeared with exit status 0. Packages are now keyed by
  `(title_id, ordinal)` where `ordinal` comes from the index folder, and
  `build_nup` refuses two packages that would occupy the same slot instead of
  dropping one.
- **`toc_size` was allocated before being bounded**, so a hostile container
  produced `MemoryError`. The TOC and `blob_offset` are now bounded against the
  real file length before the read.
- **`string_pool_off < children_offset` was only a warning**, which made the slot
  count negative and raised `struct.error`. It is now an error.
- **`iter_tree` had no depth limit**; a 1000-deep chain raised `RecursionError`.
  Nesting is capped at 256 levels with a `NupError`.
- **Non-UTF-8 file names** escaped as a `UnicodeEncodeError` traceback; they now
  raise `NupError` naming the file.
- **`header_version` was never checked**; 0 is now rejected and a newer version
  is reported as a warning with fields past 0x80 ignored, per SPEC.md 2.
- **A zero-length file's SHA-256 was never verified.** `verify_nup` now compares
  it against the hash of empty content.
- **Partially overlapping extents were undetected.** `verify_nup` sorts extents
  and reports any overlap that is not an identical dedup span.
- **`C:evil` was accepted** as a node name because only a trailing colon was
  refused; any colon is now refused, since Windows joins a drive-relative name
  by discarding the output directory.
- **`--version` was shadowed** by a display version parsed from the folder name;
  an explicit override now wins.

Two behaviours are documented rather than changed: empty directories are not
represented (the container stores files), and `--compression zstd` retains every
compressed payload until the final write.
