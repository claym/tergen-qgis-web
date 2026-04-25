# QGIS Server on k3s — Design Spec

**Date:** 2026-04-24
**Status:** Draft, pending user review
**Author:** Brainstorming session, clay + Claude

## 1. Goal

Stand up a QGIS Server on the existing `/srv/qgis` host, rendering the two GeoPackage files in `data/territories/`, and expose a thin web viewer for a small group of trusted users on the LAN/tailnet.

## 2. Inputs

Files already on disk under `/srv/qgis/data/territories/`:

| File | Size | Contents |
|---|---|---|
| `territories_draft.gpkg` | 660 KB | One layer: `territories` (174 polygons, EPSG:2264 — NC State Plane feet). Columns: `fid, geom, terr_id, addr_count, complex, care_facility, type, area_sqft, muni_name, subdiv, subdiv_names, label`. |
| `debug.gpkg` | 28 MB | 23 intermediate pipeline layers named `step_200_*` through `step_820_*` (road classifications, subdivisions, blocks, addresses, parcels, merged/healed territories). All EPSG:2264. |
| `territory_details.csv` | 12 KB | Redundant with the `territories` table (same columns except for one unused field). Not used by the viewer. |

## 3. Constraints & context

- **Host:** Ubuntu 24.04, k3s single-node (`devbox`, 192.168.1.70). Also on Tailscale (`100.117.43.89`).
- **Cluster state:** Traefik is the default ingress (already listening on :80/:443 via `LoadBalancer`, external IP 192.168.1.70). `local-path-provisioner` is available. Helm is installed. No Tailscale operator, no cert-manager, no user workloads.
- **Audience:** me + a few known people — LAN/tailnet only. No public exposure.
- **Viewer scope:** display + attribute popups. Read-only. No editing, no filter/search, no print layouts, no attribute table.
- **Layers in viewer:** all 24 layers exposed and toggleable; only `territories` visible by default.
- **Tooling posture:** use helm where there's a sensible use for it.

## 4. Architecture

```
 *.devbox DNS query
    │
    ▼
 dnsmasq on host (192.168.1.70:53)  ──►  resolves *.devbox → 192.168.1.70
    │
    ▼
 browser → http://qgis.devbox/
    │
    ▼
 Traefik Ingress (192.168.1.70:80, Host=qgis.devbox)
    │
    ├─►  /            → Service: viewer       → Pod: nginx (serves index.html from ConfigMap)
    └─►  /ows         → Service: qgis-server  → Pod: QGIS Server (FCGI)
                                                     │
                                                     ▼
                                                PVC (local-path, hostPath → /srv/qgis)
                                                ├── data/territories/*.gpkg      (RO)
                                                └── project.qgs                  (generated)
```

Every future service on this box follows the same pattern: pick a subdomain of `.devbox`, write an Ingress with `host: <name>.devbox`, done. dnsmasq's wildcard record handles resolution for all of them without any further DNS work.

**Namespace:** `qgis`

**Workloads inside the namespace:**

| Kind | Name | Purpose |
|---|---|---|
| Deployment | `qgis-server` | FCGI QGIS Server reading `project.qgs`, serving WMS/WFS/WMTS on `/ows` |
| Deployment | `viewer` | nginx-alpine serving Leaflet static page from a ConfigMap |
| Service (ClusterIP) | `qgis-server`, `viewer` | internal routing targets for the Ingress |
| Ingress | `qgis` | Traefik-routed, path-prefix `/` → viewer, `/ows` → qgis-server |
| PersistentVolumeClaim | `qgis-data` | `local-path` storage class, hostPath-backed to `/srv/qgis` |
| ConfigMap | `viewer-html` | `index.html` + Leaflet init JS (bundled inline, no build step) |
| ConfigMap | `project-generator` | Python script + XML template that writes `project.qgs` |
| Job | `project-generator` | One-shot, runs on install and on every `helm upgrade` (post-install/post-upgrade hooks) |

## 5. Components

### 5.1 QGIS Server pod
- Image: `camptocamp/qgis-server:3.34` (QGIS LTS, FCGI-based)
- Exposes port 80 inside the pod
- Mounts the PVC at `/srv/qgis` **read-only** (server only reads `project.qgs` and the `.gpkg` files; writes happen exclusively from the generator Job)
- Env:
  - `QGIS_SERVER_LOG_LEVEL=0`
  - `QGIS_PROJECT_FILE=/srv/qgis/project.qgs`
- Resources: 256Mi request / 1Gi limit, 100m / 500m CPU (plenty for 174 polygons + occasional pipeline-layer renders on a small tailnet)
- Liveness/readiness: HTTP GET `/ows/?SERVICE=WMS&REQUEST=GetCapabilities` returns 200

### 5.2 Viewer pod
- Image: `nginx:1.27-alpine`
- `/usr/share/nginx/html/index.html` mounted from ConfigMap `viewer-html`
- Vanilla Leaflet via CDN — no build step, no SPA framework
- What's in `index.html`:
  - Leaflet map initialized on the NC state plane area (auto-fit to the WMS capabilities bbox on load)
  - OpenStreetMap base tile layer (`https://tile.openstreetmap.org/{z}/{x}/{y}.png`, standard attribution)
  - One WMS overlay pointed at `/ows` (same origin — Traefik routes `/ows` to QGIS Server)
  - Sidebar with 24 layer checkboxes: `territories` visible by default under a "Main" group; all `step_*` layers unchecked under a collapsed "Debug" group
  - Click handler → WMS `GetFeatureInfo` → HTML popup showing the feature's attribute table
- Resources: 32Mi / 128Mi, 10m / 100m CPU

### 5.3 Project generator Job
- Runs as a helm hook: `helm.sh/hook: post-install,post-upgrade` with `hook-delete-policy: before-hook-creation,hook-succeeded`
- Image: `python:3.12-slim`
- Python script (in `ConfigMap: project-generator`, mounted as a file) does:
  1. Opens each `.gpkg` via `sqlite3` (GeoPackage is a SQLite file — no PyQGIS or GDAL needed; pure stdlib means no pip install in the Job)
  2. Reads `gpkg_contents`, `gpkg_geometry_columns`, and the layer feature tables to extract: layer name, geometry type, CRS (EPSG:2264), bounding box, column definitions
  3. Writes `project.qgs` XML using `xml.etree.ElementTree` (stdlib — no Jinja2 dependency). The generator mounts the PVC **read-write**:
     - Project CRS: EPSG:3857
     - Each layer declared with its `2264` source CRS (QGIS Server reprojects on the fly)
     - All layers `queryable=true` (enables `GetFeatureInfo`)
     - Visibility: `territories` visible; all others hidden
     - Symbology: simple single-fill polygons with 40% opacity and a generated-per-layer hue; `territories` gets categorized styling by `muni_name`
  4. Writes `project.qgs` into the PVC at `/srv/qgis/project.qgs`
- Mount: PVC at `/srv/qgis` **read-write** (only place in the system that writes there)
- Idempotent: overwrites on each run. If a user has hand-edited `project.qgs` in QGIS Desktop and wants to keep it, they skip running `helm upgrade` or set `values.generator.enabled=false`.

### 5.4 Ingress
- Traefik, **host-based routing** on `qgis.devbox`
- Rules (both matching `host: qgis.devbox`):
  - `PathPrefix(/ows)` → Service `qgis-server:80`
  - `/` (catch-all) → Service `viewer:80`
- No TLS, no middleware (no auth, no rate limit)
- **Prerequisite:** `qgis.devbox` must resolve to `192.168.1.70` on the client's machine. This is handled once at the host level by the dnsmasq wildcard (§8.5), not per-service.

## 6. Helm chart layout

```
chart/
├── Chart.yaml                   # apiVersion: v2, version: 0.1.0, appVersion: "3.34"
├── values.yaml                  # image tags, resource limits, PVC path, hostname
├── README.md
└── templates/
    ├── _helpers.tpl
    ├── namespace.yaml           # conditional on values.createNamespace
    ├── pvc.yaml
    ├── configmap-viewer.yaml    # index.html (plain text block)
    ├── configmap-generator.yaml # Python script + Jinja template
    ├── job-project-generator.yaml
    ├── deployment-qgis-server.yaml
    ├── deployment-viewer.yaml
    ├── service-qgis-server.yaml
    ├── service-viewer.yaml
    └── ingress.yaml
```

**`values.yaml` top-level keys (expected):**
```yaml
namespace: qgis
createNamespace: true
hostPath: /srv/qgis
image:
  qgisServer: camptocamp/qgis-server:3.34
  viewer: nginx:1.27-alpine
  generator: python:3.12-slim
resources: { ... }
ingress:
  className: traefik
  host: qgis.devbox    # *.devbox wildcard is resolved by host-level dnsmasq (see §8.5)
generator:
  enabled: true   # set false to stop regenerating project.qgs on upgrades
```

**Install:**
```bash
helm install qgis ./chart -n qgis --create-namespace
```

**Upgrade (re-generates project.qgs, rolls deployments):**
```bash
helm upgrade qgis ./chart -n qgis
```

**Uninstall:**
```bash
helm uninstall qgis -n qgis
# PVC is kept (cluster policy); data on /srv/qgis is never touched on uninstall
```

## 7. Data flow (request-level)

1. Browser → DNS query for `qgis.devbox` → host dnsmasq returns `192.168.1.70`
2. Browser → `http://qgis.devbox/` → Traefik (matches `Host: qgis.devbox`) → `viewer` Service → nginx → returns `index.html`
3. Leaflet loads, requests:
   `http://qgis.devbox/ows/?SERVICE=WMS&REQUEST=GetCapabilities`
4. Traefik routes `/ows` (same host) → `qgis-server` Service → pod → FCGI → reads `project.qgs` → returns capabilities XML
5. Leaflet fits the map to the reported bounding box and renders the default tile layer
6. User clicks a polygon:
   `GET /ows/?SERVICE=WMS&REQUEST=GetFeatureInfo&LAYERS=territories&I=..&J=..&...`
7. QGIS Server returns an HTML snippet with the feature attributes; Leaflet displays it in a popup

Same-origin throughout: no CORS needed, cookies (if any) stay scoped to `qgis.devbox`.

## 8. Network exposure & auth

- **LAN reach:** Traefik already binds to 192.168.1.70 — LAN users reach the viewer directly, provided their machine resolves `qgis.devbox` via host dnsmasq (see §8.5).
- **Tailscale reach:** users on the tailnet reach 192.168.1.70 via a Tailscale subnet route advertised from this host:
  ```bash
  sudo tailscale up --advertise-routes=192.168.1.0/24
  ```
  And accepting the route from the tailnet admin UI. Documented in the chart README as a one-time step; not automated by the chart. For `qgis.devbox` name resolution from the tailnet, configure Tailscale split DNS (Option C, future) or add `/etc/hosts` entries on each tailnet client.
- **Auth:** none. The network (LAN/tailnet) is the gate.
- **TLS:** none. Plain HTTP. Acceptable for LAN + tailnet. Add cert-manager later if browser warnings become annoying.

## 8.5 Host-level prerequisite: `*.devbox` DNS via dnsmasq

This is **shared infrastructure** for all future services on this host, not part of the qgis helm chart. Set up once; every new service gets a free `<name>.devbox` hostname.

**What it does:** resolves any `*.devbox` hostname to `192.168.1.70` for clients that use this host as their DNS resolver.

**How it runs:**
- **Package:** `dnsmasq` from Ubuntu apt (preferred over running it in k3s — DNS is infrastructure that shouldn't depend on the cluster being healthy)
- **Config file:** `/etc/dnsmasq.d/devbox.conf`:
  ```
  # Wildcard: every *.devbox name resolves to this host
  address=/.devbox/192.168.1.70

  # Bind to the LAN + Tailscale IPs, NOT 127.0.0.53 (systemd-resolved owns that)
  listen-address=192.168.1.70,100.117.43.89
  bind-interfaces

  # Upstream resolver for everything else
  server=1.1.1.1
  server=1.0.0.1
  ```
- **systemd-resolved conflict (Ubuntu 24.04):** systemd-resolved binds `127.0.0.53:53` but leaves other interfaces free. The `listen-address` + `bind-interfaces` combo above is the clean fix — dnsmasq only binds the LAN and Tailscale IPs, no conflict.
- **Firewall:** open UDP/TCP 53 on those interfaces (LAN is usually open; Tailscale too by default).

**How clients find it:**
- **LAN clients:** set their DNS resolver to `192.168.1.70`, either manually or via router DHCP. A typical home router lets you set a "custom DNS server" globally.
- **Tailscale clients:** set up split DNS in the Tailscale admin UI — add nameserver `100.117.43.89` scoped to domain `devbox`. Tailnet devices (including phones, macOS, Windows) will then resolve `*.devbox` via your dnsmasq automatically.
- **Fallback for any client:** `/etc/hosts` entry `192.168.1.70 qgis.devbox` always works.

**Verification:**
```bash
dig @192.168.1.70 qgis.devbox        # answer: 192.168.1.70
dig @192.168.1.70 anything.devbox    # answer: 192.168.1.70 (wildcard works)
dig @192.168.1.70 google.com         # answer: real google IPs (upstream works)
```

**Scope note:** implementing this dnsmasq setup is included in the implementation plan for this spec as a **prerequisite task** (must be in place before the helm chart is useful). It is decoupled from the chart itself — once live, future service specs can assume `*.devbox` already works.

## 9. Optional future add-ons (documented, not installed now)

| Add-on | How to add | When |
|---|---|---|
| **Tailscale split DNS for `*.devbox`** | Tailscale admin UI → DNS → add nameserver `100.117.43.89` restricted to domain `devbox` | When tailnet users want `qgis.devbox` to resolve without hosts-file edits |
| **Tailscale Kubernetes Operator** | `helm install tailscale-operator tailscale/tailscale-operator` | If you'd rather have MagicDNS hostnames like `qgis.<tailnet>.ts.net` |
| **cert-manager** | `helm install cert-manager jetstack/cert-manager` | If TLS is ever required |
| **QWC2 (if scope grows)** | `helm install qwc camptocamp/helm-qwc-services` | If someone later wants filter/search/attribute table/print layouts. Would replace the thin viewer. |

Installing any of these is a straightforward `helm install` that composes alongside our chart — no refactor needed.

## 10. Out of scope

- WFS-T editing from the browser
- User accounts, SSO, row-level security
- Print/PDF layouts
- Public internet exposure with TLS
- Attribute table, filtering, search UI
- Continuous reload of `project.qgs` on gpkg changes (regeneration is manual via `helm upgrade`)

## 11. Open questions / assumptions locked in

These are my best-guess defaults; flag any you want changed before implementation:

- **Basemap:** OpenStreetMap public tiles. Alternatives: Carto Positron, Esri World Topo, no basemap.
- **PVC strategy:** `local-path` with hostPath to `/srv/qgis` so edits to the source gpkg files are picked up immediately. Alternative: copy files into a pod-native volume (safer but requires re-copy on update).
- **QGIS Server image:** `camptocamp/qgis-server:3.34` LTS. Alternatives: `3liz/qgis-map-server`, a fresh build from `camptocamp/docker-qgis-server` Dockerfile.
- **Categorized styling for territories:** by `muni_name`. Alternatives: by `type` (rural/subdivision/complex), by `terr_id` (one color per territory), single fill.
- **Hostname + dnsmasq:** `http://qgis.devbox/` and `/ows` on the same host, resolved by wildcard `*.devbox` in a host-level dnsmasq service. Chosen over path-prefix so adding new services is just a new Ingress manifest + a new subdomain.
- **TLD `.devbox`:** non-public, collision-free. Alternatives were `.home.arpa` (officially reserved for local use) or `.lan`.

## 12. Success criteria

**DNS prerequisite (one-time):**
- `dig @192.168.1.70 qgis.devbox` returns `192.168.1.70`
- `dig @192.168.1.70 anything-new.devbox` returns `192.168.1.70` (wildcard works)
- `dig @192.168.1.70 google.com` returns real Google IPs (upstream resolution works)

**Helm chart install:**
- `helm install qgis ./chart -n qgis --create-namespace` completes without error.
- `kubectl -n qgis get pods` shows `qgis-server` and `viewer` both `Running` with `Ready 1/1`.
- `project.qgs` exists at `/srv/qgis/project.qgs` after install.

**Service behavior (from a client configured to use host dnsmasq):**
- `curl -H "Host: qgis.devbox" http://192.168.1.70/ows/?SERVICE=WMS\&REQUEST=GetCapabilities` returns a WMS capabilities XML listing all 24 layers.
- Opening `http://qgis.devbox/` in a browser renders the Leaflet map with territories drawn on top of OSM, the layer sidebar populated, and clicking a territory shows a popup with its attributes.

**Pattern reusability:**
- `helm uninstall qgis -n qgis && helm install qgis ./chart -n qgis --create-namespace` reaches the same state (idempotency).
- Adding a second, unrelated test service with `host: foo.devbox` via a second Ingress makes `http://foo.devbox/` reachable without any DNS or dnsmasq changes.
