# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
# SPDX-License-Identifier: MIT
"""DDR-P3.40 regression guard — the published ip.yml version must be tagged.

rr_ddr_stream 0.3.2 was published to the registry while the default branch's
ip.yml said 0.3.2 and the repo carried no v0.3.2 tag, so the published version
could not be traced to a tag. routertl's watcher
``scripts/check_manifest_publish_drift.py`` (RTL-P2.1318) found it as
``manifest-untagged``; the fix was an annotated v0.3.2 tag, pushed by the owner.

This guard drives the SHIPPED watcher through its real configuration surface
(``--registry URL``) — nothing in it is monkeypatched or re-implemented here:

* a local registry stub (127.0.0.1) answers ``GET /publish/direct`` and
  ``GET /v1/routertl/rr_ddr_stream/<v>`` exactly as the watcher reads them,
  with ``git_url`` pointing at a bare git fixture in tmp_path;
* the fixture receives this checkout's HEAD WITHOUT tags.

RED leg: the watcher must report ``manifest-untagged`` for the ip.yml version
against the untagged fixture (proves the check is alive on this project's
content). GREEN leg: this repository's OWN ``v<version>`` tag is fetched into
the fixture and the same watcher run must come back clean. If this repository
has no tag for the version its ip.yml declares — the DDR-P3.40 defect, or a
future version bump published without tagging — the GREEN leg fails with
PUBLISHED_VERSION_UNTAGGED.

No outward network: the registry stub is loopback and the fixture is a local
path. The watcher is located through the importable routertl ``sdk`` package
(its checkout root holds ``scripts/``), or ``RR_MANIFEST_PUBLISH_DRIFT_SCRIPT``;
without either the module skips, so the suite stays runnable standalone.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE = "routertl/rr_ddr_stream"
NAMESPACE, NAME = PACKAGE.split("/")


def _watcher_script() -> Path:
    override = os.environ.get("RR_MANIFEST_PUBLISH_DRIFT_SCRIPT")
    if override:
        return Path(override)
    try:
        import sdk  # routertl checkout, bench-installed
    except ImportError:
        pytest.skip("routertl sdk not importable — cannot locate "
                    "scripts/check_manifest_publish_drift.py")
    script = Path(sdk.__file__).resolve().parents[1] / "scripts" / "check_manifest_publish_drift.py"
    if not script.is_file():
        pytest.skip(f"routertl checkout has no {script}")
    return script


def _git_env() -> dict[str, str]:
    # A pre-commit hook exports GIT_DIR/GIT_INDEX_FILE; never let them retarget
    # the fixture operations at the committing repo.
    env = {k: v for k, v in os.environ.items()
           if k not in ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          env=_git_env(), timeout=120)


def _manifest_version(rev: str) -> str:
    shown = _git("show", f"{rev}:ip.yml", cwd=REPO_ROOT)
    assert shown.returncode == 0, shown.stderr
    return str((yaml.safe_load(shown.stdout) or {}).get("version", ""))


class _Registry:
    """Loopback stand-in for the two registry endpoints the watcher reads."""

    def __init__(self, version: str, git_url: str) -> None:
        routes = {
            "/publish/direct": {"packages": [{
                "namespace": NAMESPACE, "name": NAME,
                "latest": version, "versions": [version],
            }]},
            f"/v1/{NAMESPACE}/{NAME}/{version}": {
                "namespace": NAMESPACE, "name": NAME, "version": version,
                "git_url": git_url,
            },
        }

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 (http.server API)
                body = routes.get(self.path)
                self.send_response(200 if body is not None else 404)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(body or {"detail": "not found"}).encode())

            def log_message(self, *_: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _run_watcher(script: Path, registry: str, cache: Path) -> tuple[int, dict]:
    env = _git_env()
    for k in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        env.pop(k, None)
    env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost"
    env["XDG_CACHE_HOME"] = str(cache)
    proc = subprocess.run([sys.executable, str(script), "--registry", registry, "--json"],
                          capture_output=True, text=True, env=env, timeout=300)
    assert proc.returncode in (0, 1), (
        f"watcher did not produce a verdict (rc={proc.returncode}):\n{proc.stderr}")
    return proc.returncode, json.loads(proc.stdout)


def _kinds(report: dict) -> list[str]:
    return [d["kind"] for d in report["drift"] if d["package"] == PACKAGE]


def test_published_manifest_version_is_tagged(tmp_path: Path) -> None:
    script = _watcher_script()
    version = _manifest_version("HEAD")
    assert version, "ip.yml at HEAD declares no version"
    tag = f"v{version}"

    fixture = tmp_path / "rr-ddr-stream.git"
    assert _git("init", "-q", "--bare", "-b", "main", str(fixture), cwd=tmp_path).returncode == 0
    # Project content WITHOUT tags (fetch into the bare fixture, so no push
    # hook of this repo is involved).
    got = _git("fetch", "-q", "--no-tags", str(REPO_ROOT), "HEAD:refs/heads/main", cwd=fixture)
    assert got.returncode == 0, got.stderr
    assert _git("tag", cwd=fixture).stdout.strip() == "", "fixture must start untagged"

    registry = _Registry(version, str(fixture))
    try:
        # RED leg — untagged fixture: the shipped watcher must flag it.
        rc, report = _run_watcher(script, registry.url, tmp_path / "cache-red")
        assert report["sources"][PACKAGE].get("version") == version, report["sources"]
        assert "manifest-unreadable" not in _kinds(report), report["drift"]
        assert rc == 1 and "manifest-untagged" in _kinds(report), (
            f"WATCHER_BLIND: the watcher did not report {PACKAGE} {version} as "
            f"manifest-untagged against an untagged fixture (rc={rc}): {report['drift']}")
        assert any(f"no {tag} tag" in d["detail"] for d in report["drift"]), report["drift"]

        # GREEN leg — this repository's own tag for the declared version. The
        # tag's presence is judged by the WATCHER, not by a local git helper:
        # when the tag is absent here the fetch brings nothing over, and the
        # watcher run below fails with its manifest-untagged report — the
        # DDR-P3.40 defect itself.
        _git("fetch", "-q", str(REPO_ROOT), f"refs/tags/{tag}:refs/tags/{tag}", cwd=fixture)

        rc, report = _run_watcher(script, registry.url, tmp_path / "cache-green")
        assert rc == 0 and report["drift"] == [], (
            f"PUBLISHED_VERSION_UNTAGGED: the shipped watcher reports {PACKAGE} "
            f"{version} as drifted with this repository's tags fetched (rc={rc}): "
            f"{report['drift']} — ip.yml declares {version} but no {tag} tag is "
            f"present (DDR-P3.40). Tag the published commit and push the tag.")
        assert tag in report["sources"][PACKAGE].get("tags", []), report["sources"]
    finally:
        registry.close()
