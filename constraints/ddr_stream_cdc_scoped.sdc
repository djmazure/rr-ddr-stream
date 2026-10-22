# ddr_stream_cdc_scoped.sdc — scoped CDC timing constraints for the
# ddr_stream_axi / ddr_stream_avmm TOPs, Quartus (Altera/Intel) edition.
#
# ALTERA TWIN OF constraints/ddr_stream_cdc_scoped.xdc (DDR-P2.10, 2026-07-21).
# The XDC ships to Xilinx consumers via ip.lock scoped_constraints
# (`read_xdc -ref ddr_stream_axi`); Quartus has NO read_xdc-style ref scoping,
# so this file is written to be applied GLOBALLY and still bind only this IP:
# every selector is a full-hierarchical-name glob whose literal fragments
# (i_ddr_fifo_intf / i_rst_cc / i_sync_cdc_* / *_base_addr_* / *_ptr_addr_*)
# exist only inside rr_ddr_stream, and the leading `*` absorbs any consumer
# instance path (Standard "inst|sig" and Pro "entity:inst|sig" naming alike —
# Quartus wildcard matching runs over the full hierarchical name, `*` crosses
# `|` separators).
#
# WHY: without this file a Quartus consumer with related clk_sys/clk_ddr (same
# PLL) times every metastability-safe synchroniser capture as a normal
# cross-clock transfer and reports FALSE setup/hold (and recovery/removal)
# violations on the olo async-FIFO crossings — the exact failure DDR-P2.10
# records.
#
# TRANSLATION RULES (per Intel Quartus Timing Analyzer guidance — the same
# false_path + set_net_delay -max + set_max_skew trio Quartus itself emits in
# the auto-generated dcfifo CDC SDC):
#
#   * XDC `set_max_delay -datapath_only` has NO Quartus equivalent. Its intent
#     is (a) exclude the crossing from normal setup/hold analysis while
#     (b) keeping the datapath latency bounded. In SDC that maps to
#     set_false_path (kills the false setup/hold/recovery reports) PLUS
#     set_net_delay -max (re-imposes the latency bound on the crossing net).
#     For multi-bit gray/MCP buses set_max_skew is added so the bus stays
#     COHERENT — a blanket false path alone would let bit skew grow unbounded,
#     which is exactly what the XDC's single 8 ns window prevented.
#   * The XDC hardcodes 8.000 ns = min(clk_sys 10 ns, clk_ddr 8 ns) of the
#     proven validators. This file ships to consumers with UNKNOWN clocks, so
#     it uses the clock-relative Intel-canonical form instead:
#     net delay <= 0.8 * destination clock period, skew <= 0.8 * min clock
#     period — the same bound expressed portably (and what the dcfifo
#     auto-SDC uses).
#   * Where the XDC used `-from [all sequential]` because Vivado's
#     -datapath_only merely BOUNDS (never cuts) any extra same-domain paths
#     it sweeps in, a false path would CUT them. So every set_false_path below
#     names the true crossing SOURCE registers where they are IP-internal
#     (RegIn, *_sys, Rst?Latch). Section D's launch set is the synchronous
#     fan-in of stages_r[0] (DDR-P2.23), not a chip-wide get_registers *.
#
# Per-crossing map (XDC section -> SDC section):
#   XDC A "*i_ddr_fifo_intf*Reg0_reg*"            -> A (RegIn -> Reg0)
#   XDC A "*i_ddr_fifo_intf*RegN_reg*"            -> deliberately NOT targeted:
#        RegN's D input comes from Reg0/RegN-1 in the SAME (destination) clock
#        domain — there is no crossing into RegN. The XDC listed it only
#        because -datapath_only bounding same-domain shift stages is harmless;
#        a false path there would cut the real metastability-settling stages.
#        They stay normally timed at the destination clock, which is what a
#        synchroniser chain wants.
#   XDC A "*i_rst_cc*RstRqst*_reg*"               -> B. The RstRqst D inputs
#        are an intra-domain shift ('0' fill) — the actual crossing is the
#        ASYNC PRESET from the other domain's Rst?Latch (olo_base_cc_reset
#        asserts reset async, de-asserts sync). False-path Latch -> RstRqst
#        cuts the recovery/removal analysis exactly like the XDC's PRE/CLR
#        false path (XDC section B).
#   XDC A "*_base_addr_meta_reg*"                 -> C (quasi-static MCP)
#   XDC A "*i_sync_cdc_*stages_r_reg*"            -> D (single-bit 2-4 stage sync)
#   XDC A "*rd_ptr_addr_ddr_reg*" / "*wr_ptr..."  -> E (toggle-gated MCP; may
#        legitimately resolve to 0 registers: the AVMM TOP has no SW-pointer
#        plane and validators with fixed fills constant-fold them — hence
#        -nowarn + the collection-size guard)
#   XDC B PRE/CLR false path (RstRqst + RstAck)   -> B. The RstAck half needs
#        no SDC command: RstAck sync flops are olo_base_cc_bits Reg0/RegN with
#        In_Rst/Out_Rst tied '0' — they have no async control in the Quartus
#        netlist; their data crossing is already covered by A (they live under
#        i_ddr_fifo_intf, pattern *i_ddr_fifo_intf*Reg0[*]).
#
# NOT constrained here (matching the XDC 1:1): the olo_base_ram_sdp
# write-domain -> read-domain RAM data path inside olo_base_fifo_async. The
# FIFO protocol only reads an address after its gray pointer has crossed, so
# with related consumer clocks that path times normally (proven +WNS on the
# Xilinx validators). If a Quartus consumer sees violations THROUGH the FIFO
# RAM, that is a new finding — file it, do not blanket-false-path it here.
#
# Wildcard -> RTL identifier provenance (drift-guarded by
# tests/test_cdc_xdc_drift.py, which parses the get_registers patterns below):
#   RegIn / Reg0            libs/open-logic/.../olo_base_cc_bits.vhd
#   RstALatch / RstBLatch / RstRqstA2B / RstRqstB2A
#                           libs/open-logic/.../olo_base_cc_reset.vhd
#   i_rst_cc                libs/open-logic/.../olo_base_fifo_async.vhd
#   i_ddr_fifo_intf         rtl/ddr_stream_axi.vhd, rtl/ddr_stream_avmm.vhd
#   rd/wr_base_addr_sys, rd/wr_base_addr_meta
#                           rtl/ddr_stream_axi.vhd, rtl/ddr_stream_avmm.vhd
#   i_sync_cdc_* / stages_r rtl/ddr_stream_{axi,avmm}.vhd / rtl/support/ddr_sync_cdc.vhd
#   rd/wr_ptr_addr_sys, rd/wr_ptr_addr_ddr
#                           rtl/ddr_stream_axi.vhd (AXI TOP only)

# ---------------------------------------------------------------------------
# Clock identity (DDR-P2.23). Quartus Standard 23.1 — a downstream toolchain —
# accepts only `get_clocks [-nocase] [-nowarn] [<filter>]`. Both
# `get_clocks -of` and `get_clocks -of_objects` fail there with
# "More than 1 positional argument specified" (verified on 23.1std.0 build 991).
# `-of_objects` is a Prime Pro spelling and must not appear in this file.
#
# The Standard-legal netlist query that works inside read_sdc (before
# update_timing_netlist, where get_timing_paths is ignored) is
# `get_fanins -clock <registers>`. It returns the clock-source node of those
# registers. On a consumer that ties clk_sys and clk_ddr to one port
# (DE25 CLOCK2_50), both sides fan in to that same node. A set_false_path
# between them is then an intra-clock cut and Quartus Design Assistant reports
# CDC-50101. Skip the cut when the union of launch and capture clock-source
# nodes has size 1. Keep set_net_delay / set_max_skew: those bound the route,
# they do not create a false-path synchronizer.
# Unknown clocks (empty fanin, or this file sourced outside TimeQuest) fall
# open to "distinct" so a real two-clock consumer still gets the cut.
# ---------------------------------------------------------------------------
if {[info procs _rr_ds_clock_nodes] eq ""} {
    proc _rr_ds_clock_nodes {col} {
        if {[info commands get_fanins] eq "" || [info commands get_node_info] eq ""} {
            return [list]
        }
        if {[catch {set fins [get_fanins -clock $col]}]} {
            return [list]
        }
        set names [list]
        foreach_in_collection n $fins {
            if {[catch {lappend names [get_node_info -name $n]}]} {
                lappend names $n
            }
        }
        return [lsort -unique $names]
    }
}
if {[info procs _rr_ds_are_clocks_distinct] eq ""} {
    proc _rr_ds_are_clocks_distinct {from_col to_col} {
        if {[info commands get_collection_size] eq ""} {
            return 1
        }
        if {[get_collection_size $from_col] == 0 || [get_collection_size $to_col] == 0} {
            return 1
        }
        set from_nodes [_rr_ds_clock_nodes $from_col]
        set to_nodes [_rr_ds_clock_nodes $to_col]
        if {[llength $from_nodes] == 0 || [llength $to_nodes] == 0} {
            return 1
        }
        set all_nodes [lsort -unique [concat $from_nodes $to_nodes]]
        if {[llength $all_nodes] == 1} {
            return 0
        }
        return 1
    }
}

# ---------------------------------------------------------------------------
# A) olo_base_cc_bits data crossings under i_ddr_fifo_intf: gray read/write
#    pointers (i_cc_wr_rd / i_cc_rd_wr) and the reset-ack bits
#    (i_rst_cc/i_acka2b / i_ackb2a). In every olo_base_cc_bits the ONLY
#    cross-domain hop is RegIn (launch domain) -> Reg0 (capture domain), so
#    the pair below is exact — no same-domain path is cut.
#    set_max_skew keeps the gray buses coherent (gray coding tolerates 1-bit
#    ambiguity only if all bits land within one capture window).
# ---------------------------------------------------------------------------
set _rr_ds_cc_from [get_registers {*i_ddr_fifo_intf*RegIn[*]}]
set _rr_ds_cc_to   [get_registers {*i_ddr_fifo_intf*Reg0[*]}]
if {[_rr_ds_are_clocks_distinct $_rr_ds_cc_from $_rr_ds_cc_to]} {
    set_false_path -from $_rr_ds_cc_from -to $_rr_ds_cc_to
}
set_net_delay  -from $_rr_ds_cc_from -to $_rr_ds_cc_to -max \
    -get_value_from_clock_period dst_clock_period -value_multiplier 0.8
set_max_skew   -from $_rr_ds_cc_from -to $_rr_ds_cc_to \
    -get_skew_value_from_clock_period min_clock_period -skew_value_multiplier 0.8

# ---------------------------------------------------------------------------
# B) olo_base_cc_reset request crossings (async assert / sync de-assert).
#    Rst?Latch (launch domain) drives the OTHER domain's RstRqst shift register
#    (std_logic_vector(2 downto 0), olo_base_cc_reset) through its ASYNCHRONOUS
#    preset — a genuine async-reset-assertion CDC. The recovery/removal check on
#    that preset is intentionally NOT timed (safe de-assertion comes from the
#    shift + ack handshake), so the false_path stays.
#
#    DDR-T1.3 (2026-09-04): the false_path ALONE is a defect by the owner's
#    constraints-audit class table — "a reset request crossing must carry a
#    bound; a bare false path is a defect." A bare cut kills the recovery/removal
#    report (correct) AND leaves the crossing's physical route latency and
#    inter-bit skew UNBOUNDED, so the reset RELEASE becomes route-dependent
#    rather than deterministic.
#
#    NOTE — this is NOT an XDC-parity restoration. The .xdc does NOT bound this
#    crossing: its section-A `set_max_delay -datapath_only` lists RstRqst*_reg
#    as a -to *cell*, which bounds those flops' INTRA-domain D-input shift
#    ('0'-fill, same capture clock) — exactly like RegN, which we deliberately do
#    NOT constrain (see header). The genuine cross-domain event is the ASYNC
#    PRESET from the other domain's Rst?Latch, and the .xdc false-paths that on
#    the PRE/CLR pins (section B) with NO datapath bound. So the original bare
#    false_path here was a FAITHFUL translation of XDC section B; the bound added
#    below is a NEW, stricter constraint justified only by the owner's class
#    table, applied portably in Quartus terms (set_net_delay -max + set_max_skew).
#    It is strictly MORE constrained than the bare cut, so it can only catch an
#    unbounded route, never relax a met one. The recovery/removal report stays
#    cut by the false_path — the bound never re-imposes the async-preset timing.
#
#    UNVERIFIED AT THE DESK (bench handoff, docs/internal/DDR_T1_3_SDC_CDC_AUDIT
#    §5): whether Quartus applies set_net_delay/set_max_skew to a path whose
#    endpoint is reached through an ASYNC PRESET (vs a D-input net) is not shown
#    from this worktree. If STA on the deepskopion netlist reports these bounds
#    as inert on the preset path, this section needs the reset-hardening shape
#    (single reset source, synchronous deassertion) instead — see the audit doc.
# ---------------------------------------------------------------------------
set _rr_ds_rstA_from [get_registers {*i_rst_cc*RstALatch}]
set _rr_ds_rstA_to   [get_registers {*i_rst_cc*RstRqstA2B[*]}]
if {[_rr_ds_are_clocks_distinct $_rr_ds_rstA_from $_rr_ds_rstA_to]} {
    set_false_path -from $_rr_ds_rstA_from -to $_rr_ds_rstA_to
}
set_net_delay  -from $_rr_ds_rstA_from -to $_rr_ds_rstA_to -max \
    -get_value_from_clock_period dst_clock_period -value_multiplier 0.8
set_max_skew   -from $_rr_ds_rstA_from -to $_rr_ds_rstA_to \
    -get_skew_value_from_clock_period min_clock_period -skew_value_multiplier 0.8

set _rr_ds_rstB_from [get_registers {*i_rst_cc*RstBLatch}]
set _rr_ds_rstB_to   [get_registers {*i_rst_cc*RstRqstB2A[*]}]
if {[_rr_ds_are_clocks_distinct $_rr_ds_rstB_from $_rr_ds_rstB_to]} {
    set_false_path -from $_rr_ds_rstB_from -to $_rr_ds_rstB_to
}
set_net_delay  -from $_rr_ds_rstB_from -to $_rr_ds_rstB_to -max \
    -get_value_from_clock_period dst_clock_period -value_multiplier 0.8
set_max_skew   -from $_rr_ds_rstB_from -to $_rr_ds_rstB_to \
    -get_skew_value_from_clock_period min_clock_period -skew_value_multiplier 0.8

# ---------------------------------------------------------------------------
# C) Base-address quasi-static MCP: rd/wr_base_addr_sys (clk_sys CSR regs) ->
#    rd/wr_base_addr_meta (first clk_ddr capture). Software programs the base
#    before enabling the stream, so the value is stable at capture time;
#    latency stays bounded and the bus coherent, no setup/hold analysis.
#
#    DDR-P3.37 (2026-09-05, bench): this section originally used a BARE
#    get_registers (no -nowarn) with the combined rd+wr pattern. On a
#    single-plane consumer that is a defect. STA on the real deepskopion
#    de25_datalogger post-fit netlist (write-plane-on, read base-address
#    constant-folded away) showed the WRITE half binding (32 regs) but the
#    READ half absent — and Quartus emits a TMC-20026 zero-match warning
#    (332174) for EACH empty SUB-pattern of a get_registers brace-list,
#    EVEN WHEN the overall collection is non-empty. So the combined
#    collection was non-empty (wr present) yet the run still emitted two
#    `*rd_base_addr_{sys,meta}[*] could not be matched` warnings, violating
#    DDR-T1.3 DONE-WHEN (b) "zero TMC-20026 empty collections".
#
#    DDR-P3.37 silenced the combined brace-list with -nowarn. That still leaves
#    one get_registers filter whose read-plane sub-pattern matches nothing on a
#    write-only consumer (DS-P1.72 / DS-T1.20: `*rd_base_addr_sys[*]` and
#    `*rd_base_addr_meta[*]` are empty collections). DDR-P2.23 splits the read
#    and write planes into their own -nowarn queries. A present plane binds on
#    its own; an absent plane does not sit inside the other plane's filter, so
#    Quartus does not report an ignored read-base filter, and set_max_skew does
#    not group the two address buses together. Each absent plane still emits
#    one controlled post_message (owner clause 3). The message is braced so
#    Tcl does not command-substitute the `[*]` glob.
# ---------------------------------------------------------------------------
set _rr_ds_wr_base_from [get_registers -nowarn {*wr_base_addr_sys[*]}]
set _rr_ds_wr_base_to   [get_registers -nowarn {*wr_base_addr_meta[*]}]
if {[get_collection_size $_rr_ds_wr_base_to] > 0} {
    if {[_rr_ds_are_clocks_distinct $_rr_ds_wr_base_from $_rr_ds_wr_base_to]} {
        set_false_path -from $_rr_ds_wr_base_from -to $_rr_ds_wr_base_to
    }
    set_net_delay  -from $_rr_ds_wr_base_from -to $_rr_ds_wr_base_to -max \
        -get_value_from_clock_period dst_clock_period -value_multiplier 0.8
    set_max_skew   -from $_rr_ds_wr_base_from -to $_rr_ds_wr_base_to \
        -get_skew_value_from_clock_period min_clock_period -skew_value_multiplier 0.8
} else {
    if {[info commands post_message] ne ""} {
        post_message -type warning {ddr_stream_cdc_scoped.sdc section C: wr_base_addr MCP selector {*wr_base_addr_meta[*]} matched 0 registers on this build. Expected when the write plane is absent; on a write-plane config investigate a constant-folded or renamed base-address register (DDR-T1.3 audit §4a, DDR-P3.37, DDR-P2.23).}
    }
}

set _rr_ds_rd_base_from [get_registers -nowarn {*rd_base_addr_sys[*]}]
set _rr_ds_rd_base_to   [get_registers -nowarn {*rd_base_addr_meta[*]}]
if {[get_collection_size $_rr_ds_rd_base_to] > 0} {
    if {[_rr_ds_are_clocks_distinct $_rr_ds_rd_base_from $_rr_ds_rd_base_to]} {
        set_false_path -from $_rr_ds_rd_base_from -to $_rr_ds_rd_base_to
    }
    set_net_delay  -from $_rr_ds_rd_base_from -to $_rr_ds_rd_base_to -max \
        -get_value_from_clock_period dst_clock_period -value_multiplier 0.8
    set_max_skew   -from $_rr_ds_rd_base_from -to $_rr_ds_rd_base_to \
        -get_skew_value_from_clock_period min_clock_period -skew_value_multiplier 0.8
} else {
    if {[info commands post_message] ne ""} {
        post_message -type warning {ddr_stream_cdc_scoped.sdc section C: rd_base_addr MCP selector {*rd_base_addr_meta[*]} matched 0 registers on this build. Expected when the read plane is absent; on a read-plane config investigate a constant-folded or renamed base-address register (DDR-T1.3 audit §4a, DDR-P3.37, DDR-P2.23).}
    }
}

# ---------------------------------------------------------------------------
# D) rr ddr_sync_cdc single-bit synchronisers (busy/err/empty/full/pipe_busy
#    level syncs + the rd/wr software-pointer toggle syncs). Only stages_r[0]
#    captures the asynchronous d_i; stages_r[1..N-1] are same-domain settling
#    stages and stay normally timed.
#    The launch set is the synchronous fan-in of stages_r[0], not
#    `[get_registers *]`. A chip-wide from-set includes unrelated clocks
#    (JTAG, HPS) and would keep an intra-clock false path on a one-fabric-clock
#    DE25 even after the distinct-clock guard. get_fanins -synch is legal inside
#    read_sdc on Quartus Standard and Pro and returns the register that launches
#    each capture flop. The same-domain set_i/rst_i override stays normally timed.
# ---------------------------------------------------------------------------
set _rr_ds_sync_to [get_registers {*i_sync_cdc_*stages_r[0]}]
if {[info commands get_fanins] ne ""} {
    set _rr_ds_sync_from [get_fanins -synch $_rr_ds_sync_to]
} else {
    set _rr_ds_sync_from [get_registers *]
}
if {[get_collection_size $_rr_ds_sync_from] > 0 && [get_collection_size $_rr_ds_sync_to] > 0} {
    if {[_rr_ds_are_clocks_distinct $_rr_ds_sync_from $_rr_ds_sync_to]} {
        set_false_path -from $_rr_ds_sync_from -to $_rr_ds_sync_to
    }
    set_net_delay  -from $_rr_ds_sync_from -to $_rr_ds_sync_to -max \
        -get_value_from_clock_period dst_clock_period -value_multiplier 0.8
}

# ---------------------------------------------------------------------------
# E) Software-managed pointer MCP (AXI TOP only): rd/wr_ptr_addr_sys ->
#    rd/wr_ptr_addr_ddr, captured under the (D)-synchronised toggle enable, so
#    the data is stable >= num_stages destination cycles before capture.
#    May match 0 registers (AVMM TOP; validators folding the CSR pointers to
#    constants). Per DDR-T1.3 owner clause (3) a 0-match is NEVER benign and must
#    not be silently swallowed: each plane's size guard carries an `else` that
#    emits `post_message -type warning` naming the empty selector.
#    DDR-P2.23 splits the read and write pointer filters so an absent plane is
#    not an empty sub-pattern inside the other plane's brace-list.
# ---------------------------------------------------------------------------
set _rr_ds_wr_ptr_from [get_registers -nowarn {*wr_ptr_addr_sys[*]}]
set _rr_ds_wr_ptr_to   [get_registers -nowarn {*wr_ptr_addr_ddr[*]}]
if {[get_collection_size $_rr_ds_wr_ptr_to] > 0} {
    if {[_rr_ds_are_clocks_distinct $_rr_ds_wr_ptr_from $_rr_ds_wr_ptr_to]} {
        set_false_path -from $_rr_ds_wr_ptr_from -to $_rr_ds_wr_ptr_to
    }
    set_net_delay  -from $_rr_ds_wr_ptr_from -to $_rr_ds_wr_ptr_to -max \
        -get_value_from_clock_period dst_clock_period -value_multiplier 0.8
    set_max_skew   -from $_rr_ds_wr_ptr_from -to $_rr_ds_wr_ptr_to \
        -get_skew_value_from_clock_period min_clock_period -skew_value_multiplier 0.8
} else {
    if {[info commands post_message] ne ""} {
        post_message -type warning {ddr_stream_cdc_scoped.sdc section E: wr_ptr_addr MCP selector {*wr_ptr_addr_ddr[*]} matched 0 registers on this build. Expected on a HW write-plane config (section A's FIFO gray pointer is the real crossing); a DEFECT on a SW-pointer config (the pointer MCP is then UNCONSTRAINED). Confirm against enable_write_plane_g (DDR-T1.3 audit §4b, DDR-P2.23).}
    }
}

set _rr_ds_rd_ptr_from [get_registers -nowarn {*rd_ptr_addr_sys[*]}]
set _rr_ds_rd_ptr_to   [get_registers -nowarn {*rd_ptr_addr_ddr[*]}]
if {[get_collection_size $_rr_ds_rd_ptr_to] > 0} {
    if {[_rr_ds_are_clocks_distinct $_rr_ds_rd_ptr_from $_rr_ds_rd_ptr_to]} {
        set_false_path -from $_rr_ds_rd_ptr_from -to $_rr_ds_rd_ptr_to
    }
    set_net_delay  -from $_rr_ds_rd_ptr_from -to $_rr_ds_rd_ptr_to -max \
        -get_value_from_clock_period dst_clock_period -value_multiplier 0.8
    set_max_skew   -from $_rr_ds_rd_ptr_from -to $_rr_ds_rd_ptr_to \
        -get_skew_value_from_clock_period min_clock_period -skew_value_multiplier 0.8
} else {
    if {[info commands post_message] ne ""} {
        post_message -type warning {ddr_stream_cdc_scoped.sdc section E: rd_ptr_addr MCP selector {*rd_ptr_addr_ddr[*]} matched 0 registers on this build. Expected on a HW read-plane config (section A's FIFO gray pointer is the real crossing); a DEFECT on a SW-pointer config (the pointer MCP is then UNCONSTRAINED). Confirm against enable_read_plane_g (DDR-T1.3 audit §4b, DDR-P2.23).}
    }
}
