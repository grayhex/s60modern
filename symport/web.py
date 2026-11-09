"""Local web UI for the existing SymPort CLI (Python standard library only).

All privileged operations are an explicit allow-list; user input is never executed
through a shell. This server is meant for loopback/SSH tunnel access, not the Internet.
"""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import UTC, datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from symport.access import (
    SESSION_COOKIE,
    AccessConfig,
    AccessConfigError,
    LoginLimiter,
    SessionStore,
    load_access_config,
    verify_password,
)
from symport.catalog import AppCatalog
from symport.config import find_repo_root
from symport.inspector import Inspector
from symport.jailbreak import JailbreakManager
from symport.lab import EmulatorLab
from symport.library import ThemeLibrary
from symport.phone import DeviceManager
from symport.phone.backups import BackupStore, BackupStoreError, default_root
from symport.phone.bridge_security import host_allowed
from symport.phone.contacts import parse_export, vcards
from symport.phone.contracts import LIMIT
from symport.phone.media_downloads import MediaDownloads, byte_range
from symport.revive import ReviveService

ASSETS = Path(__file__).with_name("web_assets")
NAMES = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,39}$")
SIS_NAMES = re.compile(r"^[A-Za-z0-9_.-]{1,120}\.sisx?$", re.I)
VARIANTS = ("light", "dark")
BACKENDS = ("auto", "makesis", "native")
STEPS = ("identify", "backup", "transfer", "install", "activate", "standby",
         "menus", "dialogs", "apps", "restart", "uninstall")
STEP_STATUS = ("pending", "pass", "fail", "not-applicable")
REL_PATH = re.compile(r"^[^\x00-\x1f\\:]{1,200}$")
BACKUP_TIMEOUT = 6 * 3600


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


def projects(repo: Path) -> list[str]:
    root = repo / "workspace"
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir()
                  if p.is_dir() and not p.is_symlink() and NAMES.fullmatch(p.name)
                  and ((p / "symbian.toml").is_file() or
                       (p / "symbian-project.json").is_file()))


def packages(repo: Path) -> list[dict]:
    dirs = [("theme", v, repo / "build" / "symport" / "theme" / v)
            for v in VARIANTS]
    dirs += [("app", name, repo / "workspace" / name / ".symbian" / "package")
             for name in projects(repo)]
    result: list[dict] = []
    for kind, source, directory in dirs:
        if directory.is_dir():
            for p in sorted(directory.iterdir()):
                if p.is_file() and not p.is_symlink() and SIS_NAMES.fullmatch(p.name):
                    result.append({
                        "id": f"{kind}:{source}:{p.name}", "name": p.name,
                        "kind": kind, "source": source, "size": p.stat().st_size,
                        "verification": "not-tested",
                        "warning": "Пакет темы не проверен на E52" if kind == "theme" else
                                   "Приложение не проверено на E52",
                    })
    return result


def package_path(repo: Path, package_id: object) -> Path:
    if not isinstance(package_id, str):
        raise ValueError("Выбери пакет SIS/SISX")
    for p in packages(repo):
        if secrets.compare_digest(p["id"], package_id):
            directory = (repo / "build" / "symport" / "theme" / p["source"]
                         if p["kind"] == "theme" else
                         repo / "workspace" / p["source"] / ".symbian" / "package")
            found = directory / p["name"]
            if found.is_file() and not found.is_symlink():
                return found
    raise ValueError("Файл не найден среди пакетов SymPort")


def _name(value: object) -> str:
    if not isinstance(value, str) or not NAMES.fullmatch(value):
        raise ValueError("Имя проекта: буквы A-Z, цифры, _ или -, до 40 символов")
    return value


def _variant(value: object) -> str:
    if value not in VARIANTS:
        raise ValueError("Неизвестный вариант темы")
    return str(value)


def command(repo: Path, data: dict) -> list[str]:
    action = data.get("action")
    variant = data.get("variant", "light")
    if action == "doctor":
        return ["doctor"]
    if action == "theme.build":
        return ["theme", "build", "--variant", _variant(variant), "--bootstrap"]
    if action == "theme.inspect":
        return ["theme", "inspect", "--variant", _variant(variant)]
    if action == "theme.package":
        backend = data.get("backend", "auto")
        if backend not in BACKENDS:
            raise ValueError("Неподдерживаемый упаковщик")
        return ["theme", "package", "--variant", _variant(variant), "--backend", backend]
    if action == "device.list":
        return ["device", "list"]
    if action == "device.info":
        selector = _selector(data)
        return ["device", "info", *(["--device", selector] if selector else [])]
    if action == "emulator.list":
        return ["emulator", "list"]
    if action == "app.init":
        name = _name(data.get("project"))
        dest = repo / "workspace" / name
        if dest.exists():
            raise ValueError("Такой каталог приложения уже существует")
        return ["app", "init", "--name", name, "--target", "nokia-e52",
                "--dest", str(dest)]
    if action in ("app.build", "app.inspect", "app.analyze", "app.package"):
        name = _name(data.get("project"))
        if name not in projects(repo):
            raise ValueError("Приложение не найдено в workspace")
        project = str(repo / "workspace" / name)
        if action == "app.analyze":
            return ["app", "analyze", project, "--target", "nokia-e52"]
        if action == "app.inspect":
            return ["app", "inspect", "--project", project, "--target", "nokia-e52"]
        return ["app", action.removeprefix("app."), "--project", project,
                "--target", "nokia-e52"]
    if action in ("sis.inspect", "device.stage", "emulator.test"):
        pkg = str(package_path(repo, data.get("package")))
        if action == "sis.inspect":
            return ["sis", "inspect", pkg, "--target", "nokia-e52"]
        if action == "emulator.test":
            return ["emulator", "test", pkg, "--target", "nokia-e52"]
        selector = _selector(data)
        return ["device", "stage", pkg, *(["--device", selector] if selector else [])]
    raise ValueError("Неизвестная операция")


def _selector(data: dict) -> str:
    selector = data.get("selector", "")
    if not isinstance(selector, str) or len(selector) > 160 or any(
        c in selector for c in "\r\n\0"
    ):
        raise ValueError("Некорректный идентификатор USB-устройства")
    return selector


def ui_default_mode() -> str:
    """Interface mode a fresh browser starts in: 'developer' only when the operator asks for it."""
    return "developer" if os.getenv("SYMPORT_UI_MODE", "").strip().lower() == "developer" else "user"


class Workbench:
    def __init__(self, repo: Path, access: AccessConfig | None = None):
        self.repo = repo
        self.access = access or AccessConfig()
        self.sessions = SessionStore(self.access.idle_seconds, self.access.absolute_seconds)
        self.limiter = LoginLimiter()
        self.tls_ca: Path | None = None  # public certificate of SymPort's local CA, when it serves HTTPS itself
        self.devices = DeviceManager(repo)
        self.local_transport = self.devices.transport
        self.bridge_relay = None
        self.connection_lock = threading.Lock()
        self.media = MediaDownloads(repo, self.devices)
        from symport.audio.studio import RingtoneStudio
        self.ringtones = RingtoneStudio(repo, self.devices, self.media)
        self.catalog = AppCatalog(repo, self.devices)
        from symport.phone.diagnostics import DeviceDiagnostics
        self.diagnostics = DeviceDiagnostics(repo, self.devices)
        self.library = ThemeLibrary(repo)
        from symport.theme.studio import ThemeStudio
        self.theme_studio = ThemeStudio(repo)
        self.lab = EmulatorLab(repo, self.resolve_lab_package, self.devices)
        from symport.firmware import FirmwareExplorer
        self.firmware = FirmwareExplorer(repo / 'build/symport/firmware', self.devices)
        self.inspector = Inspector(repo, self.resolve_lab_package, self.devices, self.lab, self.firmware)
        self.jailbreak = JailbreakManager(repo, self.devices)
        self.revive = ReviveService(repo, self.devices)
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.Lock()
        self.jobs: list[dict] = []
        self.last_run: dict | None = None
        self.backups = BackupStore(default_root(repo))

    def resolve_lab_package(self, data):
        source, key = data.get('source'), data.get('reference')
        if source == 'theme-studio':
            return self.theme_studio.file(key)
        if source == 'lab':
            report = self.lab.report(key)
            if not report.get('package'):
                raise ValueError('Choose a package test report')
            raw = (self.lab.root / 'packages' / (report['id'] + '.bin')).read_bytes()
            import hashlib
            if hashlib.sha256(raw).hexdigest() != report['package']['sha256']:
                raise ValueError('Emulator package integrity changed')
            return report['package']['name'], raw, report['kind']
        if source == 'device':
            downloaded = self.devices.execute({'action': 'download', 'device': data.get('device'), 'path': data.get('path')})
            name = str(data.get('path', '')).replace('\\', '/').rsplit('/', 1)[-1]
            return name, base64.b64decode(downloaded['content'], validate=True), 'app'
        if source == 'build':
            path = package_path(self.repo, key)
            return path.name, path.read_bytes(), 'theme' if key.startswith('theme:') else 'app'
        if source == 'library':
            name, raw = self.library.download(key)
            return name, raw, 'theme'
        if source == 'catalog':
            entry = self.catalog.catalog.entries.get(key) if isinstance(key, str) else None
            if not entry or entry['delivery'] != 'automatic':
                raise ValueError('Скачайте ручной пакет у автора и загрузите его в Lab')
            declared = self.catalog.catalog.sources[entry['source_id']]
            artifact = next((a for a in declared['artifacts'] if a['name'].lower().endswith(('.sis', '.sisx', '.jar'))), None)
            if not artifact:
                raise ValueError('В каталоге нет подходящего пакета для теста')
            path, _ = self.catalog.downloader.fetch(declared, artifact)
            return path.name, path.read_bytes(), 'app'
        if source == 'upload':
            name, encoded = data.get('name'), data.get('content')
            if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,120}\.(sisx?|jar|jad)', name, re.I):
                raise ValueError('Нужен файл SIS/SISX/JAR/JAD с безопасным именем')
            if not isinstance(encoded, str):
                raise ValueError('Invalid package content')
            raw = base64.b64decode(encoded, validate=True)
            if len(raw) > 32 * 1024 * 1024:
                raise ValueError('Максимальный размер пакета — 32 МиБ')
            return name, raw, 'app'
        raise ValueError('Unknown package source')

    def snapshot(self) -> dict:
        with self.lock:
            jobs = [dict(j) for j in reversed(self.jobs)]
        for job in jobs:
            if job.get("status") == "running" and job.get("progress_id"):
                job["progress"] = self.backups.progress(job["progress_id"])
        probe = self.backups.root if self.backups.root.exists() else self.backups.root.parent
        try:
            free = shutil.disk_usage(probe if probe.exists() else self.repo).free
        except OSError:
            free = None
        return {"schema": "symport.web/1", "jobs": jobs,
                "projects": projects(self.repo), "packages": packages(self.repo),
                "library": self.library.list(), "device_report": self.read_report(),
                "backups": self.backups.list(), "backup_free": free}

    def _volume_root(self, device: object) -> Path:
        files = getattr(self.devices.transport, "files", None)
        roots = getattr(files, "roots", None)
        if roots is None:
            raise ValueError("Копия делается только с накопителя, смонтированного на этом сервере (не через PC Bridge)")
        if not isinstance(device, str) or device not in roots:
            raise ValueError("Выберите подключённый накопитель")
        root = roots[device]
        if not root.is_dir() or root.is_symlink():
            raise ValueError("Накопитель отключён")
        return root

    def _phone_command(self, data: dict) -> tuple[list[str], int, str, str | None]:
        """Allow-listed phone jobs. Paths are derived on the server; the browser sends ids only."""
        action = data.get("action")
        try:
            if action == "phone.backup.create":
                root = self._volume_root(data.get("device"))
                bid = self.backups.new_id(data["device"])
                args = ["phone", "backup", "create", "--volume", str(root), "--dest", str(self.backups.path(bid)),
                        "--label", data["device"], "--progress", str(self.backups.progress_path(bid))]
                return args, BACKUP_TIMEOUT, f"phone backup create {data['device']} → {bid}", bid
            if action == "phone.backup.verify":
                directory = self.backups.existing(data.get("id"))
                return (["phone", "backup", "verify", str(directory)], BACKUP_TIMEOUT,
                        f"phone backup verify {directory.name}", None)
            if action == "phone.backup.restore":
                directory = self.backups.existing(data.get("id"))
                root = self._volume_root(data.get("device"))
                args = ["phone", "backup", "restore", str(directory), "--volume", str(root)]
                apply = data.get("apply") is True
                if apply and data.get("confirm") is not True:
                    raise ValueError("Подтвердите восстановление")
                only = data.get("only")
                if only:
                    if not isinstance(only, str) or not REL_PATH.fullmatch(only) or ".." in only.split("/") \
                            or only.startswith("/"):
                        raise ValueError("Некорректный путь для частичного восстановления")
                    args += ["--only", only]
                if apply:
                    args.append("--apply")
                return (args, BACKUP_TIMEOUT,
                        f"phone backup restore {directory.name} → {data['device']}" + (" (запись)" if apply else " (план)"),
                        None)
        except BackupStoreError as exc:
            raise ValueError(str(exc)) from exc
        raise ValueError("Неизвестная операция")

    def launch(self, data: dict) -> dict:
        display, progress_id = None, None
        if isinstance(data.get("action"), str) and data["action"].startswith("phone."):
            args, timeout, display, progress_id = self._phone_command(data)
        else:
            args, timeout = command(self.repo, data), 900
        with self.lock:
            if any(job["status"] == "running" for job in self.jobs):
                raise RuntimeError("Другая операция ещё выполняется")
            job = {"id": secrets.token_hex(6), "action": data["action"],
                   "command": "symport " + (display or " ".join(args)), "status": "running",
                   "progress_id": progress_id,
                   "started": utcnow(), "finished": None,
                   "exit_code": None, "report": None, "output": ""}
            self.jobs.append(job)
            self.jobs = self.jobs[-30:]
        threading.Thread(target=self._execute, args=(job, args, timeout), daemon=True).start()
        return {"id": job["id"], "status": "running"}

    def _execute(self, job: dict, args: list[str], timeout: int = 900) -> None:
        try:
            result = subprocess.run(
                [sys.executable, "-m", "symport", *args, "--repo", str(self.repo),
                 "--json"], cwd=self.repo, timeout=timeout, capture_output=True,
                text=True, encoding="utf-8", errors="replace", check=False,
            )
            try:
                report = json.loads(result.stdout)
            except ValueError:
                report = None
            status = ("success" if result.returncode == 0 else
                      "unsupported" if result.returncode == 3 else "failed")
            if report is not None:
                # The report file exists before the job leaves "running", so a client that sees a
                # finished job can always read its report.
                output = self.repo / "build" / "symport" / "web-reports"
                output.mkdir(parents=True, exist_ok=True)
                (output / (job["id"] + ".json")).write_text(
                    json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
            with self.lock:
                job.update(status=status, exit_code=result.returncode, report=report,
                           output=(result.stderr + "\n" + result.stdout)[-16000:])
        except (OSError, subprocess.TimeoutExpired) as exc:
            with self.lock:
                job.update(status="failed", exit_code=1, output=str(exc))
        finally:
            with self.lock:
                job["finished"] = utcnow()
            if job.get("progress_id"):
                self.backups.progress_path(job["progress_id"]).unlink(missing_ok=True)

    def read_report(self) -> dict:
        path = self.repo / "build" / "symport" / "device-test-report.json"
        if path.is_file():
            try:
                obj = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(obj, dict):
                    return obj
            except (ValueError, OSError):
                pass
        return {"schema": "symport.manual-device-test/1",
                "target": "nokia-e52", "steps": {s: "pending" for s in STEPS},
                "rm": "", "firmware": "", "notes": ""}

    def save_report(self, data: dict) -> dict:
        steps = data.get("steps", {})
        if not isinstance(steps, dict) or set(steps) - set(STEPS):
            raise ValueError("Неизвестные шаги тестирования")
        if any(v not in STEP_STATUS for v in steps.values()):
            raise ValueError("Недопустимый статус теста")
        for key, limit in (("rm", 40), ("firmware", 80), ("notes", 2000)):
            value = data.get(key, "")
            if not isinstance(value, str) or len(value) > limit:
                raise ValueError("Некорректные данные: " + key)
        report = {"schema": "symport.manual-device-test/1",
                  "target": "nokia-e52", "entered_by": "user",
                  "recorded_at": utcnow(), "rm": data.get("rm", ""),
                  "firmware": data.get("firmware", ""),
                  "notes": data.get("notes", ""),
                  "steps": {s: steps.get(s, "pending") for s in STEPS},
                  "evidence": "manual user observation; no software verification"}
        path = self.repo / "build" / "symport" / "device-test-report.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
        return report


REACT_ASSETS = Path(__file__).parent / 'react_assets'


def make_handler(state: Workbench):
    class Handler(BaseHTTPRequestHandler):
        server_version = "SymPortWorkbench/0.1"

        def log_message(self, format: str, *args) -> None:
            return  # avoid logging private URL parameters

        def _host_ok(self, path: str = "") -> bool:
            if len(self.headers.get_all("Host", [])) != 1:
                return False
            host = self.headers.get("Host", "")
            if state.access.lan:
                # Exact configured LAN address(es) only; the in-container healthcheck may use loopback.
                return host in state.access.allowed_hosts or (
                    path == "/healthz" and host_allowed(host, self.server.server_port))
            public = os.getenv("SYMPORT_PUBLIC_ORIGIN", "")
            return host_allowed(host, self.server.server_port) or (public.startswith("https://") and host == urlsplit(public).netloc)

        def _cookie(self, name: str) -> str:
            try:
                jar = SimpleCookie()
                jar.load(self.headers.get("Cookie", ""))
                return jar[name].value if name in jar else ""
            except Exception:
                return ""

        def _session(self):
            return state.sessions.get(self._cookie(SESSION_COOKIE)) if state.access.lan else None

        def _token(self) -> str:
            """CSRF/media token of this request: the per-process token locally, the per-session one in LAN mode."""
            if state.access.lan:
                session = self._session()
                return session.csrf if session else ""
            return state.token

        def _https(self) -> bool:
            public = state.access.public_origin
            return state.access.tls or (bool(public) and self.headers.get("Host") == urlsplit(public).netloc)

        def _cookie_attrs(self) -> str:
            return "; HttpOnly; SameSite=Strict" + ("; Secure" if self._https() else "")

        def _media_ok(self) -> bool:
            token = self._token()
            return bool(token) and secrets.compare_digest(self._cookie("symport_media").encode(), token.encode()) \
                and self.headers.get("Sec-Fetch-Site") != "cross-site"

        def _send(self, content: bytes, mime: str, status: int = 200,
                  filename: str | None = None, session: bool = False, cookies: tuple[str, ...] = (),
                  extra: tuple[tuple[str, str], ...] = ()) -> None:
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data:; media-src 'self' blob:; script-src 'self'; "
                f"style-src 'self' 'nonce-{self._token() or secrets.token_urlsafe(12)}'; connect-src 'self' blob: http://127.0.0.1:24652; object-src 'none'; "
                "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            if session and self._token():
                self.send_header("Set-Cookie", f"symport_media={self._token()}{self._cookie_attrs()}; Path=/api/")
            for cookie in cookies:
                self.send_header("Set-Cookie", cookie)
            for key, value in extra:
                self.send_header(key, value)
            if filename:
                self.send_header("Content-Disposition",
                                 f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(content)

        def _json(self, data: dict, status: int = 200, **kwargs) -> None:
            self._send(json.dumps(data, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8", status, **kwargs)

        def _asset(self, relative: str, mime: str):
            try:
                return self._send((ASSETS / relative).read_bytes(), mime)
            except OSError:
                return self._json({"error": "Not found"}, 404)

        def _lan_get_gate(self, path: str) -> bool:
            """Home LAN: only the login screen, its assets and the health probe are anonymous. True = handled."""
            if path == "/healthz":
                self._json({"status": "ok"})
                return True
            if path == "/api/auth/session":
                session = self._session()
                self._json({"mode": "lan", "authenticated": bool(session), "tls": state.access.tls,
                            "ca": state.tls_ca is not None, **({"csrf": session.csrf} if session else {})})
                return True
            if path in ("/auth/login.js", "/auth/login.css"):
                self._asset(path.lstrip("/"), "text/javascript; charset=utf-8" if path.endswith(".js") else "text/css; charset=utf-8")
                return True
            if self._session():
                return False
            if path == "/":
                self._asset("auth/login.html", "text/html; charset=utf-8")
                return True
            if re.fullmatch(r"/(?:brand/[a-z0-9-]+\.(?:svg|png)|favicon\.svg|fonts/[A-Za-z]+\.ttf)", path):
                return False  # static login-screen artwork and fonts
            self._json({"error": "Sign in required", "login": True}, 401)
            return True

        def do_GET(self) -> None:
            url = urlsplit(self.path)
            if not self._host_ok(url.path):
                return self._json({"error": "Host not allowed"}, 403)
            if url.path == "/healthz":
                return self._json({"status": "ok"})
            if url.path == "/symport-ca.crt" and state.tls_ca is not None:
                try:  # public certificate only: anonymous on purpose, so a new computer can trust this server
                    return self._send(state.tls_ca.read_bytes(), "application/x-x509-ca-cert",
                                      filename="SymPort-Local-CA.crt")
                except OSError:
                    return self._json({"error": "Not found"}, 404)
            if state.access.lan and self._lan_get_gate(url.path):
                return
            react_root = REACT_ASSETS
            use_react = os.getenv('SYMPORT_FRONTEND', 'react') != 'classic'
            if (url.path == '/' and use_react and (react_root / 'index.html').is_file()) or url.path == '/next' or url.path.startswith('/next/'):
                relative = url.path.removeprefix('/next').lstrip('/') or 'index.html'
                root = REACT_ASSETS
                target = root / relative
                if any(part in ('.','..') for part in relative.split('/')) or '\\' in relative or not target.is_file() or target.is_symlink() or not target.resolve().is_relative_to(root.resolve()):
                    return self._json({'error':'React build not found. Run npm ci and npm run build in frontend.'},404)
                body = target.read_bytes()
                if relative == 'index.html':
                    body = body.replace(b'__SESSION_TOKEN__',self._token().encode())
                    body = body.replace(b'__UI_MODE__', ui_default_mode().encode())
                    body = body.replace(b'__AUTH_MODE__', b'lan' if state.access.lan else b'local')
                return self._send(body,'image/webp' if target.suffix.lower()=='.webp' else mimetypes.guess_type(str(target))[0] or 'application/octet-stream',session=relative=='index.html')
            static = {
                "/": ("index.html", "text/html; charset=utf-8"),
                "/classic": ("index.html", "text/html; charset=utf-8"),
                "/app.css": ("app.css", "text/css; charset=utf-8"),
                "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                "/device-workspace.js": ("device-workspace.js", "text/javascript; charset=utf-8"),
                "/device-workspace.css": ("device-workspace.css", "text/css; charset=utf-8"),
                "/manager.js": ("manager.js", "text/javascript; charset=utf-8"),
                "/connection.js": ("connection.js", "text/javascript; charset=utf-8"),
                "/ringtone.js": ("ringtone.js", "text/javascript; charset=utf-8"),
                "/ringtone.css": ("ringtone.css", "text/css; charset=utf-8"),
                "/library.js": ("library.js", "text/javascript; charset=utf-8"),
                "/suite.js": ("suite.js", "text/javascript; charset=utf-8"),
                "/catalog.js": ("catalog.js", "text/javascript; charset=utf-8"),
                "/lab.js": ("lab.js", "text/javascript; charset=utf-8"),
                "/inspector.js": ("inspector.js", "text/javascript; charset=utf-8"),
                "/preferences.js": ("preferences.js", "text/javascript; charset=utf-8"),
                "/translations.json": ("translations.json", "application/json; charset=utf-8"),
                "/preferences.css": ("preferences.css", "text/css; charset=utf-8"),
                "/compact.css": ("compact.css", "text/css; charset=utf-8"),
                "/lab.css": ("lab.css", "text/css; charset=utf-8"),
                "/nimbus.css": ("nimbus.css", "text/css; charset=utf-8"),
                "/backups.js": ("backups.js", "text/javascript; charset=utf-8"),
                "/calendar.js": ("calendar.js", "text/javascript; charset=utf-8"),
            }
            graphic = '/brand/appmark.svg' if url.path == '/favicon.svg' else url.path
            if re.fullmatch(r'/(?:brand/[a-z0-9-]+\.(?:svg|png)|icons/(?:outline|tiles)/[a-z0-9-]+\.svg)', graphic):
                try:
                    raw = (ASSETS / graphic.lstrip('/')).read_bytes()
                    return self._send(raw, 'image/svg+xml' if graphic.endswith('.svg') else 'image/png')
                except OSError:
                    return self._json({'error': 'Not found'}, 404)
            if re.fullmatch(r'/phones/[a-z0-9-]+\.(?:png|svg|webp)', url.path):
                try:  # artwork of every registered phone model (symport/phone/device_models.json)
                    return self._send((ASSETS / url.path.lstrip('/')).read_bytes(), {'.png':'image/png','.svg':'image/svg+xml','.webp':'image/webp'}[Path(url.path).suffix])
                except OSError:
                    return self._json({'error': 'Not found'}, 404)
            if url.path.startswith('/vendor/wavesurfer/'):
                relative = url.path.removeprefix('/vendor/wavesurfer/')
                if re.fullmatch(r'(?:[a-z0-9.-]+/)*[a-z0-9.-]+\.js', relative) and '..' not in relative:
                    path = ASSETS / 'vendor/wavesurfer' / relative
                    if path.is_file():
                        return self._send(path.read_bytes(), 'text/javascript; charset=utf-8')
                return self._json({'error':'Not found'},404)
            font = url.path.removeprefix('/fonts/')
            if url.path.startswith('/fonts/') and font in {'DMSans.ttf', 'FiraCode.ttf', 'PlusJakartaSans.ttf', 'Inter.ttf', 'JetBrainsMono.ttf'}:
                return self._send((ASSETS / 'fonts' / font).read_bytes(), 'font/ttf')
            if url.path in static:
                file, mime = static[url.path]
                try:
                    body = (ASSETS / file).read_bytes()
                except OSError:
                    return self._json({"error": "Not found"}, 404)
                if file == "index.html":
                    body = body.replace(b"__SESSION_TOKEN__", self._token().encode())
                return self._send(body, mime, session=file == "index.html")
            if url.path.startswith('/api/audio/'):
                try:
                    if not self._media_ok():
                        return self._json({'error':'Open SymPort before playing private audio'},403)
                    parts = url.path.split('/')
                    if len(parts) != 5:
                        raise ValueError('Invalid audio route')
                    raw, mime = state.ringtones.read(parts[3],parts[4])
                    try:
                        start,end,status = byte_range(self.headers.get('Range'),len(raw))
                    except ValueError:
                        self.send_response(416)
                        self.send_header('Content-Range',f'bytes */{len(raw)}')
                        self.send_header('Content-Length','0')
                        self.end_headers()
                        return
                    self.send_response(status)
                    self.send_header('Content-Type',mime)
                    self.send_header('Content-Length',str(end-start+1))
                    self.send_header('Accept-Ranges','bytes')
                    self.send_header('Cache-Control','no-store')
                    self.send_header('X-Content-Type-Options','nosniff')
                    self.send_header('Content-Security-Policy',"default-src 'none'; sandbox")
                    if status == 206:
                        self.send_header('Content-Range',f'bytes {start}-{end}/{len(raw)}')
                    if parse_qs(url.query).get('download') == ['1']:
                        self.send_header('Content-Disposition','attachment; filename="SymPort-ringtone.mp3"')
                    self.end_headers()
                    self.wfile.write(raw[start:end+1])
                    return
                except (ValueError,OSError,KeyError):
                    return self._json({'error':'Audio is unavailable or expired'},400)
            if url.path.startswith('/api/media/'):
                if not self._media_ok():
                    return self._json({'error': 'Open SymPort before viewing private media'}, 403)
                sent = False
                archive = None
                try:
                    if url.path.startswith('/api/media/export/'):
                        archive = state.media.archive(url.path.rsplit('/', 1)[-1])
                        size, mime, name = archive.stat().st_size, 'application/zip', 'symport-media.zip'
                        start, end, status = 0, size - 1, 200
                        def archive_chunks():
                            with archive.open('rb') as source:
                                while chunk := source.read(256 * 1024):
                                    yield chunk
                        chunks = archive_chunks()
                    elif url.path == '/api/media/file':
                        query = parse_qs(url.query)
                        item = {k: query.get(k, [''])[0] for k in ('device', 'path', 'revision')}
                        meta = state.devices.execute(dict(item, action='media-info'))
                        size, mime, name = meta['size'], meta['mime'], meta['name']
                        try:
                            start, end, status = byte_range(self.headers.get('Range'), size)
                        except ValueError:
                            self.send_response(416)
                            self.send_header('Content-Range', f'bytes */{size}')
                            self.send_header('Content-Length', '0')
                            self.send_header('Cache-Control', 'no-store')
                            self.end_headers()
                            return
                        chunks = state.media.chunks(item, start, end)
                    else:
                        raise ValueError('Media route not found')
                    self.send_response(status)
                    self.send_header('Content-Type', mime)
                    self.send_header('Content-Length', str(max(0, end - start + 1)))
                    self.send_header('Accept-Ranges', 'bytes' if not archive else 'none')
                    if status == 206:
                        self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
                    if archive or parse_qs(url.query).get('download') == ['1']:
                        self.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + quote(name, safe=''))
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('X-Content-Type-Options', 'nosniff')
                    self.send_header('Referrer-Policy', 'no-referrer')
                    self.send_header('Content-Security-Policy', "default-src 'none'; sandbox")
                    self.end_headers()
                    sent = True
                    for chunk in chunks:
                        self.wfile.write(chunk)
                    if archive:
                        archive.unlink(missing_ok=True)
                    if archive or parse_qs(url.query).get("download") == ["1"]:
                        state.devices.record("media-download", "success")
                except (ValueError, OSError, KeyError):
                    if sent:
                        self.close_connection = True
                    else:
                        return self._json({'error': 'Media unavailable or changed; refresh the gallery'}, 400)
                return
            if url.path == '/api/device/file':
                if not self._media_ok():
                    return self._json({'error': 'Open SymPort before viewing private phone files'}, 403)
                try:
                    query = parse_qs(url.query)
                    item = {k: query.get(k, [''])[0] for k in ('device', 'path')}
                    downloaded = state.devices.execute(dict(item, action='download'))
                    raw = base64.b64decode(downloaded['content'], validate=True)
                    name = str(downloaded.get('name') or item['path'].rsplit('/', 1)[-1] or 'file')
                    guessed = mimetypes.guess_type(name)[0] or 'application/octet-stream'
                    inline = guessed.split('/')[0] in ('image', 'audio', 'video') and guessed != 'image/svg+xml' \
                        or guessed in ('text/plain', 'text/html', 'application/pdf', 'application/json')
                    mime = guessed if inline else 'application/octet-stream'
                    try:
                        start, end, status = byte_range(self.headers.get('Range'), len(raw))
                    except ValueError:
                        self.send_response(416)
                        self.send_header('Content-Range', f'bytes */{len(raw)}')
                        self.send_header('Content-Length', '0')
                        self.send_header('Cache-Control', 'no-store')
                        self.end_headers()
                        return
                    self.send_response(status)
                    self.send_header('Content-Type', mime + ('; charset=utf-8' if mime.startswith('text/') else ''))
                    self.send_header('Content-Length', str(max(0, end - start + 1)))
                    self.send_header('Accept-Ranges', 'bytes')
                    if status == 206:
                        self.send_header('Content-Range', f'bytes {start}-{end}/{len(raw)}')
                    # Inline only for sub-resource loads by the page itself (preview). A top-level navigation or a
                    # request without Fetch Metadata is a download and is always served as an attachment.
                    embedded = self.headers.get('Sec-Fetch-Dest', '') in ('image', 'audio', 'video', 'iframe', 'empty', 'track')
                    disposition = 'inline' if inline and embedded and query.get('download') != ['1'] else 'attachment'
                    self.send_header('Content-Disposition', disposition + "; filename*=UTF-8''" + quote(name, safe=''))
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('X-Content-Type-Options', 'nosniff')
                    self.send_header('Referrer-Policy', 'no-referrer')
                    # Untrusted phone content: no scripts, no storage access, may be framed by SymPort itself only.
                    self.send_header('Content-Security-Policy', "default-src 'none'; img-src 'self'; media-src 'self'; style-src 'unsafe-inline'; sandbox; frame-ancestors 'self'")
                    self.end_headers()
                    self.wfile.write(raw[start:end + 1])
                    return
                except (ValueError, OSError, KeyError):
                    return self._json({'error': 'Phone file is unavailable'}, 400)
            if url.path == "/api/catalog/archive":
                try:
                    key = parse_qs(url.query).get('id', [''])[0]
                    return self._send(state.catalog.archive(key), 'application/zip', filename='symport-apps.zip')
                except (ValueError, OSError) as exc:
                    return self._json({'error': str(exc)}, 400)
            if url.path == "/api/library/download":
                try:
                    name, raw = state.library.download(parse_qs(url.query).get('id', [''])[0])
                    return self._send(raw, 'application/octet-stream', filename=name)
                except (ValueError, OSError) as exc:
                    return self._json({'error': str(exc)}, 404)
            if url.path == "/api/state":
                return self._json(state.snapshot())
            if url.path in ("/api/preview/light", "/api/preview/dark"):
                variant = url.path.rsplit("/", 1)[-1]
                path = state.repo / "theme" / "preview" / f"mock_{variant}_240x320.png"
                return self._send(path.read_bytes(), "image/png")
            if url.path in ("/api/wallpaper/light", "/api/wallpaper/dark"):
                variant = url.path.rsplit("/", 1)[-1]
                path = state.repo / "theme" / "assets" / variant / "background.png"
                return self._send(path.read_bytes(), "image/png",
                                  filename=f"E52-CLEAR-{variant}-240x320.png")
            if url.path == "/api/download":
                package_id = parse_qs(url.query).get("id", [""])[0]
                try:
                    path = package_path(state.repo, package_id)
                except ValueError as exc:
                    return self._json({"error": str(exc)}, 404)
                return self._send(path.read_bytes(), "application/octet-stream",
                                  filename=path.name)
            if url.path == "/api/device-report":
                return self._send(
                    (json.dumps(state.read_report(), ensure_ascii=False, indent=2)
                     + "\n").encode(), "application/json; charset=utf-8",
                    filename="e52-device-test-report.json")
            return self._json({"error": "Not found"}, 404)

        def _login(self) -> None:
            client = self.client_address[0]
            wait = state.limiter.retry_after(client)
            if wait:
                return self._json({"error": "Too many attempts", "retry_after": wait}, 429,
                                  extra=(("Retry-After", str(wait)),))
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 4096 or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    raise ValueError
                body = json.loads(self.rfile.read(length))
                password = body.get("password") if isinstance(body, dict) else None
                if not isinstance(password, str) or not 0 < len(password) <= 256:
                    raise ValueError
            except (ValueError, OSError):
                return self._json({"error": "Bad request"}, 400)
            if not verify_password(password, state.access.password_hash):
                state.limiter.failed(client)
                time.sleep(0.3)
                return self._json({"error": "Wrong password"}, 401)
            state.limiter.succeeded(client)
            state.sessions.revoke(self._cookie(SESSION_COOKIE))
            token, session = state.sessions.create(client)
            attrs = self._cookie_attrs()
            return self._json({"ok": True, "csrf": session.csrf}, cookies=(
                f"{SESSION_COOKIE}={token}{attrs}; Path=/",
                f"symport_media={session.csrf}{attrs}; Path=/api/"))

        def _logout(self) -> None:
            state.sessions.revoke(self._cookie(SESSION_COOKIE))
            attrs = self._cookie_attrs() + "; Max-Age=0"
            return self._json({"ok": True}, cookies=(f"{SESSION_COOKIE}={attrs}; Path=/", f"symport_media={attrs}; Path=/api/"))

        def do_POST(self) -> None:
            if not self._host_ok():
                return self._json({"error": "Host not allowed"}, 403)
            origin = self.headers.get("Origin", "")
            if state.access.lan:
                allowed = set(state.access.allowed_origins)
            else:
                allowed = {f"http://localhost:{self.server.server_port}",
                           f"http://127.0.0.1:{self.server.server_port}"}
                public = os.getenv("SYMPORT_PUBLIC_ORIGIN", "")
                if public.startswith("https://"):
                    allowed.add(public)
            if len(self.headers.get_all("Origin", [])) > 1 or (origin and origin not in allowed):
                return self._json({"error": "Origin not allowed"}, 403)
            relay_path = self.path.startswith('/api/bridge-relay/')
            if state.access.lan:
                # A browser POST must name its origin; only the key-authenticated Bridge relay may omit it.
                if not origin and not relay_path:
                    return self._json({"error": "Origin required"}, 403)
                if self.path == "/api/auth/login":
                    return self._login()
                if not relay_path:
                    session = self._session()
                    if not session:
                        return self._json({"error": "Sign in required", "login": True}, 401)
                    if not secrets.compare_digest(self.headers.get("X-Symport-Token", "").encode(), session.csrf.encode()):
                        return self._json({"error": "Bad session token"}, 403)
                    if self.path == "/api/auth/logout":
                        return self._logout()
            if self.path.startswith('/api/bridge-relay/'):
                relay = state.bridge_relay
                if not relay or not relay.authorized(self.headers.get('X-Symport-Relay-Key', '')):
                    return self._json({'error': 'Bridge lease unavailable'}, 403)
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                    if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) != 1 or not 0 < length <= 90 * 1024 * 1024:
                        raise ValueError('Invalid relay length')
                    if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                        raise ValueError('JSON required')
                    self.connection.settimeout(30)
                    data = json.loads(self.rfile.read(length))
                    if not isinstance(data, dict):
                        raise ValueError('Expected JSON object')
                    if self.path.endswith('/poll'):
                        return self._json(relay.poll())
                    if self.path.endswith('/result'):
                        return self._json(relay.complete(data))
                    raise ValueError('Unknown relay operation')
                except (ValueError, OSError):
                    return self._json({'error': 'Bridge request rejected'}, 400)
            bridge_key = os.getenv("SYMPORT_BRIDGE_KEY", "")
            if state.access.lan:
                authorized = True  # the session + CSRF token were verified above; the legacy key route is not used
            else:
                authorized = (len(bridge_key) >= 32 and secrets.compare_digest(
                    self.headers.get("X-Symport-Bridge-Key", ""), bridge_key)
                    if self.path == "/api/bridge" else secrets.compare_digest(
                        self.headers.get("X-Symport-Token", ""), state.token))
            if not authorized:
                return self._json({"error": "Bad session token"}, 403)
            if self.path == '/api/ringtone/upload':
                try:
                    length = int(self.headers.get('Content-Length','0'))
                    if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length',[])) != 1 or not 0 < length <= 64*1024**2:
                        return self._json({'error':'Upload audio up to 64 MiB'},413)
                    if self.headers.get('Content-Type','').split(';')[0] != 'application/octet-stream':
                        return self._json({'error':'Binary audio upload required'},415)
                    self.connection.settimeout(60)
                    return self._json(state.ringtones.create(self.rfile,length))
                except (ValueError,OSError) as exc:
                    return self._json({'error':str(exc)},400)
            if urlsplit(self.path).path == '/api/firmware/upload':
                if self.headers.get('Content-Type', '').split(';')[0] != 'application/octet-stream':
                    return self._json({'error': 'Binary firmware upload required'}, 415)
                try:
                    self.connection.settimeout(300)
                    args = parse_qs(urlsplit(self.path).query)
                    if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) != 1:
                        raise ValueError('Exactly one Content-Length is required')
                    length = int(self.headers.get('Content-Length', '0'))
                    return self._json(state.firmware.store.create(self.rfile, length, args.get('name', [''])[0], args.get('label', [''])[0]))
                except (ValueError, OSError) as exc:
                    return self._json({'error': str(exc)}, 400)
            if urlsplit(self.path).path == '/api/lab/firmware':
                if self.headers.get('Content-Type', '').split(';')[0] != 'application/octet-stream':
                    return self._json({'error': 'Binary firmware upload required'}, 415)
                try:
                    self.connection.settimeout(120)
                    args = parse_qs(urlsplit(self.path).query)
                    length = int(self.headers.get('Content-Length', '0'))
                    result = state.lab.upload_firmware(self.rfile, length, args.get('slot', [''])[0], args.get('name', [''])[0], args.get('bundle', [None])[0])
                    return self._json(result)
                except (ValueError, OSError) as exc:
                    return self._json({'error': str(exc)}, 400)
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self._json({"error": "JSON required"}, 415)
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self._json({"error": "Invalid length"}, 400)
            if length <= 0 or length > ((64 * 1024 * 1024 * 4 // 3 + 4096) if self.path in ("/api/manager", "/api/bridge", "/api/lab", "/api/inspector", "/api/jailbreak", "/api/revive", "/api/firmware") else (LIMIT * 4 // 3 + 4096 if self.path in ("/api/library", "/api/contacts", "/api/theme-studio") else 512_000 if self.path in ("/api/media/export", "/api/catalog") else 12_000)):
                return self._json({"error": "Invalid body size"}, 413)
            try:
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError("Expected JSON object")
                if self.path == '/api/theme-studio':
                    return self._json(state.theme_studio.execute(body))
                if self.path == '/api/diagnostics':
                    return self._json(state.diagnostics.execute(body))
                if self.path == '/api/ringtone':
                    return self._json(state.ringtones.execute(body))
                if self.path == '/api/connection':
                    with state.connection_lock:
                        if body.get('action') == 'enable':
                            from symport.phone.relay import BrowserBridgeRelay
                            if state.bridge_relay:
                                state.bridge_relay.close()
                            relay = BrowserBridgeRelay()
                            state.bridge_relay = relay
                            # Closing the old lease unblocks an in-flight operation.
                            with state.devices.lock:
                                state.devices.transport = relay
                            return self._json({'relay_token': relay.token, **relay.status()})
                        if body.get('action') == 'disconnect':
                            if state.bridge_relay:
                                state.bridge_relay.close()
                            with state.devices.lock:
                                state.devices.transport = state.local_transport
                            state.bridge_relay = None
                        return self._json(state.bridge_relay.status() if state.bridge_relay else {'mode': 'direct', 'connected': True})
                if self.path == '/api/inspector':
                    return self._json(state.inspector.execute(body))
                if self.path == '/api/firmware':
                    return self._json(state.firmware.execute(body))
                if self.path == '/api/jailbreak':
                    return self._json(state.jailbreak.execute(body))
                if self.path == '/api/revive':
                    return self._json(state.revive.execute(body))
                if self.path == '/api/lab':
                    return self._json(state.lab.execute(body))
                if self.path == "/api/catalog":
                    result = state.catalog.execute(body)
                    return self._json(result, 202 if body.get('action') in ('download', 'transfer') else 200)
                if self.path == "/api/contacts":
                    if body.get('action') == 'preview':
                        return self._json(parse_export(body.get('name'), body.get('content')))
                    if body.get('action') == 'export':
                        import io
                        import zipfile
                        cards = vcards(body.get('contacts'))
                        buffer = io.BytesIO()
                        with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                            for i, card in enumerate(cards, 1):
                                archive.writestr(f'contact-{i:04}.vcf', card.encode('utf-8'))
                        return self._json({'vcard': base64.b64encode(''.join(cards).encode()).decode(),
                                           'zip': base64.b64encode(buffer.getvalue()).decode()})
                    raise ValueError('Неизвестная операция контактов')
                if self.path == "/api/library":
                    action = body.get('action')
                    if action == 'import':
                        return self._json(state.library.import_package(body.get('name'), body.get('content', '')))
                    if action == 'prepare-build':
                        path = package_path(state.repo, body.get('package'))
                        if not str(body.get('package', '')).startswith('theme:'):
                            raise ValueError('Choose an E52 CLEAR theme package')
                        return self._json(state.library.import_package(path.name, base64.b64encode(path.read_bytes()).decode()))
                    if action == 'publish':
                        return self._json(state.library.publish(body.get('id'), body.get('confirm')))
                    raise ValueError('Unknown library operation')
                if self.path == "/api/media/export":
                    return self._json(state.media.export(body))
                if self.path in ("/api/manager", "/api/bridge"):
                    return self._json(state.devices.execute(body))
                if self.path == "/api/backups":
                    if body.get("action") != "delete":
                        raise ValueError("Неизвестная операция с копиями")
                    with state.lock:
                        if any(j["status"] == "running" for j in state.jobs):
                            raise RuntimeError("Другая операция ещё выполняется")
                    try:
                        return self._json(state.backups.delete(body.get("id"), body.get("confirm")))
                    except BackupStoreError as exc:
                        raise ValueError(str(exc)) from exc
                if self.path == "/api/run":
                    result = state.launch(body)
                    return self._json(result, 202)
                if self.path == "/api/device-report":
                    return self._json(state.save_report(body))
                return self._json({"error": "Not found"}, 404)
            except (ValueError, RuntimeError, OSError) as exc:
                return self._json({"error": str(exc)}, 400)

    return Handler


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="SymPort local browser workbench")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Bind address; 0.0.0.0 ONLY inside an isolated Docker container")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--open", action="store_true", help="Open browser")
    args = parser.parse_args(argv)
    try:
        access = load_access_config(port=args.port)
    except AccessConfigError as exc:
        print(f"SymPort: configuration error: {exc}", file=sys.stderr)
        print("SymPort did not start. Fix the setting above (nothing is served until it is safe).", file=sys.stderr)
        raise SystemExit(78) from None
    repo = find_repo_root(explicit=args.repo) if args.repo else find_repo_root()
    state = Workbench(repo, access)
    if access.tls:
        from symport import tls

        try:
            if access.tls_cert:
                material = tls.TlsMaterial(Path(access.tls_cert), Path(access.tls_key), None)
            else:
                directory = Path(access.tls_dir) if access.tls_dir else repo / "build" / "symport" / "tls"
                material = tls.ensure_certificates(directory, list(access.hosts))
            context = tls.server_context(material)
        except (OSError, ValueError) as exc:
            print(f"SymPort: configuration error: cannot prepare HTTPS: {exc}", file=sys.stderr)
            raise SystemExit(78) from None
        state.tls_ca = material.ca_file
        server = tls.TlsThreadingHTTPServer((args.host, args.port), make_handler(state))
        server.tls_context = context
    else:
        server = ThreadingHTTPServer((args.host, args.port), make_handler(state))
    if access.lan:
        shown = sorted(access.allowed_origins)
        print("SymPort Workbench — Home LAN mode (login required"
              + (", HTTPS" if access.tls else "") + "): " + ", ".join(shown))
        for warning in access.warnings:
            print("WARNING: " + warning, file=sys.stderr)
        print("Do not forward this port on your router and do not expose it to the Internet.")
    else:
        print(f"SymPort Workbench: http://127.0.0.1:{args.port}")
        print("Only serve via localhost / SSH tunnel. Never expose directly to the Internet.")
    if args.open and args.host == "127.0.0.1" and not access.lan:
        webbrowser.open(f"http://127.0.0.1:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
