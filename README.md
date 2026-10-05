# MiSTer_ppriest

A [Downloader](https://github.com/MiSTer-devel/Downloader_MiSTer) database of [ppriest](https://github.com/ppriest)'s
public MiSTer cores. It is rebuilt every day by GitHub Actions, so the cores, their latest builds and
their MRAs on your MiSTer follow the source repositories.

## Install

Add this to `downloader.ini` on the MiSTer's SD card (the cores are then installed by `update_all`
or Downloader like any others):

```ini
[ppriest/MiSTer_ppriest]
db_url = https://raw.githubusercontent.com/ppriest/MiSTer_ppriest/db/db.json.zip
```

What is currently in it is listed in [`cores.md`](https://github.com/ppriest/MiSTer_ppriest/blob/db/cores.md).

## What goes in

Every repository of the owner that is **public, not archived, not a fork** and contains at least one
`.rbf` build. New core repositories are picked up automatically; archiving a repository or making it
private removes it at the next run.

For each such repository the database lists

- the **newest dated build of every core** (`Name_YYYYMMDD.rbf`) as `_Arcade/cores/Name_YYYYMMDD.rbf`
  (the `Arcade-` prefix is dropped, as the official distribution does), and
- **all of its `.mra` files**, with the folder layout under the repository's MRA folder kept
  (`_alternatives/…`), as `_Arcade/…`.

Nothing is copied into this repository. Each file entry carries its md5, its size and a
raw.githubusercontent.com URL pinned to the commit it was read from, so the source repositories remain the
single source of truth and downloads are consistent even while a repository is being pushed to.

## How it updates

`.github/workflows/update.yml` runs daily (and on demand from the Actions tab, and when the script or
config changes). It runs `build_db.py`, which

1. lists the owner's repositories through the GitHub API,
2. clones the candidates (tree only, no checkout) and finds the builds and MRAs,
3. hashes only files it has not seen before (`cache.json` maps git blob ids to md5),
4. publishes `db.json`, `db.json.zip`, `cores.md`, `cache.json` and `status.json` to the **`db` branch**,
   but only when a path, hash or size actually changed.

The `db` branch is machine-written; the source lives on `main`.

GitHub switches off scheduled workflows in repositories with no recent activity. When the database has not
changed for a month, the run records a heartbeat in `status.json`, which keeps the schedule alive.

## Configuration

`config.json` (all optional):

| Key | Default | Meaning |
|---|---|---|
| `owner` | repository owner | GitHub user whose repositories are scanned |
| `include` | `[]` | extra `owner/name` repositories to scan, even forks or other owners' |
| `exclude` | `[]` | repository names (or `owner/name`) to skip |
| `include_forks` | `false` | also scan the owner's forks |
| `keep_builds` | `1` | newest N dated builds kept per core |
| `strip_prefix` | `["Arcade-"]` | prefixes removed from build file names |
| `categories` | `{}` | `"owner/name": "_Console"` etc. for repositories that are not arcade cores |
| `mra_folder` | `""` | put every MRA in `_Arcade/<folder>/` instead of directly in `_Arcade/` |
| `ignore_paths` | build/doc folders | regular expression of paths to ignore |

Repositories without MRAs go to `_Other` unless `categories` says otherwise.

## Run it locally

```bash
python build_db.py --out out --repos ppriest/Arcade-Seta_MiSTer,ppriest/Arcade-KonamiGX_MiSTer
```

`--repos` skips the GitHub API; without it the script needs `GITHUB_TOKEN` (optional, only for the rate limit)
and an owner (`--owner`, `config.json`, or `GITHUB_REPOSITORY_OWNER`). Python 3.9+ and git; no packages.
