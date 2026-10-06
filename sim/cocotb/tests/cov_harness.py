"""Prototype: randomized back-pressure profiles + functional coverage for cocotb.

The methodology RTL-P2.748 asks `rr` to standardise, shown working here. Two
reusable pieces:

  * randomized_ready(...) — a constrained-random pause generator for any
    `ready`/`valid` handshake, parameterised by a back-pressure PROFILE
    (none / light / heavy / ddr_cadence). Sustained-latency profiles are what
    actually exercise FIFO-accumulation corners (cf. IDP-T2.5).
  * FunctionalCoverage — declare bins + crosses, sample observed conditions,
    report which bins were HIT. A green run with empty bins is a yellow flag —
    the coverage the contract should demand, not just "a test exists".

This is deliberately small + dependency-free (no cocotb_coverage) so it can be
lifted into the SDK as the default harness.
"""
from __future__ import annotations

import random


# ── randomized back-pressure ────────────────────────────────────────────────
PROFILES = {
    "none":        dict(stall_prob=0.0,  max_stall=0),
    "light":       dict(stall_prob=0.15, max_stall=3),
    "heavy":       dict(stall_prob=0.55, max_stall=10),
    # Sustained stalls model DDR row-activation latency — the corner that grows
    # the egress FIFO and provokes long bursts (the IDP-T2.5 trigger).
    "ddr_cadence": dict(stall_prob=0.25, max_stall=64),
}


def randomized_ready(profile: str = "light", seed: int = 0):
    """Return a generator yielding True (stall) / False (accept) for a ready
    signal, per the named back-pressure profile. Use as a pause source."""
    cfg = PROFILES[profile]
    rng = random.Random(seed)
    stall_left = 0

    def gen():
        nonlocal stall_left
        while True:
            if stall_left > 0:
                stall_left -= 1
                yield True
                continue
            if rng.random() < cfg["stall_prob"]:
                stall_left = rng.randint(1, max(1, cfg["max_stall"]))
                yield True
            else:
                yield False
    return gen()


# ── functional coverage ──────────────────────────────────────────────────────
class CovBin:
    def __init__(self, name, edges, labels=None):
        self.name = name
        self.edges = edges                 # ascending upper-inclusive bin edges
        self.labels = labels or [f"<= {e}" for e in edges] + [f"> {edges[-1]}"]
        self.hits = {lbl: 0 for lbl in self.labels}

    def sample(self, value):
        for i, e in enumerate(self.edges):
            if value <= e:
                self.hits[self.labels[i]] += 1
                return
        self.hits[self.labels[-1]] += 1

    def covered(self):
        return sum(1 for v in self.hits.values() if v > 0)

    def total(self):
        return len(self.labels)


class FunctionalCoverage:
    def __init__(self):
        self.bins: dict[str, CovBin] = {}
        self.values: dict[str, set] = {}    # plain value-coverage (enums)

    def declare_bin(self, name, edges, labels=None):
        self.bins[name] = CovBin(name, edges, labels)

    def declare_values(self, name):
        self.values[name] = set()

    def sample(self, name, value):
        if name in self.bins:
            self.bins[name].sample(value)
        elif name in self.values:
            self.values[name].add(value)

    def report(self, log) -> list[str]:
        """Log per-bin hit counts; return the list of UNCOVERED bins."""
        uncovered = []
        for b in self.bins.values():
            log.info(f"[cov] {b.name}: {b.covered()}/{b.total()} bins "
                     f"-> {b.hits}")
            uncovered += [f"{b.name}:{lbl}" for lbl, n in b.hits.items() if n == 0]
        for name, seen in self.values.items():
            log.info(f"[cov] {name}: values seen = {sorted(seen)}")
        return uncovered
