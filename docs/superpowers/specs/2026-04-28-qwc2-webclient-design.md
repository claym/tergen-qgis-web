# QWC2 Web Client — Design Spec

**Date:** 2026-04-28
**Status:** Draft, pending user review
**Author:** Brainstorming session, clay + Claude
**Supersedes (in part):** the thin Leaflet viewer in `2026-04-24-qgis-server-design.md` §5.2

## 1. Goal

Replace the bare-bones Leaflet viewer with [QWC2](https://github.com/qgis/qwc2-demo-app) running standalone (static JS/HTML/CSS) against the existing QGIS Server. Add a tight iteration loop so dropping a new `.gpkg` into `data/` is reflected in the viewer within a few seconds and a browser refresh, with no `kubectl`, `helm`, or rebuild required.

## 2. Scope

**In scope:**

- Richer read-only client: layer tree (groups, opacity, reorder), identify panel, measure, scale bar, mouse coordinates, basemap switcher, share-link/permalink, fullscreen, mobile-friendly.
- Print: PDF export driven by QGIS print layouts in each project file (`GetPrint`).
- Search: per-theme attribute search via QGIS Server WFS (no SOLR).
- One theme per `*.gpkg` file, auto-discovered. Theme switcher in the client.
- Filesystem watcher that regenerates `*.qgs` and `themesConfig.json` when `*.gpkg` files change.

**Out of scope:**

- Editing (WFS-T), user accounts, per-user permissions, fulltext/fuzzy search.
- The full `qwc-services` Python microservice stack (auth, data, mapinfo, fulltext, document, elevation, …).
- Fetching qwc2 dependencies on every install (build is a one-time, vendored artifact).

## 3. Architecture

```
 *.devbox DNS query
    │
    ▼
 dnsmasq on host (192.168.1.70:53)  ──►  resolves *.devbox → 192.168.1.70
    │
    ▼
 browser → http://qgis.devbox/
    │
    ▼
 Traefik Ingress (192.168.1.70:80, Host=qgis.devbox)
    │
    ├─►  /            → Service: viewer       → Pod: nginx (serves qwc2 dist from hostPath /srv/qgis/web/)
    └─►  /ows         → Service: qgis-server  → Pod: QGIS Server (FCGI, MAP=… selects per-request)
                                                     │
                                                     ▼
                                                hostPath /srv/qgis/  (PVC, RO from server)
                                                ├── data/<dataset>/*.gpkg                 (RO)
                                                ├── projects/<gpkg-stem>.qgs              (auto-generated)
                                                └── web/
                                                    ├── index.html, *.js, *.css          (qwc2 dist)
                                                    └── themesConfig.json                 (auto-generated)

 Continuously, in-cluster:
   project-watcher Deployment ──watches /srv/qgis/data/**/*.gpkg via inotify
                              ──debounces 1s, then regenerates:
                                  /srv/qgis/projects/<stem>.qgs       (atomic rename)
                                  /srv/qgis/web/themesConfig.json     (atomic rename)
```

## 4. Components

### 4.1 viewer pod (replaces existing thin viewer)

- Image: `nginx:1.27-alpine` (unchanged).
- Mount `/srv/qgis/web/` as `/usr/share/nginx/html/` **read-only**, via the same hostPath PVC the rest of the app uses.
- Nginx config: serve static files; fall through to `/index.html` for SPA routes (`try_files $uri /index.html`).
- The qwc2 dist lives entirely on disk at `/srv/qgis/web/`. The pod is stateless and replaceable.
- Resources (unchanged): 32Mi / 128Mi, 10m / 100m CPU.

The previous chart components — `configmap-viewer.yaml` and `chart/files/index.html` — are deleted. The Leaflet viewer is gone; nothing serves it.

### 4.2 qgis-server pod (small change)

- Image: `camptocamp/qgis-server:3.34` (unchanged).
- Drop the `QGIS_PROJECT_FILE` env var. With multiple projects, qgis-server resolves the project per-request via the `MAP=` query parameter.
- Add `initContainer` that blocks until at least one `*.qgs` file exists under `/srv/qgis/projects/`:

  ```sh
  until ls /srv/qgis/projects/*.qgs >/dev/null 2>&1; do sleep 1; done
  ```

  This eliminates the cold-start race where qgis-server boots before the watcher has produced any project file.
- Liveness/readiness: hit `GET /ows/?SERVICE=WMS&REQUEST=GetCapabilities&MAP=/srv/qgis/projects/<defaultTheme>.qgs`. Default theme name from `values.defaultTheme` (or first alphabetical at probe-render time, via a small entry-point shim — see §6).

### 4.3 project-watcher Deployment (new)

Replaces the post-install `project-generator` Job entirely.

- Image: `python:3.12-slim`.
- Needs `watchdog` (one ~5 MB pure-Python wheel). Two install options, deferred to the implementation plan:
  - **Option A (default):** an `initContainer` runs `pip install --target=/deps watchdog`; both containers share an `emptyDir` mounted at `/deps`; main container sets `PYTHONPATH=/deps`. Zero custom image, slight pod-restart cost (~3 s pip download).
  - **Option B:** thin custom image `tergen/qgis-watcher:<ver>` built from `python:3.12-slim + RUN pip install watchdog`, loaded into k3s containerd via `ctr images import`. Hermetic, no network on pod start, more chart pieces.
  Pick at plan time. The watcher script and chart wiring don't change between A and B.
- Single replica. PVC mount `/srv/qgis` **read-write**.
- Python script (lives in a ConfigMap, mounted as `/app/watcher.py`):
  1. **On startup:** invoke the regen function once, synchronously. Only after the first successful regen does the script enter the watch loop. The pod's readiness probe gates on a `/tmp/ready` file the script touches after first regen.
  2. **Watch loop:** `watchdog.Observer` on `/srv/qgis/data/`, recursive, file pattern `*.gpkg`. On `created`/`modified`/`moved` events, schedule a regen with a 1 s debounce window (subsequent events within the window collapse into one regen).
  3. **Regen function:**
     - Scan `/srv/qgis/data/` recursively for `*.gpkg`.
     - For each gpkg, run the existing `generate_qgs.py` logic to produce `/srv/qgis/projects/<stem>.qgs.tmp`, then `os.rename` to `<stem>.qgs`.
     - Build `/srv/qgis/web/themesConfig.json.tmp` (one entry per gpkg — see §5), then `os.rename`.
     - Remove `*.qgs` files in `/srv/qgis/projects/` whose source gpkg has disappeared.
  4. **Pause hatch:** if `/srv/qgis/.no-regen` exists, skip regen, log once per event burst, do not touch any files. `touch /srv/qgis/.no-regen` to pause; `rm` to resume. No pod restart needed.
  5. **Heartbeat:** touch `/tmp/heartbeat` every 30 s. Liveness probe: file mtime within 60 s.
- Resources: 50m / 500m CPU, 64Mi / 256Mi memory (same envelope as the old generator Job).
- The chart's old `templates/job-project-generator.yaml` is deleted.

### 4.4 generate_qgs.py changes

The existing `chart/files/generate_qgs.py` is refactored from "scan one dir → write one project.qgs" to:

```python
def regen_all(data_dir: Path, projects_dir: Path, web_dir: Path) -> RegenReport:
    """Idempotently rebuild all per-gpkg .qgs files and themesConfig.json."""
```

Helpers extracted:

- `discover_gpkgs(data_dir) -> list[GpkgInfo]` (filename, path, mtime, layers, bbox, CRS)
- `write_project(gpkg: GpkgInfo, out: Path)` — atomic via tempfile + rename
- `write_themes_config(gpkgs: list[GpkgInfo], out: Path, default_theme: str | None)` — atomic
- `prune_orphans(projects_dir, current_stems)` — remove `.qgs` files whose source gpkg is gone

The script remains pure stdlib (sqlite3 + xml.etree + json) — no GDAL/PyQGIS, no Jinja.

It is callable two ways:

```sh
python3 generate_qgs.py --once    # regen and exit (used by tests, manual fixups)
python3 generate_qgs.py --watch   # regen, then inotify loop (used by the Deployment)
```

## 5. themesConfig.json layout

Generated per scan. One theme per gpkg. Schema follows qwc2's [themesConfig.json reference](https://qwc-services.github.io/master/references/qwc2_themesconfig/).

```jsonc
{
  "themes": {
    "title": "Themes",
    "items": [
      {
        "id": "territories_draft",
        "title": "Territories Draft",                  // title-case from filename
        "url": "/ows/?MAP=/srv/qgis/projects/territories_draft.qgs",
        "default": true,                                 // matches values.defaultTheme
        "format": "image/png",
        "tiled": true,
        "attribution": "",
        "mapCrs": "EPSG:3857",
        "additionalMouseCrs": ["EPSG:2264"],
        "bbox": {
          "crs": "EPSG:4326",
          "bounds": [/* w, s, e, n in WGS84 — read from gpkg_contents.min/max_x/y when CRS is 4326-aligned, otherwise from gpkg's native bbox after a static-table reproject; see §5.1 */]
        },
        "scales": [/* default qwc2 scale set */],
        "searchProviders": [
          {
            "provider": "qgis",
            "params": {
              "title": "Territory ID",
              "layerName": "territories",
              "expression": "\"terr_id\" ILIKE :value || '%'"
            }
          },
          {
            "provider": "qgis",
            "params": {
              "title": "Municipality",
              "layerName": "territories",
              "expression": "\"muni_name\" ILIKE :value || '%'"
            }
          }
        ],
        "backgroundLayers": [{ "name": "osm", "visibility": true }],
        "thumbnail": "img/mapthumbs/default.jpg"
      },
      {
        "id": "debug",
        "title": "Debug",
        "url": "/ows/?MAP=/srv/qgis/projects/debug.qgs",
        "default": false,
        // …same shape, search providers omitted (debug layers are pipeline output, not user-searchable)
      }
    ]
  },
  "backgroundLayers": [
    {
      "name": "osm",
      "title": "OpenStreetMap",
      "type": "osm",
      "source": "osm",
      "thumbnail": "img/mapthumbs/mapnik.png"
    }
  ],
  "defaultMapCrs": "EPSG:3857",
  "defaultBackgroundLayers": ["osm"],
  "defaultSearchProviders": [],
  "pluginData": {}
}
```

Search providers are emitted only for known curated layer/field combos. Heuristic in the generator: for any layer named `territories` (or matching a small allowlist), emit search providers on `terr_id`, `muni_name`, `subdiv` if those columns exist. Other layers ship without search providers — adding more is an overlay-level concern.

The provider schema above is illustrative; the exact key names and SQL-template syntax for the `qgis` search provider are validated against the qwc2 version vendored at implementation time (qwc2's themesConfig schema does shift across major versions). The plan should resolve this against the pinned subtree SHA.

### 5.1 bbox sourcing without `pyproj`

GeoPackage files store native-CRS bounds in `gpkg_contents.min_x/min_y/max_x/max_y`. For data already in EPSG:4326 those bounds drop straight into `bbox.bounds`. For non-4326 data (the territories gpkg is EPSG:2264), the generator reads the `gpkg_geometry_columns.srs_id` / `gpkg_spatial_ref_sys` and uses a small static lookup table mapping the project's known SRS IDs (`2264` and any future ones we ship) to a hand-computed WGS84 envelope per dataset. New unknown SRSes fall back to a global `[-180,-85,180,85]` extent and log a warning. This avoids pulling in `pyproj` (~50 MB with PROJ data files) for a fixed and small set of CRSes.

If the project ever ships data in arbitrary CRSes, swap to `pyproj` and a real per-bbox transform — but that's a one-line library swap, not a redesign.

## 6. qgis-server `defaultTheme` resolution

The viewer's nginx serves `index.html` which loads qwc2; qwc2 reads `themesConfig.json` and honors `default: true` to pick the initial theme. So qgis-server itself doesn't strictly need a "default project," because every WMS request will carry `MAP=…`.

But the readiness probe needs a stable URL. Approach: a sidecar entry-point script in qgis-server resolves a default at pod start:

```sh
DEFAULT_QGS=$(ls /srv/qgis/projects/*.qgs | head -1)
echo "QGIS_DEFAULT_PROJECT=${DEFAULT_QGS}" > /tmp/probe.env
exec /usr/local/bin/start-server
```

Liveness/readiness: `wget -qO- "http://127.0.0.1/ows/?SERVICE=WMS&REQUEST=GetCapabilities&MAP=$(cat /tmp/probe.env | cut -d= -f2)"` returns non-empty XML.

If `values.defaultTheme` is set, the watcher emits that as `default: true` in `themesConfig.json` and the entrypoint shim prefers `${DEFAULT_THEME}.qgs` over the alphabetical fallback.

## 7. qwc2 build pipeline

### 7.1 Repository layout

```
client/
├── qwc2-demo-app/          # vendored upstream via git subtree, BYTE-FOR-BYTE, NEVER hand-edited
├── overlay/                # OUR customizations only — applied on top at build time
│   ├── static/
│   │   └── config.json     # plugin set (LayerTree, Identify, Measure, Print, Permalink, Share, Search, …)
│   ├── js/
│   │   └── appConfig.js    # plugin imports for the above
│   └── icons/              # branding (optional, tiny)
├── Makefile                # build, install-into-host, smoke-test targets
└── README.md
```

### 7.2 Initial vendor

```sh
git subtree add --prefix=client/qwc2-demo-app \
    https://github.com/qgis/qwc2-demo-app master --squash
```

### 7.3 Updating from upstream

```sh
git subtree pull --prefix=client/qwc2-demo-app \
    https://github.com/qgis/qwc2-demo-app master --squash
make client       # rebuild with our overlay
# smoke-test in browser, then `git push`
```

Because nothing in `client/qwc2-demo-app/` is hand-edited, subtree pulls are conflict-free unless upstream restructures something the overlay depends on (e.g., renames `static/config.json` schema). In that case `make client` fails or produces a stale-config viewer caught on smoke-test → patch overlay → commit.

### 7.4 Build mechanics

The Makefile uses a containerized `node:22` (pinned major, not `lts`, to keep builds reproducible) so no host Node toolchain is required:

```make
CLIENT_DIR := $(CURDIR)/client
DIST_DIR   := $(CLIENT_DIR)/qwc2-demo-app/dist/QWC2App
WEB_DIR    := /srv/qgis/web
NODE_IMAGE := node:22

.PHONY: client install-client clean-client

client:
	rsync -av --delete-during $(CLIENT_DIR)/overlay/ $(CLIENT_DIR)/qwc2-demo-app/
	docker run --rm \
	    -v $(CLIENT_DIR)/qwc2-demo-app:/work -w /work \
	    -u $(shell id -u):$(shell id -g) \
	    $(NODE_IMAGE) \
	    bash -c "yarn install --frozen-lockfile && yarn build"

install-client: client
	sudo install -d $(WEB_DIR)
	sudo rsync -av --delete-during $(DIST_DIR)/ $(WEB_DIR)/

clean-client:
	rm -rf $(CLIENT_DIR)/qwc2-demo-app/dist
```

**Overlay-path discipline:** files in `client/overlay/<path>` rsync onto `client/qwc2-demo-app/<path>`. If a future upstream rename changes where (e.g.) `static/config.json` lives, the overlay needs updating in lockstep. There is no automatic schema check; smoke-test after each upstream pull.

Operator workflow:

```sh
make install-client      # one-shot: build + drop into /srv/qgis/web/
helm install qgis ./chart -n qgis --create-namespace
```

For client iteration:

```sh
# edit client/overlay/static/config.json
make install-client && # browser hard-refresh
```

### 7.5 What the overlay turns on

`client/overlay/static/config.json` enables (qwc2 plugin names):

- `Map`, `LayerTree`, `BackgroundSwitcher`, `Identify`, `Measure`, `ScaleBar`, `LocateButton`, `MapTip`, `MouseCoordinates`, `Search`, `Share`, `BottomBar`, `TopBar`, `HomeButton`, `Settings`, `ZoomIn`, `ZoomOut`, `FullScreen`, `Print`, `Permalink`, `ThemeSwitcher`.

Disabled (not in scope): `Editing`, `Login`, `RoutingDialog`, `RasterExport`, `DxfExport`, `TimeManager`, `BookmarkButton`'s authenticated mode (anonymous bookmarks fine).

## 8. Helm chart changes

| File | Change |
|---|---|
| `templates/configmap-viewer.yaml` | **Deleted** (was the inline Leaflet `index.html`). |
| `chart/files/index.html` | **Deleted**. |
| `templates/job-project-generator.yaml` | **Deleted** (subsumed by watcher). |
| `templates/configmap-generator.yaml` | Renamed → `configmap-watcher.yaml`. Holds the watchdog-driven `watcher.py`. |
| `templates/deployment-watcher.yaml` | **New**. Single-replica Deployment running watcher.py. PVC mount RW. |
| `templates/deployment-viewer.yaml` | Volume mount changes from ConfigMap (`viewer-html`) to PVC subpath `web/`, mounted RO at `/usr/share/nginx/html/`. |
| `templates/deployment-qgis-server.yaml` | Drop `QGIS_PROJECT_FILE` env. Add `initContainer` waiting for any `*.qgs`. Add probe-shim entrypoint (or readiness using a `wget` against `MAP=…` resolved at start). |
| `templates/ingress.yaml` | Unchanged. Same routes (`/` → viewer, `/ows` → qgis-server). |
| `templates/middleware-strip-ows.yaml` | Unchanged. |
| `templates/pvc.yaml` | Unchanged (still hostPath → `/srv/qgis`, RWO). |
| `values.yaml` | Add `defaultTheme` (string, optional — name of the gpkg stem to mark `default: true`). Add `watcher.enabled` (default `true`; `false` removes the watcher Deployment for fully hand-managed installs — replaces today's `generator.enabled`). Add `watcher.debounceSeconds` (default `1`). Drop `projectFile` (no longer single-project) and `generator.enabled`. |

## 9. Iteration loops, end-state

**Map data iteration (frequent — pipeline reruns):**

```sh
cp out/territories_draft.gpkg /srv/qgis/data/territories/
# watcher logs: detected change, regenerated 24 layers across 2 themes in 0.4s
# browser hard-refresh
```

**Hand-tweak symbology in QGIS Desktop (occasional):**

```sh
touch /srv/qgis/.no-regen        # pause watcher
# open /srv/qgis/projects/territories_draft.qgs in QGIS Desktop, edit symbology, save
# browser hard-refresh
# when done auto-regen-tolerable again:
rm /srv/qgis/.no-regen
```

**Client iteration (rare — overlay tweaks):**

```sh
# edit client/overlay/static/config.json
make install-client
# browser hard-refresh
```

**Upstream qwc2 update (very rare):**

```sh
git subtree pull --prefix=client/qwc2-demo-app https://github.com/qgis/qwc2-demo-app master --squash
make install-client
# smoke-test, commit
```

## 10. Failure modes and recovery

| Failure | Symptom | Recovery |
|---|---|---|
| Watcher pod crashes | `themesConfig.json` is current; new gpkg drops not reflected | Pod restarts; `--once` regen on startup catches up. |
| Bad gpkg file (corrupt) | Watcher logs error, leaves stale `<stem>.qgs` in place | Fix or remove the gpkg; next event triggers another regen. |
| `themesConfig.json` half-written | Browser sees broken JSON | Atomic rename precludes this; if it ever happens (FS-level), `rm` and pod restarts re-emit. |
| Two pods writing concurrently | Race on `*.qgs` | Watcher Deployment uses `replicas: 1` and `strategy: Recreate` — never two writers. |
| qgis-server starts before watcher | `MAP=` requests 404 | initContainer blocks until `*.qgs` exists. |
| Operator forgets `make install-client` | Empty `/srv/qgis/web/`, viewer shows nginx 403 | Document in README; `helm install` itself doesn't depend on it but the viewer pod will be useless until populated. |
| Filename collision (`Foo.gpkg` and `foo.gpkg` on case-insensitive FS) | One overwrites the other | Linux ext4 is case-sensitive — won't happen on this host. Out of scope. |

## 11. Success criteria

**Build / install:**

- `make install-client` produces `/srv/qgis/web/index.html` and a populated `/srv/qgis/web/static/` and is idempotent.
- `helm install qgis ./chart -n qgis --create-namespace` reaches `Ready` for `viewer`, `qgis-server`, and `project-watcher` Deployments.
- `/srv/qgis/projects/` contains one `.qgs` per `.gpkg` in `/srv/qgis/data/`.
- `/srv/qgis/web/themesConfig.json` exists, parses as JSON, contains one theme per gpkg.

**Service behavior:**

- Open `http://qgis.devbox/` → qwc2 loads, theme switcher shows "Territories Draft" and "Debug" (or whatever gpkg files exist), default theme matches `values.defaultTheme`.
- Click any feature → identify panel shows attributes.
- Type a partial `terr_id` or `muni_name` in the search box → matching territories appear, click zooms to the feature.
- Print → produces a PDF (assuming a print layout exists in the project; if not, generator writes a default A4-landscape layout per project — see §12).
- Permalink button copies a URL with view + visible-layers state; pasting it in another tab restores that view.

**Iteration loop:**

- `cp newdata.gpkg /srv/qgis/data/foo/` results in a new theme appearing in the qwc2 theme switcher within 5 s of a browser refresh, with no operator action.
- `rm /srv/qgis/data/foo/newdata.gpkg` removes the theme on next refresh; `/srv/qgis/projects/newdata.qgs` is also removed.
- `touch /srv/qgis/.no-regen`; replace a gpkg → watcher logs "skipping (paused)" and does not touch projects or themesConfig.

## 12. Print layout (small, but a real decision)

The auto-generated `*.qgs` files have no print layout out of the box. For Print to be useful immediately, the generator emits a single default A4-landscape layout per project containing: title (= theme title), main map frame, scale bar, north arrow, and legend. Layout name: `Default A4 Landscape`. This shows up in qwc2's Print plugin dropdown.

Users who want a custom layout: pause the watcher, hand-edit in QGIS Desktop, save. The hand-edit overrides the auto-generated layout because the watcher won't regen until unpaused. Permanent custom layouts on a per-gpkg basis is out of scope — would need a sidecar layout-template directory and overlay logic; address only if it comes up.

## 13. Open questions / locked-in defaults

- **OSM basemap, public tiles.** Same as today. Carto/Esri alternatives switchable via overlay.
- **Default theme name**: `values.defaultTheme` (unset → first alphabetical). For the current dataset, set this to `territories_draft` so the user-relevant theme opens first instead of `debug`.
- **Search providers per theme**: hardcoded heuristic (territories + named columns). Not data-driven from gpkg metadata. Override by editing the generator if a new gpkg ships with a meaningful search column the heuristic misses.
- **Print plugin** uses qgis-server's `GetPrint` endpoint via the same `/ows` route. Traefik default config passes binary `application/pdf` responses through unmodified — no extra middleware needed.
- **Vendor pin**: subtree pulls are manual, on demand. No automated upstream tracking. Pinning by SHA in the squash commit is fine for our cadence.
- **Browser cache**: dist is fingerprinted by webpack (`*.<hash>.js`), so updates self-bust. `themesConfig.json` is *not* fingerprinted; nginx config sets `Cache-Control: no-cache` for it specifically.

## 14. Decisions made during brainstorming (audit trail)

| # | Decision |
|---|---|
| 1 | Feature scope: richer viewer + print + WFS attribute search (option C in brainstorming). |
| 2 | Search depth: WFS via `qgis` provider, no SOLR (option C1). |
| 3 | Asset delivery: pre-built dist on hostPath at `/srv/qgis/web/` (option B2). |
| 4 | Iteration: filesystem watcher (inotify) regenerating both `*.qgs` and `themesConfig.json`. |
| 5 | Theme strategy: one theme per `*.gpkg`, auto-discovered (option T3). |
| 6 | Replace, don't coexist with, the existing thin Leaflet viewer. |
| 7 | qwc2 source: `git subtree`-vendored at `client/qwc2-demo-app/`, customizations in `client/overlay/` (option P2). |
