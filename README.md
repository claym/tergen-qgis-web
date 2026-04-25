# qgis

QGIS Server on k3s, serving territory GeoPackage data with a thin Leaflet viewer.

- **Spec:** `docs/superpowers/specs/2026-04-24-qgis-server-design.md`
- **Plan:** `docs/superpowers/plans/2026-04-25-qgis-server.md`

## Layout

- `data/territories/` — source GeoPackage files (gpkg)
- `dnsmasq/` — host-level wildcard DNS for `*.devbox` (shared infra, not part of the chart)
- `chart/` — helm chart for the qgis app (qgis-server + viewer + project generator)
- `docs/` — design and plan documents

## Quick start

```bash
sudo ./dnsmasq/install.sh                             # one-time, host-level
helm install qgis ./chart -n qgis --create-namespace  # deploy the app
```

Then open `http://qgis.devbox/` from any client whose DNS points at this host.
