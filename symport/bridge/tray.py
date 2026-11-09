"""Small native Windows tray/settings UI. Phone management remains in SymPort Web."""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

from . import VERSION, update
from .settings import load_config, save_config

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def autostart(enabled=None):
    if os.name != "nt":
        return False
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if enabled is None:
            try:
                value, _ = winreg.QueryValueEx(key, "SymPortBridge")
                return bool(value)
            except FileNotFoundError:
                return False
        if enabled:
            if not getattr(sys, "frozen", False):
                raise ValueError("Autostart is available in the portable exe")
            winreg.SetValueEx(key, "SymPortBridge", 0, winreg.REG_SZ, '"' + sys.executable + '"')
        else:
            try:
                winreg.DeleteValue(key, "SymPortBridge")
            except FileNotFoundError:
                pass
    return bool(enabled)


SETTINGS_SAVED = 0
SETTINGS_CLOSED = 10


def _window_command(flag: str, root: Path, port: int) -> list[str]:
    if getattr(sys, "frozen", False):
        base = [sys.executable]
    else:
        base = [sys.executable, "-m", "symport.bridge.main"]
    return [*base, flag, "--data-dir", str(root), "--port", str(port)]


def settings_command(root: Path, port: int) -> list[str]:
    """The settings window runs in its own process: Tk owns that process' main thread, so the window can
    always be closed and never shares an event loop with the tray icon."""
    return _window_command("--settings", root, port)


def diagnostics_command(root: Path, port: int) -> list[str]:
    return _window_command("--diagnostics", root, port)


TRANSPORT_LABELS = {
    "auto": "Automatic (recommended)",
    "gammu": "Gammu only",
    "pccs": "USB OBEX only (contacts and calendar, read-only)",
    "mtp": "MTP only (files, read-only)",
}
TRANSPORT_LABELS_RU = {
    "auto": "Автоматически (рекомендуется)",
    "gammu": "Только Gammu",
    "pccs": "Только USB OBEX (контакты и календарь, чтение)",
    "mtp": "Только MTP (файлы, чтение)",
}
REPORT_NAME = "symport-bridge-diagnostics.txt"


def desktop_folder() -> Path:
    desktop = Path.home() / "Desktop"
    return desktop if desktop.is_dir() else Path.home()


def logs_folder(root_dir: Path) -> Path:
    """The folder with the operations journal (action, time, status — no phone data)."""
    folder = root_dir / "build" / "symport" / "device-manager"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def open_folder(path: Path) -> bool:
    opener = getattr(os, "startfile", None)
    if opener is None:
        return False
    opener(str(path))
    return True


def save_report(path: Path, report: dict) -> Path:
    """The diagnostics as a text file. Same text as “Copy diagnostics”: versions, VID:PID, statuses and fixed Gammu codes only —
    no IMEI, serial numbers, PnP instance ids, interface paths, addresses, keys or phone data."""
    from . import doctor

    path = Path(path)
    if path.suffix.lower() != ".txt":
        path = path.with_name(path.name + ".txt")
    path.write_text(doctor.export_text(report), encoding="utf-8")
    return path


def settings_window(root_dir: Path, port: int) -> int:
    """Blocking Tk settings dialog (call from a process main thread). Returns SETTINGS_SAVED or SETTINGS_CLOSED.

    Grouped: connection (route, discovery, Gammu, profiles, notifications), updates, start-up."""
    from tkinter import BooleanVar, StringVar, Tk, filedialog, messagebox, ttk

    from . import doctor
    from .settings import load_key

    russian = doctor.windows_language_is_russian()

    def t(en, ru):
        return ru if russian else en

    labels = TRANSPORT_LABELS_RU if russian else TRANSPORT_LABELS
    result = SETTINGS_CLOSED
    root = Tk()
    root.title("SymPort Bridge · " + VERSION)
    root.resizable(False, False)
    config = load_config(root_dir)
    frame = ttk.Frame(root, padding=18)
    frame.grid()
    ttk.Label(frame, text="Nokia Symbian S60 · Local Bridge").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))

    connection = ttk.LabelFrame(frame, text=t("Connection", "Подключение"), padding=10)
    connection.grid(row=1, column=0, columnspan=3, sticky="ew")
    auto = BooleanVar(value=config.get("auto_usb", True))
    notify = BooleanVar(value=config.get("notifications", True))
    ttk.Checkbutton(connection, text=t("Discover Nokia USB devices automatically", "Искать Nokia по USB автоматически"), variable=auto).grid(
        row=0, column=0, columnspan=3, sticky="w"
    )
    preference = StringVar(value=labels.get(config.get("transport_preference", "auto"), labels["auto"]))
    ttk.Label(connection, text=t("Route to the phone", "Маршрут к телефону")).grid(row=1, column=0, sticky="w", pady=6)
    ttk.Combobox(connection, textvariable=preference, values=list(labels.values()), state="readonly", width=43).grid(
        row=1, column=1, columnspan=2, sticky="w"
    )
    gammu = StringVar(value=config.get("gammu_config", ""))
    profiles = StringVar(value=config.get("connections_file", ""))
    for row, label, value in [
        (2, t("Gammu config (optional)", "Файл настроек Gammu (необязательно)"), gammu),
        (3, t("Connection profiles (optional)", "Профили подключений (необязательно)"), profiles),
    ]:
        ttk.Label(connection, text=label).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(connection, textvariable=value, width=43).grid(row=row, column=1)
        ttk.Button(
            connection, text=t("Browse", "Обзор"), command=lambda v=value: v.set(filedialog.askopenfilename(parent=root) or v.get())
        ).grid(row=row, column=2, padx=(8, 0))
    ttk.Checkbutton(
        connection, text=t("Show notifications (connect, disconnect, route change)", "Показывать уведомления (подключение, отключение, смена маршрута)"), variable=notify
    ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(6, 0))

    updates = ttk.LabelFrame(frame, text=t("Updates", "Обновления"), padding=10)
    updates.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(10, 0))
    checks = BooleanVar(value=config.get("update_check", True))
    ttk.Checkbutton(
        updates, text=t("Check for updates daily (always asks before installing)", "Проверять обновления раз в день (установка только с вашего согласия)"), variable=checks
    ).grid(row=0, column=0, sticky="w")

    startup_group = ttk.LabelFrame(frame, text=t("Start-up", "Запуск"), padding=10)
    startup_group.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(10, 0))
    startup = BooleanVar(value=autostart())
    ttk.Checkbutton(startup_group, text=t("Start at Windows sign-in", "Запускать при входе в Windows"), variable=startup).grid(
        row=0, column=0, sticky="w"
    )
    ttk.Label(frame, text="API: http://127.0.0.1:" + str(port) + " · pair in SymPort → Connection").grid(
        row=4, column=0, columnspan=3, sticky="w", pady=10
    )

    def copy():
        root.clipboard_clear()
        root.clipboard_append(load_key(root_dir))
        root.update()
        messagebox.showinfo(
            "Bridge key",
            t("Key copied. Keep it private; configure the same key in your SymPort backend.", "Ключ скопирован. Храните его в тайне; тот же ключ нужен бэкенду SymPort."),
            parent=root,
        )

    ttk.Button(frame, text=t("Copy Bridge key", "Копировать ключ Bridge"), command=copy).grid(row=5, column=0, sticky="w")

    def close():
        root.destroy()

    def save():
        nonlocal result
        try:
            for value in (gammu.get(), profiles.get()):
                if value and (not Path(value).is_absolute() or not Path(value).is_file()):
                    raise ValueError("Choose existing absolute configuration paths")
            config.update(
                auto_usb=auto.get(),
                update_check=checks.get(),
                notifications=notify.get(),
                gammu_config=gammu.get(),
                connections_file=profiles.get(),
                transport_preference=next((k for k, v in labels.items() if v == preference.get()), "auto"),
            )
            save_config(root_dir, config)
            autostart(startup.get())
            result = SETTINGS_SAVED
            root.destroy()
        except (ValueError, OSError):
            messagebox.showerror(
                "Settings",
                t("Settings could not be applied. Check the selected files and connection profile schema.", "Не удалось применить настройки. Проверьте выбранные файлы и схему профиля подключений."),
                parent=root,
            )

    buttons = ttk.Frame(frame)
    buttons.grid(row=5, column=1, columnspan=2, sticky="e")
    ok = ttk.Button(buttons, text="OK", command=save, default="active")
    ok.grid(row=0, column=0, padx=(0, 8))
    ttk.Button(buttons, text=t("Cancel", "Отмена"), command=close).grid(row=0, column=1)
    ttk.Label(frame, text=t("No firmware flashing. Pair Bluetooth devices in Windows first.", "Прошивка не выполняется. Сначала сопрягите Bluetooth-устройства в Windows.")).grid(
        row=6, column=0, columnspan=3, sticky="w", pady=(12, 0)
    )
    root.protocol("WM_DELETE_WINDOW", close)
    root.bind("<Return>", lambda _event: save())
    root.bind("<Escape>", lambda _event: close())
    root.attributes("-topmost", True)  # appear above the other windows, then behave normally
    root.after(400, lambda: root.attributes("-topmost", False))
    root.lift()
    ok.focus_set()
    root.mainloop()
    return result


def diagnostics_window(root_dir: Path, port: int) -> int:
    """The Connections window (a native Tk window in its own process; also reachable as “Diagnostics”).

    It reads `diagnostics.json` (the safe report the running Bridge keeps current), asks the Bridge to rescan by creating
    `rescan.request`, and stores the route the user picks in `settings.json` (`transport_preference`). It never touches the phone."""
    import json
    import subprocess
    import time
    import webbrowser
    from tkinter import Text, Tk, filedialog, messagebox, ttk

    from . import doctor
    from .settings import set_preference

    russian = doctor.windows_language_is_russian()

    def t(en, ru):
        return ru if russian else en

    snapshot = root_dir / "diagnostics.json"
    root = Tk()
    root.title(t("SymPort Bridge — connections", "SymPort Bridge — подключения") + " " + VERSION)
    root.minsize(680, 600)
    frame = ttk.Frame(root, padding=14)
    frame.pack(fill="both", expand=True)
    frame.columnconfigure(0, weight=1)

    sentence = ttk.Label(frame, font=("Segoe UI", 12, "bold"), wraplength=640, justify="left")
    sentence.grid(row=0, column=0, sticky="w", pady=(0, 8))
    phone_head = ttk.Label(frame, font=("Segoe UI", 10, "bold"), wraplength=640, justify="left")
    phone_head.grid(row=1, column=0, sticky="w")
    phone_hint = ttk.Label(frame, wraplength=640, justify="left")
    phone_hint.grid(row=2, column=0, sticky="w", pady=(2, 8))
    routes_frame = ttk.Frame(frame)
    routes_frame.grid(row=3, column=0, sticky="ew", pady=(0, 8))
    routes_frame.columnconfigure(0, weight=1)
    link_head = ttk.Label(frame, font=("Segoe UI", 10, "bold"), wraplength=640, justify="left")
    link_head.grid(row=4, column=0, sticky="w")
    link_hint = ttk.Label(frame, wraplength=640, justify="left")
    link_hint.grid(row=5, column=0, sticky="w", pady=(2, 2))
    lease = ttk.Label(frame, wraplength=640, justify="left")
    lease.grid(row=6, column=0, sticky="w", pady=(0, 8))
    scan_row = ttk.Frame(frame)
    scan_row.grid(row=7, column=0, sticky="ew", pady=(0, 6))
    progress = ttk.Progressbar(scan_row, mode="indeterminate", length=160)
    progress.grid(row=0, column=0, padx=(0, 10))
    scan_label = ttk.Label(scan_row)
    scan_label.grid(row=0, column=1, sticky="w")
    rows = Text(frame, height=9, width=80, wrap="word", state="disabled", relief="solid", borderwidth=1)
    rows.grid(row=8, column=0, sticky="nsew")
    frame.rowconfigure(8, weight=1)
    state = {"report": None, "mtime": 0.0, "scanning": None, "scan_text": ""}

    def refresh():
        try:
            mtime = snapshot.stat().st_mtime
            if mtime != state["mtime"]:
                state["report"] = json.loads(snapshot.read_text(encoding="utf-8"))
                state["mtime"] = mtime
                if state["scanning"] is not None and not (root_dir / "rescan.request").exists():
                    stop_progress()  # the Bridge consumed the request and wrote a newer report
                show(state["report"])
        except (OSError, ValueError):
            pass
        if state["scanning"] is not None and time.monotonic() - state["scanning"] > 25:
            stop_progress()
            show_scan()
        root.after(1500, refresh)

    def stop_progress():
        state["scanning"] = None
        progress.stop()
        show_scan()

    def show_scan():
        if state["scanning"] is not None:
            scan_label.config(text=t("Searching…", "Идёт поиск…"))
        else:
            scan_label.config(text=(t("Last search: ", "Последний поиск: ") + state["scan_text"]) if state["scan_text"] else t("No search yet", "Поиск ещё не выполнялся"))

    def use(value):
        try:
            set_preference(root_dir, value)
        except (OSError, ValueError):
            messagebox.showerror("SymPort Bridge", t("Could not save the choice.", "Не удалось сохранить выбор."), parent=root)
            return
        messagebox.showinfo(
            "SymPort Bridge",
            t("Saved. The Bridge applies it within a few seconds; a running operation finishes first.", "Сохранено. Bridge применит выбор в течение нескольких секунд; идущая операция сначала завершится."),
            parent=root,
        )

    def show(report):
        model = doctor.window_model(report, russian)
        sentence.config(text=model["sentence"])
        phone_head.config(text=f"{model['phone_mark']}  {model['phone_summary']}")
        phone_hint.config(text=model["phone_action"])
        link_head.config(text=f"{model['link_mark']}  SymPort: {model['link_summary']}" + (f" — {model['link_origin']}" if model["link_origin"] else ""))
        link_hint.config(text=model["link_action"])
        lease.config(text=model["lease"])
        state["scan_text"] = model["scan"]
        show_scan()
        for child in list(routes_frame.winfo_children()):
            child.destroy()
        automatic = model["preference"] == "auto"
        for index, route in enumerate(model["route_rows"]):
            used = t("  · in use", "  · используется") if route["used"] else ""
            ttk.Label(routes_frame, text=f"{route['mark']}  {route['name']} — {route['state']}{used}").grid(row=index, column=0, sticky="w")
            if route["use"]:
                ttk.Button(
                    routes_frame,
                    text=t("Use", "Использовать"),
                    command=lambda value=route["use"]: use(value),
                    state="normal" if route["usable"] and not route["chosen"] else "disabled",
                ).grid(row=index, column=1, padx=(8, 0))
        ttk.Button(
            routes_frame,
            text=t("Automatic (recommended)", "Автоматически (рекомендуется)"),
            command=lambda: use("auto"),
            state="disabled" if automatic else "normal",
        ).grid(row=len(model["route_rows"]), column=0, sticky="w", pady=(4, 0))
        rows.config(state="normal")
        rows.delete("1.0", "end")
        for mark, text in model["rows"]:
            rows.insert("end", f" {mark}  {text}\n")
        rows.config(state="disabled")

    def rescan():
        try:
            (root_dir / "rescan.request").write_text("1", encoding="ascii")
        except OSError:
            messagebox.showerror("SymPort Bridge", t("Could not ask the Bridge to rescan.", "Не удалось запросить повторный поиск."), parent=root)
            return
        state["scanning"] = time.monotonic()
        progress.start(12)
        show_scan()

    def copy():
        if state["report"] is None:
            return
        root.clipboard_clear()
        root.clipboard_append(doctor.export_text(state["report"]))
        root.update()
        messagebox.showinfo(
            "SymPort Bridge",
            t("Copied. The text has no serial numbers, IMEI, paths, addresses or keys.",
              "Скопировано. В тексте нет серийных номеров, IMEI, путей, адресов и ключей."),
            parent=root,
        )

    def save():
        if state["report"] is None:
            return
        chosen = filedialog.asksaveasfilename(
            parent=root,
            title=t("Save the diagnostics report", "Сохранить отчёт диагностики"),
            initialdir=str(desktop_folder()),
            initialfile=REPORT_NAME,
            defaultextension=".txt",
            filetypes=[(t("Text file", "Текстовый файл"), "*.txt")],
        )
        if not chosen:
            return
        try:
            target = save_report(Path(chosen), state["report"])
        except OSError:
            messagebox.showerror("SymPort Bridge", t("The report could not be saved.", "Не удалось сохранить отчёт."), parent=root)
            return
        messagebox.showinfo(
            "SymPort Bridge",
            t("Saved: ", "Сохранено: ") + target.name + t(". It has no personal data.", ". В нём нет личных данных."),
            parent=root,
        )

    def logs():
        try:
            opened = open_folder(logs_folder(root_dir))
        except OSError:
            opened = False
        if not opened:
            messagebox.showinfo("SymPort Bridge", t("Open the log folder from the Bridge data directory.", "Откройте папку журналов в каталоге данных Bridge."), parent=root)

    def device_manager():
        if os.name == "nt":
            try:
                subprocess.Popen(["mmc.exe", "devmgmt.msc"])
                return
            except OSError:
                pass
        messagebox.showinfo("SymPort Bridge", t("Open Device Manager from the Windows Start menu.", "Откройте «Диспетчер устройств» из меню «Пуск»."), parent=root)

    def pair_help():
        report = state["report"]
        origin = report["link"].get("origin") if report else None
        if origin and origin.startswith(("https://", "http://127.0.0.1", "http://localhost")):
            webbrowser.open(origin + "/#phone/connection")  # only the server this Bridge was paired with
        else:
            messagebox.showinfo(
                "SymPort Bridge",
                t("Open SymPort in your browser → Connection → “Request access”, then approve the dialog on this PC. "
                  "The Bridge never connects to a server on its own.",
                  "Откройте SymPort в браузере → Подключение → «Запросить доступ» и подтвердите окно на этом ПК. "
                  "Bridge сам никогда не подключается к серверу."),
                parent=root,
            )

    buttons = ttk.Frame(frame)
    buttons.grid(row=9, column=0, sticky="ew", pady=(10, 0))
    for index, (label, command) in enumerate(
        [
            (t("Rescan", "Повторить поиск"), rescan),
            (t("Copy diagnostics", "Скопировать диагностику"), copy),
            (t("Save report…", "Сохранить отчёт…"), save),
            (t("Open log folder", "Открыть папку журналов"), logs),
            (t("Open Device Manager", "Диспетчер устройств"), device_manager),
            (t("Pair / Help", "Подключить / Помощь"), pair_help),
            (t("Reconnect to SymPort", "Переподключиться к SymPort"), pair_help),
        ]
    ):
        ttk.Button(buttons, text=label, command=command).grid(row=index // 4, column=index % 4, padx=(0, 8), pady=(0, 6), sticky="w")
    close = ttk.Button(buttons, text=t("Close", "Закрыть"), command=root.destroy, default="active")
    buttons.columnconfigure(4, weight=1)
    close.grid(row=1, column=4, sticky="e")
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.bind("<Return>", lambda _event: root.destroy())
    root.bind("<Escape>", lambda _event: root.destroy())
    root.attributes("-topmost", True)
    root.after(400, lambda: root.attributes("-topmost", False))
    root.lift()
    close.focus_set()
    show_scan()
    refresh()
    root.mainloop()
    return SETTINGS_CLOSED


RELAY_HINTS = {
    "tls-untrusted": "this PC does not trust the SymPort certificate. Pair again from SymPort (the Bridge then pins it), "
                     "or install the certificate from the SymPort login page.",
    "tls-error": "the TLS handshake with SymPort failed. Check that SymPort is opened with https.",
    "unreachable": "SymPort cannot be reached from this PC (address, port, firewall or network).",
    "rejected": "SymPort refused the access lease. Request access again from SymPort.",
    "server-error": "SymPort answered with an error. Check its container log.",
}


def relay_summary(agent) -> str:
    relay = agent.relay
    if relay is None:
        return "not paired"
    state = relay.public_state()
    if state["state"] == "connected":
        return "connected"
    if state["state"] == "connecting":
        return "connecting…"
    return "not connected — " + RELAY_HINTS.get(state["reason"], str(state["reason"]))



def phone_label(agent) -> str | None:
    """The name of the connected phone for the tooltip (e.g. “Nokia E72”); a drive or a Portable Device is not a phone name."""
    for row in list(getattr(agent.manager, "rows", {}).values()):
        if row.get("connected") and row.get("transport") in ("gammu", "pccs") and row.get("name"):
            return str(row["name"])[:60]
    return None


class TrayView:
    """Keeps the tray icon, its tooltip and the toasts in step with the Doctor report. Nothing is stored on disk."""

    def __init__(self, icon, base_image, russian: bool):
        from . import status

        self.status, self.icon, self.base, self.russian = status, icon, base_image, russian
        self.notifier = status.Notifier(russian)
        self.state: str | None = None
        self.tip: str | None = None

    def update(self, report: dict, label: str | None, notifications: bool) -> None:
        state = self.status.tray_state(report)
        if state != self.state:
            self.state = state
            self.icon.icon = self.status.badge(self.base, state)
        tip = self.status.tooltip(report, label, self.russian)
        if tip != self.tip:
            self.tip = tip
            self.icon.title = tip
        toasts = self.notifier.update(report, label)  # always advance the baseline, even when toasts are off
        if notifications:
            for text in toasts:
                self.icon.notify(text, "SymPort Bridge")


def run_tray(agent, key):
    import subprocess

    import pystray
    from PIL import Image

    from . import children, doctor

    russian = doctor.windows_language_is_russian()
    windows: dict[str, subprocess.Popen] = {}

    def open_window(name, command):
        # A native window in its own process: the tray menu and the Bridge keep running while it is open, and a second
        # click never stacks another window.
        process = windows.get(name)
        if process is not None and process.poll() is None:
            return None
        windows[name] = children.spawn(command)
        return windows[name]

    def settings():
        process = open_window("settings", settings_command(agent.root, agent.server.server_port))
        if process is None:
            return

        def apply():
            if process.wait() == SETTINGS_SAVED:
                try:
                    agent.refresh_config()
                except (ValueError, OSError):
                    pass

        threading.Thread(target=apply, daemon=True, name="bridge-settings").start()

    def connections():
        agent.write_snapshot()
        open_window("connections", diagnostics_command(agent.root, agent.server.server_port))

    def logs():
        path = agent.manager.journal
        if path.exists():
            os.startfile(path)
        else:
            connections()

    def quit_app(icon, item):
        for process in windows.values():
            if process.poll() is None:
                process.terminate()
        icon.stop()

    def message(text, flags=0x40040):  # MB_ICONINFORMATION | MB_TOPMOST
        import ctypes

        return ctypes.windll.user32.MessageBoxW(None, text, "SymPort Bridge", flags)

    def ask(text):
        return message(text, 0x40024) == 6  # MB_YESNO | MB_ICONQUESTION | MB_TOPMOST -> IDYES

    def idle():
        # Hold the manager lock for good: no phone operation starts while the installer takes over.
        return agent.manager.lock.acquire(blocking=False)

    def launch(path):
        update.launch_installer(path)
        agent.stop.set()
        icon.stop()

    def run_update(interactive):
        status = update.run_flow(
            agent.root,
            interactive=interactive,
            ask=ask,
            inform=message,
            notify=lambda text: icon.notify(text, "SymPort Bridge"),
            idle=idle,
            launch=launch,
        )
        if status in ("notified", "already-notified"):
            available.append(True)

    available: list[bool] = []

    def check_updates(icon_, item):
        threading.Thread(target=run_update, args=(True,), daemon=True, name="bridge-update").start()

    def automatic_update_check():
        config = load_config(agent.root)
        if config.get("update_check", True) and update.installed():
            if update.due(update.load_state(agent.root).get("last_check")):
                run_update(False)

    asset = Path(__file__).resolve().parents[1] / "web_assets/brand/appmark-192.png"
    base = Image.open(asset)
    icon = pystray.Icon(
        "SymPortBridge",
        base,
        "SymPort Bridge",
        pystray.Menu(
            pystray.MenuItem(
                lambda item: (
                    "Status: " + str(agent.status["state"]) + " · " + str(agent.status["connected"])
                ),
                connections,
                default=True,
            ),
            pystray.MenuItem("Connections" if not russian else "Подключения", lambda icon, item: connections()),
            pystray.MenuItem("Settings", lambda icon, item: settings()),
            pystray.MenuItem("Revoke web access", lambda icon, item: agent.revoke()),
            pystray.MenuItem("Operation logs", lambda icon, item: logs()),
            pystray.MenuItem(
                lambda item: "Install update…" if available else "Check for updates…", check_updates
            ),
            pystray.MenuItem("Exit", quit_app),
        ),
    )
    view = TrayView(icon, base, russian)

    def follow():
        while not agent.stop.wait(3):
            report = getattr(agent, "last_report", None)
            if report is None:
                continue
            try:
                notifications = bool(load_config(agent.root).get("notifications", True))
            except (ValueError, OSError):
                notifications = True
            view.update(report, phone_label(agent), notifications)

    def setup(icon_):
        icon_.visible = True
        threading.Thread(target=follow, daemon=True, name="bridge-tray").start()

    timer = threading.Timer(
        20, lambda: threading.Thread(target=automatic_update_check, daemon=True).start()
    )
    timer.daemon = True
    timer.start()
    icon.run(setup=setup)
