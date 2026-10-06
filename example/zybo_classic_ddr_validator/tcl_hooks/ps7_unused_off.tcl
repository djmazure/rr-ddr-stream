# ps7_unused_off.tcl — disable PS7 features the `zybo` (Classic) board preset
# enables but this example does not use, so BD HDL generation / validation
# doesn't fail or warn on dangling pins.
#
# RTL-P2.618 ps7_hook: listed under hardware.ps7_hooks in project.yml, applied
# AFTER routertl/boards/zybo/ps7_preset.tcl has populated the PS7 cell.
#
# The Classic `zybo` preset (StereoVision-derived, like the z020 one) leaves
# the PL->PS fabric interrupt + IRQ_F2P enabled. The DDR validator uses only
# HP0 (the IP's DMA slave, enabled in gen_bd.tcl) + M_AXI_GP0 (the CSR, also
# enabled in gen_bd.tcl) and has NO PL->PS interrupts, so left enabled the
# IRQ_F2P input dangles → validate noise / a stray unconnected PS7 input.
# Same remedy as the z020 sibling's S_AXI_HP2 cleanup and deepskopion's
# ds_irq_f2p_off.tcl.
#
# Note: unlike the z020 preset, the Classic preset ships S_AXI_HP1/2/3 already
# OFF, so no HP2 cleanup is needed here (gen_bd.tcl turns HP0 ON).

set_property -dict [list \
    CONFIG.PCW_USE_FABRIC_INTERRUPT {0} \
    CONFIG.PCW_IRQ_F2P_INTR         {0} \
] [get_bd_cells processing_system7_0]
