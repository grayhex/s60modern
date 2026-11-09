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


def settings_window(root_dir: Path, port: int) -> int:
    """Blocking Tk settings dialog (call from a process main thread). Returns SETTINGS_SAVED or SETTINGS_CLOSED."""
    from tkinter import BooleanVar, StringVar, Tk, filedialog, messagebox, ttk

    from .settings import load_key

    result = SETTINGS_CLOSED
    root = Tk()
    root.title("SymPort Bridge · " + VERSION)
    root.resizable(False, False)
    config = load_config(root_dir)
    frame = ttk.Frame(root, padding=18)
    frame.grid()
    ttk.Label(frame, text="Nokia Symbian S60 · Local Bridge").grid(
        row=0, column=0, columnspan=3, sticky="w", pady=(0, 12)
    )
    auto = BooleanVar(value=config.get("auto_usb", True))
    startup = BooleanVar(value=autostart())
    ttk.Checkbutton(frame, text="Discover Nokia USB devices automatically", variable=auto).grid(
        row=1, column=0, columnspan=3, sticky="w"
    )
    ttk.Checkbutton(frame, text="Start at Windows sign-in", variable=startup).grid(
        row=2, column=0, columnspan=3, sticky="w"
    )
    checks = BooleanVar(value=config.get("update_check", True))
    ttk.Checkbutton(
        frame, text="Check for updates daily (always asks before installing)", variable=checks
    ).grid(row=8, column=0, columnspan=3, sticky="w", pady=(8, 0))
    gammu = StringVar(value=config.get("gammu_config", ""))
    profiles = StringVar(value=config.get("connections_file", ""))
    for row, label, value in [
        (3, "Gammu config (optional)", gammu),
        (4, "Connection profiles (optional)", profiles),
    ]:
        ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=7)
        ttk.Entry(frame, textvariable=value, width=43).grid(row=row, column=1)
        ttk.Button(
            frame,
            text="Browse",
            command=lambda v=value: v.set(filedialog.askopenfilename(parent=root) or v.get()),
        ).grid(row=row, column=2, padx=(8, 0))
    ttk.Label(
        frame, text="API: http://127.0.0.1:" + str(port) + " · pair in SymPort → Connection"
    ).grid(row=5, column=0, columnspan=3, sticky="w", pady=10)

    def copy():
        root.clipboard_clear()
        root.clipboard_append(load_key(root_dir))
        root.update()
        messagebox.showinfo(
            "Bridge key",
            "Key copied. Keep it private; configure the same key in your SymPort backend.",
            parent=root,
        )

    ttk.Button(frame, text="Copy Bridge key", command=copy).grid(row=6, column=0, sticky="w")

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
                gammu_config=gammu.get(),
                connections_file=profiles.get(),
            )
            save_config(root_dir, config)
            autostart(startup.get())
            result = SETTINGS_SAVED
            root.destroy()
        except (ValueError, OSError):
            messagebox.showerror(
                "Settings",
                "Settings could not be applied. Check the selected files and connection profile schema.",
                parent=root,
            )

    buttons = ttk.Frame(frame)
    buttons.grid(row=6, column=1, columnspan=2, sticky="e")
    ok = ttk.Button(buttons, text="OK", command=save, default="active")
    ok.grid(row=0, column=0, padx=(0, 8))
    ttk.Button(buttons, text="Cancel", command=close).grid(row=0, column=1)
    ttk.Label(frame, text="No firmware flashing. Pair Bluetooth devices in Windows first.").grid(
        row=7, column=0, columnspan=3, sticky="w", pady=(12, 0)
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
    """Blocking Tk diagnostics window (own process). OK/Close, Esc, Enter and the window's X all close it.

    It only reads `diagnostics.json` (the safe report the running Bridge keeps current) and asks the Bridge to rescan by
    creating `rescan.request`; it never touches the phone itself."""
    import json
    import subprocess
    import webbrowser
    from tkinter import Text, Tk, messagebox, ttk

    from . import doctor

    russian = doctor.windows_language_is_russian()

    def t(en, ru):
        return ru if russian else en

    snapshot = root_dir / "diagnostics.json"
    root = Tk()
    root.title(t("SymPort Bridge — diagnostics", "SymPort Bridge — диагностика") + " " + VERSION)
    root.minsize(640, 520)
    frame = ttk.Frame(root, padding=14)
    frame.pack(fill="both", expand=True)
    frame.columnconfigure(0, weight=1)

    phone_head = ttk.Label(frame, font=("Segoe UI", 11, "bold"), wraplength=600, justify="left")
    phone_head.grid(row=0, column=0, sticky="w")
    phone_hint = ttk.Label(frame, wraplength=600, justify="left")
    phone_hint.grid(row=1, column=0, sticky="w", pady=(2, 10))
    link_head = ttk.Label(frame, font=("Segoe UI", 10, "bold"), wraplength=600, justify="left")
    link_head.grid(row=2, column=0, sticky="w")
    link_hint = ttk.Label(frame, wraplength=600, justify="left")
    link_hint.grid(row=3, column=0, sticky="w", pady=(2, 10))
    rows = Text(frame, height=14, width=76, wrap="word", state="disabled", relief="solid", borderwidth=1)
    rows.grid(row=4, column=0, sticky="nsew")
    frame.rowconfigure(4, weight=1)
    state = {"report": None, "mtime": 0.0}

    def refresh():
        try:
            mtime = snapshot.stat().st_mtime
            if mtime != state["mtime"]:
                state["report"] = json.loads(snapshot.read_text(encoding="utf-8"))
                state["mtime"] = mtime
                show(state["report"])
        except (OSError, ValueError):
            pass
        root.after(1500, refresh)

    def show(report):
        model = doctor.window_model(report, russian)
        phone_head.config(text=f"{model['phone_mark']}  {model['phone_summary']}")
        phone_hint.config(text=model["phone_action"])
        link_head.config(text=f"{model['link_mark']}  SymPort: {model['link_summary']}" + (f" — {model['link_origin']}" if model["link_origin"] else ""))
        link_hint.config(text=model["link_action"])
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
    buttons.grid(row=5, column=0, sticky="ew", pady=(10, 0))
    for index, (label, command) in enumerate(
        [
            (t("Rescan", "Повторить поиск"), rescan),
            (t("Copy diagnostics", "Копировать диагностику"), copy),
            (t("Open Device Manager", "Диспетчер устройств"), device_manager),
            (t("Pair / Help", "Подключить / Помощь"), pair_help),
        ]
    ):
        ttk.Button(buttons, text=label, command=command).grid(row=0, column=index, padx=(0, 8))
    close = ttk.Button(buttons, text=t("Close", "Закрыть"), command=root.destroy, default="active")
    buttons.columnconfigure(4, weight=1)
    close.grid(row=0, column=5, sticky="e")
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.bind("<Return>", lambda _event: root.destroy())
    root.bind("<Escape>", lambda _event: root.destroy())
    root.attributes("-topmost", True)
    root.after(400, lambda: root.attributes("-topmost", False))
    root.lift()
    close.focus_set()
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


def run_tray(agent, key):
    import subprocess

    import pystray
    from PIL import Image

    settings_process: list[subprocess.Popen] = []

    def settings():
        if settings_process and settings_process[0].poll() is None:
            return  # already open
        process = subprocess.Popen(settings_command(agent.root, agent.server.server_port))
        settings_process[:] = [process]

        def apply():
            if process.wait() == SETTINGS_SAVED:
                try:
                    agent.refresh_config()
                except (ValueError, OSError):
                    pass

        threading.Thread(target=apply, daemon=True, name="bridge-settings").start()

    diagnostics_process: list[subprocess.Popen] = []

    def diagnostics():
        # A native window in its own process: the tray menu and the Bridge keep running while it is open, and a
        # second click never stacks another window.
        if diagnostics_process and diagnostics_process[0].poll() is None:
            return
        agent.write_snapshot()
        diagnostics_process[:] = [subprocess.Popen(diagnostics_command(agent.root, agent.server.server_port))]

    def logs():
        path = agent.manager.journal
        if path.exists():
            os.startfile(path)
        else:
            diagnostics()

    def quit_app(icon, item):
        for process in [*settings_process, *diagnostics_process]:
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
    icon = pystray.Icon(
        "SymPortBridge",
        Image.open(asset),
        "SymPort Bridge",
        pystray.Menu(
            pystray.MenuItem(
                lambda item: (
                    "Status: " + str(agent.status["state"]) + " · " + str(agent.status["connected"])
                ),
                diagnostics,
                default=True,
            ),
            pystray.MenuItem("Settings", lambda icon, item: settings()),
            pystray.MenuItem("Diagnostics", lambda icon, item: diagnostics()),
            pystray.MenuItem("Revoke web access", lambda icon, item: agent.revoke()),
            pystray.MenuItem("Operation logs", lambda icon, item: logs()),
            pystray.MenuItem(
                lambda item: "Install update…" if available else "Check for updates…", check_updates
            ),
            pystray.MenuItem("Exit", quit_app),
        ),
    )
    timer = threading.Timer(
        20, lambda: threading.Thread(target=automatic_update_check, daemon=True).start()
    )
    timer.daemon = True
    timer.start()
    icon.run()
