# Docker installation and daily workflow

> **Без клонирования репозитория** (Portainer, UGREEN, NAS): [DEPLOY_PORTAINER.md](DEPLOY_PORTAINER.md) —
> `compose.deploy.yaml` с готовым образом `ghcr.io/grayhex/s60modern`.

## Quick installation

Requirements: Docker Engine/Desktop with Linux containers and Compose v2. Emulator Lab
additionally needs Linux amd64 capacity, about 3 GiB RAM and your own supported firmware.
From a checkout of E52Modern:

```bash
tools/install.sh --check          # validate Docker and Compose, no changes
tools/install.sh                  # create .env only if missing, build/start, wait for HTTP
# Or include the optional emulator:
tools/install.sh --emulator
```

The installer preserves existing `.env` and named volumes. `.env.example` documents
settings; no passwords/keys are shipped. Default URL is `http://127.0.0.1:8765`.
For a different port set `SYMPORT_HTTP_PORT`; the published/container port, command and
healthcheck change together so Host/Origin validation continues to match.
`tools/install.sh --no-build` starts a previously built image.

Home NAS on a trusted LAN (UGREEN, Synology, Portainer): **Home LAN mode is the default of `compose.deploy.yaml`**; instead of
an SSH tunnel set `SYMPORT_BIND=<NAS LAN IP>` and your own `SYMPORT_ADMIN_PASSWORD` (`SYMPORT_ACCESS_MODE=lan` is preset;
`compose.yaml`/`tools/install.sh` stay localhost-only)
(or `SYMPORT_ADMIN_PASSWORD_FILE` / `SYMPORT_ADMIN_PASSWORD_HASH`). SymPort then serves a login screen at
`https://<nas-ip>:8765/` (HTTPS from SymPort's own local CA by default; `SYMPORT_TLS`, `SYMPORT_TLS_CERT_FILE`/`_KEY_FILE`), accepts only that exact Host/Origin, requires a session (HttpOnly, SameSite=Strict cookie, CSRF
token, idle/absolute expiry, login rate limit) for every API, media and file route, and **refuses to start** on a missing,
weak or default password, on `SYMPORT_BIND=0.0.0.0` without an exact `SYMPORT_LAN_HOST`, or on a loopback bind. With `SYMPORT_TLS=off` (plain HTTP) the password, cookie and phone data are readable on the network and the Windows Bridge
cannot pair; behind an HTTPS reverse proxy (`SYMPORT_PUBLIC_ORIGIN`) the proxy terminates TLS. Install the CA from the login page once
on the browser/Bridge computer (see DEPLOY_PORTAINER.md). Never forward the port on your router.
Step-by-step guide, recovery and switching back: [DEPLOY_PORTAINER.md](DEPLOY_PORTAINER.md#4-ugreen--portainer-home-lan-без-ssh-и-прокси).
Windows Bridge pairing keeps its own short-lived credentials; `/api/bridge-relay/*` never bypasses the login for other routes.

Remote Linux server without Home LAN mode: forward the **same port** over SSH, e.g.
`ssh -L 8765:127.0.0.1:8765 user@server`. Open localhost in your browser. Workbench
and phone-control APIs are not intended for direct public exposure or multi-user hosting.
MGO's agent has no published ports and requires a backend-only bearer secret.

## Phone connected to the Docker host

Explicitly enrol a mounted phone/card directory (not the host root):

```bash
export SYMPORT_PHONE_DIRECTORY=/media/your-user/PHONE
# Container user 10001 must already have the required access to this directory.
docker compose -f compose.yaml -f compose.phone.yaml up -d --build
```

The directory must exist; Compose will not create a missing host path. Only `/phone`
is enrolled. Choose it in Device Manager. Mounted files do not prove phone identity,
installed application state or access to the protected phone system. USB on Windows
is not automatically visible to a remote Linux server.

## Phone connected to Windows / local PC Bridge

Run the existing local authenticated Bridge on the PC with the phone, as documented
in [DEVICE_MANAGER.md](DEVICE_MANAGER.md). For Docker, the SSH forward must live in
Workbench's **network namespace**, not only on the Linux host's unrelated loopback.
The optional overlay supplies this without publishing the Bridge API:

1. Configure an operator-owned SSH key and verify the PC's SSH host fingerprint out
   of band. Windows needs an enabled SSH server reachable through your LAN/private VPN.
2. Set `SYMPORT_SSH_TARGET=user@pc-address`, `SYMPORT_SSH_KEY_FILE` and
   `SYMPORT_SSH_KNOWN_HOSTS` to existing files outside the repository. Set
   `SYMPORT_SSH_UID/GID` to their owner, keeping the private key mode 0600.
3. Set the same 32+ character `SYMPORT_BRIDGE_KEY` on the local Bridge and in private
   `.env`. Do not commit it. The overlay forwards to the local Bridge port 8765.
4. Start:

```bash
docker compose -f compose.yaml -f compose.bridge.yaml up -d --build
```

The SSH sidecar shares Workbench loopback, reads keys read-only, strictly checks
known_hosts and forwards `127.0.0.1:18765` to the PC's `127.0.0.1:8765`. It requires
no privileged container and has no published ports. The image contains an SSH client,
not an operator key. A Windows SSH/phone session was not available for hardware testing;
Compose contract and existing Bridge mock tests cover configuration/HTTP boundaries.
For a PC behind NAT without private connectivity, establish the documented operator
SSH/private-network route first; public unauthenticated phone APIs are not a fallback.

## Logical workflow

1. Choose English/Russian and System/Light/Dark in the toolbar. Preferences persist in
   this browser. Translation catalogs and fonts are local; user data is not sent to a
   translation service. Raw JSON, filenames, certificate subjects and phone data retain
   their original values. Nimbus dark uses navy surfaces and cyan/blue accents.
2. Connect/discover the phone or explicit volume; review capabilities and identity limits.
3. Back up accessible data before mutations. Import contacts, transfer media/packages,
   or inspect a SIS before testing it in Emulator Lab and transferring the same bytes.
4. Use Certificate Inspector recommendations before considering the separate experimental
   workflow. Catalog never enables system patches automatically.

## Updates, diagnostics and data

`git pull` and `tools/install.sh` rebuild/update the selected services. Keep a backup
of named volumes before an upgrade. `docker compose down` preserves data; `down -v`
would erase it and is never run by the installer. Data volumes contain phone backups,
Inspector/Jailbreak/Lab reports and uploaded files; MGO has separate firmware/saves/auth
volumes. Do not copy private uploads into the source tree or image.

Use `docker compose ps`, `docker compose logs workbench`, optional `logs mgo-symbian`
and `logs bridge-tunnel`. A missing transport is shown as unavailable, not a fake device.
No successful physical E52 unlock, full rollback or firmware compatibility is claimed.

Validated locally: installer preflight; isolated installation on custom port 8778 with
container command, published port and healthcheck agreeing; genuine SDK signature via
HTTP; real archive inspection/key exclusion; mock phone staging/rollback; Docker image
with no uploaded executables. The optional Windows SSH link needs the operator's actual
PC/keys/verified host identity and has not been hardware-tested.

## Windows phone connection

Use **Connection → Request access** with the portable
[Windows Bridge](WINDOWS_BRIDGE.md). The agent opens an authenticated outbound
connection to this Workbench; inbound Windows ports and an SSH sidecar are not
needed. The default loopback-bound deployment works through the existing browser
SSH forward; the Windows agent must be able to reach that same localhost origin.
If 8765 is occupied, configure `SYMPORT_HTTP_PORT` and use that same port for the
Workbench, Docker mapping and browser SSH forward so Host validation still matches.
The Windows agent itself listens on a separate port, 24652.

For an HTTPS reverse proxy, explicitly set `SYMPORT_PUBLIC_ORIGIN` to its exact
origin, preserve/forward that Host and Origin, require authentication for the
Workbench UI, and **exempt only `/api/bridge-relay/poll` and
`/api/bridge-relay/result` from proxy login redirects**. Those endpoints require
the short-lived relay credential and never accept anonymous phone control.
Permit long polls (at least 35 seconds), 90 MiB JSON bodies and requests up to
15 minutes for a confirmed PIM batch. The pairing client rejects redirects.
Never expose the internal Docker port or EKA2L1 API publicly. The public-origin
setting is not authentication and does not replace the proxy's access controls.

Linux host-local additional profiles use `SYMPORT_CONNECTIONS_FILE`; bind that
operator-owned JSON and its referenced Gammu configs read-only into Workbench.
The paths in JSON must name the **container** paths. Grant only the exact serial
node you intend to use, or run the existing local Linux PC Bridge on the PC with
the phone. Docker includes Gammu, ObexFTP, BlueZ tools and pyserial, but that alone
does not grant access to a host's USB/Bluetooth stack. Do not use privileged mode
or assume a remote container can discover a phone attached to a different PC.

## Local installation and Ringtone Studio

The checked-in `compose.yaml` publishes only `127.0.0.1`, retains build/workspace
volumes and starts the Workbench without requiring Emulator Lab. FFmpeg/FFprobe
are installed in that same image; no separate audio service, port or API key is
needed. The audio worker runs with per-process CPU/memory/file/time limits.

Linux/macOS: `tools/install.sh`; Windows PowerShell: `./tools/install.ps1`.
`--check` / `-Check` validate Docker and Compose without creating `.env`, starting
services or changing volumes. `--no-build` / `-NoBuild` skip image rebuild;
`--emulator` / `-Emulator` enable the optional existing MGO profile. Updating uses
`git pull` and the same installer; do not use `down -v` for updates.

Audio sources, preview and exports are temporary, private and automatically expire.
Use **Download MP3** to retain a ringtone before updating/restarting. Phone PIM
backups and emulator firmware/results keep their existing persistent storage.

## Image build

The `Dockerfile` orders layers from the least to the most frequently changed one: system packages (apt cache
mounts) → Python dependencies, read from `pyproject.toml` by `tools/docker/requirements.py` → the vendored Nokia
Skin Compiler and `bmconv` → frontend → the application code. Editing a Python or TypeScript file therefore
rebuilds only the last layers. BuildKit is required (`docker buildx` or Docker 23+).

The image runs as user `10001`, has a built-in `HEALTHCHECK` (`/healthz`, the only anonymous endpoint besides the login screen), and carries OCI labels; pass
`--build-arg VERSION=… --build-arg REVISION=…` (Compose: `SYMPORT_VERSION`, `SYMPORT_REVISION`) to stamp them.
Compose limits container logs to 3 × 10 MiB. Behind a corporate proxy, pass the CA bundle with
`--secret id=proxy_ca,src=/path/ca.pem`. Proxmox: [DEPLOY_PROXMOX.md](DEPLOY_PROXMOX.md).

## Frontend build

Docker builds React 19 + strict TypeScript with Node 26 (LTS) in a separate stage, then
copies the static assets into the Python runtime. No Node service or extra port is
needed. `/` opens React; `/classic` is the transition rollback entry. Set
`SYMPORT_FRONTEND=classic` in `.env` and recreate Workbench to select the previous
entry globally. API, Bridge and persisted phone data stay in place.
