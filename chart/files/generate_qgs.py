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
import json as _json
import math
import re as _re
import sqlite3
import struct
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable
from xml.etree import ElementTree as ET


def atomic_write_text(path: Path, contents: str) -> None:
    """Write *contents* to *path* atomically.

    Writes to a sibling tempfile then os.rename, which is atomic on POSIX.
    Readers (qgis-server) never see a half-written file.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(contents)
    tmp.replace(path)


# ---------------------------------------------------------------------------
# CRS metadata — hardcoded for EPSG:2264 (source) and EPSG:3857 (project).
# QGIS Server needs the full spatialrefsys block (wkt, proj4, srsid, etc.)
# to perform CRS transformations. authid+srid alone are insufficient.
# ---------------------------------------------------------------------------

# Internal QGIS srsid values (from QGIS's srs.db)
_QGIS_SRSID = {
    2264: 2937,   # NAD83 / North Carolina (ftUS)
    3857: 3857,   # WGS 84 / Pseudo-Mercator  (srsid happens to equal EPSG code)
    4326: 3452,   # WGS 84 geographic
}

_CRS_DATA: dict[int, dict] = {
    2264: {
        "authid": "EPSG:2264",
        "srid": 2264,
        "srsid": _QGIS_SRSID[2264],
        "description": "NAD83 / North Carolina (ftUS)",
        "projectionacronym": "lcc",
        "ellipsoidacronym": "EPSG:7019",
        "geographicflag": "false",
        "proj4": (
            "+proj=lcc +lat_1=36.16666666666666 +lat_2=34.33333333333334"
            " +lat_0=33.75 +lon_0=-79 +x_0=609601.2192024384 +y_0=0"
            " +ellps=GRS80 +towgs84=0,0,0,0,0,0,0 +units=us-ft +no_defs"
        ),
        "wkt": (
            'PROJCS["NAD83 / North Carolina (ftUS)",'
            'GEOGCS["NAD83",'
            'DATUM["North_American_Datum_1983",'
            'SPHEROID["GRS 1980",6378137,298.257222101,'
            'AUTHORITY["EPSG","7019"]],'
            'AUTHORITY["EPSG","6269"]],'
            'PRIMEM["Greenwich",0,AUTHORITY["EPSG","8901"]],'
            'UNIT["degree",0.0174532925199433,AUTHORITY["EPSG","9122"]],'
            'AUTHORITY["EPSG","4269"]],'
            'PROJECTION["Lambert_Conformal_Conic_2SP"],'
            'PARAMETER["standard_parallel_1",36.16666666666666],'
            'PARAMETER["standard_parallel_2",34.33333333333334],'
            'PARAMETER["latitude_of_origin",33.75],'
            'PARAMETER["central_meridian",-79],'
            'PARAMETER["false_easting",1999999.9999980001],'
            'PARAMETER["false_northing",0],'
            'UNIT["US survey foot",0.3048006096012192,'
            'AUTHORITY["EPSG","9003"]],'
            'AXIS["Easting",EAST],'
            'AXIS["Northing",NORTH],'
            'AUTHORITY["EPSG","2264"]]'
        ),
    },
    3857: {
        "authid": "EPSG:3857",
        "srid": 3857,
        "srsid": _QGIS_SRSID[3857],
        "description": "WGS 84 / Pseudo-Mercator",
        "projectionacronym": "merc",
        "ellipsoidacronym": "EPSG:7030",
        "geographicflag": "false",
        "proj4": (
            "+proj=merc +a=6378137 +b=6378137 +lat_ts=0 +lon_0=0"
            " +x_0=0 +y_0=0 +k=1 +units=m +nadgrids=@null +wktext +no_defs"
        ),
        "wkt": (
            'PROJCS["WGS 84 / Pseudo-Mercator",'
            'GEOGCS["WGS 84",'
            'DATUM["WGS_1984",'
            'SPHEROID["WGS 84",6378137,298.257223563,'
            'AUTHORITY["EPSG","7030"]],'
            'AUTHORITY["EPSG","6326"]],'
            'PRIMEM["Greenwich",0,AUTHORITY["EPSG","8901"]],'
            'UNIT["degree",0.0174532925199433,AUTHORITY["EPSG","9122"]],'
            'AUTHORITY["EPSG","4326"]],'
            'PROJECTION["Mercator_1SP"],'
            'PARAMETER["central_meridian",0],'
            'PARAMETER["scale_factor",1],'
            'PARAMETER["false_easting",0],'
            'PARAMETER["false_northing",0],'
            'UNIT["metre",1,AUTHORITY["EPSG","9001"]],'
            'AXIS["Easting",EAST],'
            'AXIS["Northing",NORTH],'
            'EXTENSION["PROJ4",'
            '"+proj=merc +a=6378137 +b=6378137 +lat_ts=0 +lon_0=0'
            ' +x_0=0 +y_0=0 +k=1 +units=m +nadgrids=@null +wktext +no_defs"],'
            'AUTHORITY["EPSG","3857"]]'
        ),
    },
}


def _build_spatialrefsys(epsg: int) -> ET.Element:
    """Return a fully-populated <spatialrefsys> element for the given EPSG code.

    QGIS Server requires wkt, proj4, srsid, description etc. to perform CRS
    transformations — authid+srid alone are not sufficient.
    """
    data = _CRS_DATA.get(epsg)
    if data is None:
        # Fallback: emit minimal block for unknown CRS codes.
        el = ET.Element("spatialrefsys")
        ET.SubElement(el, "wkt")
        ET.SubElement(el, "proj4")
        ET.SubElement(el, "srsid").text = "0"
        ET.SubElement(el, "srid").text = str(epsg)
        ET.SubElement(el, "authid").text = f"EPSG:{epsg}"
        ET.SubElement(el, "description").text = f"EPSG:{epsg}"
        ET.SubElement(el, "projectionacronym")
        ET.SubElement(el, "ellipsoidacronym")
        ET.SubElement(el, "geographicflag").text = "false"
        return el

    el = ET.Element("spatialrefsys")
    ET.SubElement(el, "wkt").text = data["wkt"]
    ET.SubElement(el, "proj4").text = data["proj4"]
    ET.SubElement(el, "srsid").text = str(data["srsid"])
    ET.SubElement(el, "srid").text = str(data["srid"])
    ET.SubElement(el, "authid").text = data["authid"]
    ET.SubElement(el, "description").text = data["description"]
    ET.SubElement(el, "projectionacronym").text = data["projectionacronym"]
    ET.SubElement(el, "ellipsoidacronym").text = data["ellipsoidacronym"]
    ET.SubElement(el, "geographicflag").text = data["geographicflag"]
    return el


# ---------------------------------------------------------------------------
# Lambert Conformal Conic inverse projection — pure Python.
# Used to compute wgs84extent from EPSG:2264 bounding boxes.
# Parameters for EPSG:2264 (NAD83 / North Carolina ftUS):
#   lat_1=36.16666666666666  lat_2=34.33333333333334
#   lat_0=33.75  lon_0=-79
#   x_0=609601.2192024384 m (= 1999999.9999980001 us-ft)
#   y_0=0  ellps=GRS80  units=us-ft
# ---------------------------------------------------------------------------

_D2R = math.pi / 180.0
_US_FT_TO_M = 0.3048006096012192

# GRS80 ellipsoid
_GRS80_A = 6378137.0          # semi-major axis (m)
_GRS80_F = 1.0 / 298.257222101
_GRS80_E2 = 2 * _GRS80_F - _GRS80_F ** 2
_GRS80_E = math.sqrt(_GRS80_E2)

# LCC parameters for EPSG:2264
_LCC_PHI1 = 36.16666666666666 * _D2R
_LCC_PHI2 = 34.33333333333334 * _D2R
_LCC_PHI0 = 33.75 * _D2R
_LCC_LAM0 = -79.0 * _D2R
_LCC_X0 = 609601.2192024384    # false easting in metres
_LCC_Y0 = 0.0                  # false northing in metres


def _lcc_m(phi: float) -> float:
    """LCC helper m(phi)."""
    e = _GRS80_E
    sin_phi = math.sin(phi)
    return math.cos(phi) / math.sqrt(1.0 - e * e * sin_phi * sin_phi)


def _lcc_t(phi: float) -> float:
    """LCC helper t(phi)."""
    e = _GRS80_E
    sin_phi = math.sin(phi)
    return math.tan(math.pi / 4.0 - phi / 2.0) / (
        ((1.0 - e * sin_phi) / (1.0 + e * sin_phi)) ** (e / 2.0)
    )


# Pre-compute LCC constants for EPSG:2264
_LCC_M1 = _lcc_m(_LCC_PHI1)
_LCC_M2 = _lcc_m(_LCC_PHI2)
_LCC_T1 = _lcc_t(_LCC_PHI1)
_LCC_T2 = _lcc_t(_LCC_PHI2)
_LCC_T0 = _lcc_t(_LCC_PHI0)
_LCC_N = math.log(_LCC_M1 / _LCC_M2) / math.log(_LCC_T1 / _LCC_T2)
_LCC_F = _LCC_M1 / (_LCC_N * _LCC_T1 ** _LCC_N)
_LCC_R0 = _GRS80_A * _LCC_F * _LCC_T0 ** _LCC_N


def _epsg2264_to_wgs84(x_usft: float, y_usft: float) -> tuple[float, float]:
    """Convert EPSG:2264 (us-ft) coordinates to WGS84 lon/lat (degrees).

    Implements the inverse Lambert Conformal Conic projection for
    EPSG:2264 (NAD83 / North Carolina ftUS). Since NAD83 ≈ WGS84 at
    sub-metre accuracy, no datum shift is applied.
    """
    x_m = x_usft * _US_FT_TO_M
    y_m = y_usft * _US_FT_TO_M

    x = x_m - _LCC_X0
    y = y_m - _LCC_Y0

    r_prime = math.copysign(
        math.sqrt(x * x + (_LCC_R0 - y) ** 2),
        _LCC_N,
    )
    t_prime = (r_prime / (_GRS80_A * _LCC_F)) ** (1.0 / _LCC_N)
    theta_prime = math.atan2(x, _LCC_R0 - y)

    lam = theta_prime / _LCC_N + _LCC_LAM0

    # Iterative solution for phi (latitude)
    phi = math.pi / 2.0 - 2.0 * math.atan(t_prime)
    for _ in range(10):
        e = _GRS80_E
        sin_phi = math.sin(phi)
        phi_new = (
            math.pi / 2.0
            - 2.0 * math.atan(
                t_prime
                * ((1.0 - e * sin_phi) / (1.0 + e * sin_phi)) ** (e / 2.0)
            )
        )
        if abs(phi_new - phi) < 1e-12:
            phi = phi_new
            break
        phi = phi_new

    return lam / _D2R, phi / _D2R  # lon, lat in degrees


def _bbox_to_wgs84(
    min_x: float, min_y: float, max_x: float, max_y: float, epsg: int
) -> tuple[float, float, float, float] | None:
    """Convert a bounding box from *epsg* CRS to WGS84 lon/lat.

    Returns (west, south, east, north) or None if conversion unsupported.
    """
    if epsg == 2264:
        w, s = _epsg2264_to_wgs84(min_x, min_y)
        e, n = _epsg2264_to_wgs84(max_x, max_y)
        return w, s, e, n
    return None


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# GeoPackage introspection
# ---------------------------------------------------------------------------

# Map between GeoPackage geometry types and QGIS WKB type integers.
# Reference: QGIS QgsWkbTypes enum.
_WKB_TYPE = {
    "POINT": 1, "LINESTRING": 2, "POLYGON": 3,
    "MULTIPOINT": 4, "MULTILINESTRING": 5, "MULTIPOLYGON": 6,
    "GEOMETRY": 0, "GEOMETRYCOLLECTION": 7,
}

# Map WKB integer type -> canonical gpkg geometry type name
_WKB_INT_TO_GEOM = {
    1: "POINT", 2: "LINESTRING", 3: "POLYGON",
    4: "MULTIPOINT", 5: "MULTILINESTRING", 6: "MULTIPOLYGON",
    7: "GEOMETRYCOLLECTION",
    # 2D+Z/M variants — map to base type
    1001: "POINT", 1002: "LINESTRING", 1003: "POLYGON",
    1004: "MULTIPOINT", 1005: "MULTILINESTRING", 1006: "MULTIPOLYGON",
    2001: "POINT", 2002: "LINESTRING", 2003: "POLYGON",
    2004: "MULTIPOINT", 2005: "MULTILINESTRING", 2006: "MULTIPOLYGON",
    3001: "POINT", 3002: "LINESTRING", 3003: "POLYGON",
    3004: "MULTIPOINT", 3005: "MULTILINESTRING", 3006: "MULTIPOLYGON",
}


def _gpkg_envelope_size(flags: int) -> int:
    """Return the envelope byte count given the gpkg_flags byte."""
    indicator = (flags >> 1) & 0x07
    return {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}.get(indicator, 0)


def _wkb_type_from_blob(blob: bytes) -> int | None:
    """Extract the WKB geometry type integer from a GeoPackage geometry blob.

    A GeoPackage geometry blob starts with:
      'GP' (2 bytes magic), version (1), flags (1), srs_id (4) = 8 bytes header
    Optionally followed by an envelope (0, 32, 48, or 64 bytes).
    Then the WKB geometry: byte_order (1), wkbType (4).

    Returns the WKB type integer, or None if the blob is too short / invalid.
    """
    if not blob or len(blob) < 8:
        return None
    if blob[:2] != b"GP":
        return None
    flags = blob[3]
    env_size = _gpkg_envelope_size(flags)
    wkb_offset = 8 + env_size
    wkb = blob[wkb_offset:]
    if len(wkb) < 5:
        return None
    byte_order = wkb[0]
    if byte_order == 1:  # little-endian
        (wkb_type,) = struct.unpack_from("<I", wkb, 1)
    else:  # big-endian
        (wkb_type,) = struct.unpack_from(">I", wkb, 1)
    return wkb_type


def _detect_concrete_geometry(con: sqlite3.Connection, table: str, geom_col: str = "geom") -> str:
    """Inspect the first non-null geometry in *table* to determine its type.

    Returns the canonical gpkg geometry type string (e.g. 'POLYGON').
    Falls back to 'GEOMETRY' if detection fails.
    """
    try:
        cur = con.cursor()
        cur.execute(
            f'SELECT "{geom_col}" FROM "{table}" WHERE "{geom_col}" IS NOT NULL LIMIT 1'
        )
        row = cur.fetchone()
        if row is None or row[0] is None:
            return "GEOMETRY"
        blob = bytes(row[0])
        wkb_type = _wkb_type_from_blob(blob)
        if wkb_type is None:
            return "GEOMETRY"
        return _WKB_INT_TO_GEOM.get(wkb_type, "GEOMETRY")
    except sqlite3.Error:
        return "GEOMETRY"


def introspect_gpkg(path: Path) -> list[Layer]:
    """Return one Layer per feature table in the GeoPackage at *path*."""
    con = sqlite3.connect(path)
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT c.table_name, g.geometry_type_name, g.column_name, c.srs_id,"
            "       c.min_x, c.min_y, c.max_x, c.max_y "
            "  FROM gpkg_contents c "
            "  JOIN gpkg_geometry_columns g ON g.table_name = c.table_name "
            " WHERE c.data_type = 'features' "
            " ORDER BY c.table_name"
        )
        rows = cur.fetchall()
        layers: list[Layer] = []
        for table, geom_type, geom_col, srs_id, mnx, mny, mxx, mxy in rows:
            geom_type = (geom_type or "GEOMETRY").upper()
            # For generic GEOMETRY columns, inspect the first feature's WKB
            # to determine the concrete type (wkbType=0 prevents rendering).
            if geom_type == "GEOMETRY":
                geom_type = _detect_concrete_geometry(con, table, geom_col)
            cur.execute(f'PRAGMA table_info("{table}")')
            columns = [r[1] for r in cur.fetchall()]
            layers.append(
                Layer(
                    name=table,
                    source_path=path,
                    geometry_type=geom_type,
                    srs_id=int(srs_id),
                    columns=columns,
                    bbox=(float(mnx or 0), float(mny or 0),
                          float(mxx or 0), float(mxy or 0)),
                )
            )
        return layers
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Renderer helpers — produce a default symbol for each geometry type
# ---------------------------------------------------------------------------

def _layer_geometry_type_int(geom: str) -> int:
    return _WKB_TYPE.get(geom.upper(), 0)


def _build_default_symbol(geom_type: str) -> ET.Element:
    """Build a default <symbol> element for the given geometry type.

    QGIS Server requires at least one symbol inside renderer-v2 to produce
    any output. An empty renderer-v2 silently renders nothing.
    """
    g = geom_type.upper()
    sym = ET.Element("symbol", attrib={
        "name": "0",
        "alpha": "1",
        "clip_to_extent": "1",
        "force_rhr": "0",
        "type": (
            "fill" if g in ("POLYGON", "MULTIPOLYGON", "GEOMETRY", "GEOMETRYCOLLECTION")
            else "line" if g in ("LINESTRING", "MULTILINESTRING")
            else "marker"
        ),
    })
    data_defined = ET.SubElement(sym, "data_defined_properties")
    ET.SubElement(data_defined, "Option", attrib={"type": "Map"})

    if g in ("POLYGON", "MULTIPOLYGON", "GEOMETRY", "GEOMETRYCOLLECTION"):
        layer_el = ET.SubElement(sym, "layer", attrib={
            "class": "SimpleFill", "enabled": "1", "pass": "0",
        })
        opt = ET.SubElement(layer_el, "Option", attrib={"type": "Map"})
        for k, v in [
            ("border_width_map_unit_scale", "3x:0,0,0,0,0,0"),
            ("color", "114,155,111,255"),
            ("joinstyle", "miter"),
            ("offset", "0,0"),
            ("offset_map_unit_scale", "3x:0,0,0,0,0,0"),
            ("offset_unit", "MM"),
            ("outline_color", "35,35,35,255"),
            ("outline_style", "solid"),
            ("outline_width", "0.26"),
            ("outline_width_unit", "MM"),
            ("style", "solid"),
        ]:
            ET.SubElement(opt, "Option", attrib={"name": k, "value": v, "type": "QString"})
        ET.SubElement(layer_el, "data_defined_properties").append(
            ET.Element("Option", attrib={"type": "Map"})
        )
        ET.SubElement(layer_el, "effect")
        ET.SubElement(layer_el, "orderByClause")
    elif g in ("LINESTRING", "MULTILINESTRING"):
        layer_el = ET.SubElement(sym, "layer", attrib={
            "class": "SimpleLine", "enabled": "1", "pass": "0",
        })
        opt = ET.SubElement(layer_el, "Option", attrib={"type": "Map"})
        for k, v in [
            ("capstyle", "square"),
            ("color", "35,35,35,255"),
            ("customdash", "5;2"),
            ("joinstyle", "bevel"),
            ("offset", "0"),
            ("offset_map_unit_scale", "3x:0,0,0,0,0,0"),
            ("offset_unit", "MM"),
            ("penstyle", "solid"),
            ("width", "0.26"),
            ("width_map_unit_scale", "3x:0,0,0,0,0,0"),
            ("width_unit", "MM"),
        ]:
            ET.SubElement(opt, "Option", attrib={"name": k, "value": v, "type": "QString"})
        ET.SubElement(layer_el, "data_defined_properties").append(
            ET.Element("Option", attrib={"type": "Map"})
        )
    else:
        # Point / marker
        layer_el = ET.SubElement(sym, "layer", attrib={
            "class": "SimpleMarker", "enabled": "1", "pass": "0",
        })
        opt = ET.SubElement(layer_el, "Option", attrib={"type": "Map"})
        for k, v in [
            ("color", "114,155,111,255"),
            ("name", "circle"),
            ("outline_color", "35,35,35,255"),
            ("outline_style", "solid"),
            ("outline_width", "0"),
            ("size", "2"),
            ("size_map_unit_scale", "3x:0,0,0,0,0,0"),
            ("size_unit", "MM"),
        ]:
            ET.SubElement(opt, "Option", attrib={"name": k, "value": v, "type": "QString"})
        ET.SubElement(layer_el, "data_defined_properties").append(
            ET.Element("Option", attrib={"type": "Map"})
        )
    return sym


def _build_renderer(geom_type: str) -> ET.Element:
    """Build a <renderer-v2> with a proper default symbol."""
    rv = ET.Element("renderer-v2", attrib={
        "type": "singleSymbol",
        "symbollevels": "0",
        "enableorderby": "0",
        "forceraster": "0",
    })
    symbols = ET.SubElement(rv, "symbols")
    symbols.append(_build_default_symbol(geom_type))
    rotation = ET.SubElement(rv, "rotation")
    sizescale = ET.SubElement(rv, "sizescale")
    # Suppress unused-variable warnings
    _ = rotation, sizescale
    return rv


# ---------------------------------------------------------------------------
# Project XML assembly
# ---------------------------------------------------------------------------


def _is_visible_by_default(layer_name: str) -> bool:
    if layer_name == "territories":
        return True
    if layer_name.startswith("step_"):
        return False
    return True


def _build_maplayer(layer: Layer) -> ET.Element:
    """Build the <maplayer> element for a single layer."""
    ml = ET.Element("maplayer", attrib={
        "type": "vector",
        "geometry": layer.geometry_type.title(),
        "wkbType": str(_layer_geometry_type_int(layer.geometry_type)),
        "hasScaleBasedVisibilityFlag": "0",
    })
    ET.SubElement(ml, "id").text = layer.layer_id
    ET.SubElement(ml, "datasource").text = (
        f'{layer.source_path}|layername={layer.name}'
    )
    ET.SubElement(ml, "layername").text = layer.name
    ET.SubElement(ml, "shortname").text = layer.name

    # CRS block — full spatialrefsys required for CRS transformation
    srs = ET.SubElement(ml, "srs")
    srs.append(_build_spatialrefsys(layer.srs_id))

    # Extent in source CRS
    mnx, mny, mxx, mxy = layer.bbox
    extent_el = ET.SubElement(ml, "extent")
    ET.SubElement(extent_el, "xmin").text = f"{mnx:.6f}"
    ET.SubElement(extent_el, "ymin").text = f"{mny:.6f}"
    ET.SubElement(extent_el, "xmax").text = f"{mxx:.6f}"
    ET.SubElement(extent_el, "ymax").text = f"{mxy:.6f}"

    # WGS84 extent — QGIS Server uses this for GetCapabilities bounding boxes
    wgs84 = _bbox_to_wgs84(mnx, mny, mxx, mxy, layer.srs_id)
    if wgs84 is not None:
        w, s, e, n = wgs84
        wgs_el = ET.SubElement(ml, "wgs84extent")
        ET.SubElement(wgs_el, "xmin").text = f"{w:.6f}"
        ET.SubElement(wgs_el, "ymin").text = f"{s:.6f}"
        ET.SubElement(wgs_el, "xmax").text = f"{e:.6f}"
        ET.SubElement(wgs_el, "ymax").text = f"{n:.6f}"

    # Provider
    ET.SubElement(ml, "provider").text = "ogr"

    # Renderer with default symbol
    ml.append(_build_renderer(layer.geometry_type))

    # Flags — make layer identifiable/queryable
    flags = ET.SubElement(ml, "flags")
    ET.SubElement(flags, "Identifiable").text = "1"
    ET.SubElement(flags, "Removable").text = "1"
    ET.SubElement(flags, "Searchable").text = "1"
    ET.SubElement(flags, "Private").text = "0"

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


def render_qgs(
    layers: list[Layer],
    project_crs_authid: str = "EPSG:3857",
    *,
    project_name: str = "qgis",
    project_title: str | None = None,
) -> str:
    """Render the .qgs XML for the given layers.

    ``project_name`` is the WMS root layer Name (and the qgs ``projectname``
    attribute). ``project_title`` is the WMS service Title; defaults to
    ``project_name`` if not set. Without these, QGIS Server reports the root
    layer as nameless / "Untitled", which leaves QWC2 with an empty theme
    name and confuses the layer tree.
    """
    if project_title is None:
        project_title = project_name

    root = ET.Element("qgis", attrib={
        "version": "3.34", "projectname": project_name,
    })

    # Project CRS — full spatialrefsys block
    proj_epsg = int(project_crs_authid.split(":")[1])
    proj_crs = ET.SubElement(root, "projectCrs")
    proj_crs.append(_build_spatialrefsys(proj_epsg))

    # Layer tree (visibility ordering)
    root.append(_build_layer_tree(layers))

    # Map layers (the actual definitions)
    pl = ET.SubElement(root, "projectlayers")
    for layer in layers:
        pl.append(_build_maplayer(layer))

    # Project + WMS service properties.
    props = ET.SubElement(root, "properties")

    wms_root_name = ET.SubElement(props, "WMSRootName")
    wms_root_name.set("type", "QString")
    wms_root_name.text = project_name

    wms_service_title = ET.SubElement(props, "WMSServiceTitle")
    wms_service_title.set("type", "QString")
    wms_service_title.text = project_title

    # Empty restricted-layers list = publish everything via WMS.
    wms_layers = ET.SubElement(props, "WMSRestrictedLayers")
    wms_layers.set("type", "QStringList")

    # WFS publication — list every layer's id so QGIS Server advertises
    # FeatureTypes in WFS GetCapabilities. Without this, WFS clients see the
    # connection but no layers.
    wfs_layers = ET.SubElement(props, "WFSLayers")
    wfs_layers.set("type", "QStringList")
    for layer in layers:
        ET.SubElement(wfs_layers, "value").text = layer.layer_id

    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(
        root, encoding="unicode"
    )


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


# ---------------------------------------------------------------------------
# QWC2 themesConfig.json generation
# ---------------------------------------------------------------------------


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
    # "territories" is a case-sensitive literal — matches the on-disk
    # directory under /srv/qgis/data/. Renaming that folder would
    # change project ids and is a deliberate breaking change.
    if parent == data_dir or parent == data_dir / "territories":
        return gpkg.stem
    return f"{_slug(parent.name)}__{_slug(gpkg.stem)}"


def _smart_title(text: str) -> str:
    """Title-case a string word-by-word, leaving acronyms and pre-cased names alone.

    Splits on whitespace, then for each word: applies ``str.title()`` if the
    word is all-lowercase, otherwise returns it unchanged. Preserves
    "NCDOT", "NC", and "Mecklenburg" while normalizing "addresses_residential"
    → "Addresses Residential".

    Pre-replace underscores and hyphens with spaces if you want them split.
    """
    return " ".join(w.title() if w.islower() else w for w in text.split())


def _project_title(gpkg: Path, data_dir: Path) -> str:
    """Return the human-facing title for *gpkg*.

    Used as the QGIS WMS service title, the QWC2 theme title, and the
    QGIS Desktop connection display name.

    For top-level / ``territories/`` files, smart-title the stem (current
    behavior, but acronym-safe). For nested files, build
    ``"<folder> – <stem-titled>"``, but drop the stem when its slug is a
    case-insensitive substring of the folder slug (handles
    "Mecklenburg Greenways/Greenways.gpkg" → "Mecklenburg Greenways").

    Both folder and stem are smart-titled (see :func:`_smart_title`):
    all-lowercase words get title-cased; pre-cased words like "Mecklenburg"
    and acronyms like "NCDOT" are preserved.
    """
    parent = gpkg.parent
    pretty = lambda s: _smart_title(s.replace("_", " ").replace("-", " "))
    if parent == data_dir or parent == data_dir / "territories":
        return pretty(gpkg.stem)

    folder = parent.name
    stem_pretty = gpkg.stem.replace("_", " ").replace("-", " ")
    if _slug(stem_pretty).lower() in _slug(folder).lower():
        return folder
    return f"{pretty(folder)} – {_smart_title(stem_pretty)}"


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
            # Just the basename: themesConfig.py looks under
            # <qwc2_path>/static/assets/img/mapthumbs/<thumbnail>. A path with
            # subdirs would double-prefix and miss the file, falling through
            # to a slow WMS GetMap fallback we don't want.
            "thumbnail": "default.jpg",
        })

    # themesConfig.py expects:
    #   - defaultScales at top level (required)
    #   - backgroundLayers / pluginData / themeInfoLinks / externalLayers
    #     nested under "themes", not at top level.
    config = {
        "defaultScales": [
            4000000, 2000000, 1000000, 500000, 250000, 100000,
            50000, 25000, 10000, 5000, 2500, 1000, 500, 250, 100,
        ],
        "defaultMapCrs": "EPSG:3857",
        "defaultBackgroundLayers": ["osm"],
        "defaultSearchProviders": [],
        "defaultTheme": default_theme,
        "themes": {
            "title": "Themes",
            "items": items,
            "backgroundLayers": [
                {
                    "name": "osm",
                    "title": "OpenStreetMap",
                    "type": "osm",
                    "source": "osm",
                    "thumbnail": "img/mapthumbs/mapnik.png",
                }
            ],
            "pluginData": {},
        },
    }

    atomic_write_text(out, _json.dumps(config, indent=2) + "\n")


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
    ET.indent(wms_root, space="  ")
    ET.indent(wfs_root, space="  ")
    atomic_write_text(out_wms, prelude + ET.tostring(wms_root, encoding="unicode") + "\n")
    atomic_write_text(out_wfs, prelude + ET.tostring(wfs_root, encoding="unicode") + "\n")


def bake_themes_json(
    themes_config: Path,
    out: Path,
    *,
    scripts_dir: Path,
    web_dir: Path,
    internal_base_url: str,
) -> int:
    """Bake themes.json from themesConfig.json by invoking QWC2's themesConfig.py.

    The bake step runs in-cluster against the qgis-server Service directly
    (not through the ingress), so it doesn't depend on qgis.devbox resolving
    inside the cluster. Theme item URLs are temporarily stripped of their
    "/ows" prefix for the capabilities fetch (qgis-server only handles "/")
    and restored to the original "/ows/?MAP=..." form in the output, so the
    runtime URLs the browser uses are unchanged.
    """
    import importlib.util as _iu
    import shutil as _shutil
    import tempfile as _tempfile

    src = _json.loads(themes_config.read_text())

    # Temp config: strip "/ows" so urljoin(internal_base_url, url) hits qgis-server
    # at "/?MAP=..." (its actual path), then restore "/ows/..." in the output.
    bake_cfg = _json.loads(_json.dumps(src))  # deep copy via json
    url_map: dict[str, str] = {}
    for item in bake_cfg["themes"].get("items", []):
        orig = item.get("url", "")
        if orig.startswith("/ows/?"):
            rewritten = orig.replace("/ows/?", "/?", 1)
            item["url"] = rewritten
            url_map[rewritten] = orig

    workdir = Path(_tempfile.mkdtemp(prefix="qwc2-bake-"))
    try:
        # themesConfig.py looks for <qwc2_path>/static/assets/img/mapthumbs/<file>.
        # web_dir has the layout <web_dir>/assets/img/mapthumbs/, so make a
        # `static` symlink that points at it.
        (workdir / "static").symlink_to(web_dir)
        tmp_cfg = workdir / "themesConfig.json"
        tmp_cfg.write_text(_json.dumps(bake_cfg))

        spec = _iu.spec_from_file_location(
            "themesConfig", str(scripts_dir / "themesConfig.py")
        )
        mod = _iu.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)
        mod.baseUrl = internal_base_url
        mod.qwc2_path = str(workdir)

        themes = mod.genThemes(str(tmp_cfg))

        for item in themes["themes"].get("items", []):
            if item.get("url") in url_map:
                item["url"] = url_map[item["url"]]

        atomic_write_text(
            out,
            _json.dumps(themes, indent=2, sort_keys=True) + "\n",
        )
        return len(themes["themes"].get("items", []))
    finally:
        _shutil.rmtree(workdir, ignore_errors=True)


@dataclass
class RegenReport:
    written_projects: int
    pruned_projects: int
    skipped: bool = False  # True if .no-regen marker present


def _prune_orphans(projects_dir: Path, current_ids: set[str]) -> int:
    """Delete any *.qgs in projects_dir whose stem is not in current_ids.

    The set should contain project ids (the values produced by
    :func:`_project_id`), which for nested gpkgs include a folder prefix
    like ``Folder__stem``. Bare gpkg stems are NOT what _project_id produces
    for clipped_data files, so passing a stem set here would over-prune.
    """
    pruned = 0
    for qgs in projects_dir.glob("*.qgs"):
        if qgs.stem not in current_ids:
            qgs.unlink()
            pruned += 1
    return pruned


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
    """Idempotently rebuild all per-gpkg .qgs files and themesConfig.json.

    Honors a /srv/qgis/.no-regen marker (computed as data_dir.parent / ".no-regen"):
    if present, returns a RegenReport with skipped=True and writes nothing.

    If both ``bake_scripts_dir`` and ``bake_internal_base_url`` are given, the
    QWC2 themes.json is also baked from themesConfig.json after regeneration
    (requires ``themesConfig.py`` next to ``generate_qgs.py`` and a reachable
    qgis-server at the given URL). When omitted, the bake is skipped — useful
    in tests and one-off CLI runs.
    """
    no_regen = data_dir.parent / ".no-regen"
    if no_regen.exists():
        return RegenReport(written_projects=0, pruned_projects=0, skipped=True)

    projects_dir.mkdir(parents=True, exist_ok=True)
    web_dir.mkdir(parents=True, exist_ok=True)

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

    themes_config_path = web_dir / "themesConfig.json"
    write_themes_config(
        gpkgs=gpkgs,
        projects_dir=projects_dir,
        out=themes_config_path,
        default_theme=default_theme,
        data_dir=data_dir,
    )

    if ingress_host:
        write_connections(
            gpkgs=gpkgs,
            projects_dir=projects_dir,
            data_dir=data_dir,
            out_wms=web_dir / "qgis-wms-connections.xml",
            out_wfs=web_dir / "qgis-wfs-connections.xml",
            ingress_host=ingress_host,
        )

    if bake_scripts_dir and bake_internal_base_url:
        try:
            n = bake_themes_json(
                themes_config_path,
                web_dir / "themes.json",
                scripts_dir=bake_scripts_dir,
                web_dir=web_dir,
                internal_base_url=bake_internal_base_url,
            )
            print(f"bake: wrote themes.json with {n} theme(s)")
        except Exception as exc:
            print(f"bake failed: {exc}", file=sys.stderr)

    return RegenReport(written_projects=written, pruned_projects=pruned)


# ---------------------------------------------------------------------------
# Discovery + CLI
# ---------------------------------------------------------------------------


def discover_gpkgs(root: Path) -> list[Path]:
    return sorted(root.rglob("*.gpkg"))


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
                   help="Theme id (the value produced by _project_id) marked "
                        "as default in themesConfig. For top-level / "
                        "territories/ gpkgs this equals the bare stem; for "
                        "clipped_data/<Folder>/x.gpkg it is "
                        "'<Folder>__<x>'.")
    p.add_argument("--debounce-seconds", type=float, default=1.0,
                   help="Watch-mode debounce window (default 1.0).")
    p.add_argument("--bake-scripts-dir", default=None,
                   help="Directory containing themesConfig.py. Required to bake "
                        "themes.json; if omitted the bake step is skipped.")
    p.add_argument("--bake-internal-base-url", default=None,
                   help="Base URL for in-cluster qgis-server (e.g. "
                        "http://qgis-server). Used to fetch GetProjectSettings "
                        "during the bake; not exposed to clients.")
    p.add_argument("--ingress-host", default=None,
                   help="Public hostname for the QGIS WMS/WFS connection "
                        "bundles (e.g. 'qgis.devbox'). Required to write "
                        "qgis-wms-connections.xml and qgis-wfs-connections.xml; "
                        "if omitted those files are not written.")
    args = p.parse_args(argv)

    data_dir = Path(args.data_dir)
    projects_dir = Path(args.projects_dir)
    web_dir = Path(args.web_dir)
    bake_scripts_dir = Path(args.bake_scripts_dir) if args.bake_scripts_dir else None

    if not data_dir.is_dir():
        print(f"data_dir not found or not a directory: {data_dir}",
              file=sys.stderr)
        return 2

    report = regen_all(
        data_dir, projects_dir, web_dir, args.default_theme,
        bake_scripts_dir=bake_scripts_dir,
        bake_internal_base_url=args.bake_internal_base_url,
        ingress_host=args.ingress_host,
    )
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
                      args.default_theme, args.debounce_seconds,
                      bake_scripts_dir=bake_scripts_dir,
                      bake_internal_base_url=args.bake_internal_base_url,
                      ingress_host=args.ingress_host)


def _watch_loop(data_dir: Path, projects_dir: Path, web_dir: Path,
                default_theme: str | None, debounce_seconds: float,
                *,
                bake_scripts_dir: Path | None = None,
                bake_internal_base_url: str | None = None,
                ingress_host: str | None = None) -> int:
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
                report = regen_all(
                    data_dir, projects_dir, web_dir, default_theme,
                    bake_scripts_dir=bake_scripts_dir,
                    bake_internal_base_url=bake_internal_base_url,
                    ingress_host=ingress_host,
                )
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


if __name__ == "__main__":
    raise SystemExit(main())
