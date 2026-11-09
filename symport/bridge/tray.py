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


def settings_command(root: Path, port: int) -> list[str]:
    """The settings window runs in its own process: Tk owns that process' main thread, so the window can
    always be closed and never shares an event loop with the tray icon."""
    if getattr(sys, "frozen", False):
        base = [sys.executable]
    else:
        base = [sys.executable, "-m", "symport.bridge.main"]
    return [*base, "--settings", "--data-dir", str(root), "--port", str(port)]


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

    def diagnostics():
        import ctypes

        status = agent.status
        text = (
            "Version "
            + VERSION
            + "\nState: "
            + str(status["state"])
            + "\nConnected: "
            + str(status["connected"])
            + "\nNokia candidates: "
            + str(status["candidates"])
            + "\n\n"
            + "\n".join(status.get("diagnostics", []))
            + "\n\nOperation logs contain action/status only. Settings and private backups: "
            + str(agent.root)
        )
        ctypes.windll.user32.MessageBoxW(None, text, "SymPort Bridge diagnostics", 0x40)

    def logs():
        path = agent.manager.journal
        if path.exists():
            os.startfile(path)
        else:
            diagnostics()

    def quit_app(icon, item):
        for process in settings_process:
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
