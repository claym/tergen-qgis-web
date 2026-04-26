"""Generate a QGIS Server project (project.qgs) from one or more GeoPackage files.

Approach
--------
A QGIS .qgs file is just an XML document. We don't need PyQGIS or GDAL — we
introspect each .gpkg via the stdlib sqlite3 module (a GeoPackage is a SQLite
database) and emit a minimal but valid .qgs that QGIS Server is happy to load.

Usage
-----
    generate_qgs.py /path/to/data --output /path/to/project.qgs

The script scans `*.gpkg` recursively under the given path. Each file may
contain one or more vector layers; every feature-table layer is emitted.

Visibility convention
---------------------
Layers named `territories` are visible by default. Layers whose name starts
with `step_` are emitted but hidden. Anything else is visible.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable
from xml.etree import ElementTree as ET


@dataclass
class Layer:
    """A single feature-table layer discovered in a GeoPackage."""

    name: str
    source_path: Path
    geometry_type: str
    srs_id: int
    columns: list[str]
    bbox: tuple[float, float, float, float]  # (min_x, min_y, max_x, max_y)
    layer_id: str = field(default_factory=lambda: f"layer_{uuid.uuid4().hex[:12]}")


def introspect_gpkg(path: Path) -> list[Layer]:
    """Return one Layer per feature table in the GeoPackage at *path*."""
    con = sqlite3.connect(path)
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT c.table_name, g.geometry_type_name, c.srs_id,"
            "       c.min_x, c.min_y, c.max_x, c.max_y "
            "  FROM gpkg_contents c "
            "  JOIN gpkg_geometry_columns g ON g.table_name = c.table_name "
            " WHERE c.data_type = 'features' "
            " ORDER BY c.table_name"
        )
        rows = cur.fetchall()
        layers: list[Layer] = []
        for table, geom_type, srs_id, mnx, mny, mxx, mxy in rows:
            cur.execute(f'PRAGMA table_info("{table}")')
            columns = [r[1] for r in cur.fetchall()]
            layers.append(
                Layer(
                    name=table,
                    source_path=path,
                    geometry_type=(geom_type or "GEOMETRY").upper(),
                    srs_id=int(srs_id),
                    columns=columns,
                    bbox=(float(mnx or 0), float(mny or 0),
                          float(mxx or 0), float(mxy or 0)),
                )
            )
        return layers
    finally:
        con.close()


# Map between GeoPackage geometry types and QGIS WKB type integers.
# Reference: QGIS QgsWkbTypes enum.
_WKB_TYPE = {
    "POINT": 1, "LINESTRING": 2, "POLYGON": 3,
    "MULTIPOINT": 4, "MULTILINESTRING": 5, "MULTIPOLYGON": 6,
    "GEOMETRY": 0, "GEOMETRYCOLLECTION": 7,
}


def _layer_geometry_type_int(geom: str) -> int:
    return _WKB_TYPE.get(geom.upper(), 0)


def _is_visible_by_default(layer_name: str) -> bool:
    if layer_name == "territories":
        return True
    if layer_name.startswith("step_"):
        return False
    return True


def _build_maplayer(layer: Layer) -> ET.Element:
    """Build the <maplayer> element for a single layer."""
    crs_authid = f"EPSG:{layer.srs_id}"
    ml = ET.Element("maplayer", attrib={
        "type": "vector",
        "geometry": layer.geometry_type.title(),
        "wkbType": str(_layer_geometry_type_int(layer.geometry_type)),
    })
    ET.SubElement(ml, "id").text = layer.layer_id
    ET.SubElement(ml, "datasource").text = (
        f'{layer.source_path}|layername={layer.name}'
    )
    ET.SubElement(ml, "layername").text = layer.name
    ET.SubElement(ml, "shortname").text = layer.name
    # CRS block
    srs = ET.SubElement(ml, "srs")
    spref = ET.SubElement(srs, "spatialrefsys")
    ET.SubElement(spref, "authid").text = crs_authid
    ET.SubElement(spref, "srid").text = str(layer.srs_id)
    # Provider
    ET.SubElement(ml, "provider").text = "ogr"
    # Empty rendererv2 — QGIS Server uses defaults
    ET.SubElement(ml, "renderer-v2", attrib={
        "type": "singleSymbol", "symbollevels": "0", "enableorderby": "0",
    })
    # Mark queryable for GetFeatureInfo
    ET.SubElement(ml, "flags")
    ET.SubElement(ml, "fieldConfiguration")
    return ml


def _build_layer_tree(layers: Iterable[Layer]) -> ET.Element:
    tree = ET.Element("layer-tree-group", attrib={
        "checked": "Qt::Checked", "expanded": "1", "name": "",
    })
    for layer in layers:
        ET.SubElement(tree, "layer-tree-layer", attrib={
            "id": layer.layer_id,
            "name": layer.name,
            "providerKey": "ogr",
            "source": f'{layer.source_path}|layername={layer.name}',
            "checked": "Qt::Checked" if _is_visible_by_default(layer.name)
                       else "Qt::Unchecked",
            "expanded": "0",
        })
    return tree


def render_qgs(layers: list[Layer], project_crs_authid: str = "EPSG:3857") -> str:
    """Render the .qgs XML for the given layers."""
    root = ET.Element("qgis", attrib={
        "version": "3.34", "projectname": "qgis",
    })
    # Project CRS
    proj_crs = ET.SubElement(root, "projectCrs")
    spref = ET.SubElement(proj_crs, "spatialrefsys")
    ET.SubElement(spref, "authid").text = project_crs_authid
    # Layer tree (visibility ordering)
    root.append(_build_layer_tree(layers))
    # Map layers (the actual definitions)
    pl = ET.SubElement(root, "projectlayers")
    for layer in layers:
        pl.append(_build_maplayer(layer))
    # WMS service settings — make every layer queryable
    props = ET.SubElement(root, "properties")
    wms_layers = ET.SubElement(props, "WMSRestrictedLayers")
    wms_layers.set("type", "QStringList")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(
        root, encoding="unicode"
    )


def discover_gpkgs(root: Path) -> list[Path]:
    return sorted(root.rglob("*.gpkg"))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Generate a QGIS Server project.qgs from GeoPackage files."
    )
    p.add_argument(
        "data_dir",
        help="Directory to scan recursively for *.gpkg files",
    )
    p.add_argument(
        "--output",
        required=True,
        help="Path where project.qgs should be written",
    )
    args = p.parse_args(argv)

    data_dir = Path(args.data_dir)
    output = Path(args.output)

    if not data_dir.is_dir():
        print(f"data_dir not found or not a directory: {data_dir}",
              file=sys.stderr)
        return 2

    gpkgs = discover_gpkgs(data_dir)
    if not gpkgs:
        print(f"no .gpkg files found under {data_dir}", file=sys.stderr)
        return 1

    layers: list[Layer] = []
    for gpkg in gpkgs:
        layers.extend(introspect_gpkg(gpkg))

    if not layers:
        print("no feature-table layers discovered in any .gpkg", file=sys.stderr)
        return 1

    output.write_text(render_qgs(layers))
    print(f"wrote {output} with {len(layers)} layer(s) "
          f"from {len(gpkgs)} gpkg file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
