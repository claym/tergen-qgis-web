# qgis

QGIS Server on k3s, serving territory GeoPackage data with a QWC2 web viewer
and importable connection bundles for QGIS Desktop clients.

- **Spec:** `docs/superpowers/specs/2026-04-24-qgis-server-design.md`
- **Plan:** `docs/superpowers/plans/2026-04-25-qgis-server.md`

## Layout

- `data/territories/` — source GeoPackage files (gpkg). Authoritative.
- `dnsmasq/` — host-level wildcard DNS for `*.devbox` (shared infra, not part of the chart)
- `chart/` — helm chart for the qgis app (qgis-server + viewer + project generator)
- `client/connections/` — docs for the QGIS Desktop connection import flow (the bundles themselves are served live by the cluster)
- `docs/` — design and plan documents

## Quick start

```bash
sudo ./dnsmasq/install.sh                             # one-time, host-level
helm install qgis ./chart -n qgis --create-namespace  # deploy the app
```

Then open `http://qgis.devbox/` from any client whose DNS points at this host.

## Underlying data

Both projects are generated from GeoPackages under
`/srv/qgis/data/territories/`:

| Project (`.qgs`) | Source GeoPackage | Layers |
|---|---|---|
| `territories_draft.qgs` | `territories_draft.gpkg` | 1 (`territories`) |
| `debug.qgs` | `debug.gpkg` | 29 (`step_200_*` … `step_830_*`) |

The `.qgs` project files in `/srv/qgis/projects/` are *generated* — never
hand-edit them; they will be overwritten on the next watcher event.

## Updating data

A `project-watcher` Deployment in the `qgis` namespace tails
`/srv/qgis/data/` for `*.gpkg` filesystem events (inotify, 1s debounce) and
re-runs `regen_all` from `chart/files/generate_qgs.py`. That, atomically:

1. Rewrites `/srv/qgis/projects/*.qgs` (the QGIS Server projects).
2. Rewrites `/srv/qgis/web/themesConfig.json` (the QWC2 *source* config).
3. Bakes `/srv/qgis/web/themes.json` from that config by running
   QWC2's `themesConfig.py` (vendored at `chart/files/`) against the
   in-cluster qgis-server Service. This is the file the QWC2 frontend
   actually reads at runtime — without this step the browser would keep
   showing the layer tree from whenever the client was last built.

Behavior, in practice:

- **Row-level data changes** (insert / update / delete inside an existing
  table): show up immediately on the next request from any client. QGIS
  Server reads the gpkg through OGR per-request — there's no feature cache.
  Watcher regen is harmless but unnecessary.
- **Schema changes** (new tables, dropped tables, renamed/typed columns):
  watcher regenerates the affected `.qgs`. QGIS Server's project cache
  invalidates on `.qgs` mtime, so the next request picks it up — no pod
  restart needed.
- **New gpkg dropped in**: a new `.qgs` and a new theme appear automatically.
- **Gpkg deleted**: orphaned `.qgs` is pruned by the watcher.
- **Existing QGIS Desktop clients**: have their own connection cache; users
  must right-click the connection in the Browser panel → *Refresh* to see a
  changed layer list. Live data within an existing layer just appears on
  the next render.

Verify watcher activity:

```bash
kubectl -n qgis logs deploy/project-watcher --tail=20
```

Caveat: if a writer uses SQLite WAL mode, edits land in a sibling
`-wal` file and the main `.gpkg` may not be touched until checkpoint —
the watcher matches `*.gpkg` only. Default rollback-journal mode is fine.

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

## Service URL

QGIS Server advertises its public URL via `QGIS_SERVER_SERVICE_URL` (set in
the chart, defaulting to `http://<ingress.host>/ows`). The Traefik ingress
strips the `/ows` prefix before proxying, so without this override
GetCapabilities reports a URL that follow-up requests would 404 on.

Override in `values.yaml` if you need a different external URL:

```yaml
serviceUrl: "https://gis.example.com/ows"
```
