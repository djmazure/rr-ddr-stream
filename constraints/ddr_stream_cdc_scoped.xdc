# ddr_stream_cdc_scoped.xdc — scoped CDC timing constraints for ddr_stream_axi.
#
# CANONICAL IP CONSTRAINT (RR--P3.8). This is ddr_stream_axi's own clock-domain-
# crossing constraint set. It is CELL-RELATIVE and clock-name-INDEPENDENT — the
# leading */ in every selector matches any consumer instance path, so the IP
# carries correct CDC timing into any design regardless of its clock names or
# where the IP is instantiated (replaces deepskopion's interim
# `-from [get_clocks clk_fpga_0] -to clk_fpga_1` block).
#
# PROVEN on rr-ddr-stream/example/zybo_z7_20_ddr_validator (Vivado 2024.1, Zynq-7
# xc7z020, clk_sys 100 MHz / clk_ddr 125 MHz): without it, place-and-route WNS =
# -0.443 ns (TNS -33.4) driven entirely by these unconstrained, metastability-
# safe crossings; with it, WNS = +0.021 ns, hold met (WHS +0.052), Fmax 95.8 ->
# 125.3 MHz. All numbers are the routed sign-off.
#
# SHIPPING (RTL-P2.636, live): declared in this IP's ip.yml `build.sources.xdc`.
# `rr pkg add` vendors it into the consumer's libs/ and records it in the
# consumer ip.lock as a scoped_constraint (ref=hdl.top_module); the build emits
# `read_xdc -ref ddr_stream_axi <file>`, scoping every get_* to the IP instance.
# The consumer adds ZERO timing XDC.
#
# ----------------------------------------------------------------------------
# DDR-T1.1 (2026-06-24) — CDC AUDIT re-derivation against the 0.3.0 netlist.
#
# The `-to` set below is the COMPLETE list of asynchronous clk_sys<->clk_ddr
# crossing DESTINATION (capturing-clock) registers in the current RTL, verified
# cell-by-cell against the routed netlist of BOTH validators (xc7z020 + xc7z010,
# Vivado 2024.1) via `report_cdc`: 114 endpoints, 0 Unsafe, 0 Unknown — every
# crossing carries `Max Delay Datapath Only` (or the reset False Path). The
# crossings and the selector that covers each destination reg:
#
#   1. olo gray-pointer CDC (the FIFO read/write address Gray code crossing).
#      Lives in olo_base_fifo_async's i_cc_wr_rd / i_cc_rd_wr (entity
#      olo_base_cc_bits), whose 1st/Nth synchroniser flops are named Reg0 / RegN.
#      Both olo FIFOs sit under i_ddr_fifo_intf (i_uploader_fifo upsize +
#      i_downloader_fifo downsize), so the destinations are
#         u_ip/i_ddr_fifo_intf/.../i_fifo/i_cc_{wr_rd,rd_wr}/Reg0_reg[*]  (+RegN)
#      -> selector  *i_ddr_fifo_intf*Reg0_reg*  ||  *i_ddr_fifo_intf*RegN_reg*
#
#   2. olo reset clock-crossing (olo_base_cc_reset, instance i_rst_cc inside each
#      olo_base_fifo_async). The reset-request shift regs RstRqstA2B / RstRqstB2A
#      cross domains; their ACK return path is two nested olo_base_cc_bits
#      (i_acka2b / i_ackb2a -> Reg0/RegN, already covered by (1) because they too
#      sit under i_ddr_fifo_intf). The request regs are the explicit target here:
#         u_ip/i_ddr_fifo_intf/.../i_fifo/i_rst_cc/RstRqst{A2B,B2A}_reg[*]
#      -> selector  *i_rst_cc*RstRqst*_reg*
#      The async-PRESET (recovery/removal) crossing on those same regs is a
#      False Path — see section B.
#
#   3. base-address MCP captures (rd_/wr_base_addr_sys -> *_base_addr_meta, then
#      -> *_base_addr_ddr in the clk_ddr domain). Quasi-static multi-bit MCP; the
#      meta flop is the first clk_ddr capture. report_cdc flags CDC-5 "missing
#      ASYNC_REG" — expected and benign for a coherent MCP (not a 2-FF sync), and
#      bounded by max_delay so it is timing-safe.
#      -> selector  *_base_addr_meta_reg*
#
#   4. rr.sync_cdc destination shift register `stages_r` (ASYNC_REG="TRUE" string;
#      olo tags its async_reg as a VHDL boolean Vivado does NOT expose for
#      get_cells -filter {ASYNC_REG} — see hint H.78 / RTL-P3.602 — so these are
#      named explicitly, not auto-discovered). Covers the empty/full/busy CDCs
#      (i_sync_cdc_{up,down}_busy, _wr_err, _rd_err, _empty, _full, _pipe_busy)
#      AND the software read/write pointer toggle syncs (i_sync_cdc_rd_ptr /
#      i_sync_cdc_wr_ptr).
#      -> selector  *i_sync_cdc_*stages_r_reg*
#
#   5. software-managed pointer MCP data captures, gated by the (4) toggle enable:
#      rd_ptr_addr_sys -> rd_ptr_addr_ddr (DDR-P2.4 / DS-P2.59) and the symmetric
#      wr_ptr_addr_sys -> wr_ptr_addr_ddr (DDR-P3.16). Each is a quasi-static
#      multi-bit crossing captured under the synced toggle; it does NOT pass
#      through ASYNC_REG flops, so it must be named explicitly here. NOTE: in a
#      validator that drives a FIXED fill these CSR pointers fold to constants and
#      the *_ptr_addr_ddr regs are optimised away (0 cells) — harmless; in a live
#      consumer (e.g. rr-idp ring_demo) they survive and this selector binds them.
#      -> selector  *rd_ptr_addr_ddr_reg*  ||  *wr_ptr_addr_ddr_reg*
#
# DRIFT GUARD: tests/test_cdc_xdc_drift.py parses every `-to` selector below and
# asserts each token resolves to >=1 VHDL source across the IP closure (rtl/ +
# the vendored olo libs/) — so a future olo bump or RTL refactor that renames any
# crossing reg (Reg0/RegN/RstRqst/i_rst_cc/stages_r/...) breaks the test, not
# silicon. The guard deliberately scans BOTH rtl/ AND libs/open-logic/: the olo
# synchroniser flops (Reg0/RegN/RstRqst/i_rst_cc) live in libs/, NOT rtl/ — the
# original DDR-T1.1 "drift" report came from a grep that searched only rtl/.
# ----------------------------------------------------------------------------
#
# Two constraint kinds are required (learned 2026-05-26):
#   A) DATA crossings -> set_max_delay -datapath_only. NOTE: -datapath_only
#      REQUIRES -from (Vivado ERROR [Constraints 18-540] otherwise — a -to-only
#      form is silently rejected). -from [all sequential] -to [dest sync FFs]:
#      only the true crossing source has a path to each dest sync FF's data
#      input, so this bounds exactly the crossings with no same-domain
#      over-constraint. 8.000 ns = min(clk_sys 10 ns, clk_ddr 8 ns).
#   B) The olo reset synchroniser's async-PRESET (recovery/removal) crossing is
#      NOT a set_max_delay target — it's a recovery check on an async control
#      pin. set_false_path it (reset assertion is intentionally async; olo's
#      synchroniser handles safe deassertion). Genuine async-reset-deassertion
#      CDC, not the "glitch" fallacy.

# --- A) DATA CDC: olo gray-pointer + reset-ack sync (Reg0/RegN under
#     i_ddr_fifo_intf), olo reset request sync (RstRqst*), base-address sync
#     (_meta), rr.sync_cdc (stages_r) — incl. the read-pointer toggle sync
#     i_sync_cdc_rd_ptr / i_sync_cdc_wr_ptr (both matched by the i_sync_cdc_*
#     wildcard) — and the software pointer MCP data captures
#     (rd_ptr_addr_sys -> rd_ptr_addr_ddr, DDR-P2.4 / DS-P2.59; and the symmetric
#     wr_ptr_addr_sys -> wr_ptr_addr_ddr, DDR-P3.16). See the DDR-T1.1 audit note
#     above for the per-crossing destination-reg mapping (verified vs routed
#     netlist: 114 endpoints, 0 Unsafe).
set_max_delay -datapath_only 8.000 \
  -from [get_cells -hierarchical -filter {IS_SEQUENTIAL}] \
  -to   [get_cells -hierarchical -filter { \
            NAME =~ "*i_ddr_fifo_intf*Reg0_reg*"  || NAME =~ "*i_ddr_fifo_intf*RegN_reg*" || \
            NAME =~ "*i_rst_cc*RstRqst*_reg*"     || \
            NAME =~ "*_base_addr_meta_reg*"       || NAME =~ "*i_sync_cdc_*stages_r_reg*" || \
            NAME =~ "*rd_ptr_addr_ddr_reg*"       || NAME =~ "*wr_ptr_addr_ddr_reg*" }]

# --- B) olo reset async-preset (recovery/removal) crossing -> false path.
set_false_path -to [get_pins -quiet -of_objects \
  [get_cells -hierarchical -filter {NAME =~ "*i_rst_cc*RstRqst*_reg*" || NAME =~ "*i_rst_cc*RstAck*_reg*"}] \
  -filter {REF_PIN_NAME == PRE || REF_PIN_NAME == CLR}]
