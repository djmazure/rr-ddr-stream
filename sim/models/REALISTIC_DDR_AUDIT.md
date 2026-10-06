# Realistic DDR AXI slave model — audit of `cocotbext.axi.AxiRam` gaps

**Audience:** anyone writing a cocotb test against a DDR / AFI / HP-port AXI
master under routertl (`rr-ddr-stream`, `rr-vfifo`, `rr-idp`).

**Why this exists:** real silicon data-corruption bugs (e.g. `rr-idp IDP-T2.9`,
`rr-ddr-stream DDR-T3`) PASS simulation because the testbench models PS DDR with
`cocotbext.axi.AxiRam` — an *ideal* memory. This doc catalogs precisely which
realism knobs `AxiRam` already has and which it lacks versus a real Zynq-7 PS
DDR behind an AFI / S_AXI_HP port + DDR controller. The companion model
`realistic_ddr_axi.py` (in this dir) adds the missing realism on top of
`cocotbext.axi` (it subclasses the channel drivers, it does not reimplement
AXI).

Versions audited: `cocotbext-axi 0.1.26`
(`site-packages/cocotbext/axi/`).

---

## 1. What `AxiRam` / `AxiSlave` / `Memory` ALREADY models

`AxiRam` (`axi_ram.py`) = `AxiRamWrite` + `AxiRamRead`, each subclassing
`AxiSlaveWrite` / `AxiSlaveRead` (`axi_slave.py`) over a shared `Memory`
(`memory.py`, a `SparseMemory` byte array). The channel endpoints
(`AxiAWSink`, `AxiWSink`, `AxiBSource`, `AxiARSink`, `AxiRSource`) derive from
`StreamSink`/`StreamSource` (`stream.py`).

| Capability | Present? | Where / how |
|---|---|---|
| AXI4 burst types FIXED / INCR / WRAP | ✅ | `axi_slave.py` `_process_write`/`_process_read` (`AxiBurstType`) |
| `AWSIZE`/`ARSIZE` narrow-transfer addressing | ✅ | `num_bytes = 2**size`, `aligned_addr` math |
| 4 KiB boundary-cross assertion (INCR) | ✅ | `assert 0x1000-(aligned_addr & 0xfff) >= transfer_size` — but it **`assert`s**, it does not model the interconnect splitting |
| `WSTRB` byte-enable partial writes | ✅ | `_process_write` builds `write_ops` from contiguous strobe runs |
| `BID`/`RID` echo of `AWID`/`ARID` | ✅ | `b.bid = awid`, `r.rid = arid` |
| Per-channel backpressure / `*ready` cadence | ✅ (opt-in, **unused by default**) | every channel endpoint has `set_pause_generator()` (`stream.py` `StreamPause`); a test must wire it (`ram.write_if.w_channel.set_pause_generator(...)`). Default = no pauses, `queue_occupancy_limit=2` only |
| `BRESP=SLVERR` on a write-target exception | ✅ | `_process_write` try/except sets `b.bresp = AxiResp.SLVERR` |
| Mixed master/slave data width | ✅ (separate util) | `axil_master`/`axi_master` size-converters; the *slave* `AxiRam` is single-width |

So `AxiRam` is a faithful **AXI4 protocol** model with optional ready-cadence
backpressure. Its problem is the **memory timing/coherency model**, not the
handshake.

---

## 2. What `AxiRam` does NOT model (the silicon gaps)

A real Zynq-7 PS DDR through an AFI / S_AXI_HP port + multi-port DDR controller
behaves very differently from a byte array written synchronously on the W beat.

### G1 — Write-to-read commit latency (posted writes)
`AxiRamWrite._write` → `Memory.write` commits the byte array **synchronously,
the instant the W beat is received**, *before* BRESP. A real AFI/HP write is
**posted**: the data sits in the interconnect + DDR controller write buffer for
many cycles before it is actually in the DRAM array.
→ `AxiRam` has **zero** commit latency. A read issued one cycle after the write
always sees the new data.

### G2 — BRESP-before-commit (the classic AFI/HP hazard)
On real hardware the DDR controller can return `BVALID/BRESP=OKAY` as soon as
the write is **accepted into its buffer**, *not* when it reaches the array — an
*early* BRESP. A master that treats BRESP as "data is now globally visible"
(advances a reader-visible write pointer on BRESP) is then wrong: a reader can
race in before the posted write drains. `AxiRam` commits on the W beat and
BRESP follows that commit, so BRESP *is* a true commit barrier — the hazard is
structurally impossible.

### G3 — Read-after-write hazard window
Directly downstream of G1/G2: when a read targets an address with an **in-flight
(not-yet-committed) write**, real DDR may return **stale** data (the old array
contents), or the controller may insert a **hazard stall** to order them.
`AxiRam` always returns the freshly-written value (perfect RAW coherency). **This
is the IDP-T2.9 / DDR-T3 mechanism:** the read plane drains a ring slot whose
write has not yet committed and gets stale/scrambled data, while the *count* of
reads is correct.

### G4 — Concurrent read+write contention / shared-resource arbitration
`AxiSlaveWrite._process_write` and `AxiSlaveRead._process_read` run as two
**independent** coroutines over the shared `Memory`. They never contend: writes
commit instantly, reads serve instantly, neither throttles the other, there is
no shared-bandwidth ceiling, no bank/port arbiter. A real single HP port (one
AXI master doing **continuous concurrent** read+write, the IDP-T2.9 condition)
shares one DDR controller port: AW/AR compete for the command bus, W/R compete
for the data bus, and the arbiter interleaves them — which both *re-orders
commit-vs-drain* and *widens the RAW window* (G3). `AxiRam` models **none** of
this contention.

### G5 — AXI3 16-beat burst limit + burst splitting + `WID`
The real S_AXI_HP port is **AXI3** (`AWLEN`/`ARLEN` are 4-bit ⇒ 16-beat max).
`AxiRam` reads `awlen` as up-to-8-bit (AXI4, 256-beat) and **accepts** an
AWLEN>15 burst silently. On silicon the AXI4→AXI3 smartconnect must *split* a
long burst, and a mis-split drops beats. `AxiRam` also has no concept of AXI3
`WID` (write-data interleaving by ID). → It cannot flag an AXI3-illegal burst,
cannot model the split, cannot model `WID`.
(`rr-idp`/`rr-ddr-stream` partly cover this with the standalone
`axi3_write_bfm.py`, but that is **write-plane-only** and has no read plane, so
it cannot reproduce IDP-T2.9's scrambled *read*.)

### G6 — Interleaved / out-of-order read data by AXI ID
AXI permits read data for different `ARID`s to be **interleaved**, and write
responses / read completions to come back **out of order** across IDs. A real
DDR controller reorders to optimise row/bank access. `AxiSlaveRead` serves each
read burst **atomically and strictly in AR-acceptance order**, one burst fully
before the next. → No interleave, no per-ID reorder.

### G7 — DRAM refresh stalls (tREFI)
A DDR array periodically stops all traffic for a refresh (`tRFC`), on a `tREFI`
cadence (~7.8 µs). This produces **bursty stalls** on both R and W that can
deepen FIFOs and provoke the long bursts / pointer races. `AxiRam` never stalls
for refresh.

### G8 — Address-region (bank/row) open-page latency
First access to a closed DRAM row pays an activation penalty (`tRCD`+`tRP`); a
hit on an open row is fast. Access *pattern* therefore changes latency.
`AxiRam` is uniform-latency (effectively zero). → No bank/row model.

### G9 — Variable / jittered commit & read latency
Even absent refresh, real latency jitters (controller queue depth, port
arbitration). `AxiRam` is deterministic-zero. `CommitOrderedAxiRam` (see §3)
adds a *fixed* commit latency but not per-transfer jitter.

### G10 — Uncommitted-region poisoning (loud-fail option)
When a read overtakes an uncommitted write, returning stale-**zero** can look
like a benign zero rather than a defect. A model that returns `X` (or a poison
pattern) for an uncommitted region makes the overtaking read fail *loudly*.
`AxiRam` has no such mode.

---

## 3. Prior art already in the tree (build on it, don't duplicate)

`routertl/sdk/cocotb_helpers/axi_ram_commit.py` already ships
**`CommitOrderedAxiRam`** (RTL-P3.605 / DDR-T3). It subclasses `AxiRamWrite` and
defers the backing-store write by `commit_latency_cycles` clock cycles, so BRESP
*and* data-visibility are both delayed (it closes **G1**, and partially **G2/G3**
for a *single* outstanding write). Its own docstring lists what it defers:

> *Not in v0.1: concurrent outstanding writes; variable/refresh-jittered commit
> latency; read-side latency jitter; bank-conflict / arbitration modelling;
> uncommitted-region poisoning.*

That deferred list **is** gaps G4, G6–G10 — exactly what reproduces IDP-T2.9
(concurrent R+W contention widening the RAW window). So the realistic model in
this directory **wraps the same cocotbext channel drivers** and layers on:

- an **early-BRESP** mode (G2) — BRESP returns *before* the deferred commit;
- a shared **R+W arbiter / bandwidth ceiling** (G4) that serialises and
  interleaves the two planes on one port;
- a **RAW-hazard policy** (G3): `stale` (return pre-commit memory) or `stall`
  (delay the read until commit) or `poison` (G10);
- **AXI3** enforcement + optional split + `WID` flagging (G5);
- **refresh** stall windows (G7), **bank/row** open-page latency (G8),
  **jittered** latencies (G9);
- **out-of-order / interleaved** completion by ID (G6);
- per-channel **ready-cadence** generators (re-exposing the cocotbext pause
  machinery through one config object).

All knobs are independently toggleable, with an **`ideal` preset** that behaves
exactly like today's `AxiRam` so existing green tests stay green.

---

## 4. IDP-T2.9 mechanism this model targets

`rr-idp/hw/rtl/idp_data_plane.vhd` (G_SELF_CHECK=true) instantiates ONE
`rr.ddr_stream_axi` with **both** write plane (uploader) and read plane
(downloader) enabled on **one shared AXI master** to HP0, run **concurrently**:
the downloader drains DDR while the uploader fills it (continuous SPSC). On
silicon the read plane returns the **right count** (`SELFCHK_RD_CNT=1024`) but
**scrambled data** (`SELFCHK_ERR_CNT=985/1024`); the write extent is correct
(`RING_COMMIT_PTR=0x1000`). The sim passes with `AxiRam` because `AxiRam` has
perfect RAW coherency (G3) and zero contention (G4): the downloader can never
read a ring slot before the uploader's write to it has committed. The realistic
model's **concurrent R+W contention + write-commit latency + RAW=stale** knobs
recreate exactly the window where the downloader overtakes an uncommitted write.

**Silicon refinement (rr-idp ctx 2026-06-23):** two DDR mrd passes (immediate vs
+3 s settle) on the Classic were *identical* (0/1024 match both) ⇒ the committed
DDR image is **permanently** scrambled — write-side corruption, not purely a
transient read-vs-commit race. The mechanism is that the read plane's concurrent
traffic on the **shared** HP0 port disturbs the uploader / async-FIFO /
width-converter path so the *write data itself* lands wrong (bit30 dropped,
~+62 ramp offset, per-128-bit-beat disturbance, value duplication).

> **A read-side hazard is NOT the faithful repro.** `raw_hazard="stale"`
> reproduces only the *observable verdict* (the read-plane comparator sees the
> right COUNT, wrong DATA, verdict=FAIL) while leaving *committed* memory
> correct. Since the silicon mrd is permanently 0/1024 (write-side), a `stale`
> read-side hazard MISSES the actual defect — verifying the comparator verdict
> instead of the backing store would be a false "reproduced". No
> `corrupt_commit` / inject-corruption knob is acceptable either: injecting
> corruption fakes the bug. **The DUT must scramble its OWN writes.**

**RETRACTED — the sim "faithful repro" was a HARNESS ARTIFACT (IDP-T2.9,
2026-06-23, hint H.139).** An earlier draft of this section claimed the
coherent-contention sweep reproduced the silicon write-side scramble in sim
(stored 72/256 etc., backing-store-gated). That was **wrong**. The "scramble"
was a cocotb multi-instance artifact, not a DUT bug: `test_idp_t29_repro` rebuilt
a fresh `RealisticDdrAxiRam` per cadence WITHOUT teardown, and `cocotbext.axi`
`AxiSlave` discards its `_run_reset` watcher handle and re-spawns the channel
processors on every reset de-assert — so by profile #2 several **ghost** monitors
were all `recv()`-ing the SAME `M_AXI` W/AW bus and committing to dead backing
stores; the live model MISSED the stolen beats and its store looked displaced.

Proof (committed in `rr-idp`):
- `test_idp_t29_wobserve` — a wire tap of the DUT W-channel with a **single**
  model: **256/256 byte-perfect**, zero addr/data divergence, `WSTRB=0xffff`,
  sequential `AWADDR`. The DUT drives a flawless packed ramp.
- `test_idp_t29_ghostprobe` — toggling whether the stale monitors are silenced
  flips `shared_dense_bp` **124/256 ↔ 256/256**. The only variable is the ghosts.
- With `RealisticDdrAxiRam.stop()` teardown between profiles, `test_idp_t29_repro`
  now reports **256/256 on all 6 cadences** — the DUT write path is clean under
  every coherent shared-port contention cadence.

**Conclusion:** the DUT `ddr_stream_axi` write path is **clean in zero-delay
functional sim** even under realistic shared-port R/W contention. The silicon
IDP-T2.9 scramble is therefore **NOT functional-sim-reproducible** — it is
silicon-physical (timing / CDC at the 125:100 crossing / HP0 interconnect /
AXI3-split), pursued on hardware via the `G_DEBUG_CAPTURE` on-wire-capture
bitstream, not here. The model is still the right tool (it correctly stresses the
bus); the lesson is to verify a sim "repro" **on the wire** with **one slave per
bus per elaboration** (tear models down or use `rr sim sweep`), and to add a
`stop()`/teardown to any cocotbext slave rebuilt on a shared bus.
