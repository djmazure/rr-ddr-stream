# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
# SPDX-License-Identifier: CERN-OHL-W-2.0
#
# PolarFire SoC Discovery Kit — Libero SmartDesign builder for the MSS fabric subsystem.
# Referenced from example/pfsoc_disco_ddr_validator/project.yml tcl_hooks.gen_project.
#
# Generates the `pfsoc_mss` SmartDesign presenting the fabric interface to the
# VHDL top `top_pfsoc_disco_ddr_validator` (src/top_pfsoc_disco_ddr_validator.vhd).
#
# DDR-training requirement (DS-P1.60 / DDR-P2.18 / DDR-P2.22):
# FIC_2_ACLK and FIC_3_PCLK are clocked from u_ccc:OUT0_FABCLK_0. FIC_2 has its
# embedded DLL enabled; without a clock, the DLL never locks, which gates MSS bring-up
# and leaves DDR-PLL bit 25 = 0 (unlocked). Clocking all four FICs allows the DDR-PLL
# to lock (0x3300003f).

if {![info exists mss_cxz]} {
    set mss_cxz "mss/disco_mss.cxz"
}

# ---------------------------------------------------------------------------
# 0. VHDL-2008 project mode
# ---------------------------------------------------------------------------
project_settings -vhdl_mode {VHDL_2008}

# ---------------------------------------------------------------------------
# 0b. VHDL library mapping
# ---------------------------------------------------------------------------
if {[info exists g_libraries]} {
    dict for {lib lib_files} $g_libraries {
        if {$lib eq "work"} { continue }
        catch { add_library -library $lib }
        foreach f $lib_files {
            if {[string tolower [file extension $f]] ni {.vhd .vhdl}} { continue }
            if {[string match "rr_rea_*" [file tail $f]]} { continue }
            set proj_copy [file join $g_project_dir hdl [file tail $f]]
            if {[file exists $proj_copy]} {
                if {[catch { add_file_to_library -library $lib -file $proj_copy } lib_err]} {
                    puts "Warning: could not assign $proj_copy to library $lib: $lib_err"
                }
            } else {
                puts "Warning: library $lib source not in project hdl/: $f"
            }
        }
    }
} else {
    puts "Warning: g_libraries not provided by build_env.tcl — VHDL library mapping skipped."
}

# Use non-Xilinx FIFO stubs
build_design_hierarchy
foreach {stub module} {
    rr_fifo_async_mixed_xpm_body_stub.vhd rr_fifo_async_mixed_xpm_body
    rr_fifo_sync_xpm_body_stub.vhd        rr_fifo_sync_xpm_body
} {
    set stub_path [file join $g_project_dir hdl $stub]
    if {[file exists $stub_path]} {
        if {[catch { use_source_file -file $stub_path -module $module } use_err]} {
            puts "Warning: use_source_file $stub -> $module failed: $use_err"
        }
    }
}

# ---------------------------------------------------------------------------
# 1. Import MSS configuration and instantiate disco_mss
# ---------------------------------------------------------------------------
import_mss_component \
    -file  $mss_cxz

catch { delete_component -component_name {pfsoc_mss} }
catch { delete_component -component_name {pfsoc_ccc_100} }

create_smartdesign -sd_name {pfsoc_mss}

sd_instantiate_component \
    -sd_name {pfsoc_mss} \
    -component_name {disco_mss} \
    -instance_name {u_mss}

# Helper: create SmartDesign port and wire to MSS pin
proc pf_wire {port dir width mss_pin} {
    if {$width == 1} {
        sd_create_scalar_port -sd_name {pfsoc_mss} -port_name $port -port_direction $dir
    } else {
        sd_create_bus_port -sd_name {pfsoc_mss} -port_name $port -port_direction $dir \
            -port_range "\[[expr {$width - 1}]:0\]"
    }
    sd_connect_pins -sd_name {pfsoc_mss} -pin_names [list $port "u_mss:$mss_pin"]
}

# ---------------------------------------------------------------------------
# 2. Fabric clock & CCC: 50 MHz ref_clk -> 100 MHz clk_sys
# ---------------------------------------------------------------------------
create_and_configure_core \
    -core_vlnv {Actel:SgCore:PF_CCC:2.2.220} \
    -component_name {pfsoc_ccc_100} \
    -params [list \
        "PLL_IN_FREQ_0:50" \
        "GL0_0_IS_USED:true" \
        "GL0_0_OUT_FREQ:100" \
        "GL0_0_FABCLK_USED:true" \
    ]
sd_instantiate_component -sd_name {pfsoc_mss} -component_name {pfsoc_ccc_100} -instance_name {u_ccc}

sd_create_scalar_port  -sd_name {pfsoc_mss} -port_name {ref_clk_50mhz_i} -port_direction {IN}
sd_connect_pins        -sd_name {pfsoc_mss} -pin_names {ref_clk_50mhz_i u_ccc:REF_CLK_0}
sd_create_scalar_port  -sd_name {pfsoc_mss} -port_name {clk_fabric_o} -port_direction {OUT}
sd_connect_pins        -sd_name {pfsoc_mss} -pin_names {clk_fabric_o u_ccc:OUT0_FABCLK_0}
sd_connect_pins        -sd_name {pfsoc_mss} -pin_names {u_ccc:OUT0_FABCLK_0 u_mss:FIC_0_ACLK}
sd_connect_pins        -sd_name {pfsoc_mss} -pin_names {u_ccc:OUT0_FABCLK_0 u_mss:FIC_1_ACLK}

# DDR-training fix (DS-P1.60 / DDR-P2.18 / DDR-P2.22): clock FIC2/FIC3 from CCC.
# FIC_2 embedded DLL requires clock to lock, which unblocks MSS DDR-PLL lock.
sd_connect_pins        -sd_name {pfsoc_mss} -pin_names {u_ccc:OUT0_FABCLK_0 u_mss:FIC_2_ACLK}
sd_connect_pins        -sd_name {pfsoc_mss} -pin_names {u_ccc:OUT0_FABCLK_0 u_mss:FIC_3_PCLK}

# MSS fabric reset
sd_create_scalar_port  -sd_name {pfsoc_mss} -port_name {mss_reset_n_o} -port_direction {OUT}
sd_connect_pins        -sd_name {pfsoc_mss} -pin_names {mss_reset_n_o u_mss:MSS_RESET_N_M2F}

# ---------------------------------------------------------------------------
# 3. FIC0 — MSS initiator AXI4 ports (38 addr / 64 data / 8 id)
# ---------------------------------------------------------------------------
foreach {port dir width suffix} {
    fic0_awid_o    OUT 8  AWID    fic0_awaddr_o  OUT 38 AWADDR  fic0_awlen_o   OUT 8  AWLEN
    fic0_awsize_o  OUT 3  AWSIZE  fic0_awburst_o OUT 2  AWBURST fic0_awlock_o  OUT 1  AWLOCK
    fic0_awcache_o OUT 4  AWCACHE fic0_awprot_o  OUT 3  AWPROT  fic0_awqos_o   OUT 4  AWQOS
    fic0_awvalid_o OUT 1  AWVALID fic0_awready_i IN  1  AWREADY fic0_wdata_o   OUT 64 WDATA
    fic0_wstrb_o   OUT 8  WSTRB   fic0_wlast_o   OUT 1  WLAST   fic0_wvalid_o  OUT 1  WVALID
    fic0_wready_i  IN  1  WREADY  fic0_bid_i     IN  8  BID     fic0_bresp_i   IN  2  BRESP
    fic0_bvalid_i  IN  1  BVALID  fic0_bready_o  OUT 1  BREADY  fic0_arid_o    OUT 8  ARID
    fic0_araddr_o  OUT 38 ARADDR  fic0_arlen_o   OUT 8  ARLEN   fic0_arsize_o  OUT 3  ARSIZE
    fic0_arburst_o OUT 2  ARBURST fic0_arlock_o  OUT 1  ARLOCK  fic0_arcache_o OUT 4  ARCACHE
    fic0_arprot_o  OUT 3  ARPROT  fic0_arqos_o   OUT 4  ARQOS   fic0_arvalid_o OUT 1  ARVALID
    fic0_arready_i IN  1  ARREADY fic0_rid_i     IN  8  RID     fic0_rdata_i   IN  64 RDATA
    fic0_rresp_i   IN  2  RRESP   fic0_rlast_i   IN  1  RLAST   fic0_rvalid_i  IN  1  RVALID
    fic0_rready_o  OUT 1  RREADY
} {
    pf_wire $port $dir $width "FIC_0_AXI4_M_$suffix"
}

# ---------------------------------------------------------------------------
# 4. FIC1 — fabric -> MSS DDR AXI4 target (64-bit native, 4-bit id)
# ---------------------------------------------------------------------------
foreach {port dir width suffix} {
    fic1_awid_i    IN  4  AWID    fic1_awaddr_i  IN  38 AWADDR  fic1_awlen_i   IN  8  AWLEN
    fic1_awsize_i  IN  3  AWSIZE  fic1_awburst_i IN  2  AWBURST fic1_awlock_i  IN  1  AWLOCK
    fic1_awcache_i IN  4  AWCACHE fic1_awprot_i  IN  3  AWPROT  fic1_awqos_i   IN  4  AWQOS
    fic1_awvalid_i IN  1  AWVALID fic1_awready_o OUT 1  AWREADY fic1_wdata_i   IN  64 WDATA
    fic1_wstrb_i   IN  8  WSTRB   fic1_wlast_i   IN  1  WLAST   fic1_wvalid_i  IN  1  WVALID
    fic1_wready_o  OUT 1  WREADY  fic1_bid_o     OUT 4  BID     fic1_bresp_o   OUT 2  BRESP
    fic1_bvalid_o  OUT 1  BVALID  fic1_bready_i  IN  1  BREADY  fic1_arid_i    IN  4  ARID
    fic1_araddr_i  IN  38 ARADDR  fic1_arlen_i   IN  8  ARLEN   fic1_arsize_i  IN  3  ARSIZE
    fic1_arburst_i IN  2  ARBURST fic1_arlock_i  IN  1  ARLOCK  fic1_arcache_i IN  4  ARCACHE
    fic1_arprot_i  IN  3  ARPROT  fic1_arqos_i   IN  4  ARQOS   fic1_arvalid_i IN  1  ARVALID
    fic1_arready_o OUT 1  ARREADY fic1_rid_o     OUT 4  RID     fic1_rdata_o   OUT 64 RDATA
    fic1_rresp_o   OUT 2  RRESP   fic1_rlast_o   OUT 1  RLAST   fic1_rvalid_o  OUT 1  RVALID
    fic1_rready_i  IN  1  RREADY
} {
    pf_wire $port $dir $width "FIC_1_AXI4_S_$suffix"
}

# ---------------------------------------------------------------------------
# 4b. Tie unused MSS/CCC inputs (electrically idle)
# ---------------------------------------------------------------------------
foreach {pin lvl} {
    u_ccc:PLL_POWERDOWN_N_0 VCC
    u_mss:MMUART_1_RXD_F2M  VCC
    u_mss:MMUART_2_RXD_F2M  VCC
    u_mss:MMUART_3_RXD_F2M  VCC
    u_mss:MMUART_4_RXD_F2M  VCC
    u_mss:CAN_0_RXBUS_F2M   VCC
    u_mss:QSPI_DATA2_F2M    VCC
    u_mss:QSPI_DATA3_F2M    VCC
    u_mss:SPI_0_SS_F2M      VCC
    u_mss:SPI_0_DI_F2M      GND
    u_mss:SPI_0_CLK_F2M     GND
    u_mss:I2C_0_SCL_F2M     VCC
    u_mss:I2C_0_SDA_F2M     VCC
    u_mss:GPIO_2_F2M_31     GND
    u_mss:GPIO_2_F2M_30     GND
    u_mss:MAC_1_MDI_F2M     VCC
    u_mss:MSS_RESET_N_F2M   VCC
} {
    sd_connect_pins_to_constant -sd_name {pfsoc_mss} -pin_names $pin -value $lvl
}

# ---------------------------------------------------------------------------
# 5. F2M fabric interrupts -> PLIC
# ---------------------------------------------------------------------------
pf_wire mss_f2m_i IN 64 MSS_INT_F2M

# ---------------------------------------------------------------------------
# 6. Finalise pfsoc_mss SmartDesign
# ---------------------------------------------------------------------------
save_smartdesign   -sd_name {pfsoc_mss}
generate_component -component_name {pfsoc_mss}

# Set the top-level root module in Libero to the VHDL top
build_design_hierarchy
set_root -module {top_pfsoc_disco_ddr_validator::work}

# ---------------------------------------------------------------------------
# 7. Pin the SYNTHESIZE fileset
# ---------------------------------------------------------------------------
if {[info exists g_libraries] && [info exists syn_files]} {
    set pf_ordered {}
    set pf_seen [dict create]
    foreach comp_pat {
        component/work/disco_mss/*_syn_comps.v
        component/work/disco_mss/disco_mss.v
        component/work/pfsoc_ccc_100/*/*.v
        component/work/pfsoc_ccc_100/pfsoc_ccc_100.v
        component/work/pfsoc_mss/pfsoc_mss.vhd
    } {
        foreach pf [lsort [glob -nocomplain [file join $g_project_dir $comp_pat]]] {
            if {![dict exists $pf_seen $pf]} {
                dict set pf_seen $pf 1
                lappend pf_ordered $pf
            }
        }
    }
    set pf_bid [file join $g_project_dir hdl rr_rea_build_id_pkg.vhd]
    if {[file exists $pf_bid] && ![dict exists $pf_seen $pf_bid]} {
        dict set pf_seen $pf_bid 1
        lappend pf_ordered $pf_bid
    }
    foreach lib {olo en_tb ctu_can_fd_rtl rr_rea rr tich} {
        if {![dict exists $g_libraries $lib]} { continue }
        foreach f [dict get $g_libraries $lib] {
            if {[string match "*support/xilinx/*" $f]} { continue }
            if {[string match "*rr_rea_jtag_xilinx7.vhd" $f] || [string match "*rr_rea_jtag_intel.vhd" $f]} { continue }
            if {[string tolower [file extension $f]] ni {.vhd .vhdl .v .sv}} { continue }
            set pf [file join $g_project_dir hdl [file tail $f]]
            if {[file exists $pf] && ![dict exists $pf_seen $pf]} {
                dict set pf_seen $pf 1
                lappend pf_ordered $pf
            }
        }
    }
    set pf_libfiles [dict create]
    dict for {lib lf} $g_libraries {
        foreach f $lf { dict set pf_libfiles [file tail $f] 1 }
    }
    foreach f $syn_files {
        if {[string tolower [file extension $f]] ni {.vhd .vhdl .v .sv}} { continue }
        if {[dict exists $pf_libfiles [file tail $f]]} { continue }
        set pf [file join $g_project_dir hdl [file tail $f]]
        if {[file exists $pf] && ![dict exists $pf_seen $pf]} {
            dict set pf_seen $pf 1
            lappend pf_ordered $pf
        }
    }
    set pf_cmd [list organize_tool_files -tool {SYNTHESIZE}]
    foreach pf $pf_ordered { lappend pf_cmd -file $pf }
    lappend pf_cmd -module {top_pfsoc_disco_ddr_validator::work} -input_type {source}
    if {[catch { eval $pf_cmd } org_err]} {
        puts "Warning: organize_tool_files (SYNTHESIZE fileset) failed: $org_err"
    } else {
        puts "INFO: organize_tool_files pinned [llength $pf_ordered] synthesis sources"
    }
}
