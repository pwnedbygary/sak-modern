import json
from pathlib import Path

from sak_modern.core.bundle import build_nsp, build_xci, make_plan
from sak_modern.core.extract import extract_packages, package_label
from sak_modern.core.keys import KeyStore
from sak_modern.core.model import group_contents, load_container
from sak_modern.core.verify import verify_container


def _keys(path):
    return KeyStore.load(path)


def inspect(path, keys):
    ks = _keys(keys)
    c = load_container(path, ks)
    bases, updates, dlcs = group_contents([c])
    groups = [*bases, *updates, *dlcs]
    packages = []
    for g in groups:
        packages.append({
            "id": g.package_id,
            "kind": g.kind,
            "label": package_label(g),
            "title": f"{g.title_id:016X}",
            "version": g.version,
            "size": g.total_size,
            "ncas": len(g.entries),
        })
    return json.dumps({"type": c.kind, "packages": packages, "path": str(path)})


def extract(path, out_dir, keys, selected_ids, base_format="source"):
    ks = _keys(keys)
    ids = [x for x in selected_ids if x]
    result = extract_packages(path, out_dir, ks, base_format=base_format, selected=ids or None)
    return json.dumps([{
        "label": r.label,
        "path": str(r.path),
        "format": r.format,
        "size": r.path.stat().st_size,
    } for r in result])


def bundle(base, addons, out_dir, fmt, keys):
    ks = _keys(keys)
    plan = make_plan([base, *addons], ks)
    builder = build_xci if fmt == "xci" else build_nsp
    path = builder(plan, ks, Path(out_dir))
    return json.dumps({"path": str(path), "size": path.stat().st_size, "title": f"{plan.title_id:016X}"})


def verify(path, keys):
    rep = verify_container(path, _keys(keys))
    return json.dumps({"ok": rep.ok, "errors": rep.errors, "warnings": rep.warnings,
                       "details": rep.details})
