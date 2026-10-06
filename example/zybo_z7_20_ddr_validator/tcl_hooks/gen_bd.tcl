###############################################################################
# gen_bd.tcl — Zynq-7 PS subsystem for the DDR validator (BD-creation hook).
#
# Sourced in-session by routertl/tcl/xilinx/gen_project.tcl via project.yml
# tcl_hooks.gen_project (RTL-P3.506 model — the BD is a generation RECIPE,
# never a committed .bd artifact). The hook is classified as a BD hook (it
# contains create_bd_cell) and runs through routertl's N-pass healer +
# VLNV-upgrade pre-pass.
#
# This is a pure PS subsystem — PS7, two smartconnects, two proc_sys_reset,
# all wired internally. It exposes four surfaces to the parent RTL
# (src/top_zybo_z7_20_ddr_validator.vhd, which declares this BD as a VHDL
# `component ddr_validator_bd`):
#
#   - DDR_* / FIXED_IO_*       PS dedicated pins (inout, propagate to balls)
#   - M_AXI_GP0  (master)      AXI4-Lite → ddr_validator_csr (in user RTL)
#   - S_AXI_HP0  (slave)       AXI4, 128-bit ← ddr_stream_axi master (user RTL)
#   - fclk_clk0  (100 MHz)     AXI-Lite + system fabric clock + active-low rst
#   - fclk_clk1  (125 MHz)     HP0 DMA-path clock + active-low rst
#
# Mirrors deepskopion/hw/bd/deepskopion_bd.tcl (same board, same IP, same
# two-clock topology). Difference: the validator uses BOTH IP planes (write
# AND read through HP0), and has no EMIO peripherals.
#
# PS7 PCW config is owned by routertl (RTL-P2.618): the zybo-z7-20 board
# preset (routertl/boards/zybo-z7-20/ps7_preset.tcl) auto-applies AFTER this
# hook (resolved from hardware.platform in project.yml). This hook only
# expresses the project DELTAS the preset doesn't cover (the two fabric clock
# frequencies + FCLK1/RESET1 for the second domain).
#
# Topology (BD-internal):
#
#   ┌──────────┐                          ┌──────────────────┐
#   │          │ M_AXI_GP0 ──► S00 ──────►│ smartconnect_gp0 │ M00 ─► M_AXI_GP0
#   │   PS7    │                          └──────────────────┘        (ext port)
#   │  (Zynq   │                          ┌──────────────────┐
#   │  -7020)  │ S_AXI_HP0 ◄── M00 ◄──────│ smartconnect_hp0 │ S00 ◄─ S_AXI_HP0
#   │          │                          └──────────────────┘        (ext port)
#   │          │ FCLK_CLK0 ─┬─────────────────────────────────────► fclk_clk0
#   │          │ FCLK_CLK1 ─┴─────────────────────────────────────► fclk_clk1
#   └──────────┘ FCLK_RESET{0,1}_N ─► rst_ps7_{100,125}M ─► fclk{,_clk1}_aresetn
###############################################################################

puts "INFO: Building ddr_validator_bd (Zynq-7 PS subsystem)..."

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
create_bd_cell -type ip \
    -vlnv xilinx.com:ip:smartconnect:1.0 axi_smartconnect_hp0
set_property -dict [list \
    CONFIG.NUM_SI {1} \
    CONFIG.NUM_MI {1} \
] [get_bd_cells axi_smartconnect_hp0]

# ── PS clock topology deltas ────────────────────────────────────────────────
# FCLK_CLK0 @ 100 MHz: AXI-Lite (GP0) + system fabric. FCLK_CLK1 @ 125 MHz:
# HP0 DMA path (the IP's m_axi4 master). 125 (not 150) — Zynq-7 PLLs don't
# reliably lock to exactly 150 from the typical Zybo clock plan. The board
# preset already turned on PCW_USE_S_AXI_HP0 / PCW_USE_M_AXI_GP0 / FCLK0;
# we add FPGA freqs + the second clock/reset domain.
set_property -dict [list \
    CONFIG.PCW_FPGA0_PERIPHERAL_FREQMHZ {100} \
    CONFIG.PCW_FPGA1_PERIPHERAL_FREQMHZ {125} \
    CONFIG.PCW_EN_CLK1_PORT             {1} \
    CONFIG.PCW_EN_RST1_PORT             {1} \
] $processing_system7_0

# ── Resets ─────────────────────────────────────────────────────────────────
# One proc_sys_reset per clock domain.
create_bd_cell -type ip \
    -vlnv xilinx.com:ip:proc_sys_reset:5.0 rst_ps7_100M
create_bd_cell -type ip \
    -vlnv xilinx.com:ip:proc_sys_reset:5.0 rst_ps7_125M

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
    CONFIG.FREQ_HZ           {125000000} \
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

# Domain 1 — FCLK_CLK1 @ 125 MHz: HP0 DMA path (ddr_stream_axi master).
connect_bd_net \
    [get_bd_pins processing_system7_0/FCLK_CLK1] \
    [get_bd_pins rst_ps7_125M/slowest_sync_clk] \
    [get_bd_pins axi_smartconnect_hp0/aclk] \
    [get_bd_pins processing_system7_0/S_AXI_HP0_ACLK] \
    [get_bd_ports fclk_clk1]

connect_bd_net \
    [get_bd_pins processing_system7_0/FCLK_RESET1_N] \
    [get_bd_pins rst_ps7_125M/ext_reset_in]

connect_bd_net \
    [get_bd_pins rst_ps7_125M/peripheral_aresetn] \
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

puts "INFO: ddr_validator_bd build complete."
