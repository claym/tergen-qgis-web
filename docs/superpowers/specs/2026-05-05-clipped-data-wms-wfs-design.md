# Clipped-data WMS/WFS connections — design

## Goal

Make every `*.gpkg` under `/srv/qgis/data/clipped_data/` available to QGIS
Desktop clients as its own WMS and WFS connection, with stable, collision-free
names that keep folder context (e.g. "Mecklenburg Addresses" vs "NC Addresses"
vs "Union Addresses") and that get distributed to clients live via the
existing `qgis.devbox` ingress.

## Background

The chart's `project-watcher` already runs `chart/files/generate_qgs.py
--watch` over `/srv/qgis/data/`, recursively discovering every `*.gpkg` and
emitting one `.qgs` per file. The clipped_data gpkgs are therefore *partially*
exposed today, but with two problems:

1. **Stem collisions silently overwrite.** The project id is `gpkg.stem`.
   Three files named `addresses.gpkg` (Mecklenburg, NC, Union) all map to
   `addresses.qgs`; the watcher writes them in turn and only one survives.
   The QWC2 themes.json bake disambiguates by appending integer suffixes
   (`addresses`, `addresses1`, `addresses2`), but those ids don't correspond
   to existing `.qgs` files, so two of the three render no data.
2. **The hand-curated connection bundles only cover `territories/`.**
   `client/connections/qgis-wms-connections.xml` and
   `qgis-wfs-connections.xml` list `territories_draft` and `debug` only, so
   even when clipped_data `.qgs` files do exist, QGIS Desktop has no
   connection entries pointing at them.

## Decisions made during brainstorming

- **Granularity:** one WMS + one WFS connection per gpkg (~17 entries today).
  Not grouping per-folder, not flattening into a single "clipped_data" theme.
- **Disambiguation scope:** every gpkg under `clipped_data/` carries its
  parent-folder name in the id. Files directly under `data_dir` and files
  under `territories/` keep their existing bare-stem ids. Rationale: a
  consistent rule for clipped_data, no special-casing per file, and zero
  churn on the two oldest connections.
- **Slug shape:** `<folder_slug>__<stem>` — double underscore as the
  folder/stem boundary so the parts can be read back unambiguously.
- **Display title:** drop the stem when it is a case-insensitive substring
  of the folder slug (handles "Mecklenburg Greenways/Greenways.gpkg" →
  "Mecklenburg Greenways"); otherwise `"<folder> – <stem-titled>"`.
- **Distribution:** the watcher writes the connection XML files into
  `/srv/qgis/web/`, served live by the existing viewer nginx at
  `http://qgis.devbox/qgis-wms-connections.xml` and
  `http://qgis.devbox/qgis-wfs-connections.xml`. The static files in
  `client/connections/` are removed.

## Naming rule

Two new helpers in `chart/files/generate_qgs.py`:

```python
def _slug(s: str) -> str:
    """Replace spaces and path separators with underscores; otherwise pass through."""

def _project_id(gpkg: Path, data_dir: Path) -> str:
    """The canonical id — also the .qgs stem and the MAP= value."""

def _project_title(gpkg: Path, data_dir: Path) -> str:
    """Human display title — used as WMSServiceTitle, QWC2 theme title,
    and the connection name attribute."""
```

`_project_id`:

- If `gpkg.parent == data_dir` *or* `gpkg` is under `data_dir / "territories"`:
  return `gpkg.stem`.
- Otherwise: return `f"{_slug(parent_folder_name)}__{gpkg.stem}"`.

`_project_title`:

- If `gpkg.parent == data_dir` or under `territories/`: return
  `gpkg.stem.replace("_", " ").replace("-", " ").title()` (current behavior).
- Otherwise: build `folder = parent_folder_name`, `stem_title =
  gpkg.stem.replace("_", " ").title()`. If `_slug(stem_title).lower()` is a
  substring of `_slug(folder).lower()`, return `folder`. Else return
  `f"{folder} – {stem_title}"` (en-dash separator).

### Concrete results across all 17 gpkgs

| gpkg path | id (`.qgs` stem) | title |
|---|---|---|
| `territories/territories_draft.gpkg` | `territories_draft` | Territories Draft |
| `territories/debug.gpkg` | `debug` | Debug |
| `clipped_data/addresses_residential.gpkg` | `clipped_data__addresses_residential` | Clipped Data – Addresses Residential |
| `clipped_data/Mecklenburg Addresses/addresses.gpkg` | `Mecklenburg_Addresses__addresses` | Mecklenburg Addresses |
| `clipped_data/Mecklenburg Greenways/Greenways.gpkg` | `Mecklenburg_Greenways__Greenways` | Mecklenburg Greenways |
| `clipped_data/Mecklenburg Streams/Creeks_Streams.gpkg` | `Mecklenburg_Streams__Creeks_Streams` | Mecklenburg Streams – Creeks Streams |
| `clipped_data/Mecklenburg Streets/Streets.gpkg` | `Mecklenburg_Streets__Streets` | Mecklenburg Streets |
| `clipped_data/Mecklenburg Subdivisions/Subdivisions.gpkg` | `Mecklenburg_Subdivisions__Subdivisions` | Mecklenburg Subdivisions |
| `clipped_data/NC Addresses/addresses.gpkg` | `NC_Addresses__addresses` | NC Addresses |
| `clipped_data/NC Municipal Boundaries/NCDOT_City_Municipal_Boundaries.gpkg` | `NC_Municipal_Boundaries__NCDOT_City_Municipal_Boundaries` | NC Municipal Boundaries – NCDOT City Municipal Boundaries |
| `clipped_data/NC Parcels/parcels_poly.gpkg` | `NC_Parcels__parcels_poly` | NC Parcels – Parcels Poly |
| `clipped_data/NC Parcels/parcels_pt.gpkg` | `NC_Parcels__parcels_pt` | NC Parcels – Parcels Pt |
| `clipped_data/NC Railroads/NCDOT_North_Carolina_Railroads.gpkg` | `NC_Railroads__NCDOT_North_Carolina_Railroads` | NC Railroads – NCDOT North Carolina Railroads |
| `clipped_data/NC Roads/NCDOT_State_Maintained_Roads.gpkg` | `NC_Roads__NCDOT_State_Maintained_Roads` | NC Roads – NCDOT State Maintained Roads |
| `clipped_data/NC Streams/Major_Hydrography.gpkg` | `NC_Streams__Major_Hydrography` | NC Streams – Major Hydrography |
| `clipped_data/Union Addresses/addresses.gpkg` | `Union_Addresses__addresses` | Union Addresses |
| `clipped_data/Union Roads/Roads.gpkg` | `Union_Roads__Roads` | Union Roads |
| `clipped_data/Union Streams/streams.gpkg` | `Union_Streams__streams` | Union Streams |
| `clipped_data/Union Subdivisions/Union_Subdivisions.gpkg` | `Union_Subdivisions__Union_Subdivisions` | Union Subdivisions |

A few titles still carry a redundant tail ("NC Roads – NCDOT State Maintained
Roads") because the stem is not a substring of the folder slug. The rule
stays conservative for now; we can tighten it (e.g. "drop stem if either
side is a substring of the other") after looking at the live UI.

## Project + theme generation changes

In `chart/files/generate_qgs.py`:

- Replace `_theme_id` and `_theme_title` with `_project_id` and
  `_project_title`. Both now take `(gpkg, data_dir)`.
- `write_project(gpkg, out, data_dir, …)` gains a `data_dir` parameter so it
  can compute the title via `_project_title(gpkg, data_dir)`. The `out`
  filename keeps being chosen by the caller.
- `regen_all` chooses each `out` path as `projects_dir / f"{_project_id(gpkg,
  data_dir)}.qgs"`.
- `write_themes_config` uses `_project_id` for the theme `id` and
  `_project_title` for `title` (both passed `data_dir`).
- `_prune_orphans` keys off the new id set instead of `gpkg.stem`. After this
  ships, the first regen will delete the stale `addresses.qgs` (the file
  that used to overwrite the other two) and create the three correctly-named
  ones.

No other call sites in `generate_qgs.py` need to know about path-based
naming — the rule is centralized in the two helpers.

### Side effect

QWC2 theme ids change for clipped_data files. A bookmark like `…/?t=Greenways`
becomes `…/?t=Mecklenburg_Greenways__Greenways`. The chart's
`defaultTheme: territories_draft` is unaffected (territories isn't renamed).
Acceptable because there are no production users.

## Connection-XML generation

New helper in `chart/files/generate_qgs.py`:

```python
def write_connections(
    gpkgs: list[Path],
    projects_dir: Path,
    data_dir: Path,
    out_wms: Path,
    out_wfs: Path,
    ingress_host: str,
) -> None: ...
```

For each gpkg (sorted by title for deterministic output), emit one
`<wms>` and one `<wfs>` element with attributes matching the current
hand-curated bundles:

- `name = f"{title} ({ingress_host})"`
- `url = f"http://{ingress_host}/ows/?MAP={projects_dir}/{id}.qgs"`
- WMS: `version="auto" ignoreGetMapURI="1" ignoreGetFeatureInfoURI="1"
  smoothPixmapTransform="0" ignoreAxisOrientation="0"
  invertAxisOrientation="0" dpiMode="7" referer="" authcfg=""
  username="" password=""`.
- WFS: `version="auto" maxnumfeatures="" pagesize=""
  pagingEnabled="default" featurePaging="default"
  ignoreAxisOrientation="0" invertAxisOrientation="0" referer=""
  authcfg="" username="" password=""`.

XML is emitted with the `<!DOCTYPE connections>` declaration and the
existing top-level `<qgsWMSConnections version="1.0">` /
`<qgsWFSConnections version="1.0">` wrappers, written atomically via
`atomic_write_text`.

Wiring:

- `regen_all` gains `ingress_host: str | None = None`. When set, it calls
  `write_connections(...)` after `write_themes_config`, writing to
  `web_dir / "qgis-wms-connections.xml"` and `web_dir /
  "qgis-wfs-connections.xml"`. When unset, the connection-XML step is
  skipped (parallel to how `bake_scripts_dir` gates the themes.json bake).
- `main` gains `--ingress-host` (string).
- The watcher Deployment in `chart/templates/deployment-watcher.yaml`
  passes `--ingress-host {{ .Values.ingress.host }}`.

## Distribution to clients

`/srv/qgis/web/` is already mounted into the viewer nginx pod and served
at the ingress root. Once the watcher writes the XML there, clients get:

```bash
curl -O http://qgis.devbox/qgis-wms-connections.xml
curl -O http://qgis.devbox/qgis-wfs-connections.xml
```

then *Browser → WMS/WMTS → Load Connections…* against the downloaded file
(same flow as today). After importing, the Browser panel shows ~19
connections; right-click → *Refresh* on a connection picks up new
schemas without re-importing.

### Nginx routing

The viewer nginx in `chart/templates/configmap-viewer-nginx.yaml` has a
generic SPA fallback (`try_files $uri $uri/ /index.html`) — if the
watcher hasn't written the XML files yet, a request would silently return
the SPA HTML, which QGIS Desktop would refuse with a confusing parse
error. Mirror the existing `themesConfig.json` exact-match block for both
new files:

```nginx
location = /qgis-wms-connections.xml {
    add_header Cache-Control "no-cache" always;
    try_files $uri =404;
}
location = /qgis-wfs-connections.xml {
    add_header Cache-Control "no-cache" always;
    try_files $uri =404;
}
```

This forces an honest 404 when missing and keeps `curl -O` re-fetches
fresh.

The `client/connections/` static XML files are removed because they would
immediately go stale relative to the live data on the cluster.
`client/connections/README.md` is rewritten to point at the URLs above and
keep the existing DNS / `ignoreGetMapURI` explanations.

## Testing

Unit tests added to `tests/test_generate_qgs.py`:

- `_project_id` and `_project_title` exercised against all 17 gpkg paths
  in the table above (parametrize over a fixture list, assert exact
  strings).
- `_project_title`'s "drop redundant stem" branch is asserted both
  positively (Mecklenburg Greenways/Greenways → "Mecklenburg Greenways")
  and negatively (NC Parcels/parcels_pt → "NC Parcels – Parcels Pt").
- `write_connections` produces XML that parses with `ElementTree`,
  contains the expected number of `<wms>` and `<wfs>` elements, has
  `name` and `url` attributes matching the rule, and is sorted by title.
- `_prune_orphans` regression: a stale `addresses.qgs` is deleted when
  the current gpkg set produces ids `Mecklenburg_Addresses__addresses`,
  `NC_Addresses__addresses`, `Union_Addresses__addresses` instead.

## Rollout

1. Land the chart change.
2. Redeploy: `helm upgrade qgis ./chart -n qgis`. The watcher restarts (its
   `checksum/script` annotation rolls because `generate_qgs.py` changed).
3. The first regen rewrites the project set: stale `addresses.qgs` (and
   any other id-renamed file) is pruned; new path-based names appear;
   `qgis-{wms,wfs}-connections.xml` are written.
4. Operators re-download the connection XML and re-import on each
   QGIS Desktop client (or right-click → *Reload Connections* if they
   prefer to keep the old import in place).

## Out of scope

- Authentication / authorization on the connection endpoints. The
  qgis.devbox ingress remains LAN/Tailscale-trusted.
- Connection bundles for any data outside `/srv/qgis/data/`.
- Auto-refresh in QGIS Desktop. QGIS does not poll connection XMLs; users
  re-download manually after a structural change.
