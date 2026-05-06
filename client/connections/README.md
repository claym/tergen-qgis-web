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
