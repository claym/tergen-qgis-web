"""Tests for chart/files/generate_qgs.py."""

import json
import sqlite3
import textwrap
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

import generate_qgs as gen


def _make_minimal_gpkg(path: Path, layer_name: str = "things") -> None:
    """Create a tiny but valid GeoPackage with a single point layer.

    Just enough metadata for the generator's introspection — gpkg_contents +
    gpkg_geometry_columns + a feature table with a few attribute columns.
    """
    con = sqlite3.connect(path)
    cur = con.cursor()
    cur.executescript(
        textwrap.dedent(
            f"""
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
            CREATE TABLE "{layer_name}" (
              fid INTEGER PRIMARY KEY,
              geom BLOB,
              name TEXT,
              count INTEGER
            );
            INSERT INTO gpkg_contents VALUES
              ('{layer_name}', 'features', '{layer_name}', '', '2026-01-01',
               1000000, 500000, 1100000, 600000, 2264);
            INSERT INTO gpkg_geometry_columns VALUES
              ('{layer_name}', 'geom', 'POINT', 2264, 0, 0);
            """
        )
    )
    con.commit()
    con.close()


def test_introspect_returns_layers_with_columns(tmp_path):
    gpkg = tmp_path / "tiny.gpkg"
    _make_minimal_gpkg(gpkg, layer_name="things")

    layers = gen.introspect_gpkg(gpkg)

    assert len(layers) == 1
    layer = layers[0]
    assert layer.name == "things"
    assert layer.geometry_type == "POINT"
    assert layer.srs_id == 2264
    assert layer.columns == ["fid", "geom", "name", "count"]
    assert layer.bbox == (1000000, 500000, 1100000, 600000)
    assert str(gpkg) in str(layer.source_path)


def test_introspect_skips_non_features(tmp_path):
    gpkg = tmp_path / "mixed.gpkg"
    _make_minimal_gpkg(gpkg)
    # Add a non-feature row
    con = sqlite3.connect(gpkg)
    con.execute(
        "INSERT INTO gpkg_contents VALUES "
        "('attr_only', 'attributes', 'attr_only', '', '2026-01-01',"
        " 0, 0, 0, 0, 2264)"
    )
    con.commit()
    con.close()

    layers = gen.introspect_gpkg(gpkg)

    assert [l.name for l in layers] == ["things"]


def test_render_qgs_produces_valid_xml_with_one_maplayer_per_layer(tmp_path):
    gpkg = tmp_path / "tiny.gpkg"
    _make_minimal_gpkg(gpkg)
    layers = gen.introspect_gpkg(gpkg)

    xml_str = gen.render_qgs(layers, project_crs_authid="EPSG:3857")

    root = ET.fromstring(xml_str)
    assert root.tag == "qgis"
    maplayers = root.findall("./projectlayers/maplayer")
    assert len(maplayers) == 1


def test_render_qgs_sets_wms_root_name_and_service_title(tmp_path):
    """QGIS Server takes the WMS root layer Name from <WMSRootName>; without
    it the root is nameless and QWC2 ends up with an empty theme name."""
    gpkg = tmp_path / "tiny.gpkg"
    _make_minimal_gpkg(gpkg)
    layers = gen.introspect_gpkg(gpkg)

    xml_str = gen.render_qgs(
        layers, project_crs_authid="EPSG:3857",
        project_name="tiny", project_title="Tiny",
    )
    root = ET.fromstring(xml_str)
    assert root.get("projectname") == "tiny"
    assert root.findtext("./properties/WMSRootName") == "tiny"
    assert root.findtext("./properties/WMSServiceTitle") == "Tiny"


def test_render_qgs_publishes_every_layer_via_wfs(tmp_path):
    main_gpkg = tmp_path / "main.gpkg"
    debug_gpkg = tmp_path / "debug.gpkg"
    _make_minimal_gpkg(main_gpkg, layer_name="territories")
    _make_minimal_gpkg(debug_gpkg, layer_name="step_500_addresses")

    layers = gen.introspect_gpkg(main_gpkg) + gen.introspect_gpkg(debug_gpkg)
    xml_str = gen.render_qgs(layers, project_crs_authid="EPSG:3857")

    root = ET.fromstring(xml_str)
    wfs_ids = {v.text for v in root.findall("./properties/WFSLayers/value")}
    assert wfs_ids == {l.layer_id for l in layers}


def test_render_qgs_marks_territories_visible_step_layers_hidden(tmp_path):
    main_gpkg = tmp_path / "main.gpkg"
    debug_gpkg = tmp_path / "debug.gpkg"
    _make_minimal_gpkg(main_gpkg, layer_name="territories")
    _make_minimal_gpkg(debug_gpkg, layer_name="step_500_addresses")

    layers = gen.introspect_gpkg(main_gpkg) + gen.introspect_gpkg(debug_gpkg)
    xml_str = gen.render_qgs(layers, project_crs_authid="EPSG:3857")

    root = ET.fromstring(xml_str)
    # layer-tree-layer's `id` is a generated UUID; visibility is keyed by `name`
    visible = {n.get("name") for n in root.findall(
        "./layer-tree-group//layer-tree-layer[@checked='Qt::Checked']"
    )}
    hidden = {n.get("name") for n in root.findall(
        "./layer-tree-group//layer-tree-layer[@checked='Qt::Unchecked']"
    )}
    assert "territories" in visible
    assert "step_500_addresses" in hidden


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


def test_atomic_write_text_replaces_target_atomically(tmp_path):
    target = tmp_path / "out.txt"
    target.write_text("old contents")

    gen.atomic_write_text(target, "new contents")

    assert target.read_text() == "new contents"
    # No leftover .tmp files
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "out.txt"]
    assert leftovers == []


def test_write_project_writes_one_qgs_per_gpkg(tmp_path):
    main_gpkg = tmp_path / "territories_draft.gpkg"
    debug_gpkg = tmp_path / "debug.gpkg"
    _make_minimal_gpkg(main_gpkg, layer_name="territories")
    _make_minimal_gpkg(debug_gpkg, layer_name="step_500_addresses")

    out_dir = tmp_path / "projects"
    out_dir.mkdir()

    gen.write_project(main_gpkg, out_dir / "territories_draft.qgs", data_dir=tmp_path)
    gen.write_project(debug_gpkg, out_dir / "debug.qgs", data_dir=tmp_path)

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
        data_dir=tmp_path,
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


def test_themes_config_matches_qwc2_themesConfig_py_schema(tmp_path):
    """themesConfig.py expects defaultScales at top level and a few keys
    nested under "themes" (backgroundLayers, pluginData). If the writer ever
    drifts back to the old layout the bake step silently breaks."""
    gpkg = tmp_path / "x.gpkg"
    _make_minimal_gpkg(gpkg, layer_name="things")
    out = tmp_path / "themesConfig.json"

    gen.write_themes_config(
        gpkgs=[gpkg], projects_dir=Path("/srv/qgis/projects"),
        out=out, default_theme="x",
        data_dir=tmp_path,
    )

    cfg = json.loads(out.read_text())
    assert isinstance(cfg.get("defaultScales"), list) and cfg["defaultScales"]
    assert "backgroundLayers" in cfg["themes"]
    assert "pluginData" in cfg["themes"]
    # And the deprecated top-level layout is gone:
    assert "backgroundLayers" not in cfg
    assert "pluginData" not in cfg


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
        data_dir=tmp_path,
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
        data_dir=tmp_path,
    )

    cfg = json.loads(out.read_text())
    assert cfg["themes"]["items"][0]["searchProviders"] == []


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


@pytest.mark.parametrize("raw,expected", [
    ("Mecklenburg Addresses", "Mecklenburg_Addresses"),
    ("NC Streams", "NC_Streams"),
    ("clipped_data", "clipped_data"),
    ("plain", "plain"),
    ("with/slash", "with_slash"),
    (r"with\backslash", "with_backslash"),
    ("two  spaces", "two_spaces"),
    ("trailing ", "trailing"),
])
def test_slug_collapses_spaces_and_slashes(raw, expected):
    assert gen._slug(raw) == expected


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


def test_project_id_slugs_unsafe_stem(tmp_path):
    """A gpkg stem with whitespace should be slugged on the way into the id —
    otherwise it would produce a malformed .qgs filename and MAP= URL."""
    data_dir = tmp_path
    folder = data_dir / "clipped_data" / "Weird Folder"
    folder.mkdir(parents=True)
    gpkg = folder / "weird name.gpkg"
    gpkg.touch()
    assert gen._project_id(gpkg, data_dir) == "Weird_Folder__weird_name"


@pytest.mark.parametrize("rel,expected", [
    # Top-level / territories: smart-title the stem (same branch as _project_title)
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


def test_project_title_preserves_acronyms_in_territories_branch(tmp_path):
    """The top-level / territories branch should also preserve acronyms,
    not lowercase them like raw .title() would."""
    data_dir = tmp_path
    (data_dir / "territories").mkdir()
    gpkg = data_dir / "territories" / "NCDOT_things.gpkg"
    gpkg.touch()
    assert gen._project_title(gpkg, data_dir) == "NCDOT Things"


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


def test_write_connections_handles_empty_gpkg_list(tmp_path):
    """A cold cluster with zero gpkgs should write valid but empty bundles
    rather than crash. The XML must still parse; it just has no entries."""
    out_wms = tmp_path / "qgis-wms-connections.xml"
    out_wfs = tmp_path / "qgis-wfs-connections.xml"

    gen.write_connections(
        gpkgs=[],
        projects_dir=Path("/srv/qgis/projects"),
        data_dir=tmp_path,
        out_wms=out_wms,
        out_wfs=out_wfs,
        ingress_host="qgis.devbox",
    )

    wms_root = ET.fromstring(out_wms.read_text())
    wfs_root = ET.fromstring(out_wfs.read_text())
    assert wms_root.tag == "qgsWMSConnections"
    assert wms_root.findall("wms") == []
    assert wfs_root.tag == "qgsWFSConnections"
    assert wfs_root.findall("wfs") == []


def test_write_connections_rejects_empty_ingress_host(tmp_path):
    """An empty ingress_host would produce a malformed http:// URL.
    Fail fast rather than silently emit broken bundles."""
    out_wms = tmp_path / "qgis-wms-connections.xml"
    out_wfs = tmp_path / "qgis-wfs-connections.xml"
    with pytest.raises(ValueError, match="ingress_host"):
        gen.write_connections(
            gpkgs=[],
            projects_dir=Path("/srv/qgis/projects"),
            data_dir=tmp_path,
            out_wms=out_wms,
            out_wfs=out_wfs,
            ingress_host="",
        )


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
