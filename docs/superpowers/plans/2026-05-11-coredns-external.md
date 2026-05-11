# CoreDNS External DNS Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the broken dnsmasq with a CoreDNS DaemonSet in k3s that resolves `*.devbox` → `100.117.43.89` for all Tailscale peers.

**Architecture:** Standalone Helm chart at `/srv/dns`, deployed to a `dns` namespace. CoreDNS runs as a `hostNetwork` DaemonSet bound to `100.117.43.89` only, serving the `devbox` zone via the `template` plugin and forwarding all other queries to Cloudflare. Tailscale split DNS routes `.devbox` queries from all tailnet devices to `100.117.43.89:53`.

**Tech Stack:** Helm 3.20, CoreDNS 1.14.2, k3s v1.34.6

**Note:** All `kubectl` commands require `KUBECONFIG=~/.kube/config kubectl` on this host. `helm` works without it. Consider `export KUBECONFIG=~/.kube/config` at the start of your session.

---

## File Map

| Path | Action | Purpose |
|------|--------|---------|
| `/srv/dns/Chart.yaml` | Create | Chart metadata |
| `/srv/dns/values.yaml` | Create | Tailscale IP, image, resources |
| `/srv/dns/templates/_helpers.tpl` | Create | Shared label helper |
| `/srv/dns/templates/configmap.yaml` | Create | Corefile ConfigMap |
| `/srv/dns/templates/daemonset.yaml` | Create | CoreDNS DaemonSet |
| `/srv/gis/tergen-qgis-web/chart/values.yaml` | Modify | Update comment referencing dnsmasq |

---

### Task 1: Initialise the chart scaffold

**Files:**
- Create: `/srv/dns/Chart.yaml`
- Create: `/srv/dns/values.yaml`
- Create: `/srv/dns/templates/_helpers.tpl`

- [ ] **Step 1: Create directory tree and Chart.yaml**

```bash
mkdir -p /srv/dns/templates
```

Create `/srv/dns/Chart.yaml`:

```yaml
apiVersion: v2
name: dns
description: External DNS server resolving *.devbox to the Tailscale IP via CoreDNS
type: application
version: 0.1.0
appVersion: "1.14.2"
maintainers:
  - name: clay
    email: clay@pfd.net
```

- [ ] **Step 2: Create values.yaml**

Create `/srv/dns/values.yaml`:

```yaml
namespace: dns

# Tailscale IP of this node. CoreDNS binds to this address and all
# *.devbox names resolve to it, so remote Tailscale peers get a routable address.
tailscaleIP: 100.117.43.89

image:
  repository: coredns/coredns
  tag: "1.14.2"
  pullPolicy: IfNotPresent

resources:
  requests:
    cpu: 10m
    memory: 32Mi
  limits:
    cpu: 100m
    memory: 64Mi
```

- [ ] **Step 3: Create _helpers.tpl**

Create `/srv/dns/templates/_helpers.tpl`:

```
{{- define "dns.labels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion }}
{{- end -}}
```

- [ ] **Step 4: Lint the skeleton**

```bash
helm lint /srv/dns
```

Expected:
```
==> Linting /srv/dns
[INFO] Chart.yaml: icon is recommended

1 chart(s) linted, 0 chart(s) failed
```

- [ ] **Step 5: Initialise git repo and commit**

```bash
cd /srv/dns
git init
git add Chart.yaml values.yaml templates/_helpers.tpl
git commit -m "chore: initialise dns helm chart scaffold"
```

---

### Task 2: CoreDNS ConfigMap (Corefile)

**Files:**
- Create: `/srv/dns/templates/configmap.yaml`

- [ ] **Step 1: Create configmap.yaml**

Create `/srv/dns/templates/configmap.yaml`:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: coredns
  namespace: {{ .Values.namespace }}
  labels:
    {{- include "dns.labels" . | nindent 4 }}
data:
  Corefile: |
    devbox {
        bind {{ .Values.tailscaleIP }}
        template IN A {
            match ^.*\.devbox\.$
            answer "{{ "{{" }} .Name {{ "}}" }} 60 IN A {{ .Values.tailscaleIP }}"
        }
        template IN AAAA {
            match ^.*\.devbox\.$
            rcode NOERROR
        }
        errors
    }
    . {
        bind {{ .Values.tailscaleIP }}
        forward . 1.1.1.1 1.0.0.1
        cache 300
        errors
        health
        ready
    }
```

The `{{ "{{" }} .Name {{ "}}" }}` escaping produces a literal `{{ .Name }}` in the rendered Corefile, which is CoreDNS template plugin syntax (not Helm). Both zones bind only to `100.117.43.89`, avoiding any conflict with systemd-resolved on loopback. The `health` plugin listens on `:8080` and `ready` on `:8181` for liveness/readiness probes.

- [ ] **Step 2: Render and verify the Corefile**

```bash
helm template dns /srv/dns | grep -A 30 "Corefile:"
```

Expected output (the `{{ .Name }}` must appear literally, not blank):

```
  Corefile: |
    devbox {
        bind 100.117.43.89
        template IN A {
            match ^.*\.devbox\.$
            answer "{{ .Name }} 60 IN A 100.117.43.89"
        }
        template IN AAAA {
            match ^.*\.devbox\.$
            rcode NOERROR
        }
        errors
    }
    . {
        bind 100.117.43.89
        forward . 1.1.1.1 1.0.0.1
        cache 300
        errors
        health
        ready
    }
```

- [ ] **Step 3: Commit**

```bash
cd /srv/dns
git add templates/configmap.yaml
git commit -m "feat: add CoreDNS Corefile ConfigMap"
```

---

### Task 3: CoreDNS DaemonSet

**Files:**
- Create: `/srv/dns/templates/daemonset.yaml`

- [ ] **Step 1: Create daemonset.yaml**

Create `/srv/dns/templates/daemonset.yaml`:

```yaml
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: coredns
  namespace: {{ .Values.namespace }}
  labels:
    {{- include "dns.labels" . | nindent 4 }}
spec:
  selector:
    matchLabels:
      app: coredns
  updateStrategy:
    type: RollingUpdate
  template:
    metadata:
      labels:
        app: coredns
        {{- include "dns.labels" . | nindent 8 }}
      annotations:
        checksum/config: {{ include (print $.Template.BasePath "/configmap.yaml") . | sha256sum }}
    spec:
      hostNetwork: true
      dnsPolicy: ClusterFirstWithHostNet
      nodeSelector:
        kubernetes.io/os: linux
      tolerations:
        - key: node-role.kubernetes.io/control-plane
          operator: Exists
          effect: NoSchedule
        - key: node-role.kubernetes.io/master
          operator: Exists
          effect: NoSchedule
      containers:
        - name: coredns
          image: {{ .Values.image.repository }}:{{ .Values.image.tag }}
          imagePullPolicy: {{ .Values.image.pullPolicy }}
          args:
            - -conf
            - /etc/coredns/Corefile
          ports:
            - name: dns-udp
              containerPort: 53
              protocol: UDP
            - name: dns-tcp
              containerPort: 53
              protocol: TCP
            - name: health
              containerPort: 8080
              protocol: TCP
            - name: ready
              containerPort: 8181
              protocol: TCP
          livenessProbe:
            httpGet:
              path: /health
              port: 8080
            initialDelaySeconds: 10
            periodSeconds: 30
            failureThreshold: 3
          readinessProbe:
            httpGet:
              path: /ready
              port: 8181
            initialDelaySeconds: 5
            periodSeconds: 10
          resources:
            {{- toYaml .Values.resources | nindent 12 }}
          securityContext:
            allowPrivilegeEscalation: false
            capabilities:
              drop:
                - ALL
              add:
                - NET_BIND_SERVICE
            readOnlyRootFilesystem: true
          volumeMounts:
            - name: config
              mountPath: /etc/coredns
              readOnly: true
      volumes:
        - name: config
          configMap:
            name: coredns
            items:
              - key: Corefile
                path: Corefile
```

`NET_BIND_SERVICE` is required to bind port 53 (privileged). `checksum/config` rolls the DaemonSet pods whenever the Corefile changes.

- [ ] **Step 2: Lint the complete chart**

```bash
helm lint /srv/dns
```

Expected:
```
==> Linting /srv/dns
[INFO] Chart.yaml: icon is recommended

1 chart(s) linted, 0 chart(s) failed
```

- [ ] **Step 3: Full template render — spot-check DaemonSet fields**

```bash
helm template dns /srv/dns | grep -E "hostNetwork|dnsPolicy|NET_BIND_SERVICE|100\.117\.43\.89"
```

Expected (all four lines present):
```
      hostNetwork: true
      dnsPolicy: ClusterFirstWithHostNet
                - NET_BIND_SERVICE
        bind 100.117.43.89
```

- [ ] **Step 4: Commit**

```bash
cd /srv/dns
git add templates/daemonset.yaml
git commit -m "feat: add CoreDNS DaemonSet"
```

---

### Task 4: Deploy and verify DNS resolution

- [ ] **Step 1: Deploy the chart**

```bash
helm upgrade --install dns /srv/dns --namespace dns --create-namespace
```

Expected:
```
Release "dns" does not exist. Installing it now.
NAME: dns
LAST DEPLOYED: ...
NAMESPACE: dns
STATUS: deployed
REVISION: 1
```

- [ ] **Step 2: Wait for DaemonSet to be ready**

```bash
KUBECONFIG=~/.kube/config kubectl rollout status daemonset/coredns -n dns
```

Expected:
```
daemon set "coredns" successfully rolled out
```

- [ ] **Step 3: Confirm pod is running and check logs for errors**

```bash
KUBECONFIG=~/.kube/config kubectl get pods -n dns
KUBECONFIG=~/.kube/config kubectl logs -n dns -l app=coredns --tail=20
```

Expected: one pod in `Running` state; logs show CoreDNS started without errors:
```
.:53
devbox.:53
[INFO] plugin/ready: Going to report readiness to 8181
[INFO] Starting server ...
```

- [ ] **Step 4: Verify wildcard A resolution**

```bash
dig @100.117.43.89 qgis.devbox A +short
dig @100.117.43.89 anything.devbox A +short
```

Expected: both return `100.117.43.89`

- [ ] **Step 5: Verify AAAA returns NOERROR with no records**

```bash
dig @100.117.43.89 qgis.devbox AAAA
```

Expected: `status: NOERROR`, no ANSWER section records.

- [ ] **Step 6: Verify forwarding for non-devbox names**

```bash
dig @100.117.43.89 google.com A +short
```

Expected: one or more public IPs (forwarded via Cloudflare).

---

### Task 5: Tailscale split DNS (manual) and cutover

- [ ] **Step 1: Add Tailscale split DNS nameserver**

In the Tailscale admin console (`https://login.tailscale.com/admin/dns`):
- Nameservers → **Add nameserver**
- Address: `100.117.43.89`
- Restrict to domain: `devbox`
- Save

- [ ] **Step 2: Verify resolution from a Tailscale peer**

From another device on the tailnet (e.g. clay-mbp), run:

```bash
dig qgis.devbox A +short
```

Expected: `100.117.43.89`

If using the iPad, browse to `http://qgis.devbox` — the QGIS Web Client should load.

- [ ] **Step 3: Disable dnsmasq**

Back on devbox:

```bash
systemctl disable --now dnsmasq
```

Expected:
```
Removed "/etc/systemd/system/multi-user.target.wants/dnsmasq.service".
```

---

### Task 6: Update qgis chart comment and commit plan

**Files:**
- Modify: `/srv/gis/tergen-qgis-web/chart/values.yaml:10`

- [ ] **Step 1: Update stale dnsmasq comment in qgis values.yaml**

In `/srv/gis/tergen-qgis-web/chart/values.yaml`, change line 10 from:
```yaml
  # Hostname must resolve to the cluster's external IP.
  # Wildcard *.devbox resolution is handled by the host's dnsmasq.
```
to:
```yaml
  # Hostname must resolve to the cluster's external IP.
  # Wildcard *.devbox resolution is served by the dns Helm chart (/srv/dns).
```

- [ ] **Step 2: Commit**

```bash
cd /srv/gis/tergen-qgis-web
git add chart/values.yaml
git commit -m "docs: update values comment — dnsmasq replaced by CoreDNS chart"
```

- [ ] **Step 3: Commit the plan**

```bash
cd /srv/gis/tergen-qgis-web
git add docs/superpowers/plans/2026-05-11-coredns-external.md
git commit -m "docs: add CoreDNS external DNS implementation plan"
```
