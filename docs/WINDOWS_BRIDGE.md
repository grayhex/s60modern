# SymPort Bridge for Windows

A portable Python-based Windows x64 tray agent. Device operations use the same
DeviceManager, Gammu, file, contact and backup implementations as Linux SymPort.
No Electron, firmware flashing, ROM, Nokia drivers or applet binaries are included.

## Install and connect

1. Download **SymPortBridge-Setup-x.y.z.exe** and `SHA256SUMS.txt` from the
   [Releases page](https://github.com/grayhex/s60modern/releases) (tags `bridge-vX.Y.Z`) and
   compare the hash (`Get-FileHash .\SymPortBridge-Setup-*.exe`). The installer is **per-user**:
   no administrator rights, no drivers, installed to `%LOCALAPPDATA%\Programs\SymPort Bridge`
   with a Start-menu entry and an optional sign-in autostart. Settings and the pairing key live in
   `%LOCALAPPDATA%\SymPortBridge` and survive updates and (by default) uninstalling.
2. Run it. Builds are **not code-signed** (no paid certificate), so Windows SmartScreen shows
   “Windows protected your PC / unknown publisher”: choose **More info → Run anyway** only after
   the SHA-256 matches. The portable `SymPortBridge-portable-x64.zip` remains available.
3. Connect the Nokia USB cable, select **PC Suite** and install a trusted Nokia
   connectivity driver separately if no COM port appears. Close Nokia Suite when
   it holds that same COM port. Mass Storage only exposes an accessible card.
4. Open **Connection** in SymPort → **Request access**. Allow local-network access
   in the browser and approve the exact SymPort origin in the Windows dialog.
5. **Find phone** → select the connection. All phone screens continue to use
   their existing UI. An outbound HTTPS relay also serves Catalog, Inspector,
   Emulator Lab transfer and authenticated media downloads through the backend.

The first-connection wizard (**Connection**) drives these steps and explains each
failure. It first sends a harmless `POST /api/bridge/pair` with `{"origin", "probe": true}`:
the Bridge answers with its version and whether pairing is available, opens no dialog and
changes no state; any other request still needs the local approval. Host enumeration
(`connection-host`) additionally returns USB vendor/product/class/status of Nokia devices (no
serial numbers or paths), the Bridge version, OS family/release and whether python-gammu is
present, so “nothing plugged in / charge-only cable” is told apart from “device seen, driver
or COM port missing”. Gammu failures are reduced to fixed reasons (`port-busy`, `port-open`,
`port-missing`, `port-permission`, `driver-missing`, `no-response`, `gammu-missing`,
`config-invalid`, `unsupported-model`); raw Gammu/OS text is never forwarded.

Use a current Chromium browser for local-network permission prompts. If browser
policy blocks loopback access, use the existing authenticated SSH PC Bridge route
in [DEVICE_MANAGER.md](DEVICE_MANAGER.md). The Workbench must be reachable by the
Windows PC. HTTP is allowed only for localhost; remote Workbench requires HTTPS. SymPort in Home LAN mode serves HTTPS itself: the Bridge pins SymPort's own certificate when you approve the pairing dialog (it shows the fingerprint), so nothing needs to be installed for the Bridge; install the CA (login page link) only to silence the browser warning (docs/DEPLOY_PORTAINER.md). If the Bridge cannot connect, the wizard and tray Diagnostics name the reason.
Keep the remote deployment behind authentication; the default Docker port stays
bound to host loopback. Set `SYMPORT_PUBLIC_ORIGIN=https://your-symport.example`
only when using a trusted authenticated HTTPS reverse proxy.

Access lasts two hours, is kept in memory and is revoked by **Disconnect Bridge**,
**Revoke web access** in the tray, or exit. Restarting the agent requires pairing
again. The relay never retries a claimed phone operation automatically. If a
connection fails during a write, read the phone before repeating the operation.

## Updates

An installed Bridge checks GitHub Releases for newer `bridge-vX.Y.Z` releases once a day (Settings →
*Check for updates*, on by default; tray menu → **Check for updates…** any time). It **only
notifies**; it downloads and installs **nothing without your confirmation of the exact version**.
After you confirm, it downloads the installer, verifies size and SHA-256, waits until no phone
operation is running (a running operation blocks the update), then runs the installer silently:
the old copy is closed, files are replaced in place, settings are kept and the new version
starts. A portable copy cannot update itself and points to the Releases page.

Safeguards: only this project's GitHub release hosts and exactly `bridge-vX.Y.Z` URLs are accepted;
the manifest (`bridge-update.json`) must describe its own release; no downgrade; a mismatched
size/hash deletes the download. Optional Ed25519 signature: once `UPDATE_PUBLIC_KEY`
(`symport/bridge/update.py`) is set, unsigned or wrongly signed manifests are rejected even if a
release asset were replaced. Key setup (free, once):

```
python tools/windows/update_key.py generate
# put the PUBLIC value into UPDATE_PUBLIC_KEY and commit it;
# store the PRIVATE seed as the GitHub Actions secret BRIDGE_UPDATE_SIGNING_KEY
```

Releasing: bump `symport/bridge/__init__.py` `VERSION`, tag `bridge-vX.Y.Z` and push the tag; the
workflow builds, tests the installer (fresh install, update over a *running* copy, data kept,
uninstall, `/REMOVEDATA=1`) and publishes installer, portable zip, `SHA256SUMS.txt` and the manifest.
Rollback: install an older `SymPortBridge-Setup-x.y.z.exe` from Releases manually (Windows allows
installing over, the installer itself does not downgrade automatically).

**Code signing without a paid certificate.** Authenticode signing is what removes the SmartScreen
warning; reputation also builds over time. Free options for open-source projects: apply to the
[SignPath Foundation](https://signpath.org/) programme, or Azure Trusted Signing (paid, low cost).
Neither is configured; until then the checksum and the optional update signature above are the
integrity guarantees, and the warning is expected.

Uninstall via *Settings → Apps* (asks whether to delete settings and the pairing key; silent:
`unins000.exe /VERYSILENT /REMOVEDATA=1`). No Nokia drivers are installed or removed.

## Tray and configuration

Tray **Settings**: automatic Nokia USB discovery (default on), optional current-user
Windows sign-in autostart (default off), Gammu configuration and connection-profile
JSON. **Diagnostics** opens the *Driver & Connection Doctor* (below). Operation logs contain action,
time and status only. Settings/private backups live in `%LOCALAPPDATA%\SymPortBridge`.
Full-volume backup jobs in the development tools still operate on volumes enrolled
on the server. For Bridge use the existing selected-folder backup and PIM backups;
Mass Storage never provides SMS or phonebook access.

The long-term SSH bridge key is protected with current-user Windows DPAPI and is
copied only by the explicit **Copy Bridge key** action.

Automatic serial discovery filters Nokia VID `0421`/Nokia host descriptions, then
requires a real Gammu manufacturer/model response matching a documented S60 model.
S40 and unknown models are not promoted to supported phones. S60 edition is a
model reference, not an OS version measured over AT. Memory-card discovery requires
Windows CIM associations back to a Nokia USB disk; writes recheck physical identity.
Unsupported counters remain absent. Disconnects are polled; no pairing/driver changes
or SMS are performed automatically.

Bluetooth must be paired in Windows first. Use an operator-enrolled profile for
Gammu Bluetooth/S60 Remote or ObexFTP; see [TRANSPORTS.md](TRANSPORTS.md). ObexFTP
is an external optional dependency and is **not** bundled in the Windows build.
An absent executable produces a connection diagnostic, not a supported capability.
The Gammu wheel's supported transports depend on its actual build/driver and must
be tested on the host; no universal Windows USB/Bluetooth compatibility is promised.

## Driver & Connection Doctor (tray → Diagnostics)

A native window that runs as its own process, so **OK/Close, Esc, Enter and the window's X always close it**, a second
click never stacks a second window, and the tray menu, discovery and the SymPort link keep working while it is open.
It reads the safe report the running Bridge keeps current and shows **separate** statuses, each with the exact next step
(English/Russian by the Windows UI language):

| Status | Meaning | What to do |
|---|---|---|
| `no-usb` | Windows sees no Nokia on USB | data cable (not charge-only), unlock the phone, choose PC Suite, another port, close Nokia Suite |
| `usb-driver-problem` | Nokia on USB, Device Manager reports a problem | open Device Manager (button); the Nokia PC Suite / Connectivity driver is needed — install it yourself from Nokia/Microsoft |
| `mtp-files` | Windows lists the phone's storages as a Portable Device (MTP): files are readable (read-only); no contacts/calendar/SMS | open the SymPort file manager; for PIM install the Nokia driver yourself and choose PC Suite; for changing files choose Mass storage |
| `mtp-only` | Windows shows the phone only as a media device (class WPD/MTP): no COM port, no drive letter — what a Nokia E72 reported on Windows 11 | for files choose Mass storage on the phone (SymPort does not read MTP yet); for contacts/calendar/SMS the Nokia PC Suite / Connectivity Cable driver must provide a COM port — install it yourself, choose PC Suite, replug |
| `no-com` | driver fine, but no COM port | switch the phone to PC Suite (not Mass storage / Media transfer), replug |
| `com-busy` | the Nokia COM port is held by another program | close Nokia Suite / Ovi Suite / terminals |
| `com-silent` | the Nokia COM port opens and is free, but the phone gives no answer on it (the Gammu AT identify failed) | switch the phone off and on, reconnect the cable, choose PC Suite only after the phone has finished starting, try another cable/port; the action text also says what the DKU-2 probe returned |
| `gammu-not-identified` | free COM port, no answer from the phone | unlock, keep PC Suite, rescan; a model-specific profile may be needed |
| `mass-storage-only` | only the drive is available | files and backups work; contacts/calendar/SMS need PC Suite + COM |
| `bluetooth-only` | a paired Nokia, nothing on USB | connect USB or use a configured Bluetooth profile |
| `ready` | a Nokia answered over its COM port | — |

The SymPort link has its own, separate states: `not-paired`, `connecting`, `connected` (with “certificate pinned”),
`tls-untrusted`, `tls-hostname` (certificate for another address), `tls-expired`, `tls-error`, `unreachable`,
`lease-expired`, `lease-missing`, `bad-token`, `rejected`, `server-error` — a TLS problem is never reported as a USB problem.
Buttons: **Rescan** (asks the running Bridge to look again at once), **Copy diagnostics**, **Open Device Manager**,
**Pair / Help** (opens only the SymPort address this Bridge was paired with, or explains the steps; the Bridge never
connects to a server on its own) and **Close**. New Nokia USB devices are noticed within about 10 seconds without pressing
Rescan. A Windows PnP record alone is never shown as “Connected”.

The USB list shows each Nokia node's class (e.g. `WPD`, `Ports`, `Modem`), the composite-interface number and Device Manager's error code (28 = no driver installed). The copied text contains versions, VID:PID, statuses and counts only — no IMEI, USB serial numbers or PnP instance IDs,
paths, addresses (the SymPort URL is reduced to its scheme), keys, tokens, contacts or SMS.


### Three independent routes

The Doctor shows three routes side by side; none implies another, and a missing one is never reported as the only dead end:

| Route | What it gives | What it needs |
|---|---|---|
| **Files over MTP** (Windows Portable Devices) | browse and download files, **read-only** | the phone shown by Windows as a portable device (`WPD`, e.g. Nokia E72 `0421:0223`); no Nokia driver, no COM port |
| **Mass storage** (drive letter) | files and backups, including writes with the usual confirmations | “Mass storage” mode on the phone |
| **PC Suite** (Gammu, COM or DKU-2) | identity, contacts, calendar, SMS — only what the capability matrix proves | the Nokia Connectivity Cable driver (installed by you) and “PC Suite” mode |

**MTP.** The Bridge uses the Windows shell's own Portable Device support to list storages (“Phone memory”, “Memory card”),
browse folders and copy a file to a private temporary folder, which is read and deleted. It never replaces or intercepts a
Windows driver. Uploading, deleting and creating folders are **not implemented** for MTP (they need verification on a real
phone first); the capability matrix marks them unsupported and every PIM operation unsupported. A `wpd-nokia` connection
never reports a phone model, firmware or identity, and nothing becomes `available` until Windows actually listed a storage.
**Not yet verified on a physical phone** — see the hardware checklist.

**COM port selection and polling.** A Nokia driver can expose two ports: the AT port (“Nokia E72 USB Modem”) and the
“USB Serial Port” the S60 Java runtime uses (`IF_JAVACOMM`). The Bridge recognises Nokia ports by their description/hwid text
(pyserial reports no VID for `NMWCD\VID_0421&…` ids), asks the **Modem port first** and **never opens the Java comm port**
(the Doctor lists it as “+1 Java comm, not used”). While nothing answers, the AT identify cycle backs off from 30 s,
doubling up to 10 minutes, so a silent phone's modem channel is not hammered (an identify can take up to 20 s per port); the
delay starts over when the set of COM ports or Nokia USB devices changes and on **Rescan**. No identify starts while another
phone operation holds the Bridge, and closing the Bridge stops an identify in flight so it does not keep a COM port open.
Not yet verified on a physical phone.

**Nokia-specific Gammu connection (DKU-2).** When Windows shows a working Nokia USB device and the AT route gave no answer
(no COM port, **or COM ports that stay silent**), the Bridge
makes one short, read-only Gammu `identify` over `connection = dku2` and then `dku2phonet` (the Nokia-specific Windows
connections that find the device through the Nokia driver). It is repeated only when the USB device set changes or you press
Rescan; a silent phone just records “no response”. Whether a given phone answers this way with the installed driver is not
guaranteed and not verified for the E72/Windows 11 combination.

**Drivers.** SymPort never installs, downloads or replaces drivers, never asks you to disable driver signature enforcement
or Secure Boot, and never installs anything silently. If the Nokia driver is missing, the Doctor says so and points to the
vendor/Microsoft route you run yourself (with your own UAC consent). Redistribution rights for Nokia drivers are not
established, so none is bundled.

**What the installer/exe contains** (checked by the exe's own `--self-test`, not by a Python on the build machine):
pyserial, python-gammu/libGammu, Pillow, pystray, Tk, websockets and the Windows runtime pieces they need. Nothing else has
to be installed for ordinary work: no Python, pip, Gammu or Visual C++ runtime. `cryptography` is deliberately **not** in
the exe: certificate pinning, update-signature verification and the Doctor use only the standard library. Settings, the
DPAPI-protected Bridge key and pinned certificates live in `%LOCALAPPDATA%\SymPortBridge` and survive updates.
Real-hardware verification steps: [BRIDGE_HARDWARE_CHECKLIST.md](BRIDGE_HARDWARE_CHECKLIST.md).

## API and security

HTTP binds `127.0.0.1:24652` only. Existing `/api/bridge` RPC requires the long-term
key, strict Host and enrolled Origin. Browser pairing requires JSON, a valid HTTPS
Origin (or local HTTP), exact origin matching and native default-No operator approval;
it returns no long-term key. Pairing is bounded and rate limited. Outbound relay
credentials are separate, expire, use TLS and reject redirects. The Workbench's
normal authenticated session alone can create/revoke a lease. Phone payloads are
transient memory, never relay logs or URLs. Delete, restore, PIM writes and SMS need
frontend confirmation plus independent local approval. Headless mode denies them.

Optional `"websocket": true` in settings enables authenticated status events at
`ws://127.0.0.1:24654/api/bridge/events`; first frame is `{"token":"..."}`.
Host/Origin are checked; only status counts, never phone data, are emitted. HTTP RPC
remains authoritative. The old Linux PC Bridge and SSH route remain supported.

## Build and licensing

GitHub Actions builds Windows x64 with Python 3.12 / PyInstaller, executes the real
frozen `.exe` against DPAPI, HTTP authentication, Origin/DNS-rebinding checks and the
bundled Gammu worker, then includes sources, dependency notices and executable hash.
Version comes from `symport.bridge.VERSION`. The PE has version resources and can
be Authenticode signed by a future release workflow; no signing key is shipped.

SymPort source remains MIT. Gammu/python-gammu are GPL-2.0-or-later; the combined
Windows binary is distributed under GPL-3.0-or-later with corresponding sources.
pystray is LGPL-3.0, PyInstaller includes a bootloader distribution exception.
No upstream GPL protocol code is copied into SymPort source. Do not redistribute a
modified binary without satisfying the bundled dependency licenses/source requirements.

Hardware USB/Bluetooth tests: **не проверено на устройстве**. Build, mock and local
HTTP checks do not establish compatibility with a physical E52. Use the manual
procedure in TRANSPORTS.md before relying on personal-data writes.

OBEX staging creates a new folder per transfer. JAD transfer is blocked through
this adapter because it cannot preserve the accompanying JAR in that same folder;
choose a JAR-only package or a mounted card. The catalog uses the same restriction.

The agent uses port **24652**, distinct from the Workbench default 8765. For the
legacy SSH route with this packaged agent, set
`SYMPORT_BRIDGE_AGENT_PORT=24652` in the Compose environment: the SSH sidecar forwards
to Windows 24652 and the backend pins that same Host port; the existing Linux bridge keeps
its default 8765. No port change is needed for browser pairing/outbound access.

Tray exit waits for an already confirmed write to finish. Revocation stops new
operations and private result delivery; it cannot undo bytes already written.
