#!/usr/bin/env bash
# Creates a Debian LXC container on a Proxmox VE host, installs Docker in it and starts SymPort from the
# published image (compose.deploy.yaml). Run it as root on the Proxmox host:
#
#   bash create-lxc.sh                       # defaults: next free CTID, local-lvm, vmbr0, DHCP
#   bash create-lxc.sh --ctid 120 --storage local-zfs --bridge vmbr1 --memory 3072
#   bash create-lxc.sh --dry-run             # print every command without running it
#
# The web UI stays bound to the container's loopback; reach it through an SSH tunnel or an authenticated
# HTTPS reverse proxy (docs/DEPLOY_PROXMOX.md). Nothing is exposed to the LAN by this script.
set -euo pipefail

CTID=""
HOSTNAME_CT="symport"
STORAGE="local-lvm"
TEMPLATE_STORAGE="local"
BRIDGE="vmbr0"
MEMORY=2048
SWAP=512
CORES=2
DISK=12
IMAGE="ghcr.io/grayhex/s60modern:latest"
COMPOSE_URL="https://raw.githubusercontent.com/grayhex/s60modern/main/compose.deploy.yaml"
DRY=0

usage() { sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; }
while [ $# -gt 0 ]; do
  case "$1" in
    --ctid) CTID="$2"; shift 2 ;;
    --hostname) HOSTNAME_CT="$2"; shift 2 ;;
    --storage) STORAGE="$2"; shift 2 ;;
    --template-storage) TEMPLATE_STORAGE="$2"; shift 2 ;;
    --bridge) BRIDGE="$2"; shift 2 ;;
    --memory) MEMORY="$2"; shift 2 ;;
    --cores) CORES="$2"; shift 2 ;;
    --disk) DISK="$2"; shift 2 ;;
    --image) IMAGE="$2"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

for value in "$MEMORY" "$CORES" "$DISK"; do
  case "$value" in ''|*[!0-9]*) echo "Memory, cores and disk must be whole numbers" >&2; exit 2 ;; esac
done
case "$HOSTNAME_CT" in *[!a-zA-Z0-9-]*|'') echo "Hostname may contain letters, digits and dashes only" >&2; exit 2 ;; esac

run() {
  if [ "$DRY" -eq 1 ]; then printf '+'; printf ' %q' "$@"; printf '\n'; else "$@"; fi
}

if [ "$DRY" -eq 0 ]; then
  command -v pct >/dev/null || { echo "pct not found: run this script on a Proxmox VE host" >&2; exit 1; }
  [ "$(id -u)" -eq 0 ] || { echo "Run as root" >&2; exit 1; }
  [ -n "$CTID" ] || CTID="$(pvesh get /cluster/nextid)"
  if pct status "$CTID" >/dev/null 2>&1; then echo "Container $CTID already exists" >&2; exit 1; fi
else
  [ -n "$CTID" ] || CTID="<next-free-id>"
fi

echo "==> Debian template"
run pveam update
if [ "$DRY" -eq 1 ]; then
  TEMPLATE="debian-12-standard_<latest>_amd64.tar.zst"
else
  TEMPLATE="$(pveam available --section system | awk '/debian-12-standard/ {print $2}' | sort -V | tail -n1)"
  [ -n "$TEMPLATE" ] || { echo "No Debian 12 template found in 'pveam available'" >&2; exit 1; }
fi
if [ "$DRY" -eq 1 ] || ! pveam list "$TEMPLATE_STORAGE" | grep -q "$TEMPLATE"; then
  run pveam download "$TEMPLATE_STORAGE" "$TEMPLATE"
fi

echo "==> Container $CTID ($HOSTNAME_CT)"
# Unprivileged container; nesting and keyctl are what Docker needs inside an LXC.
run pct create "$CTID" "$TEMPLATE_STORAGE:vztmpl/$TEMPLATE" \
  --hostname "$HOSTNAME_CT" --ostype debian --unprivileged 1 \
  --cores "$CORES" --memory "$MEMORY" --swap "$SWAP" \
  --rootfs "$STORAGE:$DISK" \
  --net0 "name=eth0,bridge=$BRIDGE,ip=dhcp,firewall=1" \
  --features nesting=1,keyctl=1 --onboot 1 --start 0
run pct start "$CTID"
[ "$DRY" -eq 1 ] || sleep 8

echo "==> Docker Engine and Compose"
run pct exec "$CTID" -- bash -ec '
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y --no-install-recommends ca-certificates curl gnupg
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  . /etc/os-release
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $VERSION_CODENAME stable" > /etc/apt/sources.list.d/docker.list
  apt-get update
  apt-get install -y --no-install-recommends docker-ce docker-ce-cli containerd.io docker-compose-plugin
  systemctl enable --now docker
'

echo "==> SymPort"
run pct exec "$CTID" -- bash -ec "
  install -d /opt/symport && cd /opt/symport
  curl -fsSL '$COMPOSE_URL' -o compose.yaml
  printf 'SYMPORT_IMAGE=%s\nSYMPORT_ACCESS_MODE=localhost\nSYMPORT_BIND=127.0.0.1\n' '$IMAGE' > .env
  docker compose up -d
"

cat <<MSG

SymPort is starting in container $CTID. It listens on the container's loopback only (127.0.0.1:8765).

Open it through an SSH tunnel from your computer:
  ssh -L 8765:127.0.0.1:8765 root@<container-ip>      # then browse to http://127.0.0.1:8765
(set a root password or add your SSH key first: pct enter $CTID)
For Home LAN access instead, edit /opt/symport/.env: SYMPORT_ACCESS_MODE=lan, SYMPORT_BIND=<container LAN IP>,
SYMPORT_ADMIN_PASSWORD=<your own secret>, then 'docker compose up -d' (docs/DEPLOY_PORTAINER.md).

Update later:   pct exec $CTID -- bash -c 'cd /opt/symport && docker compose pull && docker compose up -d'
Logs:           pct exec $CTID -- docker compose -f /opt/symport/compose.yaml logs -f
Remote access with HTTPS and authentication: docs/DEPLOY_PROXMOX.md
MSG
