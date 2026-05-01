# Top-level make targets for the qgis web stack.
#
# - make client          : build the qwc2 dist into client/qwc2-demo-app/prod/
# - make install-client  : build, then sync into /srv/qgis/web/  (requires sudo)
# - make clean-client    : delete the build output directory
# - make test            : run the python test suite

CLIENT_DIR  := $(CURDIR)/client
QWC2_DIR    := $(CLIENT_DIR)/qwc2-demo-app
OVERLAY_DIR := $(CLIENT_DIR)/overlay
DIST_DIR    := $(QWC2_DIR)/prod
WEB_DIR     := /srv/qgis/web
NODE_IMAGE  := node:22
NODE_RUNNER := docker

UID := $(shell id -u)
GID := $(shell id -g)

.PHONY: client install-client clean-client test

client:
	rsync -av --no-perms --no-owner --no-group $(OVERLAY_DIR)/ $(QWC2_DIR)/
	$(NODE_RUNNER) run --rm \
	    -v $(QWC2_DIR):/work -w /work \
	    -u $(UID):$(GID) \
	    -e HOME=/tmp \
	    $(NODE_IMAGE) \
	    bash -c "yarn install --frozen-lockfile && yarn build"

install-client: client
	sudo install -d $(WEB_DIR)
	sudo rsync -av --delete-during $(DIST_DIR)/ $(WEB_DIR)/

clean-client:
	rm -rf $(DIST_DIR) $(QWC2_DIR)/node_modules

test:
	.venv/bin/pytest tests/ -v
