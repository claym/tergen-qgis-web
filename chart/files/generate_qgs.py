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


def render_qgs(layers: list[Layer], project_crs_authid: str = "EPSG:3857") -> str:
    """Render the .qgs XML for the given layers."""
    root = ET.Element("qgis", attrib={
        "version": "3.34", "projectname": "qgis",
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

    # WMS service settings — make every layer queryable
    props = ET.SubElement(root, "properties")
    wms_layers = ET.SubElement(props, "WMSRestrictedLayers")
    wms_layers.set("type", "QStringList")

    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(
        root, encoding="unicode"
    )


def write_project(gpkg: Path, out: Path,
                  project_crs_authid: str = "EPSG:3857") -> None:
    """Generate the .qgs for a single gpkg and write it atomically to *out*."""
    layers = introspect_gpkg(gpkg)
    if not layers:
        raise ValueError(f"no feature-table layers in {gpkg}")
    atomic_write_text(out, render_qgs(layers, project_crs_authid))


# ---------------------------------------------------------------------------
# QWC2 themesConfig.json generation
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Discovery + CLI
# ---------------------------------------------------------------------------


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
