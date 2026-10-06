# ddr_stream_cdc_scoped_microchip.sdc — scoped CDC timing constraints for
# ddr_stream_axi / ddr_stream_fic, MICROCHIP LIBERO SmartTime edition.
#
# MICROCHIP/LIBERO TWIN of constraints/ddr_stream_cdc_scoped.xdc (Vivado) and
# constraints/ddr_stream_cdc_scoped.sdc (Quartus). Libero SmartTime is a
# Synopsys-style analyzer (create_clock / set_false_path / set_max_delay /
# get_cells / get_pins), NOT Quartus TimeQuest. This file therefore uses ONLY
# SmartTime-supported SDC commands — it deliberately contains none of the
# Quartus-only forms the Altera twin uses (`get_registers`, `set_net_delay`,
# `set_max_skew`, `post_message`, `get_collection_size`), which SmartTime cannot
# parse. Listing the Quartus .sdc as the PolarFire CDC constraint is the DE25
# DDR-T2.20 fail-open in a new dialect (YAML says a constraint is present;
# SmartTime has nothing it can score) — this file exists so the PolarFire target
# (targets/pfsoc_disco_fic.yml) organizes a constraint SmartTime can actually
# apply.
#
# ── WHAT IS AND IS NOT VERIFIED (read before trusting a slack from this file) ──
# VERIFIED at the desk (DDR-P2.17): dialect — every command below is a
#   SmartTime-legal Synopsys SDC command; the crossing set mirrors the proven
#   Vivado XDC (report_cdc: 114 endpoints, 0 Unsafe on the Zynq validators).
# NOT VERIFIED at the desk (needs Libero on the bench + RTL-P2.1273):
#   1. That the get_cells selectors below bind the intended registers in a
#      SynplifyPro/Libero netlist. The selectors use the RTL/olo INSTANCE-label
#      fragments (i_ddr_fifo_intf / i_rst_cc / i_sync_cdc_* / *_base_addr_* /
#      *_ptr_addr_*), which are synthesis-tool-independent, but Libero's exact
#      hierarchical-name spelling and register-array suffix are unconfirmed here.
#   2. A real SmartTime slack. RTL-P2.1273 (the Microchip organize_tool_files
#      gap) means an imported SDC does NOT yet reach SmartTime, so no scored
#      number is obtainable from this worktree — that is the ticket-acknowledged
#      block. When RTL-P2.1273 lands, this file is what SmartTime consumes.
#   The on-silicon read/write witness is DDR-P2.18.
#
# ── DIALECT TRANSLATION (Vivado XDC -> Libero SmartTime) ──
#   * The XDC's `set_max_delay -datapath_only <ns>` has no SmartTime equivalent
#     (`-datapath_only` is Vivado-only). SmartTime `set_max_delay <ns>` is the
#     correct CDC bound for a MULTI-BIT coherent crossing (gray pointers,
#     base-addr MCP, SW-pointer MCP): it RELAXES the crossing off the default
#     single-cycle setup requirement AND caps the datapath latency to <ns>, so
#     all bits of the bus land within one capture window (the coherency the XDC's
#     8 ns window and the Quartus set_max_skew both enforce). This satisfies the
#     DDR-T1.3 owner rule "a crossing must carry a BOUND; a bare false path is a
#     defect" — every data/MCP crossing below is BOUNDED, not merely cut.
#   * Single-bit 2-4-stage synchronisers (rr.sync_cdc stages_r[0]) tolerate
#     metastability by construction and carry no multi-bit coherency requirement,
#     so they are `set_false_path` (a 1-bit async capture needs no datapath bound).
#   * The olo reset synchroniser's ASYNC assertion into the other domain
#     (olo_base_cc_reset) is a genuine async-reset-assertion crossing whose
#     recovery/removal check is intentionally not timed (safe deassertion comes
#     from the synced shift+ack); it is `set_false_path`. NOT the "glitch"
#     fallacy — a real async-reset-deassertion CDC.
#   * 8.000 ns is the XDC's proven validator bound (min(clk_sys 10 ns,
#     clk_ddr 8 ns)). It ships here as a conservative default; when the PolarFire
#     consumer's clk_sys/clk_ddr are known (MSS fabric clocks, DDR-P2.18) re-tune
#     it to the tighter destination clock period. A looser real clock only makes
#     this bound stricter-than-needed — it can catch an unbounded route, never
#     relax a met one.

# ---------------------------------------------------------------------------
# 0) CROSSING-CLOCK DEFINITIONS (DDR-P2.21 — was the fail-open below).
#
# Every set_max_delay / set_false_path below is an INTER-CLOCK exception. On a
# Synopsys-style analyzer (Libero SmartTime) an inter-clock exception can only be
# SCORED against a path whose BOTH bounding clocks are DEFINED. This file
# previously carried the exceptions with NO create_clock, so SmartTime had zero
# clocks, scored ZERO constrained paths, and VERIFYTIMING reported clean for ANY
# design — the DDR-P2.18 "vacuous CDC gate" the land was refused over. The two
# crossing domains are defined here so the exceptions bind real paths and
# VERIFYTIMING has a non-zero constrained-path count to report.
#
# Clocks are declared on the REAL boundary clock ports of the design this SDC
# constrains: ddr_stream_fic (rtl/ddr_stream_fic.vhd) exposes clk_sys_i and
# clk_ddr_i as its two async-domain input ports. A create_clock naming a port the
# consuming entity does NOT expose is an empty get_ports selection — an SDC error
# (SDC0023) that ABORTS the Libero flow, and scores nothing: it is exactly the
# fail-open this section removes, not a fix for it. So these targets are the two
# ports ddr_stream_fic actually has — no speculative union with a validator-only
# net (e.g. an internal clk_i), which would match nothing on this IP and re-open
# the fail-open. Periods: clk_sys 10.000 ns (100 MHz), clk_ddr 8.000 ns (the
# tighter DDR side, matching the 8 ns crossing bound used below).
#
# NOT VERIFIED at the desk (RTL-P2.1273 — Libero cannot run from this worktree,
# so no scored SmartTime number is obtainable here): that these create_clock
# sources reconcile with the CCC's own create_generated_clock for the fabric
# clock in a full MSS build. Reconciling with an authoritative CCC clock name is
# the bench step when RTL-P2.1273 lands; the scoped IP constraint needs these
# clocks defined today so the crossing exceptions are not vacuous.
# ---------------------------------------------------------------------------
create_clock -name clk_sys -period 10.000 [get_ports {clk_sys_i}]
create_clock -name clk_ddr -period  8.000 [get_ports {clk_ddr_i}]

# ---------------------------------------------------------------------------
# A) olo gray-pointer + reset-ack data crossings under i_ddr_fifo_intf
#    (olo_base_cc_bits RegIn -> Reg0/RegN), the olo reset-request shift regs
#    (olo_base_cc_reset RstRqst*), the base-address MCP first capture
#    (*_base_addr_meta), and the SW-pointer MCP data captures
#    (*_ptr_addr_ddr). Multi-bit coherent crossings -> bounded set_max_delay.
# ---------------------------------------------------------------------------
set_max_delay 8.000 \
    -from [get_cells -hierarchical {*}] \
    -to   [get_cells -hierarchical {*i_ddr_fifo_intf*Reg0*}]
set_max_delay 8.000 \
    -from [get_cells -hierarchical {*}] \
    -to   [get_cells -hierarchical {*i_ddr_fifo_intf*RegN*}]
set_max_delay 8.000 \
    -from [get_cells -hierarchical {*}] \
    -to   [get_cells -hierarchical {*i_rst_cc*RstRqst*}]
set_max_delay 8.000 \
    -from [get_cells -hierarchical {*}] \
    -to   [get_cells -hierarchical {*base_addr_meta*}]
set_max_delay 8.000 \
    -from [get_cells -hierarchical {*}] \
    -to   [get_cells -hierarchical {*wr_ptr_addr_ddr*}]
set_max_delay 8.000 \
    -from [get_cells -hierarchical {*}] \
    -to   [get_cells -hierarchical {*rd_ptr_addr_ddr*}]

# ---------------------------------------------------------------------------
# B) rr.sync_cdc single-bit synchroniser first stage (stages_r[0]) — async
#    capture of a single-bit level/toggle; no multi-bit coherency, so a plain
#    false path (the settling stages stages_r[1..N-1] stay normally timed at the
#    destination clock).
# ---------------------------------------------------------------------------
set_false_path -to [get_cells -hierarchical {*i_sync_cdc_*stages_r*}]

# ---------------------------------------------------------------------------
# C) olo reset synchroniser async assertion (olo_base_cc_reset). The reset
#    request / ack registers are reached through an ASYNC control from the other
#    domain; the recovery/removal check on that async event is intentionally not
#    timed (deassertion is synchronised by the shift+ack handshake).
# ---------------------------------------------------------------------------
set_false_path -to [get_cells -hierarchical {*i_rst_cc*RstAck*}]
