#!/usr/bin/env bash
# GCE startup script: bring a bare Debian e2-micro up to a running instance of
# the app. Idempotent — safe to re-run, and it runs again on every boot.
set -euo pipefail

REPO_URL="https://github.com/c-m-hunt/SouthendTicketing.git"
# The ktckts rewrite lives on update-2026; master is still the old site.
REPO_BRANCH="${REPO_BRANCH:-update-2026}"
APP_DIR="/opt/southend-ticketing"

log() { echo "[startup] $*"; }

# e2-micro has 1GB of RAM, which a Docker build can exhaust. A swap file costs
# nothing on the 30GB disk and turns an OOM kill into merely a slow build.
if [ ! -f /swapfile ]; then
	log "creating 2G swapfile"
	fallocate -l 2G /swapfile
	chmod 600 /swapfile
	mkswap /swapfile
	swapon /swapfile
	echo '/swapfile none swap sw 0 0' >>/etc/fstab
fi

if ! command -v docker >/dev/null 2>&1; then
	log "installing docker"
	export DEBIAN_FRONTEND=noninteractive
	apt-get update -qq
	apt-get install -y -qq ca-certificates curl git
	install -m 0755 -d /etc/apt/keyrings
	curl -fsSL https://download.docker.com/linux/debian/gpg \
		-o /etc/apt/keyrings/docker.asc
	chmod a+r /etc/apt/keyrings/docker.asc
	echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
		>/etc/apt/sources.list.d/docker.list
	apt-get update -qq
	apt-get install -y -qq docker-ce docker-ce-cli containerd.io \
		docker-buildx-plugin docker-compose-plugin
	systemctl enable --now docker
fi

if [ -d "$APP_DIR/.git" ]; then
	log "updating existing checkout to $REPO_BRANCH"
	git -C "$APP_DIR" fetch --quiet origin "$REPO_BRANCH"
	git -C "$APP_DIR" reset --hard --quiet "origin/$REPO_BRANCH"
else
	log "cloning $REPO_URL ($REPO_BRANCH)"
	git clone --quiet --branch "$REPO_BRANCH" "$REPO_URL" "$APP_DIR"
fi

cd "$APP_DIR/deploy"
log "starting stack"
docker compose -f compose.prod.yaml up -d --build
log "done"
