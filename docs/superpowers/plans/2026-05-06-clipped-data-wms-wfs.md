# Clipped-data WMS/WFS connections — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every `*.gpkg` under `/srv/qgis/data/clipped_data/` available as its own WMS+WFS connection in QGIS Desktop, with collision-free, folder-aware names, distributed live via the existing `qgis.devbox` ingress.

**Architecture:** Centralize project-id and project-title derivation in two new helpers in `chart/files/generate_qgs.py`. Files under `data_dir` top-level or under `data_dir/territories/` keep their bare-stem id; everything else (i.e. `clipped_data/...`) gets `<folder_slug>__<stem>`. Add a `write_connections` helper that emits `qgis-{wms,wfs}-connections.xml` next to `themesConfig.json` in `/srv/qgis/web/`. The viewer nginx serves them at the ingress root; the static files in `client/connections/` are removed.

**Tech Stack:** Python 3 stdlib (xml.etree.ElementTree, sqlite3, pathlib), pytest, helm, kubernetes, nginx.

---

## Spec reference

`docs/superpowers/specs/2026-05-05-clipped-data-wms-wfs-design.md`

## File map

- **Modify** `chart/files/generate_qgs.py` — add `_slug`, `_project_id`, `_project_title`, `write_connections`; thread `data_dir` through `write_project`; thread `data_dir` and use new helpers in `write_themes_config`; thread `data_dir` and use new id in `regen_all` (`.qgs` filenames + prune key); add `--ingress-host` CLI flag and `ingress_host` parameter on `regen_all`. Replace `_theme_id` and `_theme_title` (the existing helpers) with the two new ones.
- **Modify** `tests/test_generate_qgs.py` — add tests for the new helpers, the prune regression, and `write_connections`.
- **Modify** `chart/templates/configmap-viewer-nginx.yaml` — add two `location =` blocks for the connection XMLs.
- **Modify** `chart/templates/deployment-watcher.yaml` — pass `--ingress-host {{ .Values.ingress.host }}`.
- **Modify** `client/connections/README.md` — rewrite around the live URLs.
- **Delete** `client/connections/qgis-wms-connections.xml`, `client/connections/qgis-wfs-connections.xml`.
- **Modify** `README.md` — one-paragraph note pointing operators at the live URLs.

## Test command

Project-level: `make test` (which runs `.venv/bin/pytest tests/ -v`). Single-test runs: `.venv/bin/pytest tests/test_generate_qgs.py::test_NAME -v`.

---

## Task 1: Add `_slug` helper

**Files:**
- Modify: `chart/files/generate_qgs.py` (add helper next to existing `_theme_id`/`_theme_title`)
- Test: `tests/test_generate_qgs.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_generate_qgs.py`:

```python
@pytest.mark.parametrize("raw,expected", [
    ("Mecklenburg Addresses", "Mecklenburg_Addresses"),
    ("NC Streams", "NC_Streams"),
    ("clipped_data", "clipped_data"),
    ("plain", "plain"),
    ("with/slash", "with_slash"),
    ("two  spaces", "two_spaces"),
    ("trailing ", "trailing"),
])
def test_slug_collapses_spaces_and_slashes(raw, expected):
    assert gen._slug(raw) == expected
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/pytest tests/test_generate_qgs.py::test_slug_collapses_spaces_and_slashes -v
```

Expected: FAIL with `AttributeError: module 'generate_qgs' has no attribute '_slug'` (one such failure per parametrize row).

- [ ] **Step 3: Implement `_slug`**

Add to `chart/files/generate_qgs.py`, immediately above the existing `_theme_id` definition (around line 704):

```python
import re as _re


def _slug(s: str) -> str:
    """Convert a path component into an underscore-safe id fragment.

    Replaces runs of whitespace and the path separators '/' '\\' with a single
    underscore, strips leading/trailing whitespace and underscores, and leaves
    everything else alone. The result is suitable for use as a filename stem
    or URL path segment.
    """
    s = s.strip()
    s = _re.sub(r"[\s/\\]+", "_", s)
    return s.strip("_")
```

(Add the `import re as _re` next to the other top-of-file imports if not already present; the file currently has `import json as _json` so this style matches.)

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/pytest tests/test_generate_qgs.py::test_slug_collapses_spaces_and_slashes -v
```

Expected: PASS for every parametrize row.

- [ ] **Step 5: Commit**

```bash
git add chart/files/generate_qgs.py tests/test_generate_qgs.py
git commit -m "chart(files): add _slug helper for path-component normalization"
```

---

## Task 2: Add `_project_id` helper

**Files:**
- Modify: `chart/files/generate_qgs.py`
- Test: `tests/test_generate_qgs.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_generate_qgs.py`:

```python
@pytest.mark.parametrize("rel,expected", [
    # Top-level files keep their bare stem
    ("territories_draft.gpkg", "territories_draft"),
    ("debug.gpkg", "debug"),
    # territories/ is treated as if it were top-level
    ("territories/territories_draft.gpkg", "territories_draft"),
    ("territories/debug.gpkg", "debug"),
    # clipped_data top-level: parent is "clipped_data"
    ("clipped_data/addresses_residential.gpkg",
     "clipped_data__addresses_residential"),
    # Nested folders use the folder slug as the prefix
    ("clipped_data/Mecklenburg Addresses/addresses.gpkg",
     "Mecklenburg_Addresses__addresses"),
    ("clipped_data/NC Parcels/parcels_pt.gpkg",
     "NC_Parcels__parcels_pt"),
    ("clipped_data/Union Subdivisions/Union_Subdivisions.gpkg",
     "Union_Subdivisions__Union_Subdivisions"),
])
def test_project_id_rule(tmp_path, rel, expected):
    data_dir = tmp_path
    gpkg = data_dir / rel
    gpkg.parent.mkdir(parents=True, exist_ok=True)
    gpkg.touch()
    assert gen._project_id(gpkg, data_dir) == expected
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/pytest tests/test_generate_qgs.py::test_project_id_rule -v
```

Expected: FAIL with `AttributeError: module 'generate_qgs' has no attribute '_project_id'`.

- [ ] **Step 3: Implement `_project_id`**

Add to `chart/files/generate_qgs.py`, immediately below `_slug`:

```python
def _project_id(gpkg: Path, data_dir: Path) -> str:
    """Return the canonical id for *gpkg* — the .qgs stem and the MAP= value.

    Files directly under *data_dir* and files under ``data_dir/territories/``
    keep their bare gpkg stem (preserves the existing ``territories_draft``
    and ``debug`` ids). Anything else is prefixed with the slugified parent
    folder name and a ``__`` separator. This avoids collisions across
    duplicated stems (e.g. three ``addresses.gpkg`` files in different
    Meck/NC/Union folders).
    """
    parent = gpkg.parent
    if parent == data_dir or parent == data_dir / "territories":
        return gpkg.stem
    return f"{_slug(parent.name)}__{gpkg.stem}"
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/pytest tests/test_generate_qgs.py::test_project_id_rule -v
```

Expected: PASS for every parametrize row.

- [ ] **Step 5: Commit**

```bash
git add chart/files/generate_qgs.py tests/test_generate_qgs.py
git commit -m "chart(files): add _project_id with folder-aware disambiguation"
```

---

## Task 3: Add `_project_title` helper

**Files:**
- Modify: `chart/files/generate_qgs.py`
- Test: `tests/test_generate_qgs.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_generate_qgs.py`:

```python
@pytest.mark.parametrize("rel,expected", [
    # Top-level / territories: same as the old _theme_title behavior
    ("territories_draft.gpkg", "Territories Draft"),
    ("debug.gpkg", "Debug"),
    ("territories/territories_draft.gpkg", "Territories Draft"),
    # clipped_data root: folder + stem (no redundancy)
    ("clipped_data/addresses_residential.gpkg",
     "Clipped Data – Addresses Residential"),
    # Stem-substring-of-folder cases drop the stem (smart redundancy rule)
    ("clipped_data/Mecklenburg Addresses/addresses.gpkg",
     "Mecklenburg Addresses"),
    ("clipped_data/Mecklenburg Greenways/Greenways.gpkg",
     "Mecklenburg Greenways"),
    ("clipped_data/Union Subdivisions/Union_Subdivisions.gpkg",
     "Union Subdivisions"),
    # Stem NOT a substring of folder: keep folder + stem with en-dash
    ("clipped_data/NC Parcels/parcels_pt.gpkg",
     "NC Parcels – Parcels Pt"),
    ("clipped_data/Mecklenburg Streams/Creeks_Streams.gpkg",
     "Mecklenburg Streams – Creeks Streams"),
    ("clipped_data/NC Roads/NCDOT_State_Maintained_Roads.gpkg",
     "NC Roads – NCDOT State Maintained Roads"),
])
def test_project_title_rule(tmp_path, rel, expected):
    data_dir = tmp_path
    gpkg = data_dir / rel
    gpkg.parent.mkdir(parents=True, exist_ok=True)
    gpkg.touch()
    assert gen._project_title(gpkg, data_dir) == expected
```

Note on the en-dash: it's the U+2013 character "–", not a hyphen-minus. Copy-paste from this plan rather than retyping.

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/pytest tests/test_generate_qgs.py::test_project_title_rule -v
```

Expected: FAIL with `AttributeError: module 'generate_qgs' has no attribute '_project_title'`.

- [ ] **Step 3: Implement `_project_title`**

Add to `chart/files/generate_qgs.py`, immediately below `_project_id`:

```python
def _project_title(gpkg: Path, data_dir: Path) -> str:
    """Return the human-facing title for *gpkg*.

    Used as the QGIS WMS service title, the QWC2 theme title, and the
    QGIS Desktop connection display name.

    For top-level / ``territories/`` files, title-case the stem (current
    behavior). For nested files, build ``"<folder> – <stem-titled>"``,
    but drop the stem when its slug is a case-insensitive substring of
    the folder slug (handles "Mecklenburg Greenways/Greenways.gpkg" →
    "Mecklenburg Greenways").
    """
    parent = gpkg.parent
    if parent == data_dir or parent == data_dir / "territories":
        return gpkg.stem.replace("_", " ").replace("-", " ").title()

    folder = parent.name
    stem_title = gpkg.stem.replace("_", " ").replace("-", " ").title()
    if _slug(stem_title).lower() in _slug(folder).lower():
        return folder
    folder_pretty = folder.replace("_", " ")
    return f"{folder_pretty} – {stem_title}"
```

(`–` is the en-dash, equivalent to "–". Using the escape form avoids any encoding-of-source-file ambiguity.)

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/pytest tests/test_generate_qgs.py::test_project_title_rule -v
```

Expected: PASS for every parametrize row.

- [ ] **Step 5: Commit**

```bash
git add chart/files/generate_qgs.py tests/test_generate_qgs.py
git commit -m "chart(files): add _project_title with folder + smart-redundancy rule"
```

---

## Task 4: Switch `write_themes_config` to the new helpers

The existing `_theme_id` and `_theme_title` are only called from `write_themes_config`. Replacing them in-place is the smallest possible change.

**Files:**
- Modify: `chart/files/generate_qgs.py` (the `write_themes_config` function and the deletion of `_theme_id`/`_theme_title`)
- Test: `tests/test_generate_qgs.py` (existing tests must continue to pass; add one new test for the nested case)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_generate_qgs.py`:

```python
def test_write_themes_config_uses_path_aware_ids_for_nested_gpkgs(tmp_path):
    """Nested gpkgs (e.g. clipped_data/<Folder>/x.gpkg) get folder-prefixed
    theme ids and folder-prefixed titles."""
    data_dir = tmp_path / "data"
    folder = data_dir / "clipped_data" / "Mecklenburg Addresses"
    folder.mkdir(parents=True)
    gpkg = folder / "addresses.gpkg"
    _make_minimal_gpkg(gpkg, layer_name="addresses")

    out = tmp_path / "themesConfig.json"
    gen.write_themes_config(
        gpkgs=[gpkg],
        projects_dir=Path("/srv/qgis/projects"),
        out=out,
        default_theme=None,
        data_dir=data_dir,
    )
    cfg = json.loads(out.read_text())
    items = cfg["themes"]["items"]
    assert len(items) == 1
    assert items[0]["id"] == "Mecklenburg_Addresses__addresses"
    assert items[0]["title"] == "Mecklenburg Addresses"
    assert items[0]["url"] == (
        "/ows/?MAP=/srv/qgis/projects/Mecklenburg_Addresses__addresses.qgs"
    )
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/pytest tests/test_generate_qgs.py::test_write_themes_config_uses_path_aware_ids_for_nested_gpkgs -v
```

Expected: FAIL — `write_themes_config` does not accept a `data_dir` keyword argument yet (`TypeError: write_themes_config() got an unexpected keyword argument 'data_dir'`).

- [ ] **Step 3: Update `write_themes_config` signature and body**

In `chart/files/generate_qgs.py`, change the `write_themes_config` definition. Before:

```python
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
            ...
```

After:

```python
def write_themes_config(
    gpkgs: list[Path],
    projects_dir: Path,
    out: Path,
    default_theme: str | None = None,
    *,
    data_dir: Path,
) -> None:
    """Write the qwc2 themesConfig.json from a list of gpkgs."""
    items = []
    for gpkg in sorted(gpkgs, key=lambda p: _project_id(p, data_dir)):
        tid = _project_id(gpkg, data_dir)
        w, s, e, n = _gpkg_wgs84_bbox(gpkg)
        items.append({
            "id": tid,
            "title": _project_title(gpkg, data_dir),
            ...
```

(`data_dir` is keyword-only so callers in this codebase always pass it explicitly.)

The `default` field comparison `(default_theme == tid)` is unchanged — it still compares the user-provided default-theme id (a stem like `territories_draft`) against the new id, which for top-level files is also the stem. ✓

- [ ] **Step 4: Delete the old helpers**

In `chart/files/generate_qgs.py`, delete the `_theme_id` and `_theme_title` function definitions (around lines 704-711). They have no remaining callers after Step 3.

- [ ] **Step 5: Update the one other call site that passes through `write_themes_config`**

Search the file for the `write_themes_config(` call inside `regen_all`:

```python
    themes_config_path = web_dir / "themesConfig.json"
    write_themes_config(
        gpkgs=gpkgs,
        projects_dir=projects_dir,
        out=themes_config_path,
        default_theme=default_theme,
    )
```

Add the `data_dir` keyword arg:

```python
    themes_config_path = web_dir / "themesConfig.json"
    write_themes_config(
        gpkgs=gpkgs,
        projects_dir=projects_dir,
        out=themes_config_path,
        default_theme=default_theme,
        data_dir=data_dir,
    )
```

- [ ] **Step 6: Run the full test suite**

```bash
.venv/bin/pytest tests/ -v
```

Expected: All tests PASS, including the new `test_write_themes_config_uses_path_aware_ids_for_nested_gpkgs` and every existing test. The existing tests put gpkgs at `tmp_path` (top-level), so their expected stem-based ids are still produced by `_project_id`.

If any existing test that calls `write_themes_config` directly now fails with `TypeError: write_themes_config() missing 1 required keyword-only argument: 'data_dir'`, fix the call site by passing `data_dir=tmp_path` (the gpkgs in those tests live directly under `tmp_path`).

- [ ] **Step 7: Commit**

```bash
git add chart/files/generate_qgs.py tests/test_generate_qgs.py
git commit -m "chart(files): use _project_id/_project_title in write_themes_config"
```

---

## Task 5: Switch `write_project` and `regen_all` to the new id and title

`write_project` currently passes `gpkg.stem` as `project_name` and `_theme_title(gpkg)` as `project_title` to `render_qgs`. `regen_all` chooses the output filename as `projects_dir / f"{gpkg.stem}.qgs"` and uses `gpkg.stem` for the prune key. Both need to swap to `_project_id` / `_project_title`.

**Files:**
- Modify: `chart/files/generate_qgs.py`
- Test: `tests/test_generate_qgs.py`

- [ ] **Step 1: Write the failing test for the prune regression**

Append to `tests/test_generate_qgs.py`:

```python
def test_regen_all_renames_collision_qgs_files(tmp_path):
    """When two gpkgs share a stem under different folders, both get distinct
    .qgs files (no silent overwrite) and a stale single-stem .qgs from a
    previous run is pruned."""
    data_dir = tmp_path / "data"
    projects_dir = tmp_path / "projects"
    web_dir = tmp_path / "web"
    projects_dir.mkdir(parents=True)
    (data_dir / "clipped_data" / "Mecklenburg Addresses").mkdir(parents=True)
    (data_dir / "clipped_data" / "NC Addresses").mkdir(parents=True)
    _make_minimal_gpkg(
        data_dir / "clipped_data" / "Mecklenburg Addresses" / "addresses.gpkg",
        layer_name="addresses",
    )
    _make_minimal_gpkg(
        data_dir / "clipped_data" / "NC Addresses" / "addresses.gpkg",
        layer_name="addresses",
    )

    # Pretend a previous run wrote the old stem-based file.
    (projects_dir / "addresses.qgs").write_text("<qgis/>")

    gen.regen_all(data_dir, projects_dir, web_dir, default_theme=None)

    qgs_files = sorted(p.name for p in projects_dir.glob("*.qgs"))
    assert "addresses.qgs" not in qgs_files  # stale file pruned
    assert "Mecklenburg_Addresses__addresses.qgs" in qgs_files
    assert "NC_Addresses__addresses.qgs" in qgs_files
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/pytest tests/test_generate_qgs.py::test_regen_all_renames_collision_qgs_files -v
```

Expected: FAIL — `regen_all` is still keying off `gpkg.stem`, so both gpkgs would try to write `projects/addresses.qgs` and the assertion `"addresses.qgs" not in qgs_files` fails. The Mecklenburg/NC-prefixed files won't exist either.

- [ ] **Step 3: Update `write_project` signature**

In `chart/files/generate_qgs.py`, replace the `write_project` definition. Before:

```python
def write_project(gpkg: Path, out: Path,
                  project_crs_authid: str = "EPSG:3857") -> None:
    """Generate the .qgs for a single gpkg and write it atomically to *out*."""
    layers = introspect_gpkg(gpkg)
    if not layers:
        raise ValueError(f"no feature-table layers in {gpkg}")
    atomic_write_text(out, render_qgs(
        layers, project_crs_authid,
        project_name=gpkg.stem,
        project_title=_theme_title(gpkg),
    ))
```

After:

```python
def write_project(
    gpkg: Path,
    out: Path,
    *,
    data_dir: Path,
    project_crs_authid: str = "EPSG:3857",
) -> None:
    """Generate the .qgs for a single gpkg and write it atomically to *out*.

    ``data_dir`` is required so that the project name and title can be
    derived from the gpkg's path under the data root (folder-aware ids
    avoid collisions on duplicated gpkg stems).
    """
    layers = introspect_gpkg(gpkg)
    if not layers:
        raise ValueError(f"no feature-table layers in {gpkg}")
    atomic_write_text(out, render_qgs(
        layers, project_crs_authid,
        project_name=_project_id(gpkg, data_dir),
        project_title=_project_title(gpkg, data_dir),
    ))
```

- [ ] **Step 4: Update `regen_all` to use the new id for filenames and the prune set**

In `chart/files/generate_qgs.py`, find the body of `regen_all`. Before:

```python
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
```

After:

```python
    gpkgs = discover_gpkgs(data_dir)
    current_ids = {_project_id(gpkg, data_dir) for gpkg in gpkgs}

    written = 0
    for gpkg in gpkgs:
        try:
            write_project(
                gpkg,
                projects_dir / f"{_project_id(gpkg, data_dir)}.qgs",
                data_dir=data_dir,
            )
            written += 1
        except Exception as exc:
            print(f"failed to render {gpkg.name}: {exc}", file=sys.stderr)

    pruned = _prune_orphans(projects_dir, current_ids)
```

(`_prune_orphans` is unchanged — it already takes a generic "set of allowed stems" parameter; we just pass it ids instead of raw stems.)

- [ ] **Step 5: Update other call sites of `write_project`**

Search `tests/test_generate_qgs.py` for `gen.write_project(`. The test `test_write_project_writes_one_qgs_per_gpkg` (around line 213) calls it directly:

Find every `write_project(...)` invocation in the test file and add `data_dir=<dir>` (the gpkg parent directory or the test's data root). Example:

Before:
```python
gen.write_project(gpkg, out)
```

After:
```python
gen.write_project(gpkg, out, data_dir=tmp_path)
```

(The gpkgs in those tests live directly under `tmp_path`, so passing `tmp_path` keeps the bare-stem id behavior intact.)

- [ ] **Step 6: Run the full test suite**

```bash
.venv/bin/pytest tests/ -v
```

Expected: All tests PASS, including `test_regen_all_renames_collision_qgs_files`.

- [ ] **Step 7: Commit**

```bash
git add chart/files/generate_qgs.py tests/test_generate_qgs.py
git commit -m "chart(files): key .qgs filenames and prune set by _project_id"
```

---

## Task 6: Add `write_connections` helper

`write_connections` emits `qgis-{wms,wfs}-connections.xml` with one entry per gpkg, sorted by title for stable output.

**Files:**
- Modify: `chart/files/generate_qgs.py`
- Test: `tests/test_generate_qgs.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_generate_qgs.py`:

```python
def test_write_connections_emits_one_wms_and_one_wfs_per_gpkg(tmp_path):
    data_dir = tmp_path / "data"
    projects_dir = Path("/srv/qgis/projects")
    (data_dir / "territories").mkdir(parents=True)
    (data_dir / "clipped_data" / "Mecklenburg Addresses").mkdir(parents=True)
    _make_minimal_gpkg(data_dir / "territories" / "territories_draft.gpkg")
    _make_minimal_gpkg(
        data_dir / "clipped_data" / "Mecklenburg Addresses" / "addresses.gpkg",
        layer_name="addresses",
    )

    out_wms = tmp_path / "qgis-wms-connections.xml"
    out_wfs = tmp_path / "qgis-wfs-connections.xml"

    gen.write_connections(
        gpkgs=[
            data_dir / "clipped_data" / "Mecklenburg Addresses" / "addresses.gpkg",
            data_dir / "territories" / "territories_draft.gpkg",
        ],
        projects_dir=projects_dir,
        data_dir=data_dir,
        out_wms=out_wms,
        out_wfs=out_wfs,
        ingress_host="qgis.devbox",
    )

    wms_root = ET.fromstring(out_wms.read_text())
    assert wms_root.tag == "qgsWMSConnections"
    wms_entries = wms_root.findall("wms")
    assert [e.get("name") for e in wms_entries] == [
        "Mecklenburg Addresses (qgis.devbox)",
        "Territories Draft (qgis.devbox)",
    ]
    assert wms_entries[0].get("url") == (
        "http://qgis.devbox/ows/?MAP="
        "/srv/qgis/projects/Mecklenburg_Addresses__addresses.qgs"
    )
    assert wms_entries[0].get("ignoreGetMapURI") == "1"
    assert wms_entries[0].get("ignoreGetFeatureInfoURI") == "1"
    assert wms_entries[0].get("dpiMode") == "7"

    wfs_root = ET.fromstring(out_wfs.read_text())
    assert wfs_root.tag == "qgsWFSConnections"
    wfs_entries = wfs_root.findall("wfs")
    assert [e.get("name") for e in wfs_entries] == [
        "Mecklenburg Addresses (qgis.devbox)",
        "Territories Draft (qgis.devbox)",
    ]
    assert wfs_entries[1].get("url") == (
        "http://qgis.devbox/ows/?MAP=/srv/qgis/projects/territories_draft.qgs"
    )
    assert wfs_entries[0].get("pagingEnabled") == "default"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/pytest tests/test_generate_qgs.py::test_write_connections_emits_one_wms_and_one_wfs_per_gpkg -v
```

Expected: FAIL — `AttributeError: module 'generate_qgs' has no attribute 'write_connections'`.

- [ ] **Step 3: Implement `write_connections`**

Add to `chart/files/generate_qgs.py`, immediately above `bake_themes_json` (around line 846). Pick a spot that keeps related helpers together; the exact location doesn't matter to runtime correctness.

```python
def write_connections(
    gpkgs: list[Path],
    projects_dir: Path,
    data_dir: Path,
    out_wms: Path,
    out_wfs: Path,
    ingress_host: str,
) -> None:
    """Write QGIS Desktop WMS and WFS connection bundles.

    Emits one ``<wms>`` element per gpkg into *out_wms* and one ``<wfs>``
    element per gpkg into *out_wfs*. Entries are sorted by title for
    deterministic output across regen runs.

    The ``url`` attribute is the in-cluster MAP= URL through the public
    ingress (``http://<ingress_host>/ows/?MAP=...``). ``ignoreGetMapURI`` /
    ``ignoreGetFeatureInfoURI`` force QGIS Desktop to keep using this URL
    on follow-up requests instead of falling back to the bare service
    URL advertised by ``QGIS_SERVER_SERVICE_URL`` (which lacks ``MAP=``).
    """
    entries = []
    for gpkg in gpkgs:
        pid = _project_id(gpkg, data_dir)
        title = _project_title(gpkg, data_dir)
        entries.append((
            title,
            f"{title} ({ingress_host})",
            f"http://{ingress_host}/ows/?MAP={projects_dir}/{pid}.qgs",
        ))
    entries.sort(key=lambda t: t[0].lower())

    wms_root = ET.Element("qgsWMSConnections", attrib={"version": "1.0"})
    for _title, name, url in entries:
        ET.SubElement(wms_root, "wms", attrib={
            "name": name,
            "url": url,
            "version": "auto",
            "ignoreGetMapURI": "1",
            "ignoreGetFeatureInfoURI": "1",
            "smoothPixmapTransform": "0",
            "ignoreAxisOrientation": "0",
            "invertAxisOrientation": "0",
            "dpiMode": "7",
            "referer": "",
            "authcfg": "",
            "username": "",
            "password": "",
        })

    wfs_root = ET.Element("qgsWFSConnections", attrib={"version": "1.0"})
    for _title, name, url in entries:
        ET.SubElement(wfs_root, "wfs", attrib={
            "name": name,
            "url": url,
            "version": "auto",
            "maxnumfeatures": "",
            "pagesize": "",
            "pagingEnabled": "default",
            "featurePaging": "default",
            "ignoreAxisOrientation": "0",
            "invertAxisOrientation": "0",
            "referer": "",
            "authcfg": "",
            "username": "",
            "password": "",
        })

    prelude = '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE connections>\n'
    atomic_write_text(out_wms, prelude + ET.tostring(wms_root, encoding="unicode") + "\n")
    atomic_write_text(out_wfs, prelude + ET.tostring(wfs_root, encoding="unicode") + "\n")
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/pytest tests/test_generate_qgs.py::test_write_connections_emits_one_wms_and_one_wfs_per_gpkg -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add chart/files/generate_qgs.py tests/test_generate_qgs.py
git commit -m "chart(files): add write_connections for QGIS Desktop bundles"
```

---

## Task 7: Wire `write_connections` into `regen_all` and the CLI

**Files:**
- Modify: `chart/files/generate_qgs.py`
- Test: `tests/test_generate_qgs.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_generate_qgs.py`:

```python
def test_regen_all_writes_connection_xmls_when_ingress_host_set(tmp_path):
    data_dir = tmp_path / "data"
    projects_dir = tmp_path / "projects"
    web_dir = tmp_path / "web"
    (data_dir / "clipped_data" / "Mecklenburg Addresses").mkdir(parents=True)
    _make_minimal_gpkg(
        data_dir / "clipped_data" / "Mecklenburg Addresses" / "addresses.gpkg",
        layer_name="addresses",
    )

    gen.regen_all(
        data_dir, projects_dir, web_dir, default_theme=None,
        ingress_host="qgis.devbox",
    )

    wms = (web_dir / "qgis-wms-connections.xml").read_text()
    wfs = (web_dir / "qgis-wfs-connections.xml").read_text()
    assert "Mecklenburg Addresses (qgis.devbox)" in wms
    assert "Mecklenburg Addresses (qgis.devbox)" in wfs
    assert "MAP=" + str(projects_dir) + "/Mecklenburg_Addresses__addresses.qgs" in wms


def test_regen_all_skips_connection_xmls_when_no_ingress_host(tmp_path):
    """Parallel to how the themes.json bake is opt-in; tests and one-off CLI
    runs that don't pass --ingress-host should not produce stray XML files."""
    data_dir = tmp_path / "data"
    projects_dir = tmp_path / "projects"
    web_dir = tmp_path / "web"
    (data_dir).mkdir(parents=True)
    _make_minimal_gpkg(data_dir / "thing.gpkg", layer_name="things")

    gen.regen_all(data_dir, projects_dir, web_dir, default_theme=None)

    assert not (web_dir / "qgis-wms-connections.xml").exists()
    assert not (web_dir / "qgis-wfs-connections.xml").exists()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/pytest tests/test_generate_qgs.py::test_regen_all_writes_connection_xmls_when_ingress_host_set tests/test_generate_qgs.py::test_regen_all_skips_connection_xmls_when_no_ingress_host -v
```

Expected: The first test FAILS with `TypeError: regen_all() got an unexpected keyword argument 'ingress_host'`. The second test PASSES because nothing currently writes those files.

- [ ] **Step 3: Add `ingress_host` to `regen_all` and call `write_connections`**

In `chart/files/generate_qgs.py`, change `regen_all`'s signature. Before:

```python
def regen_all(
    data_dir: Path,
    projects_dir: Path,
    web_dir: Path,
    default_theme: str | None,
    *,
    bake_scripts_dir: Path | None = None,
    bake_internal_base_url: str | None = None,
) -> RegenReport:
```

After:

```python
def regen_all(
    data_dir: Path,
    projects_dir: Path,
    web_dir: Path,
    default_theme: str | None,
    *,
    bake_scripts_dir: Path | None = None,
    bake_internal_base_url: str | None = None,
    ingress_host: str | None = None,
) -> RegenReport:
```

Then, just before `if bake_scripts_dir and bake_internal_base_url:` near the bottom of `regen_all`, add:

```python
    if ingress_host:
        write_connections(
            gpkgs=gpkgs,
            projects_dir=projects_dir,
            data_dir=data_dir,
            out_wms=web_dir / "qgis-wms-connections.xml",
            out_wfs=web_dir / "qgis-wfs-connections.xml",
            ingress_host=ingress_host,
        )
```

- [ ] **Step 4: Add the CLI flag and thread it through `main`/`_watch_loop`**

In `chart/files/generate_qgs.py`, in `main()`, add the `--ingress-host` argument next to the other bake-related args:

```python
    p.add_argument("--ingress-host", default=None,
                   help="Public hostname for the QGIS WMS/WFS connection "
                        "bundles (e.g. 'qgis.devbox'). Required to write "
                        "qgis-wms-connections.xml and qgis-wfs-connections.xml; "
                        "if omitted those files are not written.")
```

In the `regen_all(...)` call inside `main()` (around line 1041), add `ingress_host=args.ingress_host`:

```python
    report = regen_all(
        data_dir, projects_dir, web_dir, args.default_theme,
        bake_scripts_dir=bake_scripts_dir,
        bake_internal_base_url=args.bake_internal_base_url,
        ingress_host=args.ingress_host,
    )
```

In the `_watch_loop(...)` call inside `main()` (around line 1057), pass it through:

```python
    return _watch_loop(data_dir, projects_dir, web_dir,
                      args.default_theme, args.debounce_seconds,
                      bake_scripts_dir=bake_scripts_dir,
                      bake_internal_base_url=args.bake_internal_base_url,
                      ingress_host=args.ingress_host)
```

In the `_watch_loop` definition (around line 1063), extend the signature:

```python
def _watch_loop(data_dir: Path, projects_dir: Path, web_dir: Path,
                default_theme: str | None, debounce_seconds: float,
                *,
                bake_scripts_dir: Path | None = None,
                bake_internal_base_url: str | None = None,
                ingress_host: str | None = None) -> int:
```

…and inside the loop body where `regen_all` is called (around line 1098), add `ingress_host=ingress_host`:

```python
                report = regen_all(
                    data_dir, projects_dir, web_dir, default_theme,
                    bake_scripts_dir=bake_scripts_dir,
                    bake_internal_base_url=bake_internal_base_url,
                    ingress_host=ingress_host,
                )
```

- [ ] **Step 5: Run the full test suite**

```bash
.venv/bin/pytest tests/ -v
```

Expected: All tests PASS, including the two new ones from Step 1.

- [ ] **Step 6: Commit**

```bash
git add chart/files/generate_qgs.py tests/test_generate_qgs.py
git commit -m "chart(files): wire write_connections into regen_all and CLI"
```

---

## Task 8: Watcher Deployment passes `--ingress-host`

**Files:**
- Modify: `chart/templates/deployment-watcher.yaml`

- [ ] **Step 1: Add the CLI flag to the watcher container args**

In `chart/templates/deployment-watcher.yaml`, find the `command:` block (around line 48-67). After the `--bake-internal-base-url` line and before the `{{- if .Values.defaultTheme }}` block, add:

```yaml
            - --ingress-host
            - {{ .Values.ingress.host | quote }}
```

The full args block should now end with (annotations / `…`-prefix unchanged):

```yaml
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
            - --bake-scripts-dir
            - /scripts
            - --bake-internal-base-url
            - http://qgis-server.{{ .Values.namespace }}.svc.cluster.local
            - --ingress-host
            - {{ .Values.ingress.host | quote }}
            {{- if .Values.defaultTheme }}
            - --default-theme
            - {{ .Values.defaultTheme | quote }}
            {{- end }}
```

- [ ] **Step 2: Verify the chart renders cleanly**

```bash
helm template qgis chart/ | grep -A1 ingress-host
```

Expected: two lines like:

```
            - --ingress-host
            - "qgis.devbox"
```

If `helm` is unavailable on the dev box, run instead:

```bash
grep -n "ingress-host" chart/templates/deployment-watcher.yaml
```

Expected: two matching lines exist in the template.

- [ ] **Step 3: Commit**

```bash
git add chart/templates/deployment-watcher.yaml
git commit -m "chart(watcher): pass --ingress-host so connection bundles are written"
```

---

## Task 9: Viewer nginx serves the connection XMLs with no-cache + 404 fallback

The viewer nginx currently has a generic SPA fallback (`try_files $uri $uri/ /index.html`). Without explicit location blocks, a missing `qgis-wms-connections.xml` would return the SPA HTML and confuse QGIS Desktop. Mirror the existing `themesConfig.json` block.

**Files:**
- Modify: `chart/templates/configmap-viewer-nginx.yaml`

- [ ] **Step 1: Add location blocks**

In `chart/templates/configmap-viewer-nginx.yaml`, immediately after the existing `location = /themesConfig.json` block, add:

```nginx
        # Connection bundles are rewritten by the watcher; never cache, and
        # return a real 404 when missing instead of falling through to the SPA.
        location = /qgis-wms-connections.xml {
            add_header Cache-Control "no-cache" always;
            try_files $uri =404;
        }
        location = /qgis-wfs-connections.xml {
            add_header Cache-Control "no-cache" always;
            try_files $uri =404;
        }
```

The relevant section of the file should now read (existing surrounding content unchanged):

```nginx
        # themesConfig.json is rewritten by the watcher; never cache it.
        location = /themesConfig.json {
            add_header Cache-Control "no-cache" always;
            try_files $uri =404;
        }

        # Connection bundles are rewritten by the watcher; never cache, and
        # return a real 404 when missing instead of falling through to the SPA.
        location = /qgis-wms-connections.xml {
            add_header Cache-Control "no-cache" always;
            try_files $uri =404;
        }
        location = /qgis-wfs-connections.xml {
            add_header Cache-Control "no-cache" always;
            try_files $uri =404;
        }

        # Webpack-fingerprinted assets: long-cache safely.
        location ~* \.(js|css|woff2|png|jpg|svg)$ {
```

- [ ] **Step 2: Verify the configmap renders cleanly**

```bash
helm template qgis chart/ | grep -A2 'location = /qgis-w'
```

Expected: matches for both new location blocks.

If `helm` is unavailable:

```bash
grep -n "qgis-wms-connections\.xml\|qgis-wfs-connections\.xml" chart/templates/configmap-viewer-nginx.yaml
```

Expected: at least two matches (one in each new location block's pattern).

- [ ] **Step 3: Commit**

```bash
git add chart/templates/configmap-viewer-nginx.yaml
git commit -m "chart(viewer): serve qgis-{wms,wfs}-connections.xml with no-cache"
```

---

## Task 10: Replace static connection bundles with live-URL docs

**Files:**
- Delete: `client/connections/qgis-wms-connections.xml`
- Delete: `client/connections/qgis-wfs-connections.xml`
- Modify: `client/connections/README.md`

- [ ] **Step 1: Delete the static XML files**

```bash
git rm client/connections/qgis-wms-connections.xml client/connections/qgis-wfs-connections.xml
```

- [ ] **Step 2: Rewrite `client/connections/README.md`**

Replace the entire contents of `client/connections/README.md` with:

```markdown
# QGIS Desktop connection bundles

The cluster's `project-watcher` regenerates WMS and WFS connection bundles
every time a `*.gpkg` under `/srv/qgis/data/` changes, then writes them
into `/srv/qgis/web/`. The viewer nginx serves them at the ingress root,
so they are always current with whatever data is on the cluster:

- `http://qgis.devbox/qgis-wms-connections.xml` — rendered raster maps with
  legends, one connection per gpkg
- `http://qgis.devbox/qgis-wfs-connections.xml` — editable vector features,
  one connection per gpkg

## Prerequisites on the client device

1. **Network reachability**: be on the same LAN as the host, or connected
   via Tailscale to the same tailnet.
2. **DNS**: `qgis.devbox` must resolve to the host running k3s
   (`192.168.1.70` on LAN). Three options, easiest first:
   - Point the device's DNS at the host's dnsmasq (it answers `*.devbox`).
   - Add `192.168.1.70  qgis.devbox` to the device's hosts file
     (`/etc/hosts` on macOS/Linux, `C:\Windows\System32\drivers\etc\hosts`
     on Windows).
   - For Tailscale-only clients: add an entry pointing `qgis.devbox` to the
     host's Tailscale IP (`tailscale status` on the host shows it).

Verify with (note the quotes — `&` is a shell metacharacter):

```bash
curl -I 'http://qgis.devbox/ows/?MAP=/srv/qgis/projects/territories_draft.qgs&SERVICE=WMS&REQUEST=GetCapabilities'
```

should return `HTTP/1.1 200 OK`.

## Import in QGIS Desktop

Download the bundles, then import in the Browser panel.

```bash
curl -O http://qgis.devbox/qgis-wms-connections.xml
curl -O http://qgis.devbox/qgis-wfs-connections.xml
```

In the **Browser panel** on the left:

1. Right-click **WMS/WMTS** → *Load Connections…* → pick the downloaded
   `qgis-wms-connections.xml` → check all desired connections → *OK*.
2. Right-click **WFS / OGC API - Features** → *Load Connections…* → pick
   the downloaded `qgis-wfs-connections.xml` → check all desired
   connections → *OK*.

Each gpkg appears as its own expandable connection in the Browser panel;
drag layers onto the canvas.

## Picking up data updates

The server reads each GeoPackage on every request, so **row-level changes
appear immediately** on the next pan/zoom or attribute query. No client
action required.

After a **schema change** (a new layer, a renamed column), the
server-side `.qgs` is regenerated automatically by the watcher — but
QGIS Desktop caches the connection's layer list locally. To refresh:

- In the Browser panel, right-click the connection → *Refresh*, or
- Collapse and re-expand the connection.

After a **structural change to the gpkg set** (a new gpkg dropped in, or
an existing one renamed/removed), re-download the connection XML files
and re-import them. QGIS Desktop never auto-fetches the bundles.

## Why `ignoreGetMapURI=1` is set on WMS

QGIS Server reports its public URL via `QGIS_SERVER_SERVICE_URL`, which is
set chart-side to `http://qgis.devbox/ows` (no `MAP=`). Without
`ignoreGetMapURI=1`, QGIS Desktop would use that bare URL for follow-up
GetMap / GetLegendGraphic / GetFeatureInfo calls and get HTTP 500 because
the `MAP=` parameter is required. Forcing the connection URL keeps the
`MAP=` param on every call.
```

- [ ] **Step 3: Verify the directory contents**

```bash
ls client/connections/
```

Expected: only `README.md` (the two `.xml` files are gone).

- [ ] **Step 4: Commit**

```bash
git add client/connections/README.md client/connections/qgis-wms-connections.xml client/connections/qgis-wfs-connections.xml
git commit -m "docs(client): point connection bundles at live URLs"
```

(`git rm` already staged the deletions; `git add` on the README adds the rewrite. The deletions show up as removals in the commit.)

---

## Task 11: Top-level README note

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Update the "Access from QGIS Desktop on other devices" section**

In `README.md`, find the section that begins:

```markdown
## Access from QGIS Desktop on other devices

Pre-built import bundles live at `client/connections/`:

- `qgis-wms-connections.xml` — rendered raster map with legends
- `qgis-wfs-connections.xml` — editable vector features

See `client/connections/README.md` for DNS prereqs (LAN dnsmasq, hosts file,
or Tailscale IP) and the import flow. Short version: copy the XML to the
remote device, ensure `qgis.devbox` resolves, then in the Browser panel
right-click WMS/WMTS or WFS → *Load Connections…*.
```

Replace with:

```markdown
## Access from QGIS Desktop on other devices

The watcher writes WMS and WFS connection bundles into `/srv/qgis/web/`
on every regen, served live by the cluster:

- `http://qgis.devbox/qgis-wms-connections.xml` — rendered raster map
  with legends, one connection per gpkg
- `http://qgis.devbox/qgis-wfs-connections.xml` — editable vector
  features, one connection per gpkg

See `client/connections/README.md` for DNS prereqs (LAN dnsmasq, hosts
file, or Tailscale IP) and the import flow. Short version: `curl -O`
the XMLs from the URLs above, ensure `qgis.devbox` resolves, then in the
Browser panel right-click WMS/WMTS or WFS → *Load Connections…*.
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: point QGIS Desktop access section at live bundles"
```

---

## Task 12: Manual end-to-end verification

This task is hands-on — it doesn't change code. Run through it on the dev box after Tasks 1-11 land. If anything fails, raise it as a follow-up.

- [ ] **Step 1: Redeploy the chart**

```bash
helm upgrade qgis ./chart -n qgis
```

Expected: the watcher Deployment rolls (its `checksum/script` annotation rolls because `generate_qgs.py` changed); the viewer Deployment rolls (configmap changed).

- [ ] **Step 2: Watch the watcher come up and regen**

```bash
sudo kubectl -n qgis logs deploy/project-watcher --tail=50 -f
```

Expected log lines (will appear within ~10s of pod ready):

- `regen: wrote N project(s), pruned M, themesConfig OK` — N matches the count of gpkgs under `/srv/qgis/data/` (currently 17 + 2 territories = 19), M is the count of stale single-stem .qgs files removed.
- `bake: wrote themes.json with N theme(s)` — same N as above.

Press Ctrl-C to detach.

- [ ] **Step 3: Verify the connection XMLs are served**

```bash
curl -sI http://qgis.devbox/qgis-wms-connections.xml | head -3
curl -sI http://qgis.devbox/qgis-wfs-connections.xml | head -3
```

Expected for both: `HTTP/1.1 200 OK` and `Cache-Control: no-cache`.

```bash
curl -s http://qgis.devbox/qgis-wms-connections.xml | head -5
```

Expected: `<?xml ...>` followed by `<!DOCTYPE connections>` and `<qgsWMSConnections version="1.0">` and at least one `<wms ... name="..." />`.

- [ ] **Step 4: Verify a missing path returns 404, not the SPA**

```bash
curl -sI http://qgis.devbox/qgis-typo-connections.xml | head -1
```

Expected: a 200 with the SPA HTML *or* a 404 — the exact-match locations only fire for the two well-known names; the SPA fallback handles other paths. This is fine; the assertion in the spec is only about the known XMLs being correct, not about every other path.

- [ ] **Step 5: Verify on the dev workstation in QGIS Desktop**

1. `curl -O http://qgis.devbox/qgis-wms-connections.xml`
2. `curl -O http://qgis.devbox/qgis-wfs-connections.xml`
3. In QGIS Desktop, *Browser panel → WMS/WMTS → Load Connections… →* pick the downloaded WMS file → check a few of the new connections (e.g. "Mecklenburg Greenways", "NC Parcels – Parcels Pt", "Union Roads") → *OK*.
4. Repeat for WFS.
5. Expand each connection. The layer list should populate. Drag a layer onto the canvas; features should render.

If any of those connections fail with HTTP 500 or "no layers", check `sudo kubectl -n qgis logs deploy/qgis-server --tail=100` — likely a missing CRS or a malformed `.qgs`.

---

## Self-review

**Spec coverage** (each spec section → task):

- "Naming rule / `_slug`, `_project_id`, `_project_title`" → Tasks 1, 2, 3
- "Concrete results across all 17 gpkgs" → covered by parametrize tables in Tasks 2 and 3
- "Project + theme generation changes (write_themes_config)" → Task 4
- "write_project + regen_all" → Task 5
- "_prune_orphans regression" → Task 5 (test in Step 1)
- "Connection-XML generation (write_connections helper)" → Task 6
- "Wiring (regen_all + CLI flag + watcher Deployment)" → Tasks 7, 8
- "Distribution (nginx routing)" → Task 9
- "Distribution (client-side docs / static-file removal)" → Task 10
- "Top-level operator doc note" → Task 11
- "Rollout (manual verification)" → Task 12

**No placeholders** — every step contains executable code, exact paths, or exact commands.

**Type / signature consistency** — `data_dir` is keyword-only on both `write_themes_config` and `write_project`; `ingress_host` is keyword-only on `regen_all` / `_watch_loop`; `_project_id` and `_project_title` always take `(gpkg, data_dir)` positionally; `write_connections` takes `(gpkgs, projects_dir, data_dir, out_wms, out_wfs, ingress_host)` and the test in Task 6 calls it the same way as `regen_all` does in Task 7.
