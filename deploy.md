# Deploying releases to this dist repo

This repo is the **public download mirror** for private application releases.
It owns all the bookkeeping; private app repos only build and push files.

---

## How it works (architecture)

Two repos, two responsibilities:

| Repo | Responsibility |
| --- | --- |
| **Private app repo** (e.g. `automation`) | Build the installers, upload them as **GitHub Release assets** on this repo, and commit a small `<app>/<version>/release.json` describing them. Nothing else. |
| **This dist repo** | On every push, regenerate all `manifest.json` / `readme.md` files from the `release.json` files on disk. |

Installers do **not** live in git — GitHub rejects files over 100 MB and a
public repo would bloat by hundreds of MB per release. They are uploaded to a
GitHub Release tagged `<app>-<version>` instead; only the tiny `release.json`
(filenames, sizes, asset download URLs) is committed.

Per release there are two automatic commits:

1. `release: <app> <version>` — the app repo uploads the installers to a
   Release and commits `release.json`.
2. `chore: refresh release index` — this repo regenerates the index.

```
tag push (vX.Y.Z) ──▶ app repo CI builds ──▶ uploads installers to GitHub Release <app>-vX.Y.Z
                                          └─▶ commits dist/<app>/<version>/release.json
                                                        │
                                                        ▼ (push triggers)
                                          dist refresh.yml ──▶ refresh-index.py
                                                        │
                                                        ▼
                          manifest.json + readme.md (root and per-app) regenerated
```

### Layout produced

```
dist/
├── manifest.json          # source of truth: one entry per app → its latest release
├── readme.md              # rendered list of all apps (auto-generated)
├── apps/
│   └── <app>.json         # per-app config: { "product_name": "Display Name" }
└── <app>/                 # = the app repo's name
    ├── manifest.json      # source of truth: every version + per-asset metadata
    ├── readme.md          # rendered releases page (auto-generated)
    └── vX.Y.Z/
        └── release.json   # the uploaded release's assets + download URLs
```

The actual installers live in GitHub Releases (tag `<app>-vX.Y.Z`), not in git.
Everything except the files under `apps/` and `<app>/vX.Y.Z/release.json` is
**generated** — never edit `manifest.json` / `readme.md` by hand; they are
overwritten on the next push.

---

## One-time setup

### 1. This dist repo

- **Enable GitHub Actions** so `.github/workflows/refresh.yml` runs on push.
  Without it, files upload but the index never regenerates.
- Allow the workflow to push back: **Settings → Actions → General → Workflow
  permissions → Read and write** (the workflow also sets `contents: write`).

### 2. A token shared by app repos

App repos upload installers to a Release here **and** push `release.json`, both
authenticated with one token. The app repo's built-in `GITHUB_TOKEN` can't be
used — it has no access to this separate repo.

Create a **fine-grained personal access token** scoped to this dist repo with
**Contents: read and write** permission (Settings → Developer settings →
Fine-grained tokens). Add it as the `DIST_TOKEN` secret in each app repo (see
below).

> A classic PAT with the `repo` scope also works, but fine-grained is tighter —
> limit it to just this repo.

---

## Adding a new app

No changes are needed in *this* repo's code — apps register themselves by
pushing files. Do this in the **new app's private repo**:

1. **Copy the uploader** `scripts/publish-dist.py` from an existing app repo
   (it is app-agnostic — it names the dist directory after the repo).

2. **Add the publish step** to the release workflow (`.github/workflows/release.yml`),
   after the build job. Minimal version:

   ```yaml
   publish-dist:
     needs: build-tauri          # whatever your build job is called
     runs-on: ubuntu-latest
     steps:
       - uses: actions/checkout@v4
       - uses: actions/download-artifact@v4
         with:
           path: artifacts
           merge-multiple: true
       - name: Publish artifacts to the dist repo
         env:
           VERSION: ${{ github.ref_name }}
           DIST_REPO: ${{ secrets.DIST_REPO }}
           DIST_TOKEN: ${{ secrets.DIST_TOKEN }}
         run: python3 scripts/publish-dist.py
   ```

   `gh` (used for the release upload) is preinstalled on GitHub-hosted runners.

3. **Add the secrets** to that repo:
   - `DIST_REPO` — this repo as `<owner>/dist` or a git URL, e.g.
     `git@github.com:<owner>/dist` (or set `DIST_REPO_OWNER` and let it derive
     `<owner>/dist` from the Actions runtime).
   - `DIST_TOKEN` — the token with Contents: write on this repo, from setup.

4. *(Optional)* **Set a display name** in this dist repo so the readmes show a
   pretty name instead of the raw directory name. Create `apps/<app>.json`:

   ```json
   { "product_name": "My App" }
   ```

   `<app>` is the app repo's name (e.g. `automation`). If you skip this, the app
   shows under its directory name. Commit and push it here.

That's it. On the app's first release, it creates the `<app>-vX.Y.Z` Release and
`dist/<app>/<version>/release.json`, and this repo's refresh adds it to the root
list automatically.

> **Pick a unique app name.** The dist directory is the repo name by default; a
> collision would overwrite another app's folder. Override with the `APP_NAME`
> env var in the publish step if you need a different directory name.

---

## Deploying a new release

From an app repo that is already set up:

1. **Bump the version** (e.g. in `package.json` / `tauri.conf.json` / `Cargo.toml`).
2. **Commit** the bump.
3. **Tag and push** with a `vX.Y.Z` tag — this triggers the release:

   ```bash
   git tag v0.1.3
   git push origin v0.1.3
   ```

Then it is fully automatic:

1. The app repo's release workflow builds the installers for each platform.
2. `publish-dist.py` uploads them to a GitHub Release here (`<app>-v0.1.3`) and
   commits `dist/<app>/v0.1.3/release.json` (commit `release: <app> v0.1.3`).
3. That push triggers `refresh.yml` here, which runs `refresh-index.py` and
   commits the regenerated `manifest.json` / `readme.md` (root and per-app).

When both commits land, the new release is live in the root list and the app's
releases page, with download links to every installer.

> The tag **must** match `vX.Y.Z` exactly — the uploader rejects other formats.

---

## What gets generated

- **`<app>/manifest.json`** — every version of the app, each with its assets
  (filename, download URL, size, inferred platform/arch/kind) and publish date
  (from this repo's git history, so old versions keep their original date).
- **`<app>/readme.md`** — that manifest rendered as a releases page.
- **`manifest.json`** (root) — one entry per app pointing at its latest version.
- **`readme.md`** (root) — that rendered as a table of all apps.

`refresh-index.py` is a pure function of the `release.json` files on disk:
re-running it is idempotent, and removing a version directory self-heals the
index on the next run.

---

## Secrets reference (set in each app repo)

| Secret | Required | Purpose |
| --- | --- | --- |
| `DIST_TOKEN` | yes | Token with Contents: write on this repo (release upload + git push) |
| `DIST_REPO` | recommended | This repo as `<owner>/dist` or a git URL, e.g. `git@github.com:<owner>/dist` |
| `DIST_REPO_OWNER` | alternative | Owner used to derive `<owner>/dist` if `DIST_REPO` is unset |

Optional env overrides for the publish step: `APP_NAME` (dist directory +
release-tag prefix), `DIST_REPO_NAME` (default `dist`), `GIT_USER_NAME` /
`GIT_USER_EMAIL`, `ARTIFACTS_DIR` (default `artifacts`).

---

## Troubleshooting

- **Files uploaded but index didn't update** — Actions aren't enabled on this
  repo, or `refresh.yml` lacks push permission. Check this repo's Actions tab.
- **`Cannot determine the dist repo`** — neither `DIST_REPO` nor (`DIST_REPO_OWNER`
  + a CI `GITHUB_SERVER_URL`) is available. Set the `DIST_REPO` secret.
- **`gh: ... HTTP 403`, or push fails with auth error** — `DIST_TOKEN` is
  missing, expired, or lacks Contents: write on this repo.
- **App shows under its raw name** — add `apps/<app>.json` with a `product_name`.
- **`Unexpected VERSION tag format`** — the tag isn't `vX.Y.Z`.
- **Re-run a release** — pushing the same version re-uploads the installers
  (`--clobber` replaces the Release assets) and rewrites `release.json`; the
  index regenerates to match.

---

## Local testing (no push)

Dry-run the uploader against a local checkout, then the refresh, to preview the
exact output without touching the remote. With `DIST_DIR` set, the uploader
**skips** the GitHub release upload and the push — it only writes `release.json`
(its asset URLs point at where the Release *would* be):

```bash
# stage 1: write release.json into a local dist checkout (no upload, no push)
DIST_DIR=/path/to/dist \
ARTIFACTS_DIR=./artifacts \
DIST_REPO=owner/dist \
GITHUB_REPOSITORY=owner/<app> \
VERSION=v0.1.3 \
python3 scripts/publish-dist.py

# stage 2: run this repo's bookkeeping
python3 /path/to/dist/scripts/refresh-index.py
```
