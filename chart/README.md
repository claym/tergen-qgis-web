# qgis helm chart

QGIS Server + QWC2 web client for the GeoPackage files at `/srv/qgis/data/`.
A `project-watcher` Deployment continuously regenerates per-gpkg `.qgs`
files and `themesConfig.json` whenever data changes.

## Prerequisites

- k3s with Traefik ingress (already on this host).
- Host-level dnsmasq configured per `/srv/gis/tergen-qgis-web/dnsmasq/` so
  `*.devbox` resolves to `192.168.1.70`.
- helm 3.x.
- Docker on the host (for `make install-client`).
- The qwc2 dist installed on the host: `make install-client` from the repo
  root. Reads from `client/qwc2-demo-app/` (vendored upstream) plus
  `client/overlay/` (our customizations) and writes to `/srv/qgis/web/`.

## Install

```bash
make install-client                                   # one-time, builds qwc2 dist
helm install qgis ./chart -n qgis --create-namespace  # deploys the workloads
```

The `project-watcher` Deployment runs `generate_qgs.py --watch` and
populates `/srv/qgis/projects/*.qgs` plus `/srv/qgis/web/themesConfig.json`
on startup. The qgis-server pod's init container blocks until those exist.

## Iteration

Drop a new gpkg into `/srv/qgis/data/<dataset>/`; the watcher detects the
write within ~1 second, regenerates the matching `.qgs` and rewrites
`themesConfig.json`. Hard-refresh the browser.

## Pause auto-regen (e.g., to hand-edit a project file in QGIS Desktop)

```bash
touch /srv/qgis/.no-regen        # watcher logs "skipped" on every event
# … edit /srv/qgis/projects/territories_draft.qgs in QGIS Desktop, save …
rm /srv/qgis/.no-regen           # auto-regen resumes
```

## Update qwc2 from upstream

```bash
git subtree pull --prefix=client/qwc2-demo-app \
    https://github.com/qgis/qwc2-demo-app master --squash
make install-client
```

## Uninstall

```bash
helm uninstall qgis -n qgis
```

The PVC and the host-level data directory `/srv/qgis/` are not touched.
