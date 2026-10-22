#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
# SPDX-License-Identifier: MIT
"""Public release channel for rr_ddr_stream (DDR-P2.31).

The private repository keeps its full history, and some of that history (and
the trees of tags v0.2.1, v0.3.2, v0.3.3, v0.3.4) carries restricted names. The
public face is a separate mirror of sanitized squash commits. A registry entry
must resolve from that mirror, never from the private repository: 0.3.2 and
0.3.4 were published from the private checkout, so their ``git_url`` named the
private history and 0.3.4 could not be fetched by a public consumer.

Three verbs, none of which writes to a network remote:

``check --ref REF``
    Build the public tree of REF (REF minus ``PRIVATE_PATHS``) and scan it with
    the restricted-name scanner, with NO further exclusions. Exit 0 clean,
    1 hits, 2 scanner missing or broken. Nothing is ever "clean" unscanned.

``snapshot --ref REF --mirror PATH``
    Squash the public tree of REF into one commit on top of the local mirror
    checkout's HEAD, re-scan that exact commit inside the mirror, and only then
    move the mirror branch and tag ``v<ip.yml version>``. It never pushes; it
    prints the push command for the owner.

``publish --mirror PATH``
    The release-time guard in front of ``rr pkg publish``. Refuses unless the
    mirror's origin is the public mirror URL, its work tree is clean, HEAD
    carries the ``v<ip.yml version>`` tag, and HEAD's tree scans clean with no
    exclusions. Only then does it run ``rr pkg publish`` from the mirror, so the
    registry records the public ``git_url``.

The restricted-name list is private and is never copied into this repo. The
scanner is found at ``PUBLIC_TREE_SCAN_SCRIPT`` or the owner-bench default
path. Its contract: ``<scanner> <repo> --ref <ref> [--exclude P ...]``; exit 0
with a ``HITS: 0`` line is clean, exit 1 with ``name#``/``ident#`` lines is
hits, anything else is an error.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

# Tracked paths that never reach the public mirror. They hold bench evidence
# and a board example that carry restricted names by design.
PRIVATE_PATHS = (
    "docs/internal/",
    "example/axe5_eagle_lpddr4_validator/",
)

PUBLIC_MIRROR_URL = "https://github.com/djmazure/rr-ddr-stream"

DEFAULT_SCANNER = Path.home() / ".agent-wt" / "mc" / "bin" / "public_tree_scan.py"

EXIT_CLEAN, EXIT_HITS, EXIT_ERROR = 0, 1, 2

_SCAN_IDENT = {
    "GIT_AUTHOR_NAME": "public-release",
    "GIT_AUTHOR_EMAIL": "public-release@localhost",
    "GIT_COMMITTER_NAME": "public-release",
    "GIT_COMMITTER_EMAIL": "public-release@localhost",
}


class ReleaseRefused(Exception):
    """The release must not proceed; ``code`` is the process exit code."""

    def __init__(self, message: str, code: int = EXIT_HITS):
        super().__init__(message)
        self.code = code


def _git_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    # A pre-commit hook exports GIT_DIR/GIT_INDEX_FILE; never let them retarget
    # these operations at the committing repo.
    env = {k: v for k, v in os.environ.items()
           if k not in ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    if extra:
        env.update(extra)
    return env


def _git(repo: Path, *args: str, env: dict[str, str] | None = None,
         input: str | None = None, check: bool = True) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, env=env or _git_env(), input=input)
    if check and proc.returncode != 0:
        raise ReleaseRefused(
            f"git {' '.join(args)} failed in {repo}: {proc.stderr.strip()}",
            EXIT_ERROR)
    return proc.stdout


def find_scanner() -> Path:
    override = os.environ.get("PUBLIC_TREE_SCAN_SCRIPT")
    path = Path(override) if override else DEFAULT_SCANNER
    if not path.is_file():
        raise ReleaseRefused(
            f"PUBLIC_RELEASE_SCANNER_MISSING: {path} not found; a release is "
            "never made from an unscanned tree", EXIT_ERROR)
    return path


def scan(repo: Path, ref: str) -> list[str]:
    """Scan ``ref`` in ``repo`` with no exclusions; return the hit lines.

    Raises ReleaseRefused(EXIT_ERROR) when the scanner gives no verdict.
    """
    scanner = find_scanner()
    proc = subprocess.run([sys.executable, str(scanner), str(repo), "--ref", ref],
                          capture_output=True, text=True, env=_git_env())
    lines = (proc.stdout + proc.stderr).splitlines()
    hits = [l for l in lines if l.startswith(("name#", "ident#"))]
    if proc.returncode == 0 and "HITS: 0" in lines and not hits:
        return []
    if proc.returncode == 1 and hits:
        return hits
    raise ReleaseRefused(
        f"PUBLIC_RELEASE_SCAN_ERROR: scanner exited {proc.returncode} without "
        "a verdict:\n" + "\n".join(lines[-20:]), EXIT_ERROR)


def _is_private(path: str) -> bool:
    return any(path.startswith(p) for p in PRIVATE_PATHS)


def public_tree(repo: Path, ref: str) -> str:
    """Tree sha of ``ref`` with every PRIVATE_PATHS entry removed."""
    listing = _git(repo, "ls-tree", "-r", "-z", "--full-tree", ref)
    keep = [e for e in listing.split("\0")
            if e and not _is_private(e.split("\t", 1)[1])]
    with tempfile.TemporaryDirectory() as tmp:
        env = _git_env({"GIT_INDEX_FILE": str(Path(tmp) / "index")})
        _git(repo, "read-tree", "--empty", env=env)
        _git(repo, "update-index", "-z", "--index-info", env=env,
             input="".join(e + "\0" for e in keep))
        return _git(repo, "write-tree", env=env).strip()


def public_commit(repo: Path, ref: str) -> str:
    """A parentless commit object holding the public tree of ``ref``."""
    tree = public_tree(repo, ref)
    return _git(repo, "commit-tree", tree, "-m", f"public tree of {ref}",
                env=_git_env(_SCAN_IDENT)).strip()


def _refuse_hits(where: str, hits: list[str]) -> None:
    if hits:
        raise ReleaseRefused(
            f"PUBLIC_RELEASE_REFUSED: {where} contains {len(hits)} restricted "
            "entry hit(s):\n" + "\n".join(hits[:20]), EXIT_HITS)


def manifest_version(repo: Path, ref: str) -> str:
    data = yaml.safe_load(_git(repo, "show", f"{ref}:ip.yml")) or {}
    version = str(data.get("version", "")).strip()
    if not version:
        raise ReleaseRefused(f"no version in {ref}:ip.yml", EXIT_ERROR)
    return version


def cmd_check(repo: Path, ref: str) -> str:
    commit = public_commit(repo, ref)
    _refuse_hits(f"public tree of {ref}", scan(repo, commit))
    return f"PUBLIC_RELEASE_CLEAN: public tree of {ref} scanned with 0 hits"


def cmd_snapshot(repo: Path, ref: str, mirror: Path) -> str:
    version = manifest_version(repo, ref)
    tag = f"v{version}"
    if _git(mirror, "status", "--porcelain").strip():
        raise ReleaseRefused(f"mirror {mirror} has uncommitted changes", EXIT_ERROR)
    if _git(mirror, "tag", "--list", tag).strip():
        raise ReleaseRefused(f"mirror {mirror} already has tag {tag}", EXIT_ERROR)

    commit = public_commit(repo, ref)
    _refuse_hits(f"public tree of {ref}", scan(repo, commit))

    tmp_ref = "refs/public-release/snapshot"
    _git(repo, "update-ref", tmp_ref, commit)
    try:
        _git(mirror, "fetch", "--quiet", "--no-tags", str(repo.resolve()), tmp_ref)
    finally:
        _git(repo, "update-ref", "-d", tmp_ref)
    tree = _git(mirror, "rev-parse", f"{commit}^{{tree}}").strip()

    head = _git(mirror, "rev-parse", "--verify", "--quiet", "HEAD",
                check=False).strip()
    parent = ["-p", head] if head else []
    squash = _git(mirror, "commit-tree", tree, *parent,
                  "-m", f"rr_ddr_stream {version}").strip()
    # Re-scan the exact commit that would be pushed, in the mirror itself.
    _refuse_hits(f"mirror commit {squash}", scan(mirror, squash))

    branch = _git(mirror, "symbolic-ref", "--short", "HEAD").strip()
    _git(mirror, "update-ref", f"refs/heads/{branch}", squash)
    _git(mirror, "reset", "--hard", "--quiet", squash)
    _git(mirror, "tag", "-a", tag, "-m", f"rr_ddr_stream {version}", squash)
    return (f"PUBLIC_SNAPSHOT_READY: {tag} = {squash} in {mirror} (0 hits). "
            f"NOT pushed. Push needs an owner go: "
            f"git -C {mirror} push origin {branch} {tag}")


def _normalize_url(url: str) -> str:
    url = url.strip()
    m = re.match(r"^[\w.-]+@([^:]+):(.+)$", url)
    if m:
        url = f"https://{m.group(1)}/{m.group(2)}"
    url = re.sub(r"^ssh://[\w.-]+@", "https://", url)
    return url.rstrip("/").removesuffix(".git")


def cmd_publish(mirror: Path, public_url: str, rr: str,
                extra: list[str]) -> int:
    origin = _git(mirror, "remote", "get-url", "origin", check=False).strip()
    if _normalize_url(origin) != _normalize_url(public_url):
        raise ReleaseRefused(
            f"PUBLIC_RELEASE_REFUSED: origin of {mirror} is {origin or '(none)'}"
            f", not the public mirror {public_url}; the registry would record "
            "a git_url a public consumer cannot fetch", EXIT_HITS)
    if _git(mirror, "status", "--porcelain").strip():
        raise ReleaseRefused(
            f"PUBLIC_RELEASE_REFUSED: {mirror} has uncommitted changes", EXIT_HITS)
    tag = f"v{manifest_version(mirror, 'HEAD')}"
    tagged = _git(mirror, "rev-parse", "--verify", "--quiet",
                  f"refs/tags/{tag}^{{commit}}", check=False).strip()
    if tagged != _git(mirror, "rev-parse", "HEAD").strip():
        raise ReleaseRefused(
            f"PUBLIC_RELEASE_REFUSED: HEAD of {mirror} is not tagged {tag}",
            EXIT_HITS)
    _refuse_hits(f"{tag} in {mirror}", scan(mirror, f"refs/tags/{tag}"))
    print(f"PUBLIC_RELEASE_CLEAN: {tag} in {mirror} scanned with 0 hits; "
          "running rr pkg publish")
    return subprocess.run([rr, "pkg", "publish", *extra], cwd=mirror).returncode


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="verb", required=True)
    c = sub.add_parser("check", help="scan the public tree of a ref")
    c.add_argument("--repo", type=Path, default=Path("."))
    c.add_argument("--ref", default="HEAD")
    s = sub.add_parser("snapshot", help="squash a ref into the local mirror")
    s.add_argument("--repo", type=Path, default=Path("."))
    s.add_argument("--ref", required=True)
    s.add_argument("--mirror", type=Path, required=True)
    p = sub.add_parser("publish", help="guarded rr pkg publish from the mirror")
    p.add_argument("--mirror", type=Path, required=True)
    p.add_argument("--public-url", default=PUBLIC_MIRROR_URL)
    p.add_argument("--rr", default=os.environ.get("RR_BIN", "rr"))
    p.add_argument("rr_args", nargs="*", help="passed to rr pkg publish")
    args = ap.parse_args(argv)
    try:
        if args.verb == "check":
            print(cmd_check(args.repo, args.ref))
        elif args.verb == "snapshot":
            print(cmd_snapshot(args.repo, args.ref, args.mirror))
        else:
            return cmd_publish(args.mirror, args.public_url, args.rr, args.rr_args)
    except ReleaseRefused as exc:
        print(str(exc), file=sys.stderr)
        return exc.code
    return EXIT_CLEAN


if __name__ == "__main__":
    sys.exit(main())
