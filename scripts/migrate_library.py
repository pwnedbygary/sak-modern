#!/usr/bin/env python3
"""Inventory and merge a Switch ROM library, one title family at a time.

Default is a read-only plan. --apply writes merged files, verifies them, then
removes the source files for that family. A manifest is kept beside the library.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sak_modern.core.bundle import build_nsp, build_xci, default_name, estimate_xci, make_plan
from sak_modern.core.errors import SakError
from sak_modern.core.keys import KeyStore
from sak_modern.core.verify import verify_container

TID_RE = re.compile(r"\[([0-9A-Fa-f]{16})\]")
V_RE = re.compile(r"\[v(\d+)\]", re.IGNORECASE)
VIDEO_EXT = {".xci", ".nsp"}


def family_of(tid: str | None) -> str | None:
    if not tid or len(tid) != 16:
        return None
    return tid[:12] + "0000"


def classify(name: str, tid: str | None) -> str:
    if tid:
        low = int(tid[-3:], 16)
        if low == 0x800:
            return "update"
        if low not in (0x000, 0x800):
            return "dlc"
    if re.search(r"\[(UPD|UPDATE|Update)\]|\bupdate\b", name, re.IGNORECASE):
        return "update"
    if re.search(r"\[DLC|\(1G\+[^)]*D\)|\bDLC\b", name, re.IGNORECASE):
        return "dlc"
    return "base"


def version_of(name: str) -> int:
    m = V_RE.search(name)
    return int(m.group(1)) if m else 0


def unique_path(parent: Path, name: str, reserved: set[Path]) -> Path:
    p = parent / name
    if p not in reserved and not p.exists():
        return p
    stem, suffix = p.stem, p.suffix
    for i in range(2, 10000):
        q = parent / f"{stem} ({i}){suffix}"
        if q not in reserved and not q.exists():
            return q
    raise RuntimeError(f"cannot find a free filename for {name}")


def atomic_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=str(Path.home() / "switch"))
    ap.add_argument("--keys", default=None)
    ap.add_argument("--out-dir", default=None, help="default: --root")
    ap.add_argument("--apply", action="store_true", help="write and delete sources after verification")
    ap.add_argument("--no-delete", action="store_true", help="with --apply, keep source files")
    ap.add_argument("--flatten-only", action="store_true", help="move base-only files out of subfolders")
    ap.add_argument("--only", action="append", default=[], help="family ID prefix to process (repeatable)")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    root = Path(args.root).expanduser().resolve()
    out_dir = Path(args.out_dir or root).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    ks = KeyStore.load(args.keys)

    files = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in VIDEO_EXT)
    rows = []
    groups: dict[str, list[dict]] = defaultdict(list)
    for p in files:
        tid_m = TID_RE.search(p.name)
        tid = tid_m.group(1).upper() if tid_m else None
        fam = family_of(tid)
        kind = classify(p.name, tid)
        row = {
            "path": str(p), "name": p.name, "dir": str(p.parent), "ext": p.suffix.lower(),
            "tid": tid, "family": fam, "kind": kind, "version": version_of(p.name),
            "size": p.stat().st_size,
        }
        rows.append(row)
        if fam:
            groups[fam].append(row)

    selected = []
    for fam, items in groups.items():
        if not any(i["kind"] != "base" for i in items):
            continue
        if len(items) < 2:
            continue
        if args.only and not any(fam.startswith(x) for x in args.only):
            continue
        selected.append((fam, items))
    selected.sort(key=lambda kv: sum(i["size"] for i in kv[1]))

    print(f"root: {root}")
    print(f"files: {len(files)}  families: {len(groups)}  multi-file add-on families: {len(selected)}")
    print(f"add-on input bytes: {sum(i['size'] for _, items in selected for i in items) / 2**40:.2f} TiB")
    if args.limit:
        selected = selected[: args.limit]

    manifest_path = root / "_sak-modern-migration.json"
    manifest = {
        "started": datetime.now(timezone.utc).isoformat(),
        "root": str(root),
        "items": [],
        "moved": [],
        "errors": [],
    }
    if args.apply:
        atomic_json(manifest_path, manifest)

    processed = 0
    for fam, items in selected:
        base_candidates = [i for i in items if i["kind"] == "base"]
        if not base_candidates:
            print(f"SKIP {fam}: no base file")
            continue
        # Prefer a gamecard XCI, then the largest base.
        base_candidates.sort(key=lambda i: (i["ext"] != ".xci", -i["size"]))
        base = base_candidates[0]
        addons = [i for i in items if i["path"] != base["path"]]
        paths = [Path(base["path"]), *[Path(i["path"]) for i in addons]]
        print(f"\n[{fam}] base={Path(base['path']).name} addons={len(addons)}")
        try:
            plan = make_plan(paths, ks)
        except Exception as exc:
            print(f"  SKIP plan: {exc}")
            manifest["errors"].append({"family": fam, "error": str(exc)})
            continue

        fmt = "xci" if base["ext"] == ".xci" else "nsp"
        try:
            if fmt == "xci":
                estimate, card, cap = estimate_xci(plan, ks)
                if estimate > 32 << 30:
                    raise SakError(f"bundle needs {estimate / 2**30:.2f} GiB, over the 32 GiB card limit")
            else:
                estimate = plan.total_input_size
                card = cap = 0
            free = shutil.disk_usage(out_dir).free
            if free < estimate + (2 << 30):
                raise SakError(
                    f"not enough free space: need {estimate / 2**30:.2f} GiB + 2 GiB, have {free / 2**30:.2f} GiB"
                )
        except Exception as exc:
            print(f"  SKIP space/size: {exc}")
            manifest["errors"].append({"family": fam, "error": str(exc)})
            continue

        print(f"  plan: {plan.title_id:016X} v{plan.version} update={bool(plan.update)} dlc={len(plan.dlcs)} "
              f"format={fmt} estimate={estimate / 2**30:.2f} GiB card={card:#x}")
        if not args.apply:
            manifest["items"].append({
                "family": fam, "status": "planned", "format": fmt, "estimate": estimate,
                "sources": [str(p) for p in paths], "output": str(out_dir / default_name(plan, fmt)),
            })
            continue

        temp_name = f".sak-tmp-{uuid.uuid4().hex}.{fmt}"
        temp = out_dir / temp_name
        builder = build_xci if fmt == "xci" else build_nsp
        try:
            builder(plan, ks, out_dir, name=temp_name)
            rep = verify_container(temp, ks)
            if not rep.ok:
                raise SakError("verification failed: " + "; ".join(rep.errors))
        except Exception as exc:
            print(f"  FAILED: {exc}")
            temp.unlink(missing_ok=True)
            manifest["errors"].append({"family": fam, "error": str(exc)})
            atomic_json(manifest_path, manifest)
            continue

        try:
            final = unique_path(out_dir, default_name(plan, fmt), {p for p in paths})
            sources = list(paths)
            # Publish the verified output before removing any source, so a rename
            # failure can never leave the family with neither form.
            os.replace(temp, final)
        except Exception:
            temp.unlink(missing_ok=True)
            raise
        deleted = []
        if not args.no_delete:
            for src in sources:
                try:
                    if src.exists() and src.resolve().is_relative_to(root) and src != final:
                        src.unlink()
                        deleted.append(str(src))
                except Exception as exc:
                    manifest["errors"].append({"family": fam, "delete": str(src), "error": str(exc)})
        # Remove now-empty source directories, but never the library root.
        for src in sources:
            d = src.parent
            while d != root and d.is_relative_to(root):
                try:
                    d.rmdir()
                except OSError:
                    break
                d = d.parent
        manifest["items"].append({
            "family": fam, "status": "ok", "format": fmt, "output": str(final),
            "size": final.stat().st_size, "sources": deleted or [str(p) for p in sources],
            "deleted": bool(deleted), "verify": "OK",
        })
        atomic_json(manifest_path, manifest)
        processed += 1
        print(f"  OK -> {final.name} ({final.stat().st_size / 2**30:.2f} GiB), deleted {len(deleted)} sources")

    if args.flatten_only and args.apply:
        families_with_addons = {
            fam for fam, items in groups.items() if any(i["kind"] != "base" for i in items)
        }
        for row in rows:
            if row["kind"] != "base" or Path(row["dir"]) == root:
                continue
            if row.get("family") in families_with_addons:
                # Never separate a base from its update/DLC if that family was
                # skipped or failed; leave the folder intact for inspection.
                continue
            src = Path(row["path"])
            if not src.exists():
                continue
            try:
                final = unique_path(root, src.name, set())
                os.replace(src, final)
                manifest["moved"].append({"from": str(src), "to": str(final)})
                d = src.parent
                while d != root and d.is_relative_to(root):
                    try:
                        d.rmdir()
                    except OSError:
                        break
                    d = d.parent
            except Exception as exc:
                manifest["errors"].append({"move": str(src), "error": str(exc)})
        atomic_json(manifest_path, manifest)

    manifest["finished"] = datetime.now(timezone.utc).isoformat()
    if args.apply:
        atomic_json(manifest_path, manifest)
    print(f"\ndone: {processed} merged, {len(manifest['errors'])} errors")
    return 0 if not manifest["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
