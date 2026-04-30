# QWC2 Web Client Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the bare-bones Leaflet viewer with QWC2 (standalone build) and add a filesystem watcher that regenerates per-gpkg `.qgs` files and `themesConfig.json` whenever data changes.

**Architecture:** Standalone qwc2 (built once via `git subtree` + `client/overlay/` + `node:22` container, dropped onto a hostPath at `/srv/qgis/web/`). One QGIS project file per `.gpkg`, auto-discovered. A `project-watcher` Deployment runs `generate_qgs.py --watch` continuously, regenerating `*.qgs` + `themesConfig.json` on inotify events with 1-second debouncing and atomic writes.

**Tech Stack:** Python 3.12 (stdlib + `watchdog`), QGIS Server 3.34, qwc2 (React/OpenLayers — vendored), Helm 3, k3s with Traefik, nginx (alpine).

**Spec:** `docs/superpowers/specs/2026-04-28-qwc2-webclient-design.md`

---

## File Structure

**Create:**

- `Makefile` — top-level build targets (`client`, `install-client`, `clean-client`).
- `client/qwc2-demo-app/` — vendored upstream via `git subtree --squash`. **Never hand-edited.**
- `client/overlay/static/config.json` — qwc2 plugin set.
- `client/overlay/js/appConfig.js` — plugin imports for the above.
- `client/README.md` — overlay/build docs.
- `chart/templates/deployment-watcher.yaml` — single-replica watcher Deployment.

**Modify:**

- `chart/files/generate_qgs.py` — refactor to per-gpkg projects + themesConfig + `--once`/`--watch` CLI.
- `chart/templates/configmap-generator.yaml` → rename to `chart/templates/configmap-watcher.yaml`; ConfigMap name changes to `project-watcher`.
- `chart/templates/deployment-qgis-server.yaml` — drop `QGIS_PROJECT_FILE` env, add init container that waits for `*.qgs` files, change probes to use a fixed default project.
- `chart/templates/deployment-viewer.yaml` — switch volume from ConfigMap (`viewer-html`) to PVC subpath (`web/`).
- `chart/values.yaml` — add `defaultTheme`, `watcher.enabled`, `watcher.debounceSeconds`, `resources.watcher`; drop `projectFile`, `generator`, `resources.generator`.
- `chart/Chart.yaml` — bump version to `0.2.0`.
- `chart/README.md` — document `make install-client` prereq, new iteration loop, watcher pause hatch.
- `tests/test_generate_qgs.py` — extend with multi-project + themesConfig + atomic-write + prune tests.
- `.gitignore` — add `client/qwc2-demo-app/dist/`, `client/qwc2-demo-app/node_modules/`.

**Delete:**

- `chart/files/index.html`
- `chart/templates/configmap-viewer.yaml`
- `chart/templates/job-project-generator.yaml`

---

## Phase 1 — Generator refactor

The current generator writes a single combined `project.qgs`. We refactor it into a `regen_all()` orchestrator that produces one project file per gpkg plus a `themesConfig.json`, with atomic writes and a watch mode. Pure Python (stdlib + `watchdog` lazy-imported only for `--watch` mode), so the existing test suite continues to run without new system deps.

### Task 1: Add atomic-write helper

**Files:**
- Modify: `chart/files/generate_qgs.py`
- Test: `tests/test_generate_qgs.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_generate_qgs.py`:

```python
def test_atomic_write_text_replaces_target_atomically(tmp_path):
    target = tmp_path / "out.txt"
    target.write_text("old contents")

    gen.atomic_write_text(target, "new contents")

    assert target.read_text() == "new contents"
    # No leftover .tmp files
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "out.txt"]
    assert leftovers == []
```

- [ ] **Step 2: Run test to verify it fails**

```sh
.venv/bin/pytest tests/test_generate_qgs.py::test_atomic_write_text_replaces_target_atomically -v
```

Expected: `FAILED` with `AttributeError: module 'generate_qgs' has no attribute 'atomic_write_text'`.

- [ ] **Step 3: Implement `atomic_write_text`**

Add to `chart/files/generate_qgs.py`, after the imports block (around line 35, before the `_QGIS_SRSID` constant):

```python
def atomic_write_text(path: Path, contents: str) -> None:
    """Write *contents* to *path* atomically.

    Writes to a sibling tempfile then os.rename, which is atomic on POSIX.
    Readers (qgis-server) never see a half-written file.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(contents)
    tmp.replace(path)
```

- [ ] **Step 4: Run test to verify it passes**

```sh
.venv/bin/pytest tests/test_generate_qgs.py::test_atomic_write_text_replaces_target_atomically -v
```

Expected: `PASSED`.

- [ ] **Step 5: Commit**

```sh
git add chart/files/generate_qgs.py tests/test_generate_qgs.py
git commit -m "feat(generator): add atomic_write_text helper"
```

---

### Task 2: Per-gpkg `write_project` function

Refactor the file-output path of the generator so we can emit one `.qgs` per gpkg.

**Files:**
- Modify: `chart/files/generate_qgs.py`
- Test: `tests/test_generate_qgs.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_generate_qgs.py`:

```python
def test_write_project_writes_one_qgs_per_gpkg(tmp_path):
    main_gpkg = tmp_path / "territories_draft.gpkg"
    debug_gpkg = tmp_path / "debug.gpkg"
    _make_minimal_gpkg(main_gpkg, layer_name="territories")
    _make_minimal_gpkg(debug_gpkg, layer_name="step_500_addresses")

    out_dir = tmp_path / "projects"
    out_dir.mkdir()

    gen.write_project(main_gpkg, out_dir / "territories_draft.qgs")
    gen.write_project(debug_gpkg, out_dir / "debug.qgs")

    assert (out_dir / "territories_draft.qgs").exists()
    assert (out_dir / "debug.qgs").exists()

    main_root = ET.fromstring((out_dir / "territories_draft.qgs").read_text())
    main_names = {ml.findtext("layername")
                  for ml in main_root.findall("./projectlayers/maplayer")}
    debug_root = ET.fromstring((out_dir / "debug.qgs").read_text())
    debug_names = {ml.findtext("layername")
                   for ml in debug_root.findall("./projectlayers/maplayer")}

    assert main_names == {"territories"}
    assert debug_names == {"step_500_addresses"}
```

- [ ] **Step 2: Run test to verify it fails**

```sh
.venv/bin/pytest tests/test_generate_qgs.py::test_write_project_writes_one_qgs_per_gpkg -v
```

Expected: `FAILED` with `AttributeError: module 'generate_qgs' has no attribute 'write_project'`.

- [ ] **Step 3: Implement `write_project`**

Add to `chart/files/generate_qgs.py`, after the `render_qgs()` function (around line 638):

```python
def write_project(gpkg: Path, out: Path,
                  project_crs_authid: str = "EPSG:3857") -> None:
    """Generate the .qgs for a single gpkg and write it atomically to *out*."""
    layers = introspect_gpkg(gpkg)
    if not layers:
        raise ValueError(f"no feature-table layers in {gpkg}")
    atomic_write_text(out, render_qgs(layers, project_crs_authid))
```

- [ ] **Step 4: Run test to verify it passes**

```sh
.venv/bin/pytest tests/test_generate_qgs.py::test_write_project_writes_one_qgs_per_gpkg -v
```

Expected: `PASSED`.

- [ ] **Step 5: Commit**

```sh
git add chart/files/generate_qgs.py tests/test_generate_qgs.py
git commit -m "feat(generator): add write_project for per-gpkg output"
```

---

### Task 3: `themesConfig.json` writer — basic shape

We split this across two tasks: the basic shape first (one theme per gpkg with bbox), then search providers in Task 5.

**Files:**
- Modify: `chart/files/generate_qgs.py`
- Test: `tests/test_generate_qgs.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_generate_qgs.py`:

```python
import json


def test_write_themes_config_emits_one_theme_per_gpkg(tmp_path):
    main_gpkg = tmp_path / "territories_draft.gpkg"
    debug_gpkg = tmp_path / "debug.gpkg"
    _make_minimal_gpkg(main_gpkg, layer_name="territories")
    _make_minimal_gpkg(debug_gpkg, layer_name="step_500_addresses")

    out = tmp_path / "themesConfig.json"

    gen.write_themes_config(
        gpkgs=[main_gpkg, debug_gpkg],
        projects_dir=Path("/srv/qgis/projects"),
        out=out,
        default_theme="territories_draft",
    )

    cfg = json.loads(out.read_text())
    items = cfg["themes"]["items"]
    ids = {it["id"] for it in items}
    assert ids == {"territories_draft", "debug"}

    by_id = {it["id"]: it for it in items}
    assert by_id["territories_draft"]["default"] is True
    assert by_id["debug"]["default"] is False
    assert by_id["territories_draft"]["url"] == \
        "/ows/?MAP=/srv/qgis/projects/territories_draft.qgs"
    assert by_id["territories_draft"]["mapCrs"] == "EPSG:3857"
    assert by_id["territories_draft"]["bbox"]["crs"] == "EPSG:4326"
    assert len(by_id["territories_draft"]["bbox"]["bounds"]) == 4
```

- [ ] **Step 2: Run test to verify it fails**

```sh
.venv/bin/pytest tests/test_generate_qgs.py::test_write_themes_config_emits_one_theme_per_gpkg -v
```

Expected: `FAILED` with `AttributeError`.

- [ ] **Step 3: Implement `write_themes_config`**

Add to `chart/files/generate_qgs.py`, after `write_project`:

```python
import json as _json


def _theme_id(gpkg: Path) -> str:
    """The theme id is the gpkg filename without extension."""
    return gpkg.stem


def _theme_title(gpkg: Path) -> str:
    """Title-case the gpkg stem for display: territories_draft → Territories Draft."""
    return gpkg.stem.replace("_", " ").replace("-", " ").title()


def _gpkg_wgs84_bbox(
    gpkg: Path,
) -> tuple[float, float, float, float]:
    """Compute the WGS84 envelope across all feature tables in *gpkg*.

    Reads gpkg_contents.min/max_x/y per layer, projects each layer's bbox
    to WGS84 (using the static SRS lookup in _bbox_to_wgs84), unions them.
    Returns (west, south, east, north). Falls back to a global envelope
    [-180, -85, 180, 85] if no layer can be projected.
    """
    layers = introspect_gpkg(gpkg)
    proj_bboxes: list[tuple[float, float, float, float]] = []
    for layer in layers:
        mnx, mny, mxx, mxy = layer.bbox
        wgs = _bbox_to_wgs84(mnx, mny, mxx, mxy, layer.srs_id)
        if wgs is not None:
            proj_bboxes.append(wgs)
    if not proj_bboxes:
        return (-180.0, -85.0, 180.0, 85.0)
    w = min(b[0] for b in proj_bboxes)
    s = min(b[1] for b in proj_bboxes)
    e = max(b[2] for b in proj_bboxes)
    n = max(b[3] for b in proj_bboxes)
    return (w, s, e, n)


def write_themes_config(
    gpkgs: list[Path],
    projects_dir: Path,
    out: Path,
    default_theme: str | None = None,
) -> None:
    """Write the qwc2 themesConfig.json from a list of gpkgs."""
    items = []
    for gpkg in sorted(gpkgs, key=lambda p: p.stem):
        tid = _theme_id(gpkg)
        w, s, e, n = _gpkg_wgs84_bbox(gpkg)
        items.append({
            "id": tid,
            "title": _theme_title(gpkg),
            "url": f"/ows/?MAP={projects_dir}/{tid}.qgs",
            "default": (default_theme == tid),
            "format": "image/png",
            "tiled": True,
            "attribution": "",
            "mapCrs": "EPSG:3857",
            "additionalMouseCrs": ["EPSG:2264"],
            "bbox": {"crs": "EPSG:4326", "bounds": [w, s, e, n]},
            "scales": [
                4000000, 2000000, 1000000, 500000, 250000, 100000,
                50000, 25000, 10000, 5000, 2500, 1000, 500, 250, 100,
            ],
            "searchProviders": [],
            "backgroundLayers": [{"name": "osm", "visibility": True}],
            "thumbnail": "img/mapthumbs/default.jpg",
        })

    config = {
        "themes": {"title": "Themes", "items": items},
        "backgroundLayers": [
            {
                "name": "osm",
                "title": "OpenStreetMap",
                "type": "osm",
                "source": "osm",
                "thumbnail": "img/mapthumbs/mapnik.png",
            }
        ],
        "defaultMapCrs": "EPSG:3857",
        "defaultBackgroundLayers": ["osm"],
        "defaultSearchProviders": [],
        "pluginData": {},
    }

    atomic_write_text(out, _json.dumps(config, indent=2) + "\n")
```

- [ ] **Step 4: Run test to verify it passes**

```sh
.venv/bin/pytest tests/test_generate_qgs.py::test_write_themes_config_emits_one_theme_per_gpkg -v
```

Expected: `PASSED`.

- [ ] **Step 5: Commit**

```sh
git add chart/files/generate_qgs.py tests/test_generate_qgs.py
git commit -m "feat(generator): write themesConfig.json (basic shape, no search)"
```

---

### Task 4: Search-provider heuristic for `territories` layer

**Files:**
- Modify: `chart/files/generate_qgs.py`
- Test: `tests/test_generate_qgs.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_generate_qgs.py`:

```python
def _make_territories_gpkg(path: Path) -> None:
    """Make a gpkg with a layer named 'territories' and the curated columns."""
    con = sqlite3.connect(path)
    cur = con.cursor()
    cur.executescript(
        textwrap.dedent(
            """
            CREATE TABLE gpkg_contents (
              table_name TEXT PRIMARY KEY,
              data_type TEXT NOT NULL,
              identifier TEXT,
              description TEXT,
              last_change DATETIME,
              min_x DOUBLE, min_y DOUBLE, max_x DOUBLE, max_y DOUBLE,
              srs_id INTEGER
            );
            CREATE TABLE gpkg_geometry_columns (
              table_name TEXT PRIMARY KEY,
              column_name TEXT NOT NULL,
              geometry_type_name TEXT NOT NULL,
              srs_id INTEGER NOT NULL,
              z TINYINT, m TINYINT
            );
            CREATE TABLE territories (
              fid INTEGER PRIMARY KEY,
              geom BLOB,
              terr_id TEXT,
              muni_name TEXT,
              subdiv TEXT,
              type TEXT
            );
            INSERT INTO gpkg_contents VALUES
              ('territories', 'features', 'territories', '', '2026-01-01',
               1000000, 500000, 1100000, 600000, 2264);
            INSERT INTO gpkg_geometry_columns VALUES
              ('territories', 'geom', 'POLYGON', 2264, 0, 0);
            """
        )
    )
    con.commit()
    con.close()


def test_themes_config_includes_search_providers_for_territories(tmp_path):
    gpkg = tmp_path / "territories_draft.gpkg"
    _make_territories_gpkg(gpkg)
    out = tmp_path / "themesConfig.json"

    gen.write_themes_config(
        gpkgs=[gpkg],
        projects_dir=Path("/srv/qgis/projects"),
        out=out,
        default_theme="territories_draft",
    )

    cfg = json.loads(out.read_text())
    providers = cfg["themes"]["items"][0]["searchProviders"]
    titles = [p["params"]["title"] for p in providers]
    fields = [p["params"]["expression"] for p in providers]

    assert "Territory ID" in titles
    assert "Municipality" in titles
    assert "Subdivision" in titles
    assert any("terr_id" in expr for expr in fields)
    assert any("muni_name" in expr for expr in fields)


def test_themes_config_no_search_providers_when_layer_missing(tmp_path):
    gpkg = tmp_path / "debug.gpkg"
    _make_minimal_gpkg(gpkg, layer_name="step_500_addresses")
    out = tmp_path / "themesConfig.json"

    gen.write_themes_config(
        gpkgs=[gpkg],
        projects_dir=Path("/srv/qgis/projects"),
        out=out,
        default_theme=None,
    )

    cfg = json.loads(out.read_text())
    assert cfg["themes"]["items"][0]["searchProviders"] == []
```

- [ ] **Step 2: Run tests to verify they fail**

```sh
.venv/bin/pytest tests/test_generate_qgs.py::test_themes_config_includes_search_providers_for_territories tests/test_generate_qgs.py::test_themes_config_no_search_providers_when_layer_missing -v
```

Expected: both fail (search providers list is currently always empty).

- [ ] **Step 3: Add the heuristic**

In `chart/files/generate_qgs.py`, replace the empty `"searchProviders": [],` literal in `write_themes_config` with a computed value:

```python
        items.append({
            "id": tid,
            "title": _theme_title(gpkg),
            "url": f"/ows/?MAP={projects_dir}/{tid}.qgs",
            "default": (default_theme == tid),
            "format": "image/png",
            "tiled": True,
            "attribution": "",
            "mapCrs": "EPSG:3857",
            "additionalMouseCrs": ["EPSG:2264"],
            "bbox": {"crs": "EPSG:4326", "bounds": [w, s, e, n]},
            "scales": [
                4000000, 2000000, 1000000, 500000, 250000, 100000,
                50000, 25000, 10000, 5000, 2500, 1000, 500, 250, 100,
            ],
            "searchProviders": _search_providers_for(gpkg),
            "backgroundLayers": [{"name": "osm", "visibility": True}],
            "thumbnail": "img/mapthumbs/default.jpg",
        })
```

And add the helper above `write_themes_config`:

```python
# Curated search-provider heuristics. Add new entries here as more layers
# gain user-meaningful searchable columns. Keys are layer names; values are
# lists of (title, column_name) pairs.
_SEARCH_PROVIDER_HEURISTICS: dict[str, list[tuple[str, str]]] = {
    "territories": [
        ("Territory ID", "terr_id"),
        ("Municipality", "muni_name"),
        ("Subdivision", "subdiv"),
    ],
}


def _search_providers_for(gpkg: Path) -> list[dict]:
    """Return a list of qwc2 search providers for layers in *gpkg*.

    Emits providers only when (a) the layer name is in the curated heuristic
    table and (b) the named columns actually exist in the layer.
    """
    layers = introspect_gpkg(gpkg)
    providers: list[dict] = []
    for layer in layers:
        spec = _SEARCH_PROVIDER_HEURISTICS.get(layer.name)
        if not spec:
            continue
        present = set(layer.columns)
        for title, column in spec:
            if column not in present:
                continue
            providers.append({
                "provider": "qgis",
                "params": {
                    "title": title,
                    "layerName": layer.name,
                    "expression": f'"{column}" ILIKE :value || \'%\'',
                },
            })
    return providers
```

- [ ] **Step 4: Run tests to verify they pass**

```sh
.venv/bin/pytest tests/test_generate_qgs.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```sh
git add chart/files/generate_qgs.py tests/test_generate_qgs.py
git commit -m "feat(generator): heuristic search providers for territories layer"
```

---

### Task 5: `regen_all` orchestrator with prune

**Files:**
- Modify: `chart/files/generate_qgs.py`
- Test: `tests/test_generate_qgs.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_generate_qgs.py`:

```python
def test_regen_all_writes_projects_and_themes_config_idempotent(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    _make_territories_gpkg(data / "territories_draft.gpkg")
    _make_minimal_gpkg(data / "debug.gpkg", layer_name="step_500")

    projects = tmp_path / "projects"
    web = tmp_path / "web"
    projects.mkdir()
    web.mkdir()

    report1 = gen.regen_all(data, projects, web, default_theme="territories_draft")

    assert sorted(p.name for p in projects.iterdir()) == \
        ["debug.qgs", "territories_draft.qgs"]
    assert (web / "themesConfig.json").exists()
    assert report1.written_projects == 2

    # Re-running with no changes should still succeed (idempotent).
    report2 = gen.regen_all(data, projects, web, default_theme="territories_draft")
    assert report2.written_projects == 2


def test_regen_all_prunes_orphaned_qgs_files(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    _make_minimal_gpkg(data / "alpha.gpkg", layer_name="alpha")

    projects = tmp_path / "projects"
    web = tmp_path / "web"
    projects.mkdir()
    web.mkdir()

    # Pre-populate a stale .qgs that has no source gpkg
    (projects / "ghost.qgs").write_text("<qgis></qgis>")

    gen.regen_all(data, projects, web, default_theme=None)

    assert (projects / "alpha.qgs").exists()
    assert not (projects / "ghost.qgs").exists()
```

- [ ] **Step 2: Run tests to verify they fail**

```sh
.venv/bin/pytest tests/test_generate_qgs.py::test_regen_all_writes_projects_and_themes_config_idempotent tests/test_generate_qgs.py::test_regen_all_prunes_orphaned_qgs_files -v
```

Expected: both fail (`regen_all` not defined).

- [ ] **Step 3: Implement `regen_all`**

Add to `chart/files/generate_qgs.py`, after `write_themes_config`:

```python
@dataclass
class RegenReport:
    written_projects: int
    pruned_projects: int
    skipped: bool = False  # True if .no-regen marker present


def _prune_orphans(projects_dir: Path, current_stems: set[str]) -> int:
    """Delete any *.qgs in projects_dir whose stem is not in current_stems."""
    pruned = 0
    for qgs in projects_dir.glob("*.qgs"):
        if qgs.stem not in current_stems:
            qgs.unlink()
            pruned += 1
    return pruned


def regen_all(
    data_dir: Path,
    projects_dir: Path,
    web_dir: Path,
    default_theme: str | None,
) -> RegenReport:
    """Idempotently rebuild all per-gpkg .qgs files and themesConfig.json.

    Honors a /srv/qgis/.no-regen marker (computed as data_dir.parent / ".no-regen"):
    if present, returns a RegenReport with skipped=True and writes nothing.
    """
    no_regen = data_dir.parent / ".no-regen"
    if no_regen.exists():
        return RegenReport(written_projects=0, pruned_projects=0, skipped=True)

    projects_dir.mkdir(parents=True, exist_ok=True)
    web_dir.mkdir(parents=True, exist_ok=True)

    gpkgs = discover_gpkgs(data_dir)
    current_stems = {gpkg.stem for gpkg in gpkgs}

    written = 0
    for gpkg in gpkgs:
        try:
            write_project(gpkg, projects_dir / f"{gpkg.stem}.qgs")
            written += 1
        except Exception as exc:
            print(f"failed to render {gpkg.name}: {exc}", file=sys.stderr)

    pruned = _prune_orphans(projects_dir, current_stems)

    write_themes_config(
        gpkgs=gpkgs,
        projects_dir=projects_dir,
        out=web_dir / "themesConfig.json",
        default_theme=default_theme,
    )

    return RegenReport(written_projects=written, pruned_projects=pruned)
```

- [ ] **Step 4: Run tests to verify they pass**

```sh
.venv/bin/pytest tests/test_generate_qgs.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```sh
git add chart/files/generate_qgs.py tests/test_generate_qgs.py
git commit -m "feat(generator): regen_all orchestrator with orphan pruning"
```

---

### Task 6: `.no-regen` pause hatch test

**Files:**
- Test: `tests/test_generate_qgs.py`

- [ ] **Step 1: Write the test**

Append to `tests/test_generate_qgs.py`:

```python
def test_regen_all_skips_when_no_regen_marker_present(tmp_path):
    # Layout matches /srv/qgis/{.no-regen, data/, projects/, web/}
    root = tmp_path
    data = root / "data"
    projects = root / "projects"
    web = root / "web"
    data.mkdir(); projects.mkdir(); web.mkdir()
    _make_minimal_gpkg(data / "alpha.gpkg", layer_name="alpha")

    (root / ".no-regen").touch()

    report = gen.regen_all(data, projects, web, default_theme=None)

    assert report.skipped is True
    assert report.written_projects == 0
    assert list(projects.iterdir()) == []
    assert not (web / "themesConfig.json").exists()
```

- [ ] **Step 2: Run test**

```sh
.venv/bin/pytest tests/test_generate_qgs.py::test_regen_all_skips_when_no_regen_marker_present -v
```

Expected: `PASSED` (the marker logic was added in Task 5; this test pins the contract).

- [ ] **Step 3: Commit**

```sh
git add tests/test_generate_qgs.py
git commit -m "test(generator): pin .no-regen pause behaviour"
```

---

### Task 7: New CLI shape (`--once` and `--watch`)

Replace the old `--output project.qgs` CLI with the new directory-based one.

**Files:**
- Modify: `chart/files/generate_qgs.py`
- Test: `tests/test_generate_qgs.py`

- [ ] **Step 1: Update existing test for new CLI**

In `tests/test_generate_qgs.py`, replace the body of `test_main_writes_project_qgs` with this (keep the function name but rewrite the contents):

```python
def test_main_writes_project_qgs(tmp_path):
    root = tmp_path
    data = root / "data"
    projects = root / "projects"
    web = root / "web"
    data.mkdir(); projects.mkdir(); web.mkdir()
    _make_minimal_gpkg(data / "a.gpkg", layer_name="alpha")
    _make_minimal_gpkg(data / "b.gpkg", layer_name="beta")

    rc = gen.main([
        "--once",
        "--data-dir", str(data),
        "--projects-dir", str(projects),
        "--web-dir", str(web),
    ])

    assert rc == 0
    assert sorted(p.name for p in projects.iterdir()) == ["a.qgs", "b.qgs"]
    assert (web / "themesConfig.json").exists()


def test_main_once_with_default_theme_marks_default(tmp_path):
    root = tmp_path
    data = root / "data"; projects = root / "projects"; web = root / "web"
    data.mkdir(); projects.mkdir(); web.mkdir()
    _make_minimal_gpkg(data / "a.gpkg", layer_name="alpha")
    _make_minimal_gpkg(data / "b.gpkg", layer_name="beta")

    rc = gen.main([
        "--once",
        "--data-dir", str(data),
        "--projects-dir", str(projects),
        "--web-dir", str(web),
        "--default-theme", "b",
    ])
    assert rc == 0

    cfg = json.loads((web / "themesConfig.json").read_text())
    by_id = {it["id"]: it for it in cfg["themes"]["items"]}
    assert by_id["b"]["default"] is True
    assert by_id["a"]["default"] is False
```

- [ ] **Step 2: Run tests to verify they fail**

```sh
.venv/bin/pytest tests/test_generate_qgs.py::test_main_writes_project_qgs tests/test_generate_qgs.py::test_main_once_with_default_theme_marks_default -v
```

Expected: both fail (old CLI uses `--output`, not the new flags).

- [ ] **Step 3: Replace `main()` with the new CLI**

Replace the `main()` function in `chart/files/generate_qgs.py` (lines 649–688 currently) with:

```python
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Generate QGIS Server project files and qwc2 themesConfig.json from GeoPackages.",
    )
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true",
                      help="Regenerate once and exit.")
    mode.add_argument("--watch", action="store_true",
                      help="Regenerate, then watch for changes (inotify).")
    p.add_argument("--data-dir", required=True,
                   help="Directory containing *.gpkg (scanned recursively).")
    p.add_argument("--projects-dir", required=True,
                   help="Directory where per-gpkg .qgs files are written.")
    p.add_argument("--web-dir", required=True,
                   help="Directory where themesConfig.json is written.")
    p.add_argument("--default-theme", default=None,
                   help="Theme id (gpkg stem) marked as default in themesConfig.")
    p.add_argument("--debounce-seconds", type=float, default=1.0,
                   help="Watch-mode debounce window (default 1.0).")
    args = p.parse_args(argv)

    data_dir = Path(args.data_dir)
    projects_dir = Path(args.projects_dir)
    web_dir = Path(args.web_dir)

    if not data_dir.is_dir():
        print(f"data_dir not found or not a directory: {data_dir}",
              file=sys.stderr)
        return 2

    report = regen_all(data_dir, projects_dir, web_dir, args.default_theme)
    if report.skipped:
        print(".no-regen marker present; skipped initial regen", file=sys.stderr)
    else:
        print(f"regen: wrote {report.written_projects} project(s), "
              f"pruned {report.pruned_projects}, themesConfig OK")

    if args.once:
        return 0

    # --watch: enter the watch loop. Lazy-import watchdog so --once mode
    # works in environments without watchdog installed (the test suite).
    return _watch_loop(data_dir, projects_dir, web_dir,
                      args.default_theme, args.debounce_seconds)


def _watch_loop(data_dir: Path, projects_dir: Path, web_dir: Path,
                default_theme: str | None, debounce_seconds: float) -> int:
    from watchdog.events import PatternMatchingEventHandler
    from watchdog.observers import Observer
    import threading
    import time

    pending = threading.Event()

    class Handler(PatternMatchingEventHandler):
        def on_any_event(self, event):
            pending.set()

    handler = Handler(patterns=["*.gpkg"], ignore_directories=True)
    observer = Observer()
    observer.schedule(handler, str(data_dir), recursive=True)
    observer.start()
    print(f"watching {data_dir} for *.gpkg changes "
          f"(debounce={debounce_seconds}s)")

    # Heartbeat for liveness probe
    heartbeat = Path("/tmp/heartbeat")
    ready = Path("/tmp/ready")
    ready.touch()

    try:
        while True:
            if pending.wait(timeout=30.0):
                # Debounce: wait until events settle
                while pending.is_set():
                    pending.clear()
                    time.sleep(debounce_seconds)
                report = regen_all(data_dir, projects_dir, web_dir, default_theme)
                if report.skipped:
                    print(".no-regen present; skipped")
                else:
                    print(f"regen: wrote {report.written_projects} "
                          f"project(s), pruned {report.pruned_projects}")
            heartbeat.touch()
    except KeyboardInterrupt:
        observer.stop()
    observer.join()
    return 0
```

- [ ] **Step 4: Run tests to verify they pass**

```sh
.venv/bin/pytest tests/test_generate_qgs.py -v
```

Expected: all tests pass (the watch loop is not exercised — only the `--once` paths).

- [ ] **Step 5: Commit**

```sh
git add chart/files/generate_qgs.py tests/test_generate_qgs.py
git commit -m "feat(generator): new CLI with --once and --watch modes"
```

---

### Task 8: Smoke-test the generator against the real data

**Files:** none — sanity check only.

- [ ] **Step 1: Run the generator against `/srv/qgis`**

```sh
mkdir -p /tmp/qgis-test/projects /tmp/qgis-test/web
.venv/bin/python chart/files/generate_qgs.py \
    --once \
    --data-dir /srv/qgis/data \
    --projects-dir /tmp/qgis-test/projects \
    --web-dir /tmp/qgis-test/web \
    --default-theme territories_draft
```

Expected output: `regen: wrote 2 project(s), pruned 0, themesConfig OK`.

- [ ] **Step 2: Verify outputs**

```sh
ls /tmp/qgis-test/projects/
# expected: debug.qgs  territories_draft.qgs

cat /tmp/qgis-test/web/themesConfig.json | python -m json.tool | head -40
```

Expect a JSON document whose `themes.items` array has two entries, the territories one marked `"default": true`.

- [ ] **Step 3: Sanity-clean the temp dir**

```sh
rm -rf /tmp/qgis-test
```

(No commit — this is a verification step.)

---

## Phase 2 — qwc2 client build

### Task 9: Vendor qwc2-demo-app via `git subtree`

**Files:** large — `client/qwc2-demo-app/` + thousands of files from upstream.

- [ ] **Step 1: Verify a clean working tree**

```sh
git status
```

Expected: `working tree clean`.

- [ ] **Step 2: Add qwc2-demo-app subtree**

```sh
git subtree add --prefix=client/qwc2-demo-app \
    https://github.com/qgis/qwc2-demo-app master --squash
```

This creates two commits: a squash-merge commit and a wrapper merge commit. Both go on `main`.

Expected output: `Added dir 'client/qwc2-demo-app'` and a clean status afterwards.

- [ ] **Step 3: Verify structure**

```sh
ls client/qwc2-demo-app/
```

Expected entries include: `appConfig.js`, `index.html`, `js/`, `static/`, `package.json`, `webpack.config.js`, `yarn.lock` and others.

- [ ] **Step 4: Update `.gitignore` for build outputs**

Append to `.gitignore`:

```
# qwc2 client build artefacts
client/qwc2-demo-app/dist/
client/qwc2-demo-app/node_modules/
client/qwc2-demo-app/.cache/
```

- [ ] **Step 5: Commit gitignore changes**

```sh
git add .gitignore
git commit -m "chore: ignore qwc2-demo-app build artefacts"
```

(No need to commit `client/qwc2-demo-app/` — `git subtree add` already did that in step 2.)

---

### Task 10: Add `client/overlay/static/config.json`

The qwc2 plugin set we want enabled for our deployment.

**Files:**
- Create: `client/overlay/static/config.json`

- [ ] **Step 1: Inspect upstream's stock config**

```sh
head -80 client/qwc2-demo-app/static/config.json
```

This shows the canonical structure. Note plugin keys, the desktop/mobile split, and any global keys like `assetsPath`.

- [ ] **Step 2: Create the overlay config**

Create `client/overlay/static/config.json` with these contents:

```json
{
  "proxyServiceUrl": "",
  "permalinkServiceUrl": "",
  "elevationServiceUrl": "",
  "featureReportService": "",
  "editServiceUrl": "",
  "searchServiceUrl": "",
  "authServiceUrl": "",
  "mapInfoService": "",
  "translationsPath": "translations",
  "assetsPath": "assets",
  "urlPositionFormat": "centerAndZoom",
  "urlPositionCrs": "EPSG:3857",
  "omitUrlParameterUpdates": false,
  "preserveExtentOnThemeSwitch": false,
  "preserveBackgroundOnThemeSwitch": true,
  "preserveNonThemeLayersOnThemeSwitch": false,
  "preserveExtraLayersOnThemeSwitch": false,
  "allowReorderingLayers": true,
  "allowRemovingThemeLayers": false,
  "allowAddingOtherThemes": false,
  "allowFractionalZoom": false,
  "globallyDisableDockableDialogs": false,
  "themeLayersListWindowSize": [400, 600],
  "plugins": {
    "common": [
      { "name": "Map", "cfg": { "mapOptions": {} } },
      { "name": "HomeButton" },
      { "name": "LocateButton" },
      { "name": "ZoomIn" },
      { "name": "ZoomOut" },
      { "name": "Search",
        "cfg": { "showProviderSelection": false, "showProvidersInPlaceholder": false } },
      { "name": "Identify",
        "cfg": { "params": { "FI_POINT_TOLERANCE": 8, "FI_LINE_TOLERANCE": 8, "FI_POLYGON_TOLERANCE": 4 } } },
      { "name": "Measure" },
      { "name": "MouseCoordinates" },
      { "name": "ScaleBar" },
      { "name": "MapTip" },
      { "name": "Print" },
      { "name": "Permalink" },
      { "name": "Share" },
      { "name": "FullScreen" },
      { "name": "Settings" },
      { "name": "ThemeSwitcher" },
      { "name": "LayerTree",
        "cfg": { "showLegendIcons": true, "showRootEntry": false, "showQueryableIcon": true, "allowImport": false, "allowMapTips": true, "allowCompare": false, "groupTogglesSublayers": true } },
      { "name": "BackgroundSwitcher" },
      { "name": "TopBar",
        "cfg": { "appMenuClearsTask": true,
                 "menuItems": [
                   { "key": "LayerTree", "icon": "layers" },
                   { "key": "Print", "icon": "print" },
                   { "key": "Share", "icon": "share" },
                   { "key": "Help", "icon": "info" }
                 ],
                 "toolbarItems": [
                   { "key": "Measure", "icon": "measure" },
                   { "key": "Identify", "icon": "identify_region", "mode": "Region" }
                 ] } },
      { "name": "BottomBar",
        "cfg": { "viewertitleUrl": "", "termsUrl": "" } },
      { "name": "Help" }
    ],
    "mobile": [],
    "desktop": []
  }
}
```

- [ ] **Step 3: Commit**

```sh
git add client/overlay/static/config.json
git commit -m "feat(client): qwc2 overlay config (plugins enabled)"
```

---

### Task 11: Add `client/overlay/js/appConfig.js`

The plugin imports for the plugins listed in `static/config.json`.

**Files:**
- Create: `client/overlay/js/appConfig.js`

- [ ] **Step 1: Inspect upstream's stock appConfig.js**

```sh
head -60 client/qwc2-demo-app/js/appConfig.js
```

Note the plugin import pattern and the export shape.

- [ ] **Step 2: Create the overlay**

Create `client/overlay/js/appConfig.js`:

```javascript
/**
 * Overlay appConfig.js — only the plugins enabled in static/config.json
 * are imported here. Keep this file in sync with that config.
 */

import MapPlugin from 'qwc2/plugins/Map';
import HomeButtonPlugin from 'qwc2/plugins/HomeButton';
import LocateButtonPlugin from 'qwc2/plugins/LocateButton';
import ZoomInPlugin from 'qwc2/plugins/ZoomButton';
import ZoomOutPlugin from 'qwc2/plugins/ZoomButton';
import SearchPlugin from 'qwc2/plugins/Search';
import IdentifyPlugin from 'qwc2/plugins/Identify';
import MeasurePlugin from 'qwc2/plugins/Measure';
import MouseCoordinatesPlugin from 'qwc2/plugins/MouseCoordinates';
import ScaleBarPlugin from 'qwc2/plugins/ScaleBar';
import MapTipPlugin from 'qwc2/plugins/MapTip';
import PrintPlugin from 'qwc2/plugins/Print';
import PermalinkPlugin from 'qwc2/plugins/Permalink';
import SharePlugin from 'qwc2/plugins/Share';
import FullScreenPlugin from 'qwc2/plugins/FullScreen';
import SettingsPlugin from 'qwc2/plugins/Settings';
import ThemeSwitcherPlugin from 'qwc2/plugins/ThemeSwitcher';
import LayerTreePlugin from 'qwc2/plugins/LayerTree';
import BackgroundSwitcherPlugin from 'qwc2/plugins/BackgroundSwitcher';
import TopBarPlugin from 'qwc2/plugins/TopBar';
import BottomBarPlugin from 'qwc2/plugins/BottomBar';
import HelpPlugin from 'qwc2/plugins/Help';

import { UrlParams } from 'qwc2/utils/PermaLinkUtils';

export default {
    pluginsDef: {
        plugins: {
            MapPlugin,
            HomeButtonPlugin,
            LocateButtonPlugin,
            ZoomInPlugin: ZoomInPlugin('in', 'ZoomIn'),
            ZoomOutPlugin: ZoomOutPlugin('out', 'ZoomOut'),
            SearchPlugin,
            IdentifyPlugin,
            MeasurePlugin,
            MouseCoordinatesPlugin,
            ScaleBarPlugin,
            MapTipPlugin,
            PrintPlugin,
            PermalinkPlugin,
            SharePlugin,
            FullScreenPlugin,
            SettingsPlugin,
            ThemeSwitcherPlugin,
            LayerTreePlugin,
            BackgroundSwitcherPlugin,
            TopBarPlugin,
            BottomBarPlugin,
            HelpPlugin,
        },
        cfg: {},
    },
    actionLogger: (action, state) => { /* no-op */ },
    initialState: { defaultLayerVisibility: true },
    themeLayerRestorer: (missingLayers, theme, layers) => Promise.resolve({ newLayers: layers, newSubLayers: {} }),
    externalLayerRestorer: null,
    snapping: false,
    storeDebug: false,
    getMapId: () => "map",
    customAssets: {},
    haveLocale: () => true,
};
```

> **Note:** if upstream's plugin import paths differ from what is shown here (e.g., qwc2 may have moved a plugin between releases), `make client` will fail with a webpack module-resolution error. In that case: open `client/qwc2-demo-app/js/appConfig.js` to see the canonical paths for the version you vendored, and update the overlay accordingly. The error will name the missing module — fix that one import and re-run.

- [ ] **Step 3: Commit**

```sh
git add client/overlay/js/appConfig.js
git commit -m "feat(client): qwc2 overlay appConfig.js (plugin imports)"
```

---

### Task 12: Top-level Makefile

**Files:**
- Create: `Makefile`

- [ ] **Step 1: Verify Docker is available on the host**

```sh
docker --version
```

Expected: a version line (e.g., `Docker version 24.x.y`). If Docker is missing on the host: `sudo apt install docker.io` and ensure the user is in the `docker` group, OR adapt the Makefile to use `nerdctl`/`podman` (drop-in compatible for the simple `run --rm -v ... node:22 bash -c ...` invocation we use).

- [ ] **Step 2: Create Makefile**

Create `Makefile` at the repo root:

```make
# Top-level make targets for the qgis web stack.
#
# - make client          : build the qwc2 dist into client/qwc2-demo-app/dist/
# - make install-client  : build, then sync into /srv/qgis/web/  (requires sudo)
# - make clean-client    : delete the dist directory
# - make test            : run the python test suite
#
# The qwc2 build runs inside a node:22 container so no host Node toolchain is
# needed. If you don't have Docker, swap NODE_RUNNER for `nerdctl` or `podman`.

CLIENT_DIR  := $(CURDIR)/client
QWC2_DIR    := $(CLIENT_DIR)/qwc2-demo-app
OVERLAY_DIR := $(CLIENT_DIR)/overlay
DIST_DIR    := $(QWC2_DIR)/dist/QWC2App
WEB_DIR     := /srv/qgis/web
NODE_IMAGE  := node:22
NODE_RUNNER := docker

UID := $(shell id -u)
GID := $(shell id -g)

.PHONY: client install-client clean-client test

client:
	rsync -av --no-perms --no-owner --no-group $(OVERLAY_DIR)/ $(QWC2_DIR)/
	$(NODE_RUNNER) run --rm \
	    -v $(QWC2_DIR):/work -w /work \
	    -u $(UID):$(GID) \
	    -e HOME=/tmp \
	    $(NODE_IMAGE) \
	    bash -c "yarn install --frozen-lockfile && yarn build"

install-client: client
	sudo install -d $(WEB_DIR)
	sudo rsync -av --delete-during $(DIST_DIR)/ $(WEB_DIR)/

clean-client:
	rm -rf $(QWC2_DIR)/dist

test:
	.venv/bin/pytest tests/ -v
```

- [ ] **Step 3: Commit**

```sh
git add Makefile
git commit -m "build: top-level Makefile (client, install-client, test)"
```

---

### Task 13: First build + smoke-test

**Files:** none (verification only).

- [ ] **Step 1: Run the build**

```sh
make client
```

Expected: yarn install pulls dependencies (a few minutes the first time), then `yarn build` produces `client/qwc2-demo-app/dist/QWC2App/`. Final stderr line should be a webpack summary (no errors).

If yarn errors out on a missing plugin import, that means an upstream rename happened between the vendored SHA and what `client/overlay/js/appConfig.js` references. See the note in Task 11 — adjust the import path in `appConfig.js` and re-run.

- [ ] **Step 2: Verify dist exists**

```sh
ls client/qwc2-demo-app/dist/QWC2App/ | head
```

Expected entries: `index.html`, `*.js` files (fingerprinted), `assets/`, `translations/`, etc.

- [ ] **Step 3: Install to /srv/qgis/web/**

```sh
make install-client
```

Expected: rsync output ending in `total size is …`. After this:

```sh
ls /srv/qgis/web/
# expected: index.html, *.js, assets/, translations/, …
```

- [ ] **Step 4: Drop a placeholder themesConfig.json**

```sh
sudo .venv/bin/python chart/files/generate_qgs.py \
    --once \
    --data-dir /srv/qgis/data \
    --projects-dir /srv/qgis/projects \
    --web-dir /srv/qgis/web \
    --default-theme territories_draft
```

Expected: `regen: wrote 2 project(s), pruned 0, themesConfig OK`. After this `/srv/qgis/web/themesConfig.json` and `/srv/qgis/projects/*.qgs` exist.

(No commit — verification only.)

---

## Phase 3 — Chart wiring

The chart switches from a one-shot Job + ConfigMap-served HTML to a continuously-running watcher Deployment + hostPath-served qwc2 dist.

### Task 14: Delete the old viewer ConfigMap and HTML

**Files:**
- Delete: `chart/files/index.html`
- Delete: `chart/templates/configmap-viewer.yaml`

- [ ] **Step 1: Delete files**

```sh
git rm chart/files/index.html chart/templates/configmap-viewer.yaml
```

- [ ] **Step 2: Verify no other manifest references them**

```sh
grep -RInE 'viewer-html|index\.html' chart/ || echo "OK: no references"
```

Expected: `OK: no references` (the only remaining reference, in `deployment-viewer.yaml`, will be fixed in Task 17).

- [ ] **Step 3: Commit**

```sh
git commit -m "chart: drop Leaflet viewer ConfigMap and index.html"
```

---

### Task 15: Replace the Job with a watcher Deployment

**Files:**
- Delete: `chart/templates/job-project-generator.yaml`
- Rename: `chart/templates/configmap-generator.yaml` → `chart/templates/configmap-watcher.yaml`
- Create: `chart/templates/deployment-watcher.yaml`

- [ ] **Step 1: Delete the Job, rename the ConfigMap**

```sh
git rm chart/templates/job-project-generator.yaml
git mv chart/templates/configmap-generator.yaml chart/templates/configmap-watcher.yaml
```

- [ ] **Step 2: Update the renamed ConfigMap**

Replace the contents of `chart/templates/configmap-watcher.yaml` with:

```yaml
{{- if .Values.watcher.enabled -}}
apiVersion: v1
kind: ConfigMap
metadata:
  name: project-watcher
  namespace: {{ .Values.namespace }}
  labels:
    {{- include "qgis.labels" . | nindent 4 }}
data:
  generate_qgs.py: |-
{{ .Files.Get "files/generate_qgs.py" | indent 4 }}
{{- end -}}
```

- [ ] **Step 3: Create the watcher Deployment**

Create `chart/templates/deployment-watcher.yaml`:

```yaml
{{- if .Values.watcher.enabled -}}
apiVersion: apps/v1
kind: Deployment
metadata:
  name: project-watcher
  namespace: {{ .Values.namespace }}
  labels:
    {{- include "qgis.labels" . | nindent 4 }}
spec:
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels:
      {{- include "qgis.selectorLabels" . | nindent 6 }}
      app.kubernetes.io/component: project-watcher
  template:
    metadata:
      labels:
        {{- include "qgis.selectorLabels" . | nindent 8 }}
        app.kubernetes.io/component: project-watcher
      annotations:
        # Force a pod restart when the script content changes.
        checksum/script: {{ .Files.Get "files/generate_qgs.py" | sha256sum }}
    spec:
      initContainers:
        - name: install-watchdog
          image: {{ .Values.image.generator }}
          imagePullPolicy: {{ .Values.image.pullPolicy }}
          command:
            - sh
            - -c
            - pip install --no-cache-dir --target=/deps watchdog
          volumeMounts:
            - name: deps
              mountPath: /deps
          resources:
            {{- toYaml .Values.resources.watcher | nindent 12 }}
      containers:
        - name: watcher
          image: {{ .Values.image.generator }}
          imagePullPolicy: {{ .Values.image.pullPolicy }}
          env:
            - name: PYTHONPATH
              value: /deps
          command:
            - python
            - /scripts/generate_qgs.py
            - --watch
            - --data-dir
            - /srv/qgis/data
            - --projects-dir
            - /srv/qgis/projects
            - --web-dir
            - /srv/qgis/web
            - --debounce-seconds
            - {{ .Values.watcher.debounceSeconds | quote }}
            {{- if .Values.defaultTheme }}
            - --default-theme
            - {{ .Values.defaultTheme | quote }}
            {{- end }}
          volumeMounts:
            - name: data
              mountPath: /srv/qgis
            - name: scripts
              mountPath: /scripts
              readOnly: true
            - name: deps
              mountPath: /deps
          readinessProbe:
            exec:
              command: [sh, -c, "test -f /tmp/ready"]
            initialDelaySeconds: 5
            periodSeconds: 5
          livenessProbe:
            exec:
              command:
                - sh
                - -c
                - "test -f /tmp/heartbeat && [ $(($(date +%s) - $(stat -c %Y /tmp/heartbeat))) -lt 120 ]"
            initialDelaySeconds: 60
            periodSeconds: 30
          resources:
            {{- toYaml .Values.resources.watcher | nindent 12 }}
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: qgis-data
        - name: scripts
          configMap:
            name: project-watcher
            defaultMode: 0555
        - name: deps
          emptyDir: {}
{{- end -}}
```

- [ ] **Step 4: Commit**

```sh
git add chart/templates/configmap-watcher.yaml chart/templates/deployment-watcher.yaml
git rm --cached chart/templates/job-project-generator.yaml 2>/dev/null || true
git commit -m "chart: replace project-generator Job with watcher Deployment"
```

(`git rm` already staged the deletions in Step 1; the explicit `git rm --cached` line is a no-op fallback.)

---

### Task 16: Update qgis-server Deployment

Drop the single-project env var and add an init container that waits for any `*.qgs` to exist. Probes hit a wildcard request that doesn't depend on a specific project name.

**Files:**
- Modify: `chart/templates/deployment-qgis-server.yaml`

- [ ] **Step 1: Replace the file**

Replace the entire contents of `chart/templates/deployment-qgis-server.yaml` with:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: qgis-server
  namespace: {{ .Values.namespace }}
  labels:
    {{- include "qgis.labels" . | nindent 4 }}
spec:
  replicas: 1
  selector:
    matchLabels:
      {{- include "qgis.selectorLabels" . | nindent 6 }}
      app.kubernetes.io/component: qgis-server
  template:
    metadata:
      labels:
        {{- include "qgis.selectorLabels" . | nindent 8 }}
        app.kubernetes.io/component: qgis-server
    spec:
      initContainers:
        - name: wait-for-projects
          image: {{ .Values.image.viewer }}
          imagePullPolicy: {{ .Values.image.pullPolicy }}
          command:
            - sh
            - -c
            - >
              until ls /srv/qgis/projects/*.qgs >/dev/null 2>&1;
              do echo "waiting for project files..."; sleep 2; done;
              echo "found:"; ls /srv/qgis/projects/*.qgs
          volumeMounts:
            - name: data
              mountPath: /srv/qgis
              readOnly: true
      containers:
        - name: qgis-server
          image: {{ .Values.image.qgisServer }}
          imagePullPolicy: {{ .Values.image.pullPolicy }}
          ports:
            - name: http
              containerPort: 80
          env:
            - name: QGIS_SERVER_LOG_LEVEL
              value: "0"
          volumeMounts:
            - name: data
              mountPath: /srv/qgis
              readOnly: true
          # Probes hit qgis-server directly (bypassing Traefik) so they use
          # the in-pod path "/", not "/ows/".  GetCapabilities without a MAP
          # parameter returns 200 with an empty service description — that's
          # all we need to confirm liveness.
          readinessProbe:
            httpGet:
              path: /?SERVICE=WMS&REQUEST=GetCapabilities
              port: 80
            initialDelaySeconds: 5
            periodSeconds: 10
            timeoutSeconds: 5
          livenessProbe:
            httpGet:
              path: /?SERVICE=WMS&REQUEST=GetCapabilities
              port: 80
            initialDelaySeconds: 15
            periodSeconds: 30
            timeoutSeconds: 5
          resources:
            {{- toYaml .Values.resources.qgisServer | nindent 12 }}
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: qgis-data
            readOnly: true
```

- [ ] **Step 2: Commit**

```sh
git add chart/templates/deployment-qgis-server.yaml
git commit -m "chart(qgis-server): wait for project files, drop QGIS_PROJECT_FILE"
```

---

### Task 17: Switch viewer Deployment to hostPath

**Files:**
- Modify: `chart/templates/deployment-viewer.yaml`

- [ ] **Step 1: Replace the file**

Replace the entire contents of `chart/templates/deployment-viewer.yaml` with:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: viewer
  namespace: {{ .Values.namespace }}
  labels:
    {{- include "qgis.labels" . | nindent 4 }}
spec:
  replicas: 1
  selector:
    matchLabels:
      {{- include "qgis.selectorLabels" . | nindent 6 }}
      app.kubernetes.io/component: viewer
  template:
    metadata:
      labels:
        {{- include "qgis.selectorLabels" . | nindent 8 }}
        app.kubernetes.io/component: viewer
    spec:
      initContainers:
        # Refuse to start nginx if /srv/qgis/web/ is empty — that means the
        # operator never ran `make install-client`, and the viewer would just
        # serve 403s.
        - name: wait-for-dist
          image: {{ .Values.image.viewer }}
          imagePullPolicy: {{ .Values.image.pullPolicy }}
          command:
            - sh
            - -c
            - >
              until [ -f /usr/share/nginx/html/index.html ];
              do echo "waiting for /srv/qgis/web/ — run \`make install-client\` on the host"; sleep 5; done;
              echo "qwc2 dist present"
          volumeMounts:
            - name: data
              mountPath: /usr/share/nginx/html
              subPath: web
              readOnly: true
      containers:
        - name: nginx
          image: {{ .Values.image.viewer }}
          imagePullPolicy: {{ .Values.image.pullPolicy }}
          ports:
            - containerPort: 80
          volumeMounts:
            - name: data
              mountPath: /usr/share/nginx/html
              subPath: web
              readOnly: true
            - name: nginx-conf
              mountPath: /etc/nginx/conf.d/default.conf
              subPath: default.conf
              readOnly: true
          readinessProbe:
            httpGet: { path: /index.html, port: 80 }
            initialDelaySeconds: 2
            periodSeconds: 5
          livenessProbe:
            httpGet: { path: /index.html, port: 80 }
            initialDelaySeconds: 10
            periodSeconds: 15
          resources:
            {{- toYaml .Values.resources.viewer | nindent 12 }}
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: qgis-data
            readOnly: true
        - name: nginx-conf
          configMap:
            name: viewer-nginx
```

- [ ] **Step 2: Add an nginx ConfigMap for SPA fallback + no-cache on themesConfig**

Create `chart/templates/configmap-viewer-nginx.yaml`:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: viewer-nginx
  namespace: {{ .Values.namespace }}
  labels:
    {{- include "qgis.labels" . | nindent 4 }}
data:
  default.conf: |
    server {
        listen       80;
        server_name  _;

        root   /usr/share/nginx/html;
        index  index.html;

        # themesConfig.json is rewritten by the watcher; never cache it.
        location = /themesConfig.json {
            add_header Cache-Control "no-cache" always;
            try_files $uri =404;
        }

        # Webpack-fingerprinted assets: long-cache safely.
        location ~* \.(js|css|woff2|png|jpg|svg)$ {
            try_files $uri =404;
            expires 7d;
            add_header Cache-Control "public, max-age=604800";
        }

        # SPA fallback for everything else.
        location / {
            try_files $uri $uri/ /index.html;
        }
    }
```

- [ ] **Step 3: Commit**

```sh
git add chart/templates/deployment-viewer.yaml chart/templates/configmap-viewer-nginx.yaml
git commit -m "chart(viewer): serve qwc2 dist from hostPath subPath=web"
```

---

### Task 18: Update `values.yaml` and `Chart.yaml`

**Files:**
- Modify: `chart/values.yaml`
- Modify: `chart/Chart.yaml`

- [ ] **Step 1: Replace `chart/values.yaml`**

Replace the entire contents of `chart/values.yaml` with:

```yaml
namespace: qgis

# Host path on the node where gpkg files, generated project files, and the
# qwc2 dist live. Mounted via a hostPath PV.
hostPath: /srv/qgis

ingress:
  className: traefik
  # Hostname must resolve to the cluster's external IP.
  # Wildcard *.devbox resolution is handled by the host's dnsmasq.
  host: qgis.devbox

image:
  qgisServer: camptocamp/qgis-server:3.34
  viewer: nginx:1.27-alpine
  generator: python:3.12-slim   # also used by the watcher Deployment
  pullPolicy: IfNotPresent

resources:
  qgisServer:
    requests: { cpu: 100m, memory: 256Mi }
    limits:   { cpu: 500m, memory: 1Gi   }
  viewer:
    requests: { cpu: 10m,  memory: 32Mi  }
    limits:   { cpu: 100m, memory: 128Mi }
  watcher:
    requests: { cpu: 50m,  memory: 64Mi  }
    limits:   { cpu: 500m, memory: 256Mi }

# Filesystem watcher that regenerates per-gpkg .qgs and themesConfig.json
# whenever a gpkg in /srv/qgis/data/ changes.
# Set watcher.enabled=false for fully hand-managed installs (you regenerate
# project files on the host yourself).
watcher:
  enabled: true
  debounceSeconds: 1

# Theme id (gpkg stem, no .gpkg suffix) marked as default in themesConfig.json.
# Unset → first alphabetical theme opens by default.
defaultTheme: territories_draft
```

- [ ] **Step 2: Bump `chart/Chart.yaml`**

Edit `chart/Chart.yaml`. Change the `version: 0.1.0` line to `version: 0.2.0` and update the `description:` line:

```yaml
apiVersion: v2
name: qgis
description: QGIS Server on k3s with QWC2 web client and a watcher that regenerates project files from gpkg changes
type: application
version: 0.2.0
appVersion: "3.34"
keywords:
  - qgis
  - geo
  - wms
  - qwc2
maintainers:
  - name: clay
    email: clay@pfd.net
```

- [ ] **Step 3: Commit**

```sh
git add chart/values.yaml chart/Chart.yaml
git commit -m "chart: bump to 0.2.0, replace generator/projectFile with watcher knobs"
```

---

### Task 19: Update chart README

**Files:**
- Modify: `chart/README.md`

- [ ] **Step 1: Replace `chart/README.md`**

Replace the entire contents of `chart/README.md` with:

```markdown
# qgis helm chart

QGIS Server + QWC2 web client for the GeoPackage files at `/srv/qgis/data/`.
A `project-watcher` Deployment continuously regenerates per-gpkg `.qgs`
files and `themesConfig.json` whenever data changes.

## Prerequisites

- k3s with Traefik ingress (already on this host).
- Host-level dnsmasq configured per `/srv/gis/tergen-qgis-web/dnsmasq/` so
  `*.devbox` resolves to `192.168.1.70`.
- helm 3.x.
- Docker on the host (for `make install-client`).
- The qwc2 dist installed on the host: `make install-client` from the repo
  root. Reads from `client/qwc2-demo-app/` (vendored upstream) plus
  `client/overlay/` (our customizations) and writes to `/srv/qgis/web/`.

## Install

```bash
make install-client                                   # one-time, builds qwc2 dist
helm install qgis ./chart -n qgis --create-namespace  # deploys the workloads
```

The `project-watcher` Deployment runs `generate_qgs.py --watch` and
populates `/srv/qgis/projects/*.qgs` plus `/srv/qgis/web/themesConfig.json`
on startup. The qgis-server pod's init container blocks until those exist.

## Iteration

Drop a new gpkg into `/srv/qgis/data/<dataset>/`; the watcher detects the
write within ~1 second, regenerates the matching `.qgs` and rewrites
`themesConfig.json`. Hard-refresh the browser.

## Pause auto-regen (e.g., to hand-edit a project file in QGIS Desktop)

```bash
touch /srv/qgis/.no-regen        # watcher logs "skipped" on every event
# … edit /srv/qgis/projects/territories_draft.qgs in QGIS Desktop, save …
rm /srv/qgis/.no-regen           # auto-regen resumes
```

## Update qwc2 from upstream

```bash
git subtree pull --prefix=client/qwc2-demo-app \
    https://github.com/qgis/qwc2-demo-app master --squash
make install-client
```

## Uninstall

```bash
helm uninstall qgis -n qgis
```

The PVC and the host-level data directory `/srv/qgis/` are not touched.
```

- [ ] **Step 2: Commit**

```sh
git add chart/README.md
git commit -m "chart(docs): document qwc2 install + iteration loop"
```

---

## Phase 4 — Integration & verification

### Task 20: Render-only template lint

**Files:** none.

- [ ] **Step 1: Render the chart against a clean release name**

```sh
helm template qgis ./chart -n qgis > /tmp/rendered.yaml
```

Expected: no errors (warnings about deprecation notices are OK). Inspect:

```sh
grep -E '^kind: ' /tmp/rendered.yaml | sort | uniq -c
```

Expected counts: 1 ConfigMap (project-watcher), 1 ConfigMap (viewer-nginx), 2 Deployments (qgis-server, viewer), 1 Deployment (project-watcher) → 3 Deployments, 1 Ingress, 1 Middleware (TraefikMiddleware), 1 PVC, 1 PV, 2 Services. **No Job and no `viewer-html` ConfigMap.**

- [ ] **Step 2: Lint**

```sh
helm lint ./chart
```

Expected: `1 chart(s) linted, 0 chart(s) failed`.

- [ ] **Step 3: Clean up**

```sh
rm /tmp/rendered.yaml
```

(No commit — verification only.)

---

### Task 21: Run all tests

- [ ] **Step 1: Run pytest**

```sh
make test
```

Expected: all tests pass. If a test referencing the old `--output project.qgs` CLI is still present and failing, delete that test (it is superseded by `test_main_writes_project_qgs` from Task 7).

(No commit unless test deletion was needed; in that case `git commit -m "test: drop superseded --output CLI test"`.)

---

### Task 22: Deploy and end-to-end smoke

- [ ] **Step 1: Upgrade the chart**

```sh
helm upgrade --install qgis ./chart -n qgis --create-namespace
kubectl -n qgis rollout status deployment/project-watcher --timeout=2m
kubectl -n qgis rollout status deployment/qgis-server     --timeout=2m
kubectl -n qgis rollout status deployment/viewer          --timeout=2m
```

Expected: all three rollouts reach `successfully rolled out`.

- [ ] **Step 2: Verify project files exist**

```sh
ls /srv/qgis/projects/
ls /srv/qgis/web/themesConfig.json
```

Expected: at least `territories_draft.qgs` and `debug.qgs` in `projects/`; `themesConfig.json` exists.

- [ ] **Step 3: Verify the watcher pod is running and responsive**

```sh
kubectl -n qgis get pods -l app.kubernetes.io/component=project-watcher
kubectl -n qgis logs deployment/project-watcher --tail=20
```

Expected: 1/1 ready; logs show the initial regen line and the "watching … for *.gpkg changes" line.

- [ ] **Step 4: Verify WMS GetCapabilities through Traefik**

```sh
curl -sH 'Host: qgis.devbox' \
    'http://192.168.1.70/ows/?SERVICE=WMS&REQUEST=GetCapabilities&MAP=/srv/qgis/projects/territories_draft.qgs' \
  | head -40
```

Expected: a `<WMS_Capabilities>` XML document referencing `territories` (and any other layers in the gpkg).

- [ ] **Step 5: Verify the qwc2 client loads**

Open `http://qgis.devbox/` in a browser. Expected:
- The qwc2 viewer renders with map + layer tree + topbar.
- Theme switcher shows "Territories Draft" (selected) and "Debug".
- Clicking a polygon opens an Identify panel with the feature attributes.
- Typing a partial `terr_id` (e.g., `TERR-` if your data uses that pattern) into the Search box shows matches.

- [ ] **Step 6: Verify the iteration loop**

```sh
# touch any existing gpkg to trigger regen
touch /srv/qgis/data/territories/territories_draft.gpkg
sleep 2
kubectl -n qgis logs deployment/project-watcher --tail=5
```

Expected: a "regen: wrote 2 project(s)…" log line within the last 5 lines.

- [ ] **Step 7: Verify `.no-regen` pause**

```sh
sudo touch /srv/qgis/.no-regen
touch /srv/qgis/data/territories/territories_draft.gpkg
sleep 3
kubectl -n qgis logs deployment/project-watcher --tail=5
```

Expected: a ".no-regen present; skipped" log line.

```sh
sudo rm /srv/qgis/.no-regen
```

(No commit — final verification.)

---

### Task 23: Commit final integration tag

- [ ] **Step 1: Verify clean tree**

```sh
git status
```

Expected: nothing to commit, working tree clean.

- [ ] **Step 2: Tag the release**

```sh
git tag -a v0.2.0 -m "qwc2 web client + watcher-driven iteration loop"
```

(No push — leave that to the operator's discretion.)

---

## Self-Review

**Spec coverage check:**

- §2 In-scope (richer viewer, print, search, themesConfig, watcher) → covered by Tasks 1–7 (generator), 10–13 (client build), 15 (watcher Deployment).
- §3 Architecture (Traefik routes, hostPath layout) → no chart route changes; ingress.yaml unchanged; volume layout updated in Tasks 16–17.
- §4.1 viewer pod (hostPath instead of ConfigMap) → Task 17.
- §4.2 qgis-server (drop project file env, init container) → Task 16.
- §4.3 watcher Deployment (recreate strategy, watchdog initContainer, heartbeat liveness) → Task 15.
- §4.4 generator refactor (regen_all, atomic, prune) → Tasks 1–5.
- §5 themesConfig schema (one theme per gpkg, search heuristic, 4326 bbox) → Tasks 3–4.
- §5.1 bbox without pyproj (LCC inverse already in generate_qgs.py) → reuses existing `_bbox_to_wgs84`; Task 3 wires it via `_gpkg_wgs84_bbox`.
- §6 default theme resolution (alphabetical fallback in qgis-server probe URL) → Task 16 uses an unparameterized GetCapabilities probe; default theme handled by qwc2 via themesConfig.
- §7 build pipeline (subtree, overlay, Makefile, node:22) → Tasks 9–13.
- §8 chart change list → Tasks 14–19 cover every row.
- §9 iteration loops → Task 22 verifies them end-to-end.
- §10 failure modes → covered by tests (atomic-write, prune, no-regen marker) and probe design.
- §11 success criteria → all bullet points exercised in Task 22.
- §12 default print layout → **Gap.** The spec says the generator should emit a default A4 layout per project. Adding a deferred follow-up: see *Open follow-ups* below.
- §13 open questions → all locked in (basemap = OSM, defaultTheme via values, search heuristic, no automated upstream pin, no-cache on themesConfig).

**Placeholder scan:** No "TBD" / "TODO" / "fill in" markers; all code blocks contain runnable content; all referenced functions are defined in tasks.

**Type consistency:** `regen_all`, `write_project`, `write_themes_config`, `atomic_write_text`, `discover_gpkgs`, `_search_providers_for`, `_gpkg_wgs84_bbox`, `RegenReport` — all defined in their respective tasks; signatures match call sites; imports `json` (aliased to `_json`) once in the module to avoid colliding with the test file's top-level `import json`.

**Open follow-ups (NOT part of this plan, file as future work):**

- Default A4 print layout per project (spec §12). The generator currently emits no `<Layouts>` section in `.qgs`, so the qwc2 Print plugin's dropdown will be empty. Adding a default layout requires roughly 60 lines of XML in `render_qgs()` and is independent of every other piece. Would be its own ~3-task plan.

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-04-28-qwc2-webclient.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — Dispatch a fresh subagent per task with two-stage review. Best for an outcome where each task gets independent eyes; minor ergonomic overhead per task.

**2. Inline Execution** — Execute tasks in this session with batch checkpoints. Faster end-to-end; no inter-task review.

**Which approach?**
