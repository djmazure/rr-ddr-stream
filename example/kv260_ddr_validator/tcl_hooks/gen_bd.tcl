###############################################################################
# gen_bd.tcl — Zynq UltraScale+ PS subsystem for the KV260 DDR validator.
#
# Sourced in-session by routertl/tcl/xilinx/gen_project.tcl via project.yml
# tcl_hooks.gen_project (RTL-P3.506 model — the BD is a generation RECIPE,
# never a committed .bd artifact). Classified as a BD hook (it contains
# create_bd_cell) and runs through routertl's N-pass healer + VLNV-upgrade.
#
# This is the KV260 (ZynqMP, xck26) sibling of
#   ../zybo_z7_20_ddr_validator/tcl_hooks/gen_bd.tcl
# and is distilled from the PROVEN attach_h264_fb_dma recipe in
#   kv260_linux/tcl/hooks/kv260_ps_bd.tcl  (silicon-confirmed: KV260 HP0 DDR
#   writes at 0x7800_0000, M_AXI_HPM0_FPD AXI-Lite at 0xA000_0000).
#
# ── How ZynqMP differs from the Zynq-7 (PS7) validator BD ────────────────────
#   - PS cell is `zynq_ultra_ps_e` (not processing_system7). Newest in catalog.
#   - DDR + MIO are PS-INTERNAL on ZynqMP — they are NOT brought out to the PL
#     top as inout ports (unlike PS7's DDR_*/FIXED_IO_*). So this BD exposes NO
#     dedicated-pin ports and the user top (top_kv260_ddr_validator) is
#     PORT-LESS. The KV260 SOM also has no user LEDs on PL pins, so the verdict
#     is CSR-only (read over /dev/mem or JTAG), not LED-encoded.
#   - The HP slave port is S_AXI_HP0_FPD (config knob PSU__USE__S_AXI_GP2 /
#     SAXIGP2). The PS→PL AXI-Lite master is M_AXI_HPM0_FPD (config knob
#     PSU__USE__M_AXI_GP0). The board preset enables HPM0/HPM1 but NOT any HP
#     slave, so enabling S_AXI_HP0_FPD is this hook's job (the project delta).
#   - One fabric clock: pl_clk0 @ 100 MHz drives both the AXI-Lite (CSR) path
#     and the HP0 DMA path. The IP's CDC FIFO runs clk_sys == clk_ddr here
#     (degenerate but valid) — keeps the demo single-domain. proc_sys_reset
#     synchronises pl_resetn0 into fab_aresetn.
#
# This hook UNCONDITIONALLY exposes S_AXI_HP0 + M_AXI_GP0 (no KV260_FBDMA gate)
# — that gate in kv260_linux is what produced the "PS-only BD → CSR hang"
# hazard when the bit was built without it. A validator that can't reach its
# CSR is useless, so here the route is always built.
#
# It exposes three surfaces to the parent RTL (src/top_kv260_ddr_validator.vhd,
# which declares this BD as a VHDL `component ddr_validator_bd`):
#   - S_AXI_HP0  (slave)   AXI4, 128-bit ← ddr_stream_axi master (user RTL)
#   - M_AXI_GP0  (master)  AXI4-Lite     → ddr_validator_csr     (user RTL)
#   - fab_clk (100 MHz) + fab_aresetn (active-low, synchronised)
###############################################################################

puts "INFO: Building ddr_validator_bd (Zynq UltraScale+ PS subsystem, KV260)..."

# ── BD design ────────────────────────────────────────────────────────────────
# Design name == the `component ddr_validator_bd` in the user top (BD-as-
# component pattern, same as the Zynq-7 validator — no make_wrapper).
create_bd_design ddr_validator_bd
current_bd_design [get_bd_designs ddr_validator_bd]

# ── Processing System (Zynq UltraScale+) ─────────────────────────────────────
set ps_vlnv [lindex [lsort [get_ipdefs -filter {VLNV =~ "xilinx.com:ip:zynq_ultra_ps_e:*"}]] end]
puts "INFO: PS BD using $ps_vlnv on part [get_property part [current_project]]"
set ps [create_bd_cell -type ip -vlnv $ps_vlnv zynq_ultra_ps_e_0]

# ── Stage 1: authoritative AMD K26 board preset ──────────────────────────────
# DDR4-2400 controller, MIO map, clock plan — sourced from routertl's vendored
# copy of the official AMD KV260 board file (boards/kv260/.../ps_preset.tcl),
# resolved from hardware.platform: kv260 in project.yml as $g_board_dir.
if {![info exists g_board_dir]} {
    error "kv260_ddr_validator — g_board_dir is not set. project.yml must declare\
           hardware.platform: kv260 so routertl exposes the vendored AMD board files."
}
set preset_tcl [file join $g_board_dir vivado-board 1.4 ps_preset.tcl]
if {![file exists $preset_tcl]} {
    error "kv260_ddr_validator — authoritative preset not found: $preset_tcl"
}
puts "INFO: sourcing authoritative AMD K26 preset: $preset_tcl"
source $preset_tcl
apply_ps_preset $ps

# ── Stage 2: psu_init delta — on-SOM eMMC (SD0) the transcriber dropped ──────
# Verbatim from preset_s4.xml. Without these the FSBL psu_init can't init the
# eMMC controller and the exported XSA loses XPAR_PSU_SD_0_* (Linux boot dev).
# Applied IP-version-guarded to dodge [BD 41-1276] on renamed legacy params.
set _delta [list \
    CONFIG.PSU__SD0__PERIPHERAL__ENABLE {1} \
    CONFIG.PSU__SD0__SLOT_TYPE {eMMC} \
    CONFIG.PSU__SD0__RESET__ENABLE {1} \
    CONFIG.PSU__SD0__GRP_POW__ENABLE {1} \
    CONFIG.PSU__PMU__GPO2__POLARITY {high} \
]
foreach _p [list_property $ps] { set _valid($_p) 1 }
set _apply [list]
foreach {_n _v} $_delta {
    if {[info exists _valid($_n)]} { lappend _apply $_n $_v } \
    else { puts "INFO: SD0/eMMC delta param not on IP: $_n (skipped)" }
}
if {[llength $_apply]} { set_property -dict $_apply $ps }

# ── Stage 3: project delta — enable the AXI surfaces this validator uses ─────
# S_AXI_HP0_FPD (SAXIGP2) @ 128-bit = ddr_stream_axi's DMA slave.
# M_AXI_HPM0_FPD (MAXIGP0) @ 32-bit = PS→PL AXI-Lite master for the CSR.
set_property -dict [list \
    CONFIG.PSU__USE__S_AXI_GP2       {1} \
    CONFIG.PSU__SAXIGP2__DATA_WIDTH  {128} \
    CONFIG.PSU__USE__M_AXI_GP0       {1} \
    CONFIG.PSU__MAXIGP0__DATA_WIDTH  {32} \
] $ps

# ── proc_sys_reset on pl_clk0 / pl_resetn0 ───────────────────────────────────
set rst [create_bd_cell -type ip \
    -vlnv [lindex [lsort [get_ipdefs -filter {VLNV =~ "xilinx.com:ip:proc_sys_reset:*"}]] end] \
    proc_sys_reset_0]
connect_bd_net [get_bd_pins $ps/pl_clk0]    [get_bd_pins $rst/slowest_sync_clk]
connect_bd_net [get_bd_pins $ps/pl_resetn0] [get_bd_pins $rst/ext_reset_in]

# Every enabled PS↔PL AXI master exposes an *_aclk that MUST be driven or
# [BD 41-758] fails HDL generation. Clock HP0 + all HPM masters from pl_clk0.
connect_bd_net [get_bd_pins $ps/pl_clk0] [get_bd_pins $ps/saxihp0_fpd_aclk]
foreach _aclk [get_bd_pins -quiet "$ps/maxihpm*_aclk"] {
    connect_bd_net [get_bd_pins $ps/pl_clk0] $_aclk
    puts "INFO: clocked PS AXI-master pin [file tail $_aclk] from pl_clk0"
}

# ── Smartconnect HP0: external S_AXI_HP0 → PS S_AXI_HP0_FPD ───────────────────
set sc_hp0 [create_bd_cell -type ip \
    -vlnv [lindex [lsort [get_ipdefs -filter {VLNV =~ "xilinx.com:ip:smartconnect:*"}]] end] \
    axi_smartconnect_hp0]
set_property -dict [list CONFIG.NUM_SI {1} CONFIG.NUM_MI {1}] $sc_hp0
connect_bd_intf_net [get_bd_intf_pins $sc_hp0/M00_AXI] [get_bd_intf_pins $ps/S_AXI_HP0_FPD]
connect_bd_net      [get_bd_pins $ps/pl_clk0]           [get_bd_pins $sc_hp0/aclk]
connect_bd_net      [get_bd_pins $rst/peripheral_aresetn] [get_bd_pins $sc_hp0/aresetn]

# ── Smartconnect GP0: PS M_AXI_HPM0_FPD → external M_AXI_GP0 ──────────────────
set sc_gp0 [create_bd_cell -type ip \
    -vlnv [lindex [lsort [get_ipdefs -filter {VLNV =~ "xilinx.com:ip:smartconnect:*"}]] end] \
    axi_smartconnect_gp0]
set_property -dict [list CONFIG.NUM_SI {1} CONFIG.NUM_MI {1}] $sc_gp0
connect_bd_intf_net [get_bd_intf_pins $ps/M_AXI_HPM0_FPD] [get_bd_intf_pins $sc_gp0/S00_AXI]
connect_bd_net      [get_bd_pins $ps/pl_clk0]             [get_bd_pins $sc_gp0/aclk]
connect_bd_net      [get_bd_pins $rst/peripheral_aresetn] [get_bd_pins $sc_gp0/aresetn]

# ── External interface ports ─────────────────────────────────────────────────
# AXI4 slave in (accepts ddr_stream_axi master). 128-bit data, 2-bit ID —
# matches the IP record fields sliced to the BD width in the user top.
set hp0 [create_bd_intf_port -mode Slave -vlnv xilinx.com:interface:aximm_rtl:1.0 S_AXI_HP0]
set_property -dict [list \
    CONFIG.PROTOCOL   {AXI4} \
    CONFIG.ADDR_WIDTH {32} \
    CONFIG.DATA_WIDTH {128} \
    CONFIG.ID_WIDTH   {2} \
    CONFIG.HAS_BURST  {1} \
    CONFIG.HAS_LOCK   {1} \
    CONFIG.HAS_CACHE  {1} \
    CONFIG.HAS_QOS    {1} \
] $hp0
connect_bd_intf_net $hp0 [get_bd_intf_pins $sc_hp0/S00_AXI]

# AXI-Lite master out (drives ddr_validator_csr slave in user RTL).
set gp0 [create_bd_intf_port -mode Master -vlnv xilinx.com:interface:aximm_rtl:1.0 M_AXI_GP0]
set_property -dict [list \
    CONFIG.PROTOCOL   {AXI4LITE} \
    CONFIG.ADDR_WIDTH {32} \
    CONFIG.DATA_WIDTH {32} \
    CONFIG.HAS_BURST  {0} \
    CONFIG.HAS_CACHE  {0} \
    CONFIG.HAS_LOCK   {0} \
    CONFIG.HAS_QOS    {0} \
    CONFIG.HAS_REGION {0} \
] $gp0
connect_bd_intf_net [get_bd_intf_pins $sc_gp0/M00_AXI] $gp0

# ── Fabric clock + reset out ports ───────────────────────────────────────────
set clk_port [create_bd_port -dir O -type clk fab_clk]
set_property CONFIG.ASSOCIATED_BUSIF {S_AXI_HP0:M_AXI_GP0} $clk_port
connect_bd_net [get_bd_pins $ps/pl_clk0] $clk_port
set rstn_port [create_bd_port -dir O -type rst fab_aresetn]
set_property CONFIG.POLARITY ACTIVE_LOW $rstn_port
connect_bd_net [get_bd_pins $rst/peripheral_aresetn] $rstn_port

# ── Address map ──────────────────────────────────────────────────────────────
# External HP0 master → PS DDR (auto over the HP0 DDR window; the IP ring at
# ddr_base 0x7000_0000 lives inside it).
set _seg [get_bd_addr_segs -quiet $ps/SAXIGP2/HP0_DDR_LOW]
if {$_seg eq ""} { set _seg [get_bd_addr_segs -quiet $ps/SAXIGP2/HP0_DDR_LOWOCM] }
if {$_seg eq ""} { error "kv260_ddr_validator — could not resolve PS HP0 DDR segment under SAXIGP2." }
assign_bd_address -target_address_space [get_bd_addr_spaces S_AXI_HP0] $_seg
puts "INFO: S_AXI_HP0 mapped over PS HP0 DDR window"

# PS HPM0_FPD master → external M_AXI_GP0 region — CSR at 0xA000_0000, 64 KiB
# (must agree with sw/linux/system_memory_map.yml + the CSR's 12-bit decode).
assign_bd_address -target_address_space [get_bd_addr_spaces $ps/Data] \
    [get_bd_addr_segs M_AXI_GP0/Reg] -range 64K -offset 0xA0000000
puts "INFO: M_AXI_GP0 mapped to 0xA000_0000 (ddr_validator_csr)"

# Resolve any remaining default segments, then validate & save.
assign_bd_address -quiet
regenerate_bd_layout
# validate_bd_design returns 0 even on [BD 41-758] clock errors (they only fail
# later at generate_target), so check explicitly and abort loud.
if {[catch {validate_bd_design} _vbd_err]} {
    error "kv260_ddr_validator — validate_bd_design failed: $_vbd_err"
}
save_bd_design

puts "INFO: ddr_validator_bd (KV260) build complete."
