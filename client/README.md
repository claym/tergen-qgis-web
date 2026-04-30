# client/

QWC2 web client build.

- `qwc2-demo-app/` — vendored upstream from https://github.com/qgis/qwc2-demo-app via
  `git subtree`. **Never edit files in this directory by hand.**
- `overlay/` — our customizations. Files here are rsynced *over* the vendored tree
  at build time. Currently:
  - `static/config.json` — qwc-services URLs blanked (we don't run any), plugin
    list trimmed to read-only, EPSG:2264 added to known projections.

## Build

From the repo root:

```
make install-client       # build dist + sync to /srv/qgis/web/  (requires sudo + docker)
```

Dev iteration on `overlay/static/config.json`:

```
# edit overlay/static/config.json
make install-client
# hard-refresh browser
```

## Update qwc2 from upstream

```
git subtree pull --prefix=client/qwc2-demo-app \
    https://github.com/qgis/qwc2-demo-app master --squash
make install-client
# smoke-test in browser; fix overlay schema drift if needed
```
