# qgis helm chart

QGIS Server + thin Leaflet viewer for the GeoPackage files at `/srv/qgis/data/`.

## Prerequisites

- k3s with Traefik ingress (already on this host).
- Host-level dnsmasq configured per `/srv/qgis/dnsmasq/` so `*.devbox` resolves
  to `192.168.1.70`.
- helm 3.x.

## Install

```bash
helm install qgis ./chart -n qgis --create-namespace
```

The post-install hook generates `/srv/qgis/project.qgs` from any `*.gpkg` files
discovered under `/srv/qgis/data/` and starts QGIS Server on top of it.

## Upgrade (regenerates project.qgs)

```bash
helm upgrade qgis ./chart -n qgis
```

To keep a hand-edited `project.qgs` across upgrades:

```bash
helm upgrade qgis ./chart -n qgis --set generator.enabled=false
```

## Uninstall

```bash
helm uninstall qgis -n qgis
```

The PVC and the host-level data directory `/srv/qgis/data/` are not touched.

## Configuration

See `values.yaml` for all knobs. Common overrides:

| Key | Default | Purpose |
|---|---|---|
| `ingress.host` | `qgis.devbox` | Hostname Traefik routes |
| `image.qgisServer` | `camptocamp/qgis-server:3.34` | QGIS Server image |
| `hostPath` | `/srv/qgis` | Node path mounted into pods |
| `generator.enabled` | `true` | Re-run project.qgs generator on upgrades |

## Architecture

See `docs/superpowers/specs/2026-04-24-qgis-server-design.md` for the full design.
