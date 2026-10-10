#!/usr/bin/env python3
"""Build a MiSTer Downloader database from an owner's public, non-archived core repositories.

    python build_db.py --out out                       # discover the owner's repositories (needs GITHUB_TOKEN)
    python build_db.py --out out --repos o/a,o/b       # explicit list, no GitHub API needed

A repository is a core when it is public, not archived, not a fork, and contains at least one
``.rbf`` build. For each one the newest dated build of every core (``Name_YYYYMMDD.rbf``) and all of
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
    files = [f for f in tree(d) if not ignore.search(f[0])]
    rbfs = [f for f in files if f[0].lower().endswith(".rbf")]
    if not rbfs:
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

    return {"repo": full, "head": head, "category": category, "files": entries,
            "builds": [(posixpath.basename(p), dte) for dte, p, _, _ in builds], "mras": len(mras)}


# ---------------------------------------------------------------------------------------------
# database
# ---------------------------------------------------------------------------------------------

def assemble(scans: list[dict], db_id: str, db_url: str) -> dict:
    files: dict[str, dict] = {}
    for s in sorted(scans, key=lambda s: s["repo"].lower()):
        for dest, e in s["files"].items():
            if dest in files:
                print(f"warning: {dest} from {s['repo']} collides with an earlier repository; keeping the first", file=sys.stderr)
                continue
            files[dest] = e
    folders: dict[str, dict] = {}
    for dest in files:
        parts = dest.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            folders["/".join(parts[:i])] = {}
    return {"db_id": db_id, "db_url": db_url, "timestamp": int(dt.datetime.now(dt.timezone.utc).timestamp()),
            "files": dict(sorted(files.items())), "folders": dict(sorted(folders.items()))}


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
        print(f"{full}: {len(s['builds'])} build(s), {s['mras']} MRAs, pinned at {s['head'][:8]}")
    print(f"{len(candidates)} candidate repositories, {len(scans)} with a core build; skipped (no .rbf): {', '.join(skipped) or '-'}")
    if not scans:
        print("error: no core repositories found; refusing to publish an empty database", file=sys.stderr)
        return 1

    new = assemble(scans, db_id, db_url)
    old = load_json(os.path.join(a.out, "db.json"), {})
    now = dt.datetime.now(dt.timezone.utc)
    status = load_json(os.path.join(a.out, "status.json"), {})
    changed = signature(new) != signature(old)
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
