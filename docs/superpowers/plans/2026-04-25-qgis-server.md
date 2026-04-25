# QGIS Server on k3s — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up a QGIS Server in k3s that serves the two GeoPackage files at `/srv/qgis/data/territories/*.gpkg`, with a thin Leaflet web viewer reachable at `http://qgis.devbox/` from the LAN/tailnet.

**Architecture:** Two-phase implementation. Phase 1 sets up host-level dnsmasq with a wildcard `*.devbox` record (one-time shared infra for all future services on this box). Phase 2 builds and deploys a small in-repo helm chart with two Deployments (qgis-server, viewer), a hostPath PVC, a project-generator Job, and a Traefik Ingress on `qgis.devbox`.

**Tech Stack:**
- k3s 1.x with Traefik ingress (already installed)
- helm 3.x (already installed)
- dnsmasq from Ubuntu apt
- `camptocamp/qgis-server:3.34` (FCGI)
- `nginx:1.27-alpine` (static viewer)
- `python:3.12-slim` (one-shot project generator using stdlib `sqlite3` + `xml.etree.ElementTree`)
- Leaflet via CDN (no JS build step)
- `pytest` for the project-generator unit tests

**Spec:** `/srv/qgis/docs/superpowers/specs/2026-04-24-qgis-server-design.md`

---

## Phase 0 — Repo bootstrap

### Task 1: Initialize git repo and directory structure

**Files:**
- Create: `/srv/qgis/.gitignore`
- Create: `/srv/qgis/README.md`

- [ ] **Step 1: Verify we're in the right place**

```bash
cd /srv/qgis && pwd
ls -la
```
Expected: `/srv/qgis` printed; `data/` and `docs/` directories present.

- [ ] **Step 2: Initialize git**

```bash
cd /srv/qgis
git init -b main
git config user.email "clay@pfd.net"
git config user.name "Clay"
```
Expected: `Initialized empty Git repository in /srv/qgis/.git/`.

- [ ] **Step 3: Write `.gitignore`**

```gitignore
# Python
__pycache__/
*.pyc
.pytest_cache/
.venv/

# Helm
chart/charts/
chart/Chart.lock

# Editor
.vscode/
.idea/
*.swp

# OS
.DS_Store

# Generated artifacts (kept on PVC, not in repo)
project.qgs
```

- [ ] **Step 4: Write minimal `README.md`**

```markdown
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
```

- [ ] **Step 5: Make the initial commit**

```bash
cd /srv/qgis
git add .gitignore README.md docs/
git commit -m "initial: spec, plan, and repo bootstrap"
git status
```
Expected: working tree clean, one commit on `main`.

---

## Phase 1 — dnsmasq prerequisite

### Task 2: Write the dnsmasq config file

**Files:**
- Create: `/srv/qgis/dnsmasq/devbox.conf`

- [ ] **Step 1: Confirm host networking facts that the config depends on**

```bash
ip -4 addr show | grep -E 'inet 192\.168\.|inet 100\.'
systemctl is-active systemd-resolved
ss -lunp 'sport = :53'
```
Expected:
- `inet 192.168.1.70/24 ...` on the LAN interface
- `inet 100.117.43.89/32 ...` on `tailscale0`
- `systemd-resolved` is `active`
- `ss` shows `127.0.0.53:53` owned by `systemd-resolve` (no other binders on port 53)

If any of these don't match, **stop** and reconcile with the spec assumptions before continuing.

- [ ] **Step 2: Create the dnsmasq directory**

```bash
mkdir -p /srv/qgis/dnsmasq
```

- [ ] **Step 3: Write `dnsmasq/devbox.conf`**

```
# /etc/dnsmasq.d/devbox.conf — wildcard *.devbox resolution
#
# Installed by /srv/qgis/dnsmasq/install.sh.
# This is host-level shared infrastructure for every service running on
# this box. It is intentionally separate from any helm chart.

# Wildcard: every *.devbox name resolves to this host.
address=/.devbox/192.168.1.70

# Bind only to the LAN and Tailscale interface IPs.
# We must NOT bind 127.0.0.53 (owned by systemd-resolved on Ubuntu 24.04)
# and we don't want to bind every interface (that would conflict).
listen-address=192.168.1.70,100.117.43.89
bind-interfaces

# Don't read /etc/resolv.conf (which points at systemd-resolved and would
# create a resolution loop). Use upstream resolvers explicitly.
no-resolv
server=1.1.1.1
server=1.0.0.1

# Cache size for upstream answers
cache-size=1000

# Log queries when debugging (commented out by default — uncomment + reload to use)
# log-queries
```

- [ ] **Step 4: Lint the config offline**

```bash
dnsmasq --test --conf-file=/srv/qgis/dnsmasq/devbox.conf
```
Expected: `dnsmasq: syntax check OK.`

If `dnsmasq` is not installed yet, install it first: `sudo apt-get install -y dnsmasq` — this command also runs in Task 3.

- [ ] **Step 5: Commit**

```bash
cd /srv/qgis
git add dnsmasq/devbox.conf
git commit -m "dnsmasq: wildcard *.devbox config"
```

---

### Task 3: Write the dnsmasq installer script

**Files:**
- Create: `/srv/qgis/dnsmasq/install.sh`

- [ ] **Step 1: Define the failing acceptance test**

The test for this task is the `dig` verification in Task 4. Step 5 of Task 4 is the failing-state check (script not yet run → `qgis.devbox` does not resolve). We write the script here.

- [ ] **Step 2: Write `dnsmasq/install.sh`**

```bash
#!/usr/bin/env bash
# Idempotent dnsmasq installer for *.devbox wildcard DNS.
# Re-runnable: copying the same conf is a no-op; restarting is cheap.

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo." >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF_SRC="${SCRIPT_DIR}/devbox.conf"
CONF_DST="/etc/dnsmasq.d/devbox.conf"

echo "==> Installing dnsmasq via apt"
DEBIAN_FRONTEND=noninteractive apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y dnsmasq

echo "==> Disabling dnsmasq's stub resolver behavior conflicts"
# On Ubuntu, dnsmasq's default config tries to be a system resolver.
# We override only via /etc/dnsmasq.d/, so make sure that drop-in dir is read.
if ! grep -q '^conf-dir=/etc/dnsmasq.d' /etc/dnsmasq.conf 2>/dev/null; then
  echo "  (dnsmasq.conf already includes /etc/dnsmasq.d by default on Ubuntu)"
fi

echo "==> Writing ${CONF_DST}"
install -m 0644 "${CONF_SRC}" "${CONF_DST}"

echo "==> Validating config"
dnsmasq --test

echo "==> Enabling and restarting dnsmasq"
systemctl enable dnsmasq
systemctl restart dnsmasq

echo "==> Waiting for dnsmasq to bind"
sleep 1
systemctl is-active dnsmasq

echo "==> Verifying wildcard resolution"
if dig +short @192.168.1.70 qgis.devbox | grep -q '^192\.168\.1\.70$'; then
  echo "  qgis.devbox -> 192.168.1.70 ✓"
else
  echo "  qgis.devbox did not resolve to 192.168.1.70" >&2
  exit 1
fi
if dig +short @192.168.1.70 anything-else.devbox | grep -q '^192\.168\.1\.70$'; then
  echo "  anything-else.devbox -> 192.168.1.70 ✓"
else
  echo "  wildcard match failed" >&2
  exit 1
fi
if dig +short @192.168.1.70 google.com | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$'; then
  echo "  upstream resolution working ✓"
else
  echo "  upstream resolution failed" >&2
  exit 1
fi

echo
echo "Done. Configure clients to use 192.168.1.70 (or 100.117.43.89 over Tailscale)"
echo "as their DNS server, OR add an /etc/hosts fallback entry."
```

- [ ] **Step 3: Make it executable**

```bash
chmod +x /srv/qgis/dnsmasq/install.sh
```

- [ ] **Step 4: Lint with shellcheck if available, else bash -n**

```bash
shellcheck /srv/qgis/dnsmasq/install.sh 2>/dev/null || bash -n /srv/qgis/dnsmasq/install.sh && echo "syntax OK"
```
Expected: no warnings or `syntax OK`.

- [ ] **Step 5: Commit**

```bash
cd /srv/qgis
git add dnsmasq/install.sh
git commit -m "dnsmasq: idempotent installer script"
```

---

### Task 4: Run the installer and verify resolution

This task does the actual install. It modifies the host (apt install, /etc, systemd). It is idempotent — re-running is safe.

- [ ] **Step 1: Pre-state — confirm dnsmasq is not yet running our config**

```bash
dig +short +timeout=2 @192.168.1.70 qgis.devbox || echo "no answer (expected)"
```
Expected: empty result or `no answer (expected)`. If you already see `192.168.1.70` here, the install was previously done — that's fine.

- [ ] **Step 2: Run the installer**

```bash
sudo /srv/qgis/dnsmasq/install.sh
```
Expected: prints `==>` lines for each step, ends with `Done.` and three `✓` checkmarks for the three dig assertions.

- [ ] **Step 3: Independent verification (the script's checks already passed, but run them again from the user shell)**

```bash
dig +short @192.168.1.70 qgis.devbox
dig +short @192.168.1.70 anything-new.devbox
dig +short @192.168.1.70 example.com
```
Expected:
- First two: `192.168.1.70`
- Third: a real IP (e.g. `93.184.215.14` or similar)

- [ ] **Step 4: Verify systemd-resolved did not break**

```bash
resolvectl status | head -20
systemctl is-active systemd-resolved
```
Expected: `systemd-resolved` still `active`. (We only added a separate dnsmasq listening on different IPs; the default stub resolver on `127.0.0.53` is untouched.)

- [ ] **Step 5: Verify the host can still reach the internet**

```bash
ping -c 2 1.1.1.1
curl -sI https://www.google.com | head -1
```
Expected: pings succeed, HTTP `200` or `301` from Google.

- [ ] **Step 6: No commit needed for runtime state**

This task changed `/etc/dnsmasq.d/devbox.conf` and the systemd state of dnsmasq, both of which are the *result* of running the script (already committed in Task 3). Nothing in the repo changed.

---

### Task 5: Document client-side DNS configuration

**Files:**
- Create: `/srv/qgis/dnsmasq/README.md`

- [ ] **Step 1: Write `dnsmasq/README.md`**

```markdown
# devbox wildcard DNS

This directory holds the host-level dnsmasq config that resolves any `*.devbox`
hostname to this server (`192.168.1.70`). It is shared infrastructure for every
service running on this box and is **not** part of any helm chart.

## What got installed

- `dnsmasq` apt package
- `/etc/dnsmasq.d/devbox.conf` (copied from `devbox.conf` in this directory)
- `dnsmasq` systemd service, listening on:
  - `192.168.1.70:53` (LAN)
  - `100.117.43.89:53` (Tailscale)

systemd-resolved is untouched on `127.0.0.53:53`.

## Run / re-run

```bash
sudo ./install.sh
```

The installer is idempotent. Edit `devbox.conf` and re-run to apply changes.

## Configuring clients to use this resolver

### LAN clients

Set DNS to `192.168.1.70` either:

- Globally via your router's DHCP "DNS server" setting (preferred — affects every
  device on the LAN automatically), or
- Per-machine in network settings.

Verify on a client:

```bash
dig qgis.devbox          # should answer 192.168.1.70
```

### Tailscale clients

Open the [Tailscale admin DNS page](https://login.tailscale.com/admin/dns):

1. Add a custom nameserver: `100.117.43.89`
2. Restrict to domain: `devbox`
3. Save.

Tailnet devices (laptops, phones) will then resolve `*.devbox` via this dnsmasq
automatically while leaving everything else on their normal resolver.

### Fallback — `/etc/hosts`

If a client can't be reconfigured to use a different DNS server, add:

```
192.168.1.70 qgis.devbox
```

You'll need a new line for each new service, which is why dnsmasq is preferred.

## Adding new services

Once dnsmasq is running, *any* `*.devbox` hostname is reachable. To add a new
service `foo`:

1. Deploy it to k3s with an Ingress whose `host: foo.devbox`.
2. Open `http://foo.devbox/` from any client configured per the section above.

No DNS changes are needed for new services.

## Troubleshooting

| Symptom | Check |
|---|---|
| `dig @192.168.1.70 qgis.devbox` times out | `systemctl status dnsmasq`, check it's listening with `ss -lunp 'sport = :53'` |
| Resolves to wrong IP | Inspect `/etc/dnsmasq.d/devbox.conf`, restart dnsmasq |
| Client gets `NXDOMAIN` | Client probably isn't using this resolver. `dig qgis.devbox` will show which server was queried |
| Internet stops working from clients | Either dnsmasq's `server=` upstream is unreachable, or the client is using only this resolver and a local network is blocking 1.1.1.1 |
```

- [ ] **Step 2: Commit**

```bash
cd /srv/qgis
git add dnsmasq/README.md
git commit -m "dnsmasq: client setup docs"
```

---

## Phase 2 — Helm chart skeleton

### Task 6: Create the helm chart bootstrap

**Files:**
- Create: `/srv/qgis/chart/Chart.yaml`
- Create: `/srv/qgis/chart/values.yaml`
- Create: `/srv/qgis/chart/.helmignore`
- Create: `/srv/qgis/chart/templates/_helpers.tpl`
- Create: `/srv/qgis/chart/templates/namespace.yaml`

- [ ] **Step 1: Create chart directory structure**

```bash
mkdir -p /srv/qgis/chart/{templates,files}
```

- [ ] **Step 2: Write `chart/Chart.yaml`**

```yaml
apiVersion: v2
name: qgis
description: QGIS Server on k3s with thin Leaflet viewer
type: application
version: 0.1.0
appVersion: "3.34"
keywords:
  - qgis
  - geo
  - wms
maintainers:
  - name: clay
    email: clay@pfd.net
```

- [ ] **Step 3: Write `chart/values.yaml`**

```yaml
# Namespace for the chart.
namespace: qgis
createNamespace: true

# Host path on the node where the gpkg files and project.qgs live.
# This is mounted via a hostPath PV. Files outside data/ on this path are
# never written by the chart except for project.qgs (written by the
# project-generator Job).
hostPath: /srv/qgis

ingress:
  className: traefik
  # Hostname must resolve to the cluster's external IP.
  # Wildcard *.devbox resolution is handled by the host's dnsmasq
  # (see /srv/qgis/dnsmasq/).
  host: qgis.devbox

image:
  qgisServer: camptocamp/qgis-server:3.34
  viewer: nginx:1.27-alpine
  generator: python:3.12-slim
  pullPolicy: IfNotPresent

resources:
  qgisServer:
    requests: { cpu: 100m, memory: 256Mi }
    limits:   { cpu: 500m, memory: 1Gi   }
  viewer:
    requests: { cpu: 10m,  memory: 32Mi  }
    limits:   { cpu: 100m, memory: 128Mi }
  generator:
    requests: { cpu: 50m,  memory: 64Mi  }
    limits:   { cpu: 500m, memory: 256Mi }

# Set generator.enabled=false to keep a hand-edited project.qgs across upgrades.
generator:
  enabled: true

# Path inside the qgis-server container where the project file lives.
# Must match where the generator writes it.
projectFile: /srv/qgis/project.qgs
```

- [ ] **Step 4: Write `chart/.helmignore`**

```
# Patterns to ignore when building helm chart packages
.git/
.gitignore
.DS_Store
*.bak
*.swp
*.tmp
.idea/
.vscode/
__pycache__/
*.pyc
.pytest_cache/
```

- [ ] **Step 5: Write `chart/templates/_helpers.tpl`**

```yaml
{{/* Standard chart name (used as base of resource names) */}}
{{- define "qgis.name" -}}
qgis
{{- end -}}

{{/* Common labels applied to all resources */}}
{{- define "qgis.labels" -}}
app.kubernetes.io/name: {{ include "qgis.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{/* Selector labels (a stable subset, used for matchLabels in Deployments) */}}
{{- define "qgis.selectorLabels" -}}
app.kubernetes.io/name: {{ include "qgis.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
```

- [ ] **Step 6: Write `chart/templates/namespace.yaml`**

```yaml
{{- if .Values.createNamespace -}}
apiVersion: v1
kind: Namespace
metadata:
  name: {{ .Values.namespace }}
  labels:
    {{- include "qgis.labels" . | nindent 4 }}
{{- end -}}
```

- [ ] **Step 7: Lint the chart**

```bash
cd /srv/qgis
helm lint ./chart
```
Expected: `1 chart(s) linted, 0 chart(s) failed`. Some `[INFO]` lines are OK; no `[ERROR]` or `[WARNING]` other than the standard "icon is recommended" info.

- [ ] **Step 8: Render templates and confirm namespace YAML is generated**

```bash
helm template qgis /srv/qgis/chart | head -20
```
Expected: a YAML stream beginning with the Namespace resource for `qgis`.

- [ ] **Step 9: Commit**

```bash
cd /srv/qgis
git add chart/
git commit -m "chart: bootstrap (Chart.yaml, values, helpers, namespace)"
```

---

## Phase 3 — Viewer (without QGIS Server yet)

This phase deploys the static viewer page first so we can verify Ingress and DNS end-to-end before introducing QGIS Server. Until Phase 6 lands, the page will load Leaflet but the WMS layer will fail (no `/ows` route yet) — that's expected.

### Task 7: Write the static viewer HTML

**Files:**
- Create: `/srv/qgis/chart/files/index.html`

- [ ] **Step 1: Write `chart/files/index.html`**

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Territories — QGIS Viewer</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
        integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY="
        crossorigin="" />
  <style>
    html, body { margin: 0; padding: 0; height: 100%; font-family: system-ui, sans-serif; }
    #wrap   { display: flex; height: 100vh; }
    #side   { width: 260px; padding: 12px; overflow-y: auto; border-right: 1px solid #ddd; background: #fafafa; }
    #map    { flex: 1; }
    h2      { margin: 0 0 8px; font-size: 14px; text-transform: uppercase; color: #444; }
    .group  { margin-bottom: 12px; }
    .group label { display: block; font-size: 13px; line-height: 1.6; cursor: pointer; }
    .group label input { margin-right: 6px; }
    summary { cursor: pointer; font-size: 14px; }
    .leaflet-popup-content table { font-size: 12px; border-collapse: collapse; }
    .leaflet-popup-content td    { padding: 2px 6px; border-bottom: 1px solid #eee; }
    .leaflet-popup-content td:first-child { color: #666; font-weight: 600; }
  </style>
</head>
<body>
  <div id="wrap">
    <div id="side">
      <h2>Layers</h2>
      <div id="layer-controls"></div>
    </div>
    <div id="map"></div>
  </div>

  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
          integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo="
          crossorigin=""></script>
  <script>
    // Hostname is whatever loaded the page; we call OWS on the same origin
    // so Traefik can route /ows to qgis-server without CORS.
    const OWS = '/ows';

    // Default visible vs hidden layers.
    // The list of layers is discovered at runtime from GetCapabilities.
    const VISIBLE_BY_DEFAULT = new Set(['territories']);

    const map = L.map('map').setView([35.18, -80.66], 11); // approx Union County, NC

    // Basemap.
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
      maxZoom: 19
    }).addTo(map);

    // Per-layer WMS tile layers, keyed by layer name.
    const wmsLayers = new Map();

    function makeWmsLayer(name) {
      return L.tileLayer.wms(OWS, {
        layers: name,
        format: 'image/png',
        transparent: true,
        version: '1.3.0',
        tiled: true
      });
    }

    function addControl(name, group) {
      const wrap = document.getElementById('layer-controls');
      let groupEl = document.getElementById('group-' + group);
      if (!groupEl) {
        const details = document.createElement('details');
        details.id = 'group-' + group;
        if (group === 'main') details.open = true;
        const summary = document.createElement('summary');
        summary.textContent = group === 'main' ? 'Main' : 'Debug';
        details.appendChild(summary);
        const div = document.createElement('div');
        div.className = 'group';
        details.appendChild(div);
        wrap.appendChild(details);
        groupEl = div;
        details.dataset.body = '';
        details._body = div;
      }
      const body = groupEl.classList ? groupEl : groupEl._body;
      const label = document.createElement('label');
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.checked = VISIBLE_BY_DEFAULT.has(name);
      cb.addEventListener('change', () => {
        const layer = wmsLayers.get(name);
        if (cb.checked) layer.addTo(map);
        else map.removeLayer(layer);
      });
      label.appendChild(cb);
      label.appendChild(document.createTextNode(' ' + name));
      body.appendChild(label);

      const layer = makeWmsLayer(name);
      wmsLayers.set(name, layer);
      if (cb.checked) layer.addTo(map);
    }

    // Discover layers from QGIS Server capabilities.
    fetch(OWS + '/?SERVICE=WMS&REQUEST=GetCapabilities&VERSION=1.3.0')
      .then(r => r.text())
      .then(xml => {
        const doc = new DOMParser().parseFromString(xml, 'application/xml');
        const layers = doc.getElementsByTagName('Layer');
        // The top-level Layer is the project; its child Layer elements are the actual layers.
        const named = [];
        for (const el of layers) {
          const nameEl = el.getElementsByTagName('Name')[0];
          if (!nameEl) continue;
          const name = nameEl.textContent.trim();
          if (!name || name === 'qgis') continue;
          named.push(name);
        }
        for (const name of named) {
          const group = name.startsWith('step_') ? 'debug' : 'main';
          addControl(name, group);
        }
        // Fit map to overall bounding box (use territories bbox if present).
        const bboxEls = doc.getElementsByTagName('EX_GeographicBoundingBox');
        if (bboxEls.length > 0) {
          const e = bboxEls[0];
          const w = parseFloat(e.getElementsByTagName('westBoundLongitude')[0].textContent);
          const easting = parseFloat(e.getElementsByTagName('eastBoundLongitude')[0].textContent);
          const s = parseFloat(e.getElementsByTagName('southBoundLatitude')[0].textContent);
          const n = parseFloat(e.getElementsByTagName('northBoundLatitude')[0].textContent);
          map.fitBounds([[s, w], [n, easting]]);
        }
      })
      .catch(err => {
        const msg = document.createElement('div');
        msg.style = 'padding:8px;color:#900;font-size:12px';
        msg.textContent = 'Could not load layer list (QGIS Server not yet ready): ' + err;
        document.getElementById('side').appendChild(msg);
      });

    // Click → GetFeatureInfo for every visible WMS layer
    map.on('click', async (e) => {
      const visible = [...wmsLayers.entries()].filter(([n, l]) => map.hasLayer(l)).map(([n]) => n);
      if (visible.length === 0) return;
      const size = map.getSize();
      const point = map.latLngToContainerPoint(e.latlng);
      const bbox = map.getBounds();
      // QGIS Server WMS 1.3.0 expects bbox in CRS-axis order; for EPSG:4326 that's lat,lon.
      const params = new URLSearchParams({
        SERVICE: 'WMS',
        VERSION: '1.3.0',
        REQUEST: 'GetFeatureInfo',
        LAYERS: visible.join(','),
        QUERY_LAYERS: visible.join(','),
        CRS: 'EPSG:4326',
        BBOX: [bbox.getSouth(), bbox.getWest(), bbox.getNorth(), bbox.getEast()].join(','),
        WIDTH: size.x,
        HEIGHT: size.y,
        I: Math.round(point.x),
        J: Math.round(point.y),
        INFO_FORMAT: 'application/json',
        FEATURE_COUNT: 5
      });
      const r = await fetch(OWS + '/?' + params);
      if (!r.ok) return;
      const data = await r.json();
      if (!data.features || data.features.length === 0) return;
      const f = data.features[0];
      const rows = Object.entries(f.properties || {})
        .map(([k, v]) => `<tr><td>${k}</td><td>${v ?? ''}</td></tr>`)
        .join('');
      const html = `<table>${rows}</table>`;
      L.popup().setLatLng(e.latlng).setContent(html).openOn(map);
    });
  </script>
</body>
</html>
```

- [ ] **Step 2: Validate HTML basic structure**

```bash
python3 -c "import html.parser, sys
class P(html.parser.HTMLParser):
    def error(self, msg): raise ValueError(msg)
P().feed(open('/srv/qgis/chart/files/index.html').read())
print('html parse OK')"
```
Expected: `html parse OK`

- [ ] **Step 3: Commit**

```bash
cd /srv/qgis
git add chart/files/index.html
git commit -m "chart: viewer index.html with leaflet + wms + getfeatureinfo"
```

---

### Task 8: Viewer ConfigMap, Deployment, Service, Ingress

**Files:**
- Create: `/srv/qgis/chart/templates/configmap-viewer.yaml`
- Create: `/srv/qgis/chart/templates/deployment-viewer.yaml`
- Create: `/srv/qgis/chart/templates/service-viewer.yaml`
- Create: `/srv/qgis/chart/templates/ingress.yaml`

- [ ] **Step 1: Write `chart/templates/configmap-viewer.yaml`**

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: viewer-html
  namespace: {{ .Values.namespace }}
  labels:
    {{- include "qgis.labels" . | nindent 4 }}
data:
  index.html: |-
{{ .Files.Get "files/index.html" | indent 4 }}
```

- [ ] **Step 2: Write `chart/templates/deployment-viewer.yaml`**

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: viewer
  namespace: {{ .Values.namespace }}
  labels:
    {{- include "qgis.labels" . | nindent 4 }}
spec:
  replicas: 1
  selector:
    matchLabels:
      {{- include "qgis.selectorLabels" . | nindent 6 }}
      app.kubernetes.io/component: viewer
  template:
    metadata:
      labels:
        {{- include "qgis.selectorLabels" . | nindent 8 }}
        app.kubernetes.io/component: viewer
    spec:
      containers:
        - name: nginx
          image: {{ .Values.image.viewer }}
          imagePullPolicy: {{ .Values.image.pullPolicy }}
          ports:
            - containerPort: 80
          volumeMounts:
            - name: html
              mountPath: /usr/share/nginx/html
              readOnly: true
          readinessProbe:
            httpGet: { path: /, port: 80 }
            initialDelaySeconds: 2
            periodSeconds: 5
          livenessProbe:
            httpGet: { path: /, port: 80 }
            initialDelaySeconds: 10
            periodSeconds: 15
          resources:
            {{- toYaml .Values.resources.viewer | nindent 12 }}
      volumes:
        - name: html
          configMap:
            name: viewer-html
```

- [ ] **Step 3: Write `chart/templates/service-viewer.yaml`**

```yaml
apiVersion: v1
kind: Service
metadata:
  name: viewer
  namespace: {{ .Values.namespace }}
  labels:
    {{- include "qgis.labels" . | nindent 4 }}
spec:
  type: ClusterIP
  selector:
    {{- include "qgis.selectorLabels" . | nindent 4 }}
    app.kubernetes.io/component: viewer
  ports:
    - port: 80
      targetPort: 80
      protocol: TCP
```

- [ ] **Step 4: Write `chart/templates/ingress.yaml`**

This initial version routes only `/` → viewer. Phase 6 will add `/ows` → qgis-server.

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: qgis
  namespace: {{ .Values.namespace }}
  labels:
    {{- include "qgis.labels" . | nindent 4 }}
spec:
  ingressClassName: {{ .Values.ingress.className }}
  rules:
    - host: {{ .Values.ingress.host }}
      http:
        paths:
          # /ows is added in Phase 6 when qgis-server exists
          - path: /
            pathType: Prefix
            backend:
              service:
                name: viewer
                port:
                  number: 80
```

- [ ] **Step 5: Lint and template**

```bash
cd /srv/qgis
helm lint ./chart
helm template qgis ./chart | grep -E '^(kind|  name):' | head -30
```
Expected: lint passes; you see Namespace, ConfigMap, Deployment, Service, Ingress (and helpers).

- [ ] **Step 6: Install the chart**

```bash
sudo cp /etc/rancher/k3s/k3s.yaml /tmp/kube.yaml
sudo chown "$USER" /tmp/kube.yaml
export KUBECONFIG=/tmp/kube.yaml

helm install qgis /srv/qgis/chart -n qgis --create-namespace
```
Expected: `STATUS: deployed`. (If you've already given your user permanent kubeconfig access from the brainstorming session, skip the cp/chown and just `export KUBECONFIG=~/.kube/config`.)

- [ ] **Step 7: Wait for the viewer pod to become Ready**

```bash
kubectl -n qgis rollout status deploy/viewer --timeout=60s
kubectl -n qgis get pods,svc,ingress
```
Expected: viewer pod `1/1 Running`; viewer service `ClusterIP`; ingress `qgis` exists with host `qgis.devbox`.

- [ ] **Step 8: Verify the viewer page is reachable**

```bash
curl -sI http://qgis.devbox/ | head -1
curl -s http://qgis.devbox/ | grep -c '<title>Territories'
```
Expected: `HTTP/1.1 200 OK` and a count of `1`.

If `curl: Could not resolve host: qgis.devbox`, the dnsmasq install in Phase 1 didn't reach this host's resolver. As a fallback for the test, force the Host header:
```bash
curl -sI -H 'Host: qgis.devbox' http://192.168.1.70/ | head -1
```

- [ ] **Step 9: Browser smoke test**

Open `http://qgis.devbox/` in a browser. You should see the Leaflet map with OSM tiles, a sidebar that says "Layers", and an inline error message indicating "QGIS Server not yet ready" — this is **expected** because Phase 6 hasn't deployed qgis-server yet.

- [ ] **Step 10: Commit**

```bash
cd /srv/qgis
git add chart/templates/configmap-viewer.yaml chart/templates/deployment-viewer.yaml \
        chart/templates/service-viewer.yaml chart/templates/ingress.yaml
git commit -m "chart: viewer deployment, service, ingress"
```

---

## Phase 4 — Persistent storage for project file + gpkg

### Task 9: PVC + PV for `/srv/qgis`

**Files:**
- Create: `/srv/qgis/chart/templates/pvc.yaml`

The chart provisions a PV that hostPath-mounts `/srv/qgis` from the node, plus a PVC that binds it. Using a manually-created PV (rather than the dynamic local-path provisioner) lets us pin the path exactly to `/srv/qgis` rather than to a random `/var/lib/rancher/k3s/storage/pvc-...` directory.

- [ ] **Step 1: Write `chart/templates/pvc.yaml`**

```yaml
# Statically-provisioned PV bound to /srv/qgis on the node.
apiVersion: v1
kind: PersistentVolume
metadata:
  name: qgis-data-pv
  labels:
    {{- include "qgis.labels" . | nindent 4 }}
spec:
  capacity:
    storage: 1Gi
  accessModes:
    - ReadWriteOnce
  persistentVolumeReclaimPolicy: Retain
  storageClassName: ""
  hostPath:
    path: {{ .Values.hostPath }}
    type: Directory
  nodeAffinity:
    required:
      nodeSelectorTerms:
        - matchExpressions:
            - key: kubernetes.io/hostname
              operator: Exists
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: qgis-data
  namespace: {{ .Values.namespace }}
  labels:
    {{- include "qgis.labels" . | nindent 4 }}
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 1Gi
  storageClassName: ""
  volumeName: qgis-data-pv
```

- [ ] **Step 2: Lint**

```bash
cd /srv/qgis
helm lint ./chart
```
Expected: passes.

- [ ] **Step 3: Helm upgrade**

```bash
helm upgrade qgis /srv/qgis/chart -n qgis
```
Expected: `STATUS: deployed`, no errors.

- [ ] **Step 4: Verify PV is `Bound`**

```bash
kubectl get pv qgis-data-pv
kubectl -n qgis get pvc qgis-data
```
Expected: PV STATUS `Bound`, claim `qgis/qgis-data`. PVC STATUS `Bound`.

- [ ] **Step 5: Commit**

```bash
cd /srv/qgis
git add chart/templates/pvc.yaml
git commit -m "chart: hostPath PV + PVC bound to /srv/qgis"
```

---

## Phase 5 — Project generator (TDD on the Python script)

### Task 10: Test scaffolding for the project generator

**Files:**
- Create: `/srv/qgis/chart/files/generate_qgs.py` (empty stub for now)
- Create: `/srv/qgis/tests/__init__.py`
- Create: `/srv/qgis/tests/test_generate_qgs.py`
- Create: `/srv/qgis/tests/conftest.py`
- Create: `/srv/qgis/pyproject.toml`

- [ ] **Step 1: Set up a virtual env and pytest**

```bash
cd /srv/qgis
python3 -m venv .venv
. .venv/bin/activate
pip install -U pip pytest
```

- [ ] **Step 2: Write `pyproject.toml` for pytest config**

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-v"
```

- [ ] **Step 3: Write `tests/__init__.py` (empty file)**

Just create it: `touch /srv/qgis/tests/__init__.py`.

- [ ] **Step 4: Write `tests/conftest.py` to expose the chart's files dir on sys.path**

```python
import sys
from pathlib import Path

# Make chart/files importable as if it were a package
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "chart" / "files"))
```

- [ ] **Step 5: Write the first failing test in `tests/test_generate_qgs.py`**

```python
"""Tests for chart/files/generate_qgs.py."""

import sqlite3
import textwrap
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

import generate_qgs as gen


def _make_minimal_gpkg(path: Path, layer_name: str = "things") -> None:
    """Create a tiny but valid GeoPackage with a single point layer.

    Just enough metadata for the generator's introspection — gpkg_contents +
    gpkg_geometry_columns + a feature table with a few attribute columns.
    """
    con = sqlite3.connect(path)
    cur = con.cursor()
    cur.executescript(
        textwrap.dedent(
            f"""
            CREATE TABLE gpkg_contents (
              table_name TEXT PRIMARY KEY,
              data_type TEXT NOT NULL,
              identifier TEXT,
              description TEXT,
              last_change DATETIME,
              min_x DOUBLE, min_y DOUBLE, max_x DOUBLE, max_y DOUBLE,
              srs_id INTEGER
            );
            CREATE TABLE gpkg_geometry_columns (
              table_name TEXT PRIMARY KEY,
              column_name TEXT NOT NULL,
              geometry_type_name TEXT NOT NULL,
              srs_id INTEGER NOT NULL,
              z TINYINT, m TINYINT
            );
            CREATE TABLE "{layer_name}" (
              fid INTEGER PRIMARY KEY,
              geom BLOB,
              name TEXT,
              count INTEGER
            );
            INSERT INTO gpkg_contents VALUES
              ('{layer_name}', 'features', '{layer_name}', '', '2026-01-01',
               1000000, 500000, 1100000, 600000, 2264);
            INSERT INTO gpkg_geometry_columns VALUES
              ('{layer_name}', 'geom', 'POINT', 2264, 0, 0);
            """
        )
    )
    con.commit()
    con.close()


def test_introspect_returns_layers_with_columns(tmp_path):
    gpkg = tmp_path / "tiny.gpkg"
    _make_minimal_gpkg(gpkg, layer_name="things")

    layers = gen.introspect_gpkg(gpkg)

    assert len(layers) == 1
    layer = layers[0]
    assert layer.name == "things"
    assert layer.geometry_type == "POINT"
    assert layer.srs_id == 2264
    assert layer.columns == ["fid", "geom", "name", "count"]
    assert layer.bbox == (1000000, 500000, 1100000, 600000)
    assert str(gpkg) in str(layer.source_path)


def test_introspect_skips_non_features(tmp_path):
    gpkg = tmp_path / "mixed.gpkg"
    _make_minimal_gpkg(gpkg)
    # Add a non-feature row
    con = sqlite3.connect(gpkg)
    con.execute(
        "INSERT INTO gpkg_contents VALUES "
        "('attr_only', 'attributes', 'attr_only', '', '2026-01-01',"
        " 0, 0, 0, 0, 2264)"
    )
    con.commit()
    con.close()

    layers = gen.introspect_gpkg(gpkg)

    assert [l.name for l in layers] == ["things"]


def test_render_qgs_produces_valid_xml_with_one_maplayer_per_layer(tmp_path):
    gpkg = tmp_path / "tiny.gpkg"
    _make_minimal_gpkg(gpkg)
    layers = gen.introspect_gpkg(gpkg)

    xml_str = gen.render_qgs(layers, project_crs_authid="EPSG:3857")

    root = ET.fromstring(xml_str)
    assert root.tag == "qgis"
    maplayers = root.findall("./projectlayers/maplayer")
    assert len(maplayers) == 1


def test_render_qgs_marks_territories_visible_step_layers_hidden(tmp_path):
    main_gpkg = tmp_path / "main.gpkg"
    debug_gpkg = tmp_path / "debug.gpkg"
    _make_minimal_gpkg(main_gpkg, layer_name="territories")
    _make_minimal_gpkg(debug_gpkg, layer_name="step_500_addresses")

    layers = gen.introspect_gpkg(main_gpkg) + gen.introspect_gpkg(debug_gpkg)
    xml_str = gen.render_qgs(layers, project_crs_authid="EPSG:3857")

    root = ET.fromstring(xml_str)
    # layer-tree-layer's `id` is a generated UUID; visibility is keyed by `name`
    visible = {n.get("name") for n in root.findall(
        "./layer-tree-group//layer-tree-layer[@checked='Qt::Checked']"
    )}
    hidden = {n.get("name") for n in root.findall(
        "./layer-tree-group//layer-tree-layer[@checked='Qt::Unchecked']"
    )}
    assert "territories" in visible
    assert "step_500_addresses" in hidden


def test_main_writes_project_qgs(tmp_path, monkeypatch):
    src = tmp_path / "data"
    out = tmp_path / "project.qgs"
    src.mkdir()
    _make_minimal_gpkg(src / "a.gpkg", layer_name="alpha")
    _make_minimal_gpkg(src / "b.gpkg", layer_name="beta")

    rc = gen.main([str(src), "--output", str(out)])

    assert rc == 0
    assert out.exists()
    root = ET.fromstring(out.read_text())
    names = {ml.findtext("layername") for ml in root.findall("./projectlayers/maplayer")}
    assert names == {"alpha", "beta"}
```

- [ ] **Step 6: Write a stub `chart/files/generate_qgs.py` so import succeeds**

```python
"""Project file generator for QGIS Server. Stub — implementation in next task."""
```

- [ ] **Step 7: Run the tests and confirm they fail**

```bash
cd /srv/qgis
. .venv/bin/activate
pytest tests/test_generate_qgs.py -v
```
Expected: 5 tests, 5 failures (`AttributeError: module 'generate_qgs' has no attribute 'introspect_gpkg'` etc).

- [ ] **Step 8: Commit**

```bash
cd /srv/qgis
git add tests/ pyproject.toml chart/files/generate_qgs.py
git commit -m "tests: failing tests for generate_qgs"
```

---

### Task 11: Implement `generate_qgs.py`

**Files:**
- Modify: `/srv/qgis/chart/files/generate_qgs.py`

- [ ] **Step 1: Replace `chart/files/generate_qgs.py` with the implementation**

```python
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
```

- [ ] **Step 2: Run tests, confirm they pass**

```bash
cd /srv/qgis
. .venv/bin/activate
pytest tests/test_generate_qgs.py -v
```
Expected: 5 passed.

- [ ] **Step 3: Smoke test against the real data**

```bash
cd /srv/qgis
. .venv/bin/activate
python chart/files/generate_qgs.py data/territories --output /tmp/project.qgs
xmllint --noout /tmp/project.qgs && echo "valid xml"
grep -c '<maplayer ' /tmp/project.qgs
```
Expected:
- `wrote /tmp/project.qgs with 24 layer(s) from 2 gpkg file(s)`
- `valid xml`
- count `24`

(If `xmllint` isn't installed: `python3 -c "import xml.etree.ElementTree as ET; ET.parse('/tmp/project.qgs'); print('valid xml')"`.)

- [ ] **Step 4: Commit**

```bash
cd /srv/qgis
git add chart/files/generate_qgs.py
git commit -m "feat: project.qgs generator (sqlite3 introspection + xml.etree)"
```

---

### Task 12: ConfigMap + Job for the project generator

**Files:**
- Create: `/srv/qgis/chart/templates/configmap-generator.yaml`
- Create: `/srv/qgis/chart/templates/job-project-generator.yaml`

- [ ] **Step 1: Write `chart/templates/configmap-generator.yaml`**

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: project-generator
  namespace: {{ .Values.namespace }}
  labels:
    {{- include "qgis.labels" . | nindent 4 }}
data:
  generate_qgs.py: |-
{{ .Files.Get "files/generate_qgs.py" | indent 4 }}
```

- [ ] **Step 2: Write `chart/templates/job-project-generator.yaml`**

```yaml
{{- if .Values.generator.enabled -}}
apiVersion: batch/v1
kind: Job
metadata:
  name: project-generator-{{ .Release.Revision }}
  namespace: {{ .Values.namespace }}
  labels:
    {{- include "qgis.labels" . | nindent 4 }}
  annotations:
    "helm.sh/hook": post-install,post-upgrade
    "helm.sh/hook-weight": "0"
    "helm.sh/hook-delete-policy": before-hook-creation,hook-succeeded
spec:
  ttlSecondsAfterFinished: 600
  backoffLimit: 1
  template:
    metadata:
      labels:
        {{- include "qgis.selectorLabels" . | nindent 8 }}
        app.kubernetes.io/component: project-generator
    spec:
      restartPolicy: Never
      containers:
        - name: generator
          image: {{ .Values.image.generator }}
          imagePullPolicy: {{ .Values.image.pullPolicy }}
          command:
            - python
            - /scripts/generate_qgs.py
            - /srv/qgis/data
            - --output
            - {{ .Values.projectFile }}
          volumeMounts:
            - name: data
              mountPath: /srv/qgis
            - name: scripts
              mountPath: /scripts
              readOnly: true
          resources:
            {{- toYaml .Values.resources.generator | nindent 12 }}
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: qgis-data
        - name: scripts
          configMap:
            name: project-generator
            defaultMode: 0555
{{- end -}}
```

- [ ] **Step 3: Lint and template**

```bash
cd /srv/qgis
helm lint ./chart
helm template qgis ./chart | grep -E 'kind: Job' -A 1
```
Expected: lint passes; template includes `kind: Job` named `project-generator-1` (the placeholder release revision).

- [ ] **Step 4: Helm upgrade**

```bash
helm upgrade qgis /srv/qgis/chart -n qgis
```
Expected: `STATUS: deployed`. The post-upgrade Job runs immediately; helm waits for it before declaring success.

- [ ] **Step 5: Verify the Job ran and project.qgs exists**

```bash
kubectl -n qgis get jobs
kubectl -n qgis logs -l app.kubernetes.io/component=project-generator --tail=20
ls -la /srv/qgis/project.qgs
head -5 /srv/qgis/project.qgs
```
Expected:
- A Job listed as `Completed`
- Logs end with `wrote /srv/qgis/project.qgs with 24 layer(s) from 2 gpkg file(s)`
- `project.qgs` exists, ~tens of KB
- First lines look like:
  ```
  <?xml version="1.0" encoding="UTF-8"?>
  <qgis version="3.34" projectname="qgis">
    <projectCrs>...
  ```

- [ ] **Step 6: Commit**

```bash
cd /srv/qgis
git add chart/templates/configmap-generator.yaml chart/templates/job-project-generator.yaml
git commit -m "chart: project.qgs generator (configmap + post-install/upgrade Job)"
```

---

## Phase 6 — QGIS Server itself

### Task 13: qgis-server Deployment + Service + middleware

**Files:**
- Create: `/srv/qgis/chart/templates/deployment-qgis-server.yaml`
- Create: `/srv/qgis/chart/templates/service-qgis-server.yaml`
- Create: `/srv/qgis/chart/templates/middleware-strip-ows.yaml`
- Modify: `/srv/qgis/chart/templates/ingress.yaml` (add `/ows` rule + middleware annotation)

- [ ] **Step 1: Write `chart/templates/deployment-qgis-server.yaml`**

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: qgis-server
  namespace: {{ .Values.namespace }}
  labels:
    {{- include "qgis.labels" . | nindent 4 }}
spec:
  replicas: 1
  selector:
    matchLabels:
      {{- include "qgis.selectorLabels" . | nindent 6 }}
      app.kubernetes.io/component: qgis-server
  template:
    metadata:
      labels:
        {{- include "qgis.selectorLabels" . | nindent 8 }}
        app.kubernetes.io/component: qgis-server
    spec:
      containers:
        - name: qgis-server
          image: {{ .Values.image.qgisServer }}
          imagePullPolicy: {{ .Values.image.pullPolicy }}
          ports:
            - name: http
              containerPort: 80
          env:
            - name: QGIS_SERVER_LOG_LEVEL
              value: "0"
            - name: QGIS_PROJECT_FILE
              value: {{ .Values.projectFile | quote }}
          volumeMounts:
            - name: data
              mountPath: /srv/qgis
              readOnly: true
          # Probes hit the pod directly (bypassing Traefik / stripPrefix),
          # so they use the in-pod path "/", not "/ows/".
          readinessProbe:
            httpGet:
              path: /?SERVICE=WMS&REQUEST=GetCapabilities
              port: 80
            initialDelaySeconds: 5
            periodSeconds: 10
            timeoutSeconds: 5
          livenessProbe:
            httpGet:
              path: /?SERVICE=WMS&REQUEST=GetCapabilities
              port: 80
            initialDelaySeconds: 15
            periodSeconds: 30
            timeoutSeconds: 5
          resources:
            {{- toYaml .Values.resources.qgisServer | nindent 12 }}
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: qgis-data
            readOnly: true
```

- [ ] **Step 2: Write `chart/templates/service-qgis-server.yaml`**

```yaml
apiVersion: v1
kind: Service
metadata:
  name: qgis-server
  namespace: {{ .Values.namespace }}
  labels:
    {{- include "qgis.labels" . | nindent 4 }}
spec:
  type: ClusterIP
  selector:
    {{- include "qgis.selectorLabels" . | nindent 4 }}
    app.kubernetes.io/component: qgis-server
  ports:
    - port: 80
      targetPort: 80
      protocol: TCP
```

- [ ] **Step 3: Add a Traefik `stripPrefix` Middleware so qgis-server sees `/?...` instead of `/ows/?...`**

`camptocamp/qgis-server` exposes its FCGI handler at `/`, not `/ows`. Without
prefix stripping, browser requests to `/ows/?SERVICE=WMS&...` arrive at the
pod as `/ows/?...` and 404. The Middleware below strips `/ows` from the path
*before* it hits the qgis-server Service.

Create `chart/templates/middleware-strip-ows.yaml`:

```yaml
apiVersion: traefik.io/v1alpha1
kind: Middleware
metadata:
  name: strip-ows
  namespace: {{ .Values.namespace }}
  labels:
    {{- include "qgis.labels" . | nindent 4 }}
spec:
  stripPrefix:
    prefixes:
      - /ows
```

- [ ] **Step 4: Replace `chart/templates/ingress.yaml` to add the `/ows` rule and reference the middleware**

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: qgis
  namespace: {{ .Values.namespace }}
  labels:
    {{- include "qgis.labels" . | nindent 4 }}
  annotations:
    # Apply strip-ows ONLY for the /ows path. Per-path middlewares aren't
    # supported in stock k8s Ingress, but Traefik's annotation lets us
    # attach middlewares globally to this Ingress; strip-ows only matches
    # paths starting with /ows so it leaves the viewer alone.
    traefik.ingress.kubernetes.io/router.middlewares: {{ .Values.namespace }}-strip-ows@kubernetescrd
spec:
  ingressClassName: {{ .Values.ingress.className }}
  rules:
    - host: {{ .Values.ingress.host }}
      http:
        paths:
          - path: /ows
            pathType: Prefix
            backend:
              service:
                name: qgis-server
                port:
                  number: 80
          - path: /
            pathType: Prefix
            backend:
              service:
                name: viewer
                port:
                  number: 80
```

Note: ordering matters. Specific paths (`/ows`) come before catch-alls (`/`).

- [ ] **Step 5: Lint and helm upgrade**

```bash
cd /srv/qgis
helm lint ./chart
helm upgrade qgis /srv/qgis/chart -n qgis
```
Expected: lint passes; upgrade succeeds. The post-upgrade generator Job re-runs (overwrites `project.qgs`); qgis-server pod starts.

- [ ] **Step 6: Wait for qgis-server to become Ready**

```bash
kubectl -n qgis rollout status deploy/qgis-server --timeout=120s
kubectl -n qgis get pods
```
Expected: `qgis-server-...` pod in `Running` state, `READY 1/1`.

- [ ] **Step 7: Verify WMS GetCapabilities returns the 24 layers**

```bash
curl -s "http://qgis.devbox/ows/?SERVICE=WMS&REQUEST=GetCapabilities" \
  | grep -c '<Layer queryable'
```
Expected: at least `24` (top-level Layer wraps the 24 child Layers, so a higher count is fine — we just want ≥24).

If `qgis.devbox` doesn't resolve from this host, fall back to:
```bash
curl -s -H 'Host: qgis.devbox' \
  "http://192.168.1.70/ows/?SERVICE=WMS&REQUEST=GetCapabilities" \
  | grep -c '<Layer'
```

- [ ] **Step 8: Verify a single-layer GetMap returns a PNG**

```bash
curl -s "http://qgis.devbox/ows/?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap&LAYERS=territories&CRS=EPSG:3857&BBOX=-8975000,4180000,-8950000,4200000&WIDTH=256&HEIGHT=256&FORMAT=image/png" \
  -o /tmp/test.png
file /tmp/test.png
```
Expected: `/tmp/test.png: PNG image data, 256 x 256, ...`.

- [ ] **Step 9: Commit**

```bash
cd /srv/qgis
git add chart/templates/deployment-qgis-server.yaml \
        chart/templates/service-qgis-server.yaml \
        chart/templates/middleware-strip-ows.yaml \
        chart/templates/ingress.yaml
git commit -m "chart: qgis-server deployment, service, ingress with stripPrefix"
```

---

## Phase 7 — End-to-end browser verification

### Task 14: Open the viewer and confirm the full flow

This task involves opening the viewer in a browser. It's manual but each check is concrete.

- [ ] **Step 1: Open `http://qgis.devbox/` in a browser**

(The browser must be on a machine where `qgis.devbox` resolves to `192.168.1.70` — either via the dnsmasq this server provides, or via an `/etc/hosts` entry.)

- [ ] **Step 2: Visual checks**

- [ ] OSM basemap tiles load
- [ ] The "Layers" sidebar appears with two `<details>` groups: **Main** (open) and **Debug** (collapsed)
- [ ] Under Main, `territories` is checked
- [ ] Under Debug, all 23 `step_*` layers appear, all unchecked
- [ ] The map zoom is fitted to the territories bounding box (Union County, NC area)
- [ ] Territory polygons are visible on top of OSM tiles

- [ ] **Step 3: Click any territory polygon**

A popup appears showing a table of attributes including `terr_id`, `addr_count`, `muni_name`, `subdiv`, `area_sqft`, etc.

- [ ] **Step 4: Toggle a debug layer**

Tick the checkbox for `step_500_addresses`. Many small features should render. Untick it; they disappear.

- [ ] **Step 5: Browser console check**

Open dev tools → Console. There should be no errors except possibly OSM tile 429s (rate limiting on heavy use — not a real problem). Any `qgis.devbox/ows/...` errors are real and need investigation.

- [ ] **Step 6: Capture a screenshot to commit (optional but useful as documentation)**

```bash
mkdir -p /srv/qgis/docs/screenshots
# (Take a screenshot via your OS, save to docs/screenshots/viewer.png)
```

- [ ] **Step 7: Commit any screenshots**

```bash
cd /srv/qgis
test -d docs/screenshots && git add docs/screenshots/ && git commit -m "docs: viewer screenshot" || echo "(no screenshots, skipped)"
```

---

## Phase 8 — Verify the pattern is reusable

### Task 15: Deploy a throwaway second service to prove `*.devbox` works for all comers

The success criterion in §12 of the spec includes: "Adding a second, unrelated test service with `host: foo.devbox` makes `http://foo.devbox/` reachable without any DNS or dnsmasq changes."

We do this with one ephemeral manifest, verify, then remove it.

- [ ] **Step 1: Apply a throwaway nginx + service + ingress**

```bash
kubectl create namespace foo
kubectl -n foo create deployment hello --image=nginxdemos/hello:plain-text --port=80
kubectl -n foo expose deployment hello --port=80
kubectl -n foo create ingress foo --class=traefik --rule="foo.devbox/*=hello:80"
kubectl -n foo rollout status deploy/hello --timeout=60s
```

- [ ] **Step 2: Verify it's reachable via the new hostname**

```bash
curl -s http://foo.devbox/ | head -3
# Or via Host header fallback:
# curl -s -H 'Host: foo.devbox' http://192.168.1.70/ | head -3
```
Expected: a small text response from nginxdemos/hello (server hostname, IP, etc.). This confirms wildcard `*.devbox` resolution + Traefik host-based routing both work.

- [ ] **Step 3: Tear it down**

```bash
kubectl delete namespace foo
```
Expected: namespace `foo` not present in `kubectl get ns`.

- [ ] **Step 4: No commit needed**

This was a runtime sanity check; nothing in the repo changed.

---

## Phase 9 — Final polish

### Task 16: Chart README

**Files:**
- Create: `/srv/qgis/chart/README.md`

- [ ] **Step 1: Write `chart/README.md`**

```markdown
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
```

- [ ] **Step 2: Commit**

```bash
cd /srv/qgis
git add chart/README.md
git commit -m "docs: chart README"
```

---

### Task 17: Verify full uninstall/reinstall idempotency

- [ ] **Step 1: Uninstall**

```bash
helm uninstall qgis -n qgis
kubectl delete namespace qgis  # cleans up the stuck PVC
kubectl delete pv qgis-data-pv
```
Expected: all resources gone. `/srv/qgis/data/` and `/srv/qgis/project.qgs` are still present on disk (PV reclaim policy is Retain, but the file was on hostPath all along).

- [ ] **Step 2: Reinstall from scratch**

```bash
helm install qgis /srv/qgis/chart -n qgis --create-namespace
kubectl -n qgis rollout status deploy/qgis-server --timeout=120s
kubectl -n qgis rollout status deploy/viewer --timeout=60s
```
Expected: both rollouts succeed.

- [ ] **Step 3: End-to-end recheck**

```bash
curl -sI http://qgis.devbox/ | head -1
curl -s "http://qgis.devbox/ows/?SERVICE=WMS&REQUEST=GetCapabilities" | grep -c '<Layer'
```
Expected: HTTP `200`, layer count ≥24.

- [ ] **Step 4: No commit needed**

Idempotency verification leaves the repo unchanged.

---

## Self-review checklist (for the implementer, before declaring done)

Verify each spec section maps to at least one completed task:

| Spec section | Plan task(s) |
|---|---|
| §3 Constraints (k3s, Traefik, helm) | Tasks 6, 8, 13 |
| §4 Architecture (diagram, namespace `qgis`) | Tasks 6, 8, 9, 12, 13 |
| §5.1 QGIS Server pod (image, env, RO mount, probes) | Task 13 |
| §5.2 Viewer pod (nginx + Leaflet from CM) | Tasks 7, 8 |
| §5.3 Project generator (sqlite3, ElementTree, RW mount, helm hook) | Tasks 10, 11, 12 |
| §5.4 Ingress (host=qgis.devbox, /ows + /) | Tasks 8, 13 |
| §6 Helm chart layout | Tasks 6, 7, 8, 9, 12, 13, 16 |
| §7 Data flow | Validated by Task 14 |
| §8 Network exposure & auth | Tasks 4, 14 |
| §8.5 dnsmasq prerequisite | Tasks 2, 3, 4, 5 |
| §11 Assumptions (basemap, image, hostname) | Encoded in values.yaml (Task 6) and index.html (Task 7) |
| §12 Success criteria — DNS prerequisite | Task 4 |
| §12 Success criteria — Helm chart install | Tasks 8, 9, 12, 13 |
| §12 Success criteria — Service behavior | Tasks 13, 14 |
| §12 Success criteria — Pattern reusability (`foo.devbox`) | Task 15 |
| §12 Success criteria — Idempotency | Task 17 |

If any spec section is uncovered, add a task before claiming done.
