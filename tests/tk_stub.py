"""A Tk stand-in for tests (Tk is not available on the CI runners): records widgets and lets a test press buttons."""
from __future__ import annotations

import sys
import types
from unittest import mock


class Var:
    def __init__(self, value=None):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class Tk:
    def __init__(self, monkeypatch):
        self.bound: dict = {}
        self.protocols: list = []
        self.buttons: list[tuple[str, object, dict]] = []  # (text, command, kwargs), in creation order
        self.labels: list[mock.MagicMock] = []
        self.combobox: tuple | None = None
        self.variables: list[Var] = []
        self.root = mock.MagicMock(name="root")
        self.root.bind.side_effect = lambda key, fn: self.bound.__setitem__(key, fn)
        self.root.protocol.side_effect = lambda name, fn: self.protocols.append((name, fn))
        tk, ttk = types.ModuleType("tkinter"), types.ModuleType("tkinter.ttk")
        tk.Tk = lambda: self.root
        tk.BooleanVar = tk.StringVar = self._var
        tk.filedialog, tk.messagebox = mock.MagicMock(name="filedialog"), mock.MagicMock(name="messagebox")
        tk.Text = lambda *a, **k: mock.MagicMock()
        widget = lambda *a, **k: mock.MagicMock()  # noqa: E731
        ttk.Frame = ttk.Entry = ttk.Checkbutton = ttk.LabelFrame = ttk.Progressbar = widget
        ttk.Button, ttk.Label, ttk.Combobox = self._button, self._label, self._combobox
        tk.ttk = ttk
        self.filedialog, self.messagebox = tk.filedialog, tk.messagebox
        monkeypatch.setitem(sys.modules, "tkinter", tk)
        monkeypatch.setitem(sys.modules, "tkinter.ttk", ttk)
        monkeypatch.setitem(sys.modules, "tkinter.filedialog", tk.filedialog)
        monkeypatch.setitem(sys.modules, "tkinter.messagebox", tk.messagebox)

    def _var(self, value=None):
        self.variables.append(Var(value))
        return self.variables[-1]

    def _button(self, parent, text="", command=None, **kw):
        self.buttons.append((text, command, kw))
        return mock.MagicMock()

    def _label(self, parent, text="", **kw):
        label = mock.MagicMock()
        label.initial = text
        self.labels.append(label)
        return label

    def _combobox(self, parent, textvariable=None, values=(), **kw):
        self.combobox = (textvariable, list(values))
        return mock.MagicMock()

    def button(self, text):
        """The command of the last button with this text."""
        return next(command for label, command, _ in reversed(self.buttons) if label == text)

    def button_state(self, text):
        return next(kw.get("state") for label, _, kw in reversed(self.buttons) if label == text)

    def texts(self) -> list[str]:
        """Every label text, using the last text set with .config(text=...)."""
        out = []
        for label in self.labels:
            calls = [c.kwargs["text"] for c in label.config.call_args_list if "text" in c.kwargs]
            out.append(calls[-1] if calls else label.initial)
        return out
