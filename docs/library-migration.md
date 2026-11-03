# Library migration

`scripts/migrate_library.py` inventories a Switch ROM directory, merges each
base+update+DLC title family into one file in the library root, verifies the
output, and only then removes the source files for that family.

It is conservative by default:

```bash
# Read-only plan: no files are written or deleted
python scripts/migrate_library.py --keys /path/to/prod.keys

# Apply, but keep the sources for review
python scripts/migrate_library.py --keys /path/to/prod.keys --apply --no-delete

# Apply and delete sources after each verified output
python scripts/migrate_library.py --keys /path/to/prod.keys --apply
```

Useful controls:

- `--root DIR` — library root (default is the local Switch ROM directory).
- `--out-dir DIR` — write outputs somewhere else while testing.
- `--only FAMILY` — restrict to one title family; repeatable.
- `--limit N` — process the first N families by size.
- `--flatten-only` — with `--apply`, move base-only games out of subfolders
  after merging, but never split a family that has update/DLC.
- A manifest is written to `<root>/_sak-modern-migration.json` with source paths,
  outputs, sizes, and verification results.

Safety rules enforced by the script:

1. Build to a temporary name.
2. Run structural verification.
3. Publish the verified output.
4. Delete only the files that were actually used in that bundle.
5. Never delete a source if verification failed.
6. Never delete the library root.

Keys: use the newest `prod.keys` you have. Title-key NCAs from newer firmware
need the corresponding `master_key_XX`/`titlekek_XX`; the script reports a
missing-key error and leaves that family untouched.
