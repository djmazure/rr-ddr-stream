###############################################################################
# gen_bd.tcl — Zynq-7 PS subsystem for the DDR validator (BD-creation hook).
#              ORIGINAL Digilent Zybo (Classic, PB200-279, xc7z010clg400-1).
#
# Sourced in-session by routertl/tcl/xilinx/gen_project.tcl via project.yml
# tcl_hooks.gen_project (RTL-P3.506 model — the BD is a generation RECIPE,
# never a committed .bd artifact). The hook is classified as a BD hook (it
# contains create_bd_cell) and runs through routertl's N-pass healer +
# VLNV-upgrade pre-pass.
#
# Sibling of example/zybo_z7_20_ddr_validator/tcl_hooks/gen_bd.tcl. SAME
# topology (PS7 + 2× smartconnect + 2× proc_sys_reset, GP0 + HP0). Three
# Classic-specific deltas vs the z020 sibling:
#
#   1. The Classic `zybo` board preset (routertl/boards/zybo/ps7_preset.tcl)
#      ships GP0 + HP0 *disabled* (PCW_USE_M_AXI_GP0 0 / PCW_USE_S_AXI_HP0 0).
#      The z020 preset ships them on. So this hook ENABLES them explicitly
#      after the preset applies. (The z020 sibling does not need to.)
#   2. FCLK1 = 100 MHz (the HP0 DMA path), NOT 125. The xc7z010 is the small
#      part (~17600 LUTs); the ring's burstw_setter / axi4w / ptr_mgr paths
#      top out around ~120 MHz, so 125 is marginal. 100 MHz is the
#      known-closing single-rate. The async-FIFO CDC inside the IP still
#      crosses two physically distinct FCLK outputs (FCLK0=FCLK1=100), so the
#      CDC is still exercised end-to-end — it is just a 1:1 ratio here.
#   3. The Classic preset leaves the PL->PS fabric interrupt + IRQ_F2P on;
#      this example has no PL->PS IRQ, so tcl_hooks/ps7_unused_off.tcl turns
#      them off (mirrors the z020 sibling's S_AXI_HP2 cleanup).
#
# Surfaces exposed to the parent RTL (src/top_zybo_classic_ddr_validator.vhd,
# which declares this BD as a VHDL `component ddr_validator_bd`):
#
#   - DDR_* / FIXED_IO_*       PS dedicated pins (inout, propagate to balls)
#   - M_AXI_GP0  (master)      AXI4-Lite → ddr_validator_csr (in user RTL)
#   - S_AXI_HP0  (slave)       AXI4, 128-bit ← ddr_stream_axi master (user RTL)
#   - fclk_clk0  (100 MHz)     AXI-Lite + system fabric clock + active-low rst
#   - fclk_clk1  (100 MHz)     HP0 DMA-path clock + active-low rst
#
# PS7 PCW config is owned by routertl (RTL-P2.618): the `zybo` board preset
# (routertl/boards/zybo/ps7_preset.tcl) auto-applies AFTER this hook (resolved
# from hardware.platform). This hook expresses the project DELTAS the preset
# doesn't cover (GP0/HP0 enable + the two fabric clock freqs + FCLK1/RESET1).
###############################################################################

puts "INFO: Building ddr_validator_bd (Zynq-7 PS subsystem, Classic xc7z010)..."

# ── BD design ──────────────────────────────────────────────────────────────
# Hook owns BD creation directly (post-RTL-P3.506 — design name comes from the
# hook, matching the `component ddr_validator_bd` in the user top).
create_bd_design ddr_validator_bd
current_bd_design [get_bd_designs ddr_validator_bd]

# ── Processing System 7 ──────────────────────────────────────────────────
set processing_system7_0 [create_bd_cell -type ip \
    -vlnv xilinx.com:ip:processing_system7:5.5 processing_system7_0]

# ── Smart Connects ─────────────────────────────────────────────────────────
# PS GP0 master → external M_AXI_GP0 port (CSR slave is in user RTL).
create_bd_cell -type ip \
    -vlnv xilinx.com:ip:smartconnect:1.0 axi_smartconnect_gp0
set_property -dict [list \
    CONFIG.NUM_SI {1} \
    CONFIG.NUM_MI {1} \
] [get_bd_cells axi_smartconnect_gp0]

# External S_AXI_HP0 port (ddr_stream_axi master is in user RTL) → PS HP0 slave.
# The smartconnect also performs the AXI4→AXI3 conversion the HP port needs
# (HP ports are AXI3, 16-beat max): it splits any AXI4 burst >16 into legal
# AXI3 sub-bursts. (max_burst_len_g, which would cap bursts at the source, is
# the rr-idp vendored copy's generic — DDR-P2.5 — not yet in this canonical
# IP; the z020 sibling relies on the same smartconnect split and works.)
create_bd_cell -type ip \
    -vlnv xilinx.com:ip:smartconnect:1.0 axi_smartconnect_hp0
set_property -dict [list \
    CONFIG.NUM_SI {1} \
    CONFIG.NUM_MI {1} \
] [get_bd_cells axi_smartconnect_hp0]

# ── PS GP0/HP0 enable + clock topology deltas ───────────────────────────────
# The Classic `zybo` preset ships GP0+HP0 DISABLED — enable them here (the
# z020 preset ships them on, so its sibling hook omits these two lines).
# FCLK_CLK0 @ 100 MHz: AXI-Lite (GP0) + system fabric. FCLK_CLK1 @ 100 MHz:
# HP0 DMA path (the IP's m_axi4 master) — 100 (not 125) to close on the small
# z010 ring paths (see header note 2).
set_property -dict [list \
    CONFIG.PCW_USE_M_AXI_GP0            {1} \
    CONFIG.PCW_USE_S_AXI_HP0            {1} \
    CONFIG.PCW_S_AXI_HP0_DATA_WIDTH     {64} \
    CONFIG.PCW_FPGA0_PERIPHERAL_FREQMHZ {100} \
    CONFIG.PCW_FPGA1_PERIPHERAL_FREQMHZ {100} \
    CONFIG.PCW_EN_CLK1_PORT             {1} \
    CONFIG.PCW_EN_RST1_PORT             {1} \
] $processing_system7_0

# ── Resets ─────────────────────────────────────────────────────────────────
# One proc_sys_reset per clock domain.
create_bd_cell -type ip \
    -vlnv xilinx.com:ip:proc_sys_reset:5.0 rst_ps7_100M
create_bd_cell -type ip \
    -vlnv xilinx.com:ip:proc_sys_reset:5.0 rst_ps7_100M_b

# ── Internal AXI Connections ───────────────────────────────────────────────
connect_bd_intf_net \
    [get_bd_intf_pins processing_system7_0/M_AXI_GP0] \
    [get_bd_intf_pins axi_smartconnect_gp0/S00_AXI]

connect_bd_intf_net \
    [get_bd_intf_pins axi_smartconnect_hp0/M00_AXI] \
    [get_bd_intf_pins processing_system7_0/S_AXI_HP0]

# ── External Interface Ports ───────────────────────────────────────────────
# PS dedicated pins (DDR + FIXED_IO) — propagate to physical balls via the
# user top. Vivado appends `_0` to make_bd_intf_pins_external names; rename
# back so the component decl can use the natural DDR / FIXED_IO names.
make_bd_intf_pins_external [get_bd_intf_pins processing_system7_0/DDR]
make_bd_intf_pins_external [get_bd_intf_pins processing_system7_0/FIXED_IO]
set_property name DDR      [get_bd_intf_ports DDR_0]
set_property name FIXED_IO [get_bd_intf_ports FIXED_IO_0]

# AXI-Lite master out (drives ddr_validator_csr slave in user RTL).
set m_axi_gp0_port [create_bd_intf_port -mode Master \
    -vlnv xilinx.com:interface:aximm_rtl:1.0 M_AXI_GP0]
set_property -dict [list \
    CONFIG.PROTOCOL    {AXI4LITE} \
    CONFIG.ADDR_WIDTH  {32} \
    CONFIG.DATA_WIDTH  {32} \
    CONFIG.HAS_BURST   {0} \
    CONFIG.HAS_CACHE   {0} \
    CONFIG.HAS_LOCK    {0} \
    CONFIG.HAS_QOS     {0} \
    CONFIG.HAS_REGION  {0} \
] $m_axi_gp0_port
connect_bd_intf_net $m_axi_gp0_port \
    [get_bd_intf_pins axi_smartconnect_gp0/M00_AXI]

# AXI4 slave in (accepts ddr_stream_axi master in user RTL). 128-bit data,
# 2-bit ID — matches the IP record fields sliced to the BD width in the top.
set s_axi_hp0_port [create_bd_intf_port -mode Slave \
    -vlnv xilinx.com:interface:aximm_rtl:1.0 S_AXI_HP0]
set_property -dict [list \
    CONFIG.PROTOCOL    {AXI4} \
    CONFIG.ADDR_WIDTH  {32} \
    CONFIG.DATA_WIDTH  {128} \
    CONFIG.ID_WIDTH    {2} \
    CONFIG.HAS_BURST   {1} \
    CONFIG.HAS_LOCK    {1} \
    CONFIG.HAS_CACHE   {1} \
    CONFIG.HAS_QOS     {1} \
] $s_axi_hp0_port
connect_bd_intf_net $s_axi_hp0_port \
    [get_bd_intf_pins axi_smartconnect_hp0/S00_AXI]

# Clock + reset out ports. Each clock carries its associated busif + reset so
# downstream STA derives the generated clocks correctly.
set fclk_clk0_port [create_bd_port -dir O -type clk fclk_clk0]
set_property -dict [list \
    CONFIG.FREQ_HZ           {100000000} \
    CONFIG.ASSOCIATED_BUSIF  {M_AXI_GP0} \
    CONFIG.ASSOCIATED_RESET  {fclk_aresetn} \
] $fclk_clk0_port
set fclk_aresetn_port [create_bd_port -dir O -type rst fclk_aresetn]
set_property CONFIG.POLARITY ACTIVE_LOW $fclk_aresetn_port

set fclk_clk1_port [create_bd_port -dir O -type clk fclk_clk1]
set_property -dict [list \
    CONFIG.FREQ_HZ           {100000000} \
    CONFIG.ASSOCIATED_BUSIF  {S_AXI_HP0} \
    CONFIG.ASSOCIATED_RESET  {fclk_clk1_aresetn} \
] $fclk_clk1_port
set fclk_clk1_aresetn_port [create_bd_port -dir O -type rst fclk_clk1_aresetn]
set_property CONFIG.POLARITY ACTIVE_LOW $fclk_clk1_aresetn_port

# ── Clock + Reset Distribution ─────────────────────────────────────────────
# Domain 0 — FCLK_CLK0 @ 100 MHz: AXI-Lite GP0 path + system fabric.
connect_bd_net \
    [get_bd_pins processing_system7_0/FCLK_CLK0] \
    [get_bd_pins rst_ps7_100M/slowest_sync_clk] \
    [get_bd_pins axi_smartconnect_gp0/aclk] \
    [get_bd_pins processing_system7_0/M_AXI_GP0_ACLK] \
    [get_bd_ports fclk_clk0]

connect_bd_net \
    [get_bd_pins processing_system7_0/FCLK_RESET0_N] \
    [get_bd_pins rst_ps7_100M/ext_reset_in]

connect_bd_net \
    [get_bd_pins rst_ps7_100M/peripheral_aresetn] \
    [get_bd_pins axi_smartconnect_gp0/aresetn] \
    [get_bd_ports fclk_aresetn]

# Domain 1 — FCLK_CLK1 @ 100 MHz: HP0 DMA path (ddr_stream_axi master).
connect_bd_net \
    [get_bd_pins processing_system7_0/FCLK_CLK1] \
    [get_bd_pins rst_ps7_100M_b/slowest_sync_clk] \
    [get_bd_pins axi_smartconnect_hp0/aclk] \
    [get_bd_pins processing_system7_0/S_AXI_HP0_ACLK] \
    [get_bd_ports fclk_clk1]

connect_bd_net \
    [get_bd_pins processing_system7_0/FCLK_RESET1_N] \
    [get_bd_pins rst_ps7_100M_b/ext_reset_in]

connect_bd_net \
    [get_bd_pins rst_ps7_100M_b/peripheral_aresetn] \
    [get_bd_pins axi_smartconnect_hp0/aresetn] \
    [get_bd_ports fclk_clk1_aresetn]

# ── Address Map ────────────────────────────────────────────────────────────
# PS GP0 → external M_AXI_GP0 region — CSR lives at 0x4000_0000, 4 KiB
# (must agree with sw/linux/system_memory_map.yml + the CSR's 12-bit decode).
assign_bd_address -offset 0x40000000 -range 0x00001000 \
    -target_address_space [get_bd_addr_spaces processing_system7_0/Data] \
    [get_bd_addr_segs M_AXI_GP0/Reg]

# External master via S_AXI_HP0 → PS DDR. Auto-assign over HP0_DDR_LOWOCM
# (covers the full PS DDR window the HP0 slave reaches; the IP ring at
# ddr_base 0x1000_0000 lives inside it).
assign_bd_address \
    -target_address_space [get_bd_addr_spaces S_AXI_HP0] \
    [get_bd_addr_segs processing_system7_0/S_AXI_HP0/HP0_DDR_LOWOCM]

# ── Validate & Save ──────────────────────────────────────────────────────
regenerate_bd_layout
validate_bd_design
save_bd_design

puts "INFO: ddr_validator_bd (Classic xc7z010) build complete."
