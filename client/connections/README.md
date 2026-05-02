# QGIS Desktop connection bundles

Pre-built `qgsWMSConnections` and `qgsWFSConnections` import files for the
`debug` and `territories_draft` projects, pointing at the in-cluster
QGIS Server via the `qgis.devbox` Traefik ingress.

## Files

- `qgis-wms-connections.xml` — WMS connections for both projects
- `qgis-wfs-connections.xml` — WFS connections for both projects

## Prerequisites on the client device

1. **Network reachability**: be on the same LAN as the host, or connected via
   Tailscale to the same tailnet.
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
curl -I 'http://qgis.devbox/ows/?MAP=/srv/qgis/projects/debug.qgs&SERVICE=WMS&REQUEST=GetCapabilities'
```

should return `HTTP/1.1 200 OK`.

## Import in QGIS Desktop

In the **Browser panel** on the left:

1. Right-click **WMS/WMTS** → *Load Connections…* → pick
   `qgis-wms-connections.xml` → check both → *OK*.
2. Right-click **WFS / OGC API - Features** → *Load Connections…* → pick
   `qgis-wfs-connections.xml` → check both → *OK*.

Each project then appears as an expandable connection in the Browser; drag
layers onto the canvas.

## Picking up data updates

The server reads each GeoPackage on every request, so **row-level changes
appear immediately** on the next pan/zoom or attribute query. No client
action required.

After a **schema change** (a new layer, a renamed column), the
server-side `.qgs` is regenerated automatically by the watcher — but
QGIS Desktop caches the connection's layer list locally. To refresh:

- In the Browser panel, right-click the connection → *Refresh*, or
- Collapse and re-expand the connection.

## Why `ignoreGetMapURI=1` is set on WMS

QGIS Server reports its public URL via `QGIS_SERVER_SERVICE_URL`, which is
set chart-side to `http://qgis.devbox/ows` (no `MAP=`). Without
`ignoreGetMapURI=1`, QGIS Desktop would use that bare URL for follow-up
GetMap / GetLegendGraphic / GetFeatureInfo calls and get HTTP 500 because
the `MAP=` parameter is required. Forcing the connection URL keeps the
`MAP=` param on every call.
