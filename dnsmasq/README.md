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
