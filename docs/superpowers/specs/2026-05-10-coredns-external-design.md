# External DNS via CoreDNS on k3s

**Date:** 2026-05-10
**Status:** Approved

## Problem

`*.devbox` hostnames (e.g. `qgis.devbox`) resolve on the local LAN via the existing DNS servers
at 192.168.1.86/192.168.1.62, but Tailscale peers on remote networks have no path to those
servers and cannot resolve the names. The previous solution (dnsmasq bound to both the LAN IP
and the Tailscale IP) failed on 2026-05-04 due to a startup ordering race with Tailscale, and
has been down since.

## Goal

`*.devbox` resolves to `100.117.43.89` (devbox's Tailscale IP) for all Tailscale peers,
enabling remote devices to reach `qgis.devbox` (and any future `*.devbox` service) over
Tailscale without DNS changes on the client.

## Solution

Deploy a standalone CoreDNS Helm chart at `/srv/dns` as a Kubernetes DaemonSet on the k3s
cluster. Configure Tailscale split DNS to route `.devbox` queries to `100.117.43.89`.
Disable dnsmasq.

## Architecture

### Chart location

```
/srv/dns/
├── Chart.yaml
├── values.yaml
└── templates/
    ├── namespace.yaml
    ├── configmap.yaml
    ├── daemonset.yaml
    └── _helpers.tpl
```

Deployed independently from the `qgis` chart into a `dns` namespace.

### DaemonSet

- `hostNetwork: true` — pod binds directly to host network interfaces; no Service needed
- `dnsPolicy: ClusterFirstWithHostNet` — pod's own DNS still works correctly
- `nodeSelector: kubernetes.io/os: linux`
- Tolerates control-plane taint so it runs on single-node k3s
- Liveness/readiness probes via CoreDNS `health` plugin on port 8080

### Corefile

```
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
}
```

Both zones bind only to `100.117.43.89`, avoiding any conflict with systemd-resolved on
loopback (127.0.0.53). Wildcard `*.devbox` A queries return `100.117.43.89`. AAAA queries
return NOERROR with no records. All other queries forward to Cloudflare resolvers.

### values.yaml

`tailscaleIP: 100.117.43.89` — used throughout templates so the IP is not hardcoded.

## Tailscale wiring (manual, one-time)

In the Tailscale admin console:
DNS → Nameservers → Add nameserver `100.117.43.89` restricted to domain `devbox`

This routes all `.devbox` queries from every tailnet device to the CoreDNS DaemonSet.

## Cutover

1. Deploy the chart: `helm upgrade --install dns /srv/dns -n dns --create-namespace`
2. Verify: `dig @100.117.43.89 qgis.devbox` returns `100.117.43.89`
3. Add Tailscale split DNS nameserver (admin console)
4. Verify from iPad: browse to `http://qgis.devbox`
5. Disable dnsmasq: `systemctl disable --now dnsmasq`

## Why DaemonSet over Deployment

A DaemonSet is semantically correct for host-level network services — one instance per node.
On a single-node cluster it behaves identically to a Deployment, but scales naturally if
additional nodes are added without chart changes.

## What is not in scope

- DNS for anything other than `*.devbox`
- Modifying the existing k3s CoreDNS (kube-dns) ConfigMap
- Exposing any port other than 53 (UDP/TCP) externally
