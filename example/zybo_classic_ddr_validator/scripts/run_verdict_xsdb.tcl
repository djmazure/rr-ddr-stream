# run_verdict_xsdb.tcl — in-fabric round-trip verdict for the Classic Zybo
# (xc7z010) ddr_stream_axi validator. Drives the BIST via the AXI-Lite CSR
# @ 0x4000_0000 over the PS M_AXI_GP0 master (reachable from XSDB through the
# APU after ps7_init) and reads STATUS / ERROR_COUNT / WORDS_* / FIRST_ERR_*.
#
# Prereq: the .bit is already loaded on the board cable (rr program run
#         --cable <serial>). Run:
#           RR_JTAG_SERIAL=<serial> xsdb scripts/run_verdict_xsdb.tcl
#         (or pass the serial as the first script argument). The serial is
#         REQUIRED: it scopes every target selection to that board's cable.
#         `rr hardware doctor` prints it.
#
# NOTE: on Vivado-2024.1 xsdb, `mrd -value` returns a DECIMAL string (not hex);
# proc rd trims it and returns the integer as-is. STATUS bit map: [1]done
# [2]pass [3]fail [4]wr_idle [5]rd_idle. A clean run ends STATUS=0x36.
#
# RESULT 2026-06-22 on board (xc7z010clg400-1), FCLK1=100 MHz:
#   IDENT=0x0DD50200; scenarios 0 (roundtrip N=256..8192) and 1 (boundary
#   N=64K/256K) all PASS, ERR=0, FIRST_ERR_INDEX=0xFFFFFFFF, full traffic both
#   ways. N=0 and scenario-3 stub correctly report done+SKIP (pass=0,fail=0),
#   proving the pass latch is traffic-gated, not stuck. => the IDP-T2.5 Classic
#   DDR corruption is SPECIFIC TO THE PS-READBACK PATH, not the IP write->DDR
#   datapath. WNS=+0.811ns, 25.2% LUTs.

set CTRL 0x40000000; set STATUS 0x40000004; set SCEN 0x40000008; set NWORDS 0x4000000C
set ERRCNT 0x40000010; set WWR 0x40000014; set WRD 0x40000018
set FEIDX 0x4000001C; set FEEXP 0x40000020; set FEGOT 0x40000024; set IDENT 0x4000003C
set SCRIPT_DIR [file dirname [file normalize [info script]]]
set PS7INIT "$SCRIPT_DIR/../project/rr_ddr_stream_zybo_classic_ddr_validator.gen/sources_1/bd/ddr_validator_bd/ip/ddr_validator_bd_processing_system7_0_0/ps7_init.tcl"
# Board selection: refuse rather than pick the first Cortex-A9 on the chain.
set SERIAL ""
if {[info exists ::env(RR_JTAG_SERIAL)]} { set SERIAL $::env(RR_JTAG_SERIAL) }
if {$SERIAL eq "" && [info exists ::argv] && [llength $::argv] > 0} { set SERIAL [lindex $::argv 0] }
if {$SERIAL eq ""} {
  puts stderr "ERROR: JTAG cable serial not set. Export RR_JTAG_SERIAL=<serial> (or pass it as the first argument); `rr hardware doctor` prints it."
  exit 1
}
connect
targets -set -nocase -filter "name =~ \"*Cortex-A9*#0*\" && jtag_cable_name =~ \"*$SERIAL*\""
rst -processor; after 200
source $PS7INIT; ps7_init; ps7_post_config; after 300
targets -set -nocase -filter "name =~ \"*Cortex-A9*#0*\" && jtag_cable_name =~ \"*$SERIAL*\""

# rd: return the 32-bit value at addr. On this xsdb, `mrd -value` returns a
# DECIMAL string already (e.g. "232063488" for 0x0DD50200), so just trim+return.
proc rd {addr} {
  return [string trim [mrd -value -force $addr]]
}

set id [rd $IDENT]
puts [format "IDENT          = 0x%08X  (expect 0x0DD50200)" $id]

proc run_scn {scn n name} {
  global CTRL STATUS SCEN NWORDS ERRCNT WWR WRD FEIDX FEEXP FEGOT
  mwr -force $NWORDS $n
  mwr -force $SCEN   $scn
  after 10
  mwr -force $CTRL 0x1
  set done 0
  for {set i 0} {$i < 600} {incr i} {
    after 20
    if {[expr ([rd $STATUS] >> 1) & 1]} { set done 1; break }
  }
  set st [rd $STATUS]; set ec [rd $ERRCNT]
  set ww [rd $WWR];    set wr [rd $WRD]
  set fi [rd $FEIDX];  set fe [rd $FEEXP]; set fg [rd $FEGOT]
  set pass [expr ($st >> 2) & 1]
  set fail [expr ($st >> 3) & 1]
  set v "INDETERMINATE"
  if {$pass && !$fail} { set v "PASS" }
  if {$fail}          { set v "FAIL" }
  puts [format "SCN %d %-22s N=%-7d done=%d STATUS=0x%02X ERR=%d WW=%d WR=%d FEIDX=0x%08X EXP=0x%08X GOT=0x%08X => %s" \
        $scn $name $n $done $st $ec $ww $wr $fi $fe $fg $v]
}

run_scn 0 8192    "roundtrip"
run_scn 0 8192    "roundtrip-repeat"
run_scn 1 65536   "boundary-64K"
run_scn 1 262144  "boundary-256K"
run_scn 0 256     "roundtrip-small"
disconnect
