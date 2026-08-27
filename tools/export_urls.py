#!/usr/bin/env python3
"""Export one url per paper -- the whole archive as a plain list.

Retired rows are excluded: they are duplicates and dead entries the site
never shows, and a list that includes them misrepresents the archive's size.

    python tools/export_urls.py                     # every live paper
    python tools/export_urls.py --source ssrn       # one source
    python tools/export_urls.py --format json       # url + uid + title
"""
import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import store                                        # noqa: E402

OUT = pathlib.Path("export")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="",
                    help="substring match on source/doi prefix, e.g. ssrn, nber, arxiv")
    ap.add_argument("--format", choices=("txt", "json", "csv"), default="txt")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    con = store.connect()
    rows = []
    for uid, title, url, meta in con.execute(
            "SELECT uid, title, url, meta FROM items"):
        try:
            m = json.loads(meta) or {}
        except Exception:                            # noqa: BLE001
            m = {}
        if m.get("retired"):
            continue
        doi = (m.get("doi") or "") or (uid[4:] if uid.startswith("doi:") else "")
        src = m.get("source") or ""
        if args.source:
            q = args.source.lower()
            if q not in src.lower() and q not in doi.lower() and q not in uid.lower():
                continue
        # a paper without a url still has a DOI often enough to be worth one
        u = url or (f"https://doi.org/{doi}" if doi else "")
        if not u:
            continue
        rows.append({"url": u, "uid": uid, "title": title or "", "source": src})

    OUT.mkdir(parents=True, exist_ok=True)
    stem = f"urls{'_' + args.source if args.source else ''}"
    dest = pathlib.Path(args.out) if args.out else OUT / f"{stem}.{args.format}"
    if args.format == "txt":
        dest.write_text("\n".join(r["url"] for r in rows) + "\n", encoding="utf-8")
    elif args.format == "json":
        dest.write_text(json.dumps(rows, indent=1, ensure_ascii=False), encoding="utf-8")
    else:
        import csv                                   # noqa: PLC0415
        with open(dest, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=["url", "uid", "title", "source"])
            w.writeheader()
            w.writerows(rows)
    print(f"[urls] {len(rows):,} papers -> {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
