"""Local mounted volumes and operator-enrolled Nokia/S60 protocol adapters."""

from __future__ import annotations

import json
import os
from pathlib import Path

from .connections import load_profiles
from .files import MountedVolumes
from .gammu_adapter import GammuAdapter
from .obex import ObexAdapter
from .wpd import WpdAdapter


class LocalTransports:
    def __init__(self, repo: Path | None = None, *, volume_identities: dict[str, str] | None = None):
        repo = repo or Path.cwd()
        self.files = MountedVolumes(json.loads(os.getenv("SYMPORT_DEVICE_ROOTS", "{}")), identities=volume_identities)
        self.gammu = GammuAdapter(repo, "gammu", lambda: os.getenv("SYMPORT_GAMMU_CONFIG"))
        self.adapters = {"gammu": self.gammu}
        self.configuration_error = None
        try:
            for profile in load_profiles(os.getenv("SYMPORT_CONNECTIONS_FILE")):
                if profile["id"] in self.files.roots:
                    raise ValueError("A connection identifier conflicts with an enrolled volume")
                self.adapters[profile["id"]] = (
                    GammuAdapter(
                        repo, profile["id"], profile["config"], profile["name"], profile["kind"]
                    )
                    if profile["kind"].startswith("gammu-")
                    else ObexAdapter(profile)
                )
        except (ValueError, OSError):
            self.configuration_error = "Connection configuration is invalid; existing mounted-volume and legacy Gammu settings remain available"
            self.adapters = {"gammu": self.gammu}
        if os.name == "nt" and os.getenv("SYMPORT_WPD", "1") != "0":
            # A Nokia that Windows shows as a Portable Device (MTP): read-only file access, no PIM, never a phone identity.
            self.wpd = WpdAdapter()
            self.adapters[self.wpd.id] = self.wpd
        self.last_connections = []

    @property
    def personal(self):
        return self.gammu.personal

    @personal.setter
    def personal(self, value):
        self.gammu.personal = value

    def devices(self) -> list[dict]:
        result = self.files.devices()
        self.last_connections = []
        for adapter in self.adapters.values():
            row = adapter.device()
            if row is not None:
                result.append(row)
                self.last_connections.append(
                    {
                        k: row.get(k)
                        for k in (
                            "id",
                            "name",
                            "transport",
                            "connection_kind",
                            "connected",
                            "capabilities",
                            "capability_matrix",
                            "capability_fingerprint",
                            "capabilities_checked_at",
                            "failure",
                            "model",
                            "rm",
                            "firmware",
                            "evidence",
                            "configured_target",
                        )
                    }
                )
        return result

    def diagnostics(self) -> list[dict]:
        reports = list(self.last_connections)
        if self.configuration_error:
            reports.append(
                {
                    "id": "configuration",
                    "connected": False,
                    "capabilities": [],
                    "evidence": self.configuration_error,
                }
            )
        return reports

    def execute(self, data: dict) -> dict:
        if data.get('action') == 'connection-host':
            from symport.bridge.discovery import host_candidates
            return host_candidates()
        adapter = self.adapters.get(data.get("device"))
        if adapter is not None:
            if data["action"] == "connection-diagnose":
                return {"connection": adapter.device()}
            return adapter.execute(data)
        if data.get("action") in ("media-list", "media-info", "media-chunk", "media-thumbnail"):
            from . import media

            return media.execute(self.files, data)
        if data.get("action") == "apps-inventory":
            root = self.files.roots.get(data.get("device"))
            if root is None or not root.is_dir() or root.is_symlink():
                raise ValueError("Unknown or disconnected device")
            from . import inventory

            return inventory.scan(root)
        return self.files.execute(data)
