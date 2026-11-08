"""compose.deploy.yaml must work from one pasted file: published image, no build, no repo-relative paths."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")
ROOT = Path(__file__).resolve().parents[1]
deploy = yaml.safe_load((ROOT / "compose.deploy.yaml").read_text(encoding="utf-8"))
local = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
workflow = yaml.safe_load((ROOT / ".github/workflows/publish-image.yml").read_text(encoding="utf-8"))


def test_single_file_uses_the_published_image_and_never_builds():
    service = deploy["services"]["workbench"]
    assert "build" not in service and "build" not in str(deploy["services"]["mgo-symbian"])
    assert service["image"].startswith("${SYMPORT_IMAGE:-ghcr.io/grayhex/s60modern:")
    text = (ROOT / "compose.deploy.yaml").read_text(encoding="utf-8")
    assert "env_file" not in text and not re.search(r"""(^|[\s"':])\.{1,2}/""", text, re.M)
    assert not any(isinstance(v, dict) and v.get("type") == "bind" for v in service["volumes"])


def test_deployment_keeps_the_security_posture_of_the_local_compose():
    service, base = deploy["services"]["workbench"], local["services"]["workbench"]
    for key in ("cap_drop", "security_opt", "pids_limit", "init", "healthcheck", "command"):
        assert service[key] == base[key], key
    assert service["ports"][0].startswith("${SYMPORT_BIND:-127.0.0.1}:"), "nothing is published beyond loopback until the operator names the NAS address"
    lan_keys = {"SYMPORT_ACCESS_MODE", "SYMPORT_BIND", "SYMPORT_LAN_HOST", "SYMPORT_ADMIN_PASSWORD",
                "SYMPORT_ADMIN_PASSWORD_HASH", "SYMPORT_ADMIN_PASSWORD_FILE", "SYMPORT_SESSION_IDLE_MINUTES",
                "SYMPORT_SESSION_MAX_MINUTES"}
    assert set(service["environment"]) == set(base["environment"]) | lan_keys
    assert deploy["networks"]["emulator"]["internal"] is True
    assert deploy["services"]["mgo-symbian"]["profiles"] == ["emulator"]
    assert deploy["services"]["mgo-symbian"]["image"] == local["services"]["mgo-symbian"]["image"]


def test_publish_workflow_gates_every_architecture_on_a_smoke_test():
    job = workflow["jobs"]["build"]
    steps = [s.get("name", "") for s in job["steps"]]
    smoke = next(i for i, n in enumerate(steps) if n.startswith("Smoke test"))
    push = next(i for i, n in enumerate(steps) if n.startswith("Push by digest"))
    assert smoke < push, "an image is pushed only after its smoke test passed"
    platforms = {m["platform"]: m["required"] for m in job["strategy"]["matrix"]["include"]}
    assert platforms == {"linux/amd64": True, "linux/arm64": False}
    assert job["strategy"]["fail-fast"] is False
    manifest = workflow["jobs"]["manifest"]
    assert manifest["needs"] == "build" and "needs.build.result == 'success'" in manifest["if"]
    tags = next(s for s in manifest["steps"] if s.get("id") == "meta")["with"]["tags"]
    assert "value=latest,enable={{is_default_branch}}" in tags and "type=semver,pattern={{version}}" in tags
    assert workflow["permissions"] == {"contents": "read"} and job["permissions"]["packages"] == "write"
    assert workflow[True]["push"]["tags"] == ["v*.*.*", "[0-9]*.[0-9]*.[0-9]*"]


def test_home_lan_is_the_default_mode_and_ships_no_credentials():
    service = deploy["services"]["workbench"]
    env = service["environment"]
    assert env["SYMPORT_ACCESS_MODE"] == "${SYMPORT_ACCESS_MODE:-lan}"
    assert env["SYMPORT_BIND"] == "${SYMPORT_BIND:-127.0.0.1}", "the app must see the address Docker publishes"
    assert env["SYMPORT_ADMIN_PASSWORD"] == "${SYMPORT_ADMIN_PASSWORD:-}", "no default or example password"
    assert env["SYMPORT_ADMIN_PASSWORD_HASH"] == "${SYMPORT_ADMIN_PASSWORD_HASH:-}"
    assert service["ports"] == ["${SYMPORT_BIND:-127.0.0.1}:${SYMPORT_HTTP_PORT:-8765}:${SYMPORT_HTTP_PORT:-8765}"]
    text = "\n".join(line for line in (ROOT / "compose.deploy.yaml").read_text(encoding="utf-8").splitlines()
                     if not line.lstrip().startswith("#"))  # comments may show an example address
    assert "0.0.0.0:" not in text and "192.168." not in text, "no LAN address is hardcoded in the product"
    probe = " ".join(service["healthcheck"]["test"])
    assert "/healthz" in probe and "/api/state" not in probe, "the probe must work without a login"
    assert "/healthz" in " ".join(local["services"]["workbench"]["healthcheck"]["test"])
    assert "/healthz" in (ROOT / "Dockerfile").read_text(encoding="utf-8")
