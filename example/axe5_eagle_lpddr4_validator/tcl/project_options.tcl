# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
# SPDX-License-Identifier: MIT
#
# Quartus assignments for axe5_eagle_lpddr4_validator (rr tcl_hooks).
# Device A5ED065BB32AE4SR0, Quartus Prime Pro 26.1. Pin positions are board
# facts of the Arrow AXE5-Eagle (board vendor reference design pinout).
#
# hps_subsys is added to the project by rr (generated from ip.yml). Only the
# fabric LPDDR4 controller is added here: the pre_build hook
# scripts/generate_fabric_emif.py generates it outside the HPS subsystem.
# The HPS LPDDR4 pins (hps_mem_*) are fixed-function pins of the HPS memory
# bank; the Fitter places them. rr sets DEVICE_INITIALIZATION_CLOCK itself
# because the project contains HPS / EMIF IP.

set_global_assignment -name OPTIMIZATION_MODE "BALANCED"
set_global_assignment -name VHDL_INPUT_VERSION VHDL_2008

set_global_assignment -name QIP_FILE \
    "[file normalize $g_root_dir/ip/emif_a5/golden_ghrd/emif_subsystem/emif_bank2a_fpga/emif_bank2a_fpga.qip]"

# Reset push button and status LEDs (3.3 V LVCMOS).
set_location_assignment PIN_CK134 -to fpga_rst_n
set_instance_assignment -name IO_STANDARD "3.3-V LVCMOS" -to fpga_rst_n
set_location_assignment PIN_CK125 -to user_led[0]
set_location_assignment PIN_CL125 -to user_led[1]
set_location_assignment PIN_BR118 -to user_led[2]
set_location_assignment PIN_CF118 -to user_led[3]
set_location_assignment PIN_BW118 -to user_led[4]
set_location_assignment PIN_CA118 -to user_led[5]
set_location_assignment PIN_CL128 -to user_led[6]
set_location_assignment PIN_CK128 -to user_led[7]
foreach led_i {0 1 2 3 4 5 6 7} {
    set_instance_assignment -name IO_STANDARD "3.3-V LVCMOS" -to user_led[$led_i]
}

# USB 3.1 PHY reference clock (required by the HPS component, CML).
set_location_assignment PIN_AP120 -to usb31_phy_refclk_p
set_instance_assignment -name IO_STANDARD "Current Mode Logic (CML)" -to usb31_phy_refclk_p

# HPS dedicated I/O, 1.8 V. Must match the pin-mux of ip/agilex_hps/agilex_hps.ip.
set_location_assignment PIN_T132 -to hps_io_hps_osc_clk
set_instance_assignment -name IO_STANDARD "1.8 V" -to hps_io_hps_osc_clk
set_location_assignment PIN_E135 -to hps_io_sdmmc_data0
set_instance_assignment -name IO_STANDARD "1.8 V" -to hps_io_sdmmc_data0
set_location_assignment PIN_F132 -to hps_io_sdmmc_data1
set_instance_assignment -name IO_STANDARD "1.8 V" -to hps_io_sdmmc_data1
set_location_assignment PIN_D132 -to hps_io_sdmmc_cclk
set_instance_assignment -name IO_STANDARD "1.8 V" -to hps_io_sdmmc_cclk
set_location_assignment PIN_AA135 -to hps_io_sdmmc_data2
set_instance_assignment -name IO_STANDARD "1.8 V" -to hps_io_sdmmc_data2
set_location_assignment PIN_V127 -to hps_io_sdmmc_data3
set_instance_assignment -name IO_STANDARD "1.8 V" -to hps_io_sdmmc_data3
set_location_assignment PIN_AB132 -to hps_io_sdmmc_cmd
set_instance_assignment -name IO_STANDARD "1.8 V" -to hps_io_sdmmc_cmd
set_location_assignment PIN_P132 -to hps_io_usb1_clk
set_instance_assignment -name IO_STANDARD "1.8 V" -to hps_io_usb1_clk
set_location_assignment PIN_L135 -to hps_io_usb1_stp
set_instance_assignment -name IO_STANDARD "1.8 V" -to hps_io_usb1_stp
set_location_assignment PIN_J135 -to hps_io_usb1_dir
set_instance_assignment -name IO_STANDARD "1.8 V" -to hps_io_usb1_dir
set_location_assignment PIN_AD135 -to hps_io_usb1_data0
set_instance_assignment -name IO_STANDARD "1.8 V" -to hps_io_usb1_data0
set_location_assignment PIN_M132 -to hps_io_usb1_data1
set_instance_assignment -name IO_STANDARD "1.8 V" -to hps_io_usb1_data1
set_location_assignment PIN_AD134 -to hps_io_usb1_nxt
set_instance_assignment -name IO_STANDARD "1.8 V" -to hps_io_usb1_nxt
set_location_assignment PIN_K132 -to hps_io_usb1_data2
set_instance_assignment -name IO_STANDARD "1.8 V" -to hps_io_usb1_data2
set_location_assignment PIN_AG129 -to hps_io_usb1_data3
set_instance_assignment -name IO_STANDARD "1.8 V" -to hps_io_usb1_data3
set_location_assignment PIN_J134 -to hps_io_usb1_data4
set_instance_assignment -name IO_STANDARD "1.8 V" -to hps_io_usb1_data4
set_location_assignment PIN_AG120 -to hps_io_usb1_data5
set_instance_assignment -name IO_STANDARD "1.8 V" -to hps_io_usb1_data5
set_location_assignment PIN_G134 -to hps_io_usb1_data6
set_instance_assignment -name IO_STANDARD "1.8 V" -to hps_io_usb1_data6
set_location_assignment PIN_G135 -to hps_io_usb1_data7
set_instance_assignment -name IO_STANDARD "1.8 V" -to hps_io_usb1_data7
set_location_assignment PIN_M127 -to hps_io_emac2_tx_clk
set_instance_assignment -name IO_STANDARD "1.8 V" -to hps_io_emac2_tx_clk
set_location_assignment PIN_K127 -to hps_io_emac2_tx_ctl
set_instance_assignment -name IO_STANDARD "1.8 V" -to hps_io_emac2_tx_ctl
set_location_assignment PIN_M124 -to hps_io_emac2_rx_clk
set_instance_assignment -name IO_STANDARD "1.8 V" -to hps_io_emac2_rx_clk
set_location_assignment PIN_AB127 -to hps_io_emac2_rx_ctl
set_instance_assignment -name IO_STANDARD "1.8 V" -to hps_io_emac2_rx_ctl
set_location_assignment PIN_K124 -to hps_io_emac2_txd0
set_instance_assignment -name IO_STANDARD "1.8 V" -to hps_io_emac2_txd0
set_location_assignment PIN_Y127 -to hps_io_emac2_txd1
set_instance_assignment -name IO_STANDARD "1.8 V" -to hps_io_emac2_txd1
set_location_assignment PIN_H127 -to hps_io_emac2_rxd0
set_instance_assignment -name IO_STANDARD "1.8 V" -to hps_io_emac2_rxd0
set_location_assignment PIN_AB124 -to hps_io_emac2_rxd1
set_instance_assignment -name IO_STANDARD "1.8 V" -to hps_io_emac2_rxd1
set_location_assignment PIN_F127 -to hps_io_emac2_txd2
set_instance_assignment -name IO_STANDARD "1.8 V" -to hps_io_emac2_txd2
set_location_assignment PIN_Y124 -to hps_io_emac2_txd3
set_instance_assignment -name IO_STANDARD "1.8 V" -to hps_io_emac2_txd3
set_location_assignment PIN_F124 -to hps_io_emac2_rxd2
set_instance_assignment -name IO_STANDARD "1.8 V" -to hps_io_emac2_rxd2
set_location_assignment PIN_D124 -to hps_io_emac2_rxd3
set_instance_assignment -name IO_STANDARD "1.8 V" -to hps_io_emac2_rxd3
set_location_assignment PIN_T127 -to hps_io_mdio2_mdio
set_instance_assignment -name IO_STANDARD "1.8 V" -to hps_io_mdio2_mdio
set_location_assignment PIN_Y132 -to hps_io_mdio2_mdc
set_instance_assignment -name IO_STANDARD "1.8 V" -to hps_io_mdio2_mdc
set_location_assignment PIN_W134 -to hps_io_uart0_tx
set_instance_assignment -name IO_STANDARD "1.8 V" -to hps_io_uart0_tx
set_location_assignment PIN_AK115 -to hps_io_uart0_rx
set_instance_assignment -name IO_STANDARD "1.8 V" -to hps_io_uart0_rx
set_location_assignment PIN_U134 -to hps_io_i2c0_sda
set_instance_assignment -name IO_STANDARD "1.8 V" -to hps_io_i2c0_sda
set_location_assignment PIN_AL120 -to hps_io_i2c0_scl
set_instance_assignment -name IO_STANDARD "1.8 V" -to hps_io_i2c0_scl
set_location_assignment PIN_U135 -to hps_io_gpio1
set_instance_assignment -name IO_STANDARD "1.8 V" -to hps_io_gpio1
set_location_assignment PIN_R134 -to hps_io_gpio6
set_instance_assignment -name IO_STANDARD "1.8 V" -to hps_io_gpio6
set_location_assignment PIN_AG115 -to hps_io_gpio7
set_instance_assignment -name IO_STANDARD "1.8 V" -to hps_io_gpio7
set_location_assignment PIN_N135 -to hps_io_gpio8
set_instance_assignment -name IO_STANDARD "1.8 V" -to hps_io_gpio8
set_location_assignment PIN_AK120 -to hps_io_gpio9
set_instance_assignment -name IO_STANDARD "1.8 V" -to hps_io_gpio9
set_location_assignment PIN_N134 -to hps_io_gpio10
set_instance_assignment -name IO_STANDARD "1.8 V" -to hps_io_gpio10
set_location_assignment PIN_B134 -to hps_io_gpio28
set_instance_assignment -name IO_STANDARD "1.8 V" -to hps_io_gpio28
set_location_assignment PIN_T124 -to hps_io_gpio34
set_instance_assignment -name IO_STANDARD "1.8 V" -to hps_io_gpio34
set_location_assignment PIN_P124 -to hps_io_gpio35
set_instance_assignment -name IO_STANDARD "1.8 V" -to hps_io_gpio35

# Fabric LPDDR4 (memory under test). The controller sets the I/O standard of
# the memory pins; only the reference clock needs one here.
set_location_assignment PIN_BW78 -to lpddr4b_ref_clk
set_instance_assignment -name IO_STANDARD "1.1V True Differential Signaling" -to lpddr4b_ref_clk
set_instance_assignment -name INPUT_TERMINATION DIFFERENTIAL -to lpddr4b_ref_clk
set_location_assignment PIN_BH89 -to lpddr4b_oct_rzqin
set_location_assignment PIN_BM81 -to mem_0_ck_t
set_location_assignment PIN_BP81 -to mem_0_ck_c
set_location_assignment PIN_BH92 -to mem_0_reset_n
set_location_assignment PIN_BR78 -to mem_0_cs
set_location_assignment PIN_BR81 -to mem_0_cke
set_location_assignment PIN_BR89 -to mem_0_ca[0]
set_location_assignment PIN_BU89 -to mem_0_ca[1]
set_location_assignment PIN_BR92 -to mem_0_ca[2]
set_location_assignment PIN_BU92 -to mem_0_ca[3]
set_location_assignment PIN_BW89 -to mem_0_ca[4]
set_location_assignment PIN_CA89 -to mem_0_ca[5]
set_location_assignment PIN_CL91 -to mem_0_dq[0]
set_location_assignment PIN_CK94 -to mem_0_dq[1]
set_location_assignment PIN_CK97 -to mem_0_dq[2]
set_location_assignment PIN_CL97 -to mem_0_dq[3]
set_location_assignment PIN_CK80 -to mem_0_dq[4]
set_location_assignment PIN_CL82 -to mem_0_dq[5]
set_location_assignment PIN_CK76 -to mem_0_dq[6]
set_location_assignment PIN_CL76 -to mem_0_dq[7]
set_location_assignment PIN_CC92 -to mem_0_dq[8]
set_location_assignment PIN_CA92 -to mem_0_dq[9]
set_location_assignment PIN_CF92 -to mem_0_dq[10]
set_location_assignment PIN_CH92 -to mem_0_dq[11]
set_location_assignment PIN_CA81 -to mem_0_dq[12]
set_location_assignment PIN_CC81 -to mem_0_dq[13]
set_location_assignment PIN_CH78 -to mem_0_dq[14]
set_location_assignment PIN_CF78 -to mem_0_dq[15]
set_location_assignment PIN_BR69 -to mem_0_dq[16]
set_location_assignment PIN_BU69 -to mem_0_dq[17]
set_location_assignment PIN_BR71 -to mem_0_dq[18]
set_location_assignment PIN_BU71 -to mem_0_dq[19]
set_location_assignment PIN_BU59 -to mem_0_dq[20]
set_location_assignment PIN_BR59 -to mem_0_dq[21]
set_location_assignment PIN_BW59 -to mem_0_dq[22]
set_location_assignment PIN_CA59 -to mem_0_dq[23]
set_location_assignment PIN_CF71 -to mem_0_dq[24]
set_location_assignment PIN_CH71 -to mem_0_dq[25]
set_location_assignment PIN_CC71 -to mem_0_dq[26]
set_location_assignment PIN_CA71 -to mem_0_dq[27]
set_location_assignment PIN_CF62 -to mem_0_dq[28]
set_location_assignment PIN_CH62 -to mem_0_dq[29]
set_location_assignment PIN_CF59 -to mem_0_dq[30]
set_location_assignment PIN_CH59 -to mem_0_dq[31]
set_location_assignment PIN_CL88 -to mem_0_dqs_t[0]
set_location_assignment PIN_CK88 -to mem_0_dqs_c[0]
set_location_assignment PIN_CH89 -to mem_0_dqs_t[1]
set_location_assignment PIN_CF89 -to mem_0_dqs_c[1]
set_location_assignment PIN_BW69 -to mem_0_dqs_t[2]
set_location_assignment PIN_CA69 -to mem_0_dqs_c[2]
set_location_assignment PIN_CH69 -to mem_0_dqs_t[3]
set_location_assignment PIN_CF69 -to mem_0_dqs_c[3]
set_location_assignment PIN_CK85 -to mem_0_dmi[0]
set_location_assignment PIN_CF81 -to mem_0_dmi[1]
set_location_assignment PIN_BU62 -to mem_0_dmi[2]
set_location_assignment PIN_CA62 -to mem_0_dmi[3]

export_assignments
