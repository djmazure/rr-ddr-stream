# ps7_unused_off.tcl — disable PS7 features the zybo-z7-20 board preset enables
# but this example does not use, so BD HDL generation doesn't fail on dangling
# pins.
#
# RTL-P2.618 ps7_hook: listed under hardware.ps7_hooks in project.yml, applied
# AFTER routertl/boards/zybo-z7-20/ps7_preset.tcl has populated the PS7 cell.
#
# The canonical zybo-z7-20 preset is StereoVision-derived and enables
# S_AXI_HP2 (second-camera baseline) plus the PL->PS fabric interrupt. The DDR
# validator uses only HP0 (the IP's DMA slave) + M_AXI_GP0 (the CSR) and has no
# PL->PS interrupts, so left enabled they break / dirty the build:
#   - S_AXI_HP2 on  => S_AXI_HP2_ACLK clock pin dangles =>
#       ERROR [BD 41-758] Hdl Generation failed (observed 2026-05-26).
#   - IRQ_F2P  on   => unconnected PS7 input + validate noise.
# Same remedy as deepskopion's ds_hp2_off.tcl / ds_irq_f2p_off.tcl hooks.

set_property -dict [list \
    CONFIG.PCW_USE_S_AXI_HP2        {0} \
    CONFIG.PCW_USE_FABRIC_INTERRUPT {0} \
    CONFIG.PCW_IRQ_F2P_INTR         {0} \
] [get_bd_cells processing_system7_0]
