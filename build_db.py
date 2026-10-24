#!/usr/bin/env python3
"""Build a MiSTer Downloader database from an owner's public, non-archived core repositories.

    python build_db.py --out out                       # discover the owner's repositories (needs GITHUB_TOKEN)
    python build_db.py --out out --repos o/a,o/b       # explicit list, no GitHub API needed

A repository is a core when it is public, not archived, not a fork, and contains at least one
``.rbf`` build (a repository named in ``extra_files`` also counts, for those files). For each one the newest dated build of every core (``Name_YYYYMMDD.rbf``) and all of
its ``.mra`` files are listed in the database. Nothing is copied: each file entry points at
raw.githubusercontent.com pinned to the commit it was read from, with its md5 and size, so the source
repositories stay the single source of truth.

Output in ``--out`` (this is the ``db`` branch of the repository):
    db.json, db.json.zip   the Downloader database (the zip is what downloader.ini points at)
    cores.md               human-readable list of what is in it
    cache.json             git blob sha -> md5, so unchanged files are never downloaded again
    status.json            when the last check ran and when the database last changed

Standard library and git only.
"""
from __future__ import annotations

import argparse
import csv
import io
import datetime as dt
import hashlib
import json
import os
import posixpath
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile

API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"
DATED = re.compile(r"^(?P<stem>.+?)_(?P<date>\d{8})\.rbf$", re.I)

ZIP_ATTR = re.compile(r"""<rom\b[^>]*?\bzip\s*=\s*["']([^"']+)["']""", re.I | re.S)

DEFAULTS = {
    "owner": None,                      # default: GITHUB_REPOSITORY_OWNER
    "include": [],                      # extra "owner/name" repositories, scanned even if forks
    "exclude": [],                      # "name" or "owner/name" to skip
    "include_forks": False,
    "strip_prefix": ["Arcade-"],        # removed from build file names, as the official distribution does
    "categories": {},                   # "owner/name": "_Console" | "_Computer" | "_Utility" | "_Other" | "_Arcade"
    # not part of a release: build output, simulation, docs, and the work-in-progress folders
    # `unsupported` and `_dev` (their MRAs would also pull the MRA root up to the repository root)
    "ignore_paths": r"(^|/)(output_files|sim|docs?|\.github|unsupported|_dev)(/|$)",
    "keep_builds": 1,                   # newest N dated builds kept per core
    "mra_folder": "",                   # put every MRA under _Arcade/<mra_folder>/ ("" = directly in _Arcade)
    # plain files installed at a path from the SD card's root, taken from a repository (which needs no .rbf for this): a list of
    # {"repo": "owner/name", "from": "releases", "files": ["Scripts/x.sh", "games/x/x"]}: <from>/<file> is installed as <file>
    "extra_files": [],
    # ROM zips the MRAs name, listed as external files (not copied; downloaded from the archive.org MAME set)
    "external": {
        "enabled": True,
        "dest": "games/mame",
        "item": "mame-roms-merged_",
        "folder": "MAME ROMs (merged)",
        "fallback_csv": "https://raw.githubusercontent.com/meathax/meatcores/main/external_files.csv",
    },
}


# ---------------------------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------------------------

def api_get(path: str, token: str | None):
    req = urllib.request.Request(API + path, headers={"Accept": "application/vnd.github+json", "User-Agent": "mister-db-builder"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def list_owner_repos(owner: str, token: str | None) -> list[dict]:
    out, page = [], 1
    while True:
        chunk = api_get(f"/users/{owner}/repos?type=owner&per_page=100&page={page}", token)
        out += chunk
        if len(chunk) < 100:
            return out
        page += 1


def select_repos(repos: list[dict], cfg: dict, self_full: str | None = None) -> list[str]:
    """Candidate repositories: public, not archived/disabled, not a fork (unless allowed or listed),
    not excluded, not this repository itself."""
    skip = {x.lower() for x in cfg["exclude"]}
    chosen = []
    for r in repos:
        full, name = r["full_name"], r["name"]
        if self_full and full.lower() == self_full.lower():
            continue
        if full.lower() in skip or name.lower() in skip:
            continue
        if r.get("private") or r.get("archived") or r.get("disabled"):
            continue
        if r.get("fork") and not cfg["include_forks"]:
            continue
        chosen.append(full)
    return chosen


# ---------------------------------------------------------------------------------------------
# git plumbing
# ---------------------------------------------------------------------------------------------

def git(*args: str, cwd: str | None = None, text: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=text, check=True)


def clone(full: str, work: str) -> str:
    d = os.path.join(work, full.replace("/", "__"))
    if not os.path.isdir(os.path.join(d, ".git")):
        # MRAs (small) come with the clone; the build files are fetched only when hashing needs them.
        git("clone", "-q", "--filter=blob:limit=1m", "--no-checkout", "--depth", "1", f"https://github.com/{full}.git", d)
    return d


def tree(d: str) -> list[tuple[str, str, int]]:
    """(path, blob sha, size) for every file at HEAD."""
    out = git("ls-tree", "-r", "-l", "-z", "HEAD", cwd=d).stdout
    files = []
    for rec in out.split("\0"):
        if not rec:
            continue
        meta, path = rec.split("\t", 1)
        mode, typ, sha, size = meta.split()
        if typ == "blob":
            files.append((path, sha, int(size)))
    return files


def md5_blob(d: str, sha: str) -> str:
    data = subprocess.run(["git", "cat-file", "blob", sha], cwd=d, capture_output=True, check=True).stdout
    return hashlib.md5(data).hexdigest()


# ---------------------------------------------------------------------------------------------
# one repository -> files of the database
# ---------------------------------------------------------------------------------------------

def strip_prefix(name: str, prefixes: list[str]) -> str:
    for p in prefixes:
        if name.lower().startswith(p.lower()):
            return name[len(p):]
    return name


def scan_repo(full: str, work: str, cfg: dict, cache: dict) -> dict | None:
    """Everything this repository contributes, or None when it has no ``.rbf`` build."""
    d = clone(full, work)
    head = git("rev-parse", "HEAD", cwd=d).stdout.strip()
    ignore = re.compile(cfg["ignore_paths"], re.I)
    everything = tree(d)
    files = [f for f in everything if not ignore.search(f[0])]
    rbfs = [f for f in files if f[0].lower().endswith(".rbf")]
    extras = [x for x in cfg.get("extra_files") or [] if x["repo"].lower() == full.lower()]
    if not rbfs and not extras:
        return None
    mras = [f for f in files if f[0].lower().endswith(".mra") and f[2] > 0]

    # newest N dated builds per core name (undated builds are their own core)
    cores: dict[str, list[tuple[str, str, str, int]]] = {}
    for path, sha, size in rbfs:
        base = posixpath.basename(path)
        m = DATED.match(base)
        stem, date = (m.group("stem"), m.group("date")) if m else (base[:-4], "")
        cores.setdefault(stem.lower(), []).append((date, path, sha, size))
    builds = []
    for stem, items in cores.items():
        items.sort(key=lambda x: (x[0], -len(x[1])), reverse=True)
        builds += items[: max(1, int(cfg["keep_builds"]))]

    category = cfg["categories"].get(full) or ("_Arcade" if mras else "_Other")
    entries: dict[str, dict] = {}

    def add(dest: str, path: str, sha: str, size: int):
        if sha not in cache:
            cache[sha] = md5_blob(d, sha)
        entries[dest] = {"hash": cache[sha], "size": size,
                         "url": f"{RAW}/{full}/{head}/{urllib.parse.quote(path)}"}

    for date, path, sha, size in builds:
        name = strip_prefix(posixpath.basename(path), cfg["strip_prefix"])
        add(f"{category}/cores/{name}" if category == "_Arcade" else f"{category}/{name}", path, sha, size)

    if mras:
        root = posixpath.commonpath([posixpath.dirname(p) or "." for p, _, _ in mras])
        root = "" if root == "." else root
        top = category if not cfg["mra_folder"] else f"{category}/{cfg['mra_folder'].strip('/')}"
        for path, sha, size in mras:
            rel = path[len(root) + 1:] if root else path
            add(f"{top}/{rel}", path, sha, size)

    installed: list[str] = []
    by_path = {p: (sha, size) for p, sha, size in everything}
    for x in extras:
        src = (x.get("from") or "").strip("/")
        for rel in x["files"]:
            path = f"{src}/{rel}" if src else rel
            if path not in by_path:
                print(f"warning: {full}: extra file {path} not found", file=sys.stderr)
                continue
            add(rel, path, *by_path[path])
            installed.append(rel)

    zips: set[str] = set()
    groups: set[tuple[str, ...]] = set()
    for _, sha, _ in mras:
        text = subprocess.run(["git", "cat-file", "blob", sha], cwd=d, capture_output=True, check=True).stdout.decode("utf-8", "replace")
        for m in ZIP_ATTR.finditer(text):
            names = tuple(z.strip() for z in m.group(1).split("|") if z.strip().lower().endswith(".zip"))
            zips.update(names)
            groups.add(names)
    return {"zips": sorted(zips), "zip_groups": sorted(groups), "repo": full, "head": head, "category": category, "files": entries,
            "builds": [(posixpath.basename(p), dte) for dte, p, _, _ in builds], "mras": len(mras), "extras": installed}


# ---------------------------------------------------------------------------------------------
# database
# ---------------------------------------------------------------------------------------------

DB_VERSION = 1      # "v": the version of the Downloader database format (a spec field Downloader reads, not a build counter)


def assemble(scans: list[dict], db_id: str, db_url: str, external: dict[str, dict] | None = None) -> dict:
    files: dict[str, dict] = {}
    for s in sorted(scans, key=lambda s: s["repo"].lower()):
        for dest, e in s["files"].items():
            if dest in files:
                print(f"warning: {dest} from {s['repo']} collides with an earlier repository; keeping the first", file=sys.stderr)
                continue
            files[dest] = e
    for dest, e in (external or {}).items():
        files.setdefault(dest, e)
    folders: dict[str, dict] = {}
    for dest in files:
        parts = dest.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            folders["/".join(parts[:i])] = {}
    return {"db_id": db_id, "db_url": db_url, "v": DB_VERSION, "timestamp": int(dt.datetime.now(dt.timezone.utc).timestamp()),
            "files": dict(sorted(files.items())), "folders": dict(sorted(folders.items()))}


def fetch(url: str, timeout: int = 120) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "mister-db-builder"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def rom_index(ext: dict) -> dict[str, tuple[int, str]]:
    """zip name -> (size, md5) of every zip in the archive.org item, falling back to a published CSV."""
    index: dict[str, tuple[int, str]] = {}
    try:
        meta = json.loads(fetch(f"https://archive.org/metadata/{ext['item']}", 300))
        prefix = ext["folder"].strip("/") + "/"
        for f in meta.get("files", []):
            n = f.get("name", "")
            if n.startswith(prefix) and n.lower().endswith(".zip") and "/" not in n[len(prefix):] and f.get("md5") and f.get("size"):
                index[n[len(prefix):]] = (int(f["size"]), f["md5"].lower())
    except Exception as e:
        print(f"warning: archive.org metadata unavailable ({e})", file=sys.stderr)
    if not index and ext.get("fallback_csv"):
        try:
            for row in csv.reader(fetch(ext["fallback_csv"]).decode("utf-8").splitlines()[1:]):
                if len(row) >= 4 and row[2].isdigit() and row[3]:
                    index[posixpath.basename(row[0])] = (int(row[2]), row[3].lower())
        except Exception as e:
            print(f"warning: fallback external list unavailable ({e})", file=sys.stderr)
    return index


def external_files(scans: list[dict], ext: dict, known: dict) -> tuple[dict[str, dict], list[str]]:
    """Database entries for every ROM zip the scanned MRAs name. `known` is the remembered
    zip -> [size, md5], updated in place so an archive.org outage never drops entries."""
    wanted = sorted({z for s in scans for z in s.get("zips", [])}, key=str.lower)
    if any(z not in known for z in wanted):
        for z, (size, md5) in rom_index(ext).items():
            known[z] = [size, md5]
    base = f"https://archive.org/download/{ext['item']}/{urllib.parse.quote(ext['folder'])}"
    entries = {}
    for z in wanted:
        if z not in known:
            continue
        size, md5 = known[z]
        # overwrite=false: a zip the user already has (from any set) is left alone
        entries[f"{ext['dest'].strip('/')}/{z}"] = {"hash": md5, "size": size, "url": f"{base}/{urllib.parse.quote(z)}", "overwrite": False}
    # a clone's zip is normally absent from a merged set; the MRA then names its parent too ("clone.zip|parent.zip"),
    # so a zip only counts as missing when none of the zips in its list is available
    missing = sorted({g[0] for s in scans for g in s.get("zip_groups", []) if g and not any(z in known for z in g)}, key=str.lower)
    return entries, missing


def external_csv(entries: dict[str, dict]) -> str:
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["Path in MiSTer", "URL", "Size in bytes (optional but recommended)", "MD5 Hash (optional but recommended)", "Filter Terms (optional)"])
    for p, e in sorted(entries.items()):
        w.writerow([p, e["url"], e["size"], e["hash"], ""])
    return out.getvalue()


def signature(db: dict) -> dict:
    """What decides whether the database changed: paths, hashes, sizes (not URLs or the timestamp)."""
    return {p: (e["hash"], e["size"]) for p, e in db.get("files", {}).items()}


def load_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path: str, obj, **kw):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, **kw)
        f.write("\n")


def cores_markdown(scans: list[dict], db: dict, when: str) -> str:
    lines = [f"# Cores in this database", "", f"Updated {when}. {len(db['files'])} files from {len(scans)} repositories.", "",
             "| Repository | Builds | Latest build | MRAs |", "|---|---|---|---|"]
    for s in sorted(scans, key=lambda s: s["repo"].lower()):
        names = ", ".join(f"`{n}`" for n, _ in s["builds"])
        latest = max((d for _, d in s["builds"] if d), default="")
        latest = f"{latest[:4]}-{latest[4:6]}-{latest[6:]}" if latest else ""
        lines.append(f"| [{s['repo']}](https://github.com/{s['repo']}) | {names} | {latest} | {s['mras']} |")
    extras = [(s["repo"], e) for s in sorted(scans, key=lambda s: s["repo"].lower()) for e in s.get("extras") or []]
    if extras:
        lines += ["", "## Other files", "", "| Repository | Installed at |", "|---|---|"]
        lines += [f"| [{r}](https://github.com/{r}) | `{e}` |" for r, e in extras]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True, help="output directory (the db branch checkout)")
    ap.add_argument("--config", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"))
    ap.add_argument("--repos", help="comma-separated owner/name list instead of asking the GitHub API")
    ap.add_argument("--owner", help="GitHub user whose repositories to scan (default: config or GITHUB_REPOSITORY_OWNER)")
    ap.add_argument("--work", help="directory for repository clones (default: a temporary one)")
    ap.add_argument("--force", action="store_true", help="publish even when nothing changed")
    a = ap.parse_args()

    cfg = {**DEFAULTS, **load_json(a.config, {})}
    token = os.environ.get("GITHUB_TOKEN")
    self_full = os.environ.get("GITHUB_REPOSITORY")
    owner = a.owner or cfg["owner"] or os.environ.get("GITHUB_REPOSITORY_OWNER") or (self_full or "/").split("/")[0]
    if a.repos:
        candidates = [r.strip() for r in a.repos.split(",") if r.strip()]
    else:
        if not owner:
            ap.error("no owner: pass --owner, set it in config.json, or run inside GitHub Actions")
        candidates = select_repos(list_owner_repos(owner, token), cfg, self_full)
    candidates += [r for r in cfg["include"] if r not in candidates]
    db_id = self_full or f"{owner}/mister-db"
    db_url = f"{RAW}/{db_id}/db/db.json.zip"

    os.makedirs(a.out, exist_ok=True)
    cache = load_json(os.path.join(a.out, "cache.json"), {})
    work = a.work or tempfile.mkdtemp(prefix="mister-db-")
    scans, skipped = [], []
    for full in candidates:
        try:
            s = scan_repo(full, work, cfg, cache)
        except subprocess.CalledProcessError as e:
            print(f"warning: {full}: {(e.stderr or str(e)).strip().splitlines()[-1:]}", file=sys.stderr)
            continue
        if s is None:
            skipped.append(full)
            continue
        scans.append(s)
        print(f"{full}: {len(s['builds'])} build(s), {s['mras']} MRAs, {len(s['extras'])} extra file(s), pinned at {s['head'][:8]}")
    print(f"{len(candidates)} candidate repositories, {len(scans)} with a core build; skipped (no .rbf): {', '.join(skipped) or '-'}")
    if not scans:
        print("error: no core repositories found; refusing to publish an empty database", file=sys.stderr)
        return 1

    ext = cfg.get("external") or {}
    ext_entries: dict[str, dict] = {}
    if ext.get("enabled"):
        ext = {**DEFAULTS["external"], **ext}
        known = load_json(os.path.join(a.out, "external_cache.json"), {})
        ext_entries, missing = external_files(scans, ext, known)
        write_json(os.path.join(a.out, "external_cache.json"), {z: known[z] for z in sorted(known) if z in {p.rsplit("/", 1)[1] for p in ext_entries}}, indent=0)
        print(f"{len(ext_entries)} external ROM zips; not in the archive.org set: {len(missing)}")
        write_json(os.path.join(a.out, "external_missing.json"), missing, indent=0)
        with open(os.path.join(a.out, "external_files.csv"), "w", encoding="utf-8", newline="") as f:
            f.write(external_csv(ext_entries))
    new = assemble(scans, db_id, db_url, ext_entries)
    old = load_json(os.path.join(a.out, "db.json"), {})
    now = dt.datetime.now(dt.timezone.utc)
    status = load_json(os.path.join(a.out, "status.json"), {})
    changed = signature(new) != signature(old) or old.get("v") != DB_VERSION   # an older file without "v" (it used to say "version", which Downloader ignores) is republished once
    if changed or a.force:
        write_json(os.path.join(a.out, "db.json"), new, indent=1)
        with zipfile.ZipFile(os.path.join(a.out, "db.json.zip"), "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("db.json", json.dumps(new, ensure_ascii=False, indent=1) + "\n")
        with open(os.path.join(a.out, "cores.md"), "w", encoding="utf-8") as f:
            f.write(cores_markdown(scans, new, now.strftime("%Y-%m-%d")))
        status["changed"] = now.isoformat(timespec="seconds")
    # only blobs still in use stay in the cache
    live = {e["hash"] for e in new["files"].values()}
    write_json(os.path.join(a.out, "cache.json"), {k: v for k, v in sorted(cache.items()) if v in live})
    # status.json is touched when the database changed, or monthly so the repository never looks idle
    # (GitHub disables scheduled workflows in repositories without recent activity)
    last = dt.datetime.fromisoformat(status["checked"]) if status.get("checked") else None
    if changed or a.force or last is None or (now - last).days >= 30:
        status["checked"] = now.isoformat(timespec="seconds")
    write_json(os.path.join(a.out, "status.json"), status, indent=1)
    print("database changed" if changed else "database unchanged", f"({len(new['files'])} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
