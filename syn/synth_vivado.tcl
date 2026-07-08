# Vivado batch synthesis (+ optional P&R) for vhsnunzip, emitting accurate
# timing + utilization for the real xcvu5p part.
#
# Usage:
#   vivado -mode batch -nolog -nojournal -source syn/synth_vivado.tcl \
#          -tclargs [TOP] [MODE]
#     TOP  : top entity            (default vhsnunzip_unbuffered)
#     MODE : full  = synth+opt+place+route -> accurate WNS/Fmax  [default]
#            fast  = synth only    -> quick, rough estimate
#
# Uses the primitive RAM (vhsnunzip_ram.syn.vhd, unisim/unimacro); the behavioral
# vhsnunzip_ram.sim.vhd is excluded. Machine-readable results are printed as
# "METRIC <key> <value>" lines and full reports written under syn/vivado_build/.
set top  [lindex $argv 0]
set mode [lindex $argv 1]
if {$top eq ""}  { set top vhsnunzip_unbuffered }
if {$mode eq ""} { set mode full }

# Part + RAM style are overridable via env so the target can change without
# editing this file. Default is a Kintex-7 (7-series is what this Vivado has
# device support for; UltraScale+/vu5p is not installed). 7-series has no
# UltraRAM, so use the design's RAM_STYLE=block (BRAM) mode.
#   For the original vu5p reference:  VIVADO_PART=xcvu5p-flva2104-2-i
#                                     VIVADO_RAMSTYLE=ultra  (needs US+ installed)
set part     [expr {[info exists ::env(VIVADO_PART)]     ? $::env(VIVADO_PART)     : "xc7k160tfbg484-2"}]
set ramstyle [expr {[info exists ::env(VIVADO_RAMSTYLE)] ? $::env(VIVADO_RAMSTYLE) : "block"}]
set period 4.0

set outdir syn/vivado_build/$top
file mkdir $outdir

# RTL in dependency order; synthesis RAM variant; no testbenches, no .sim files.
set files [list \
  rtl/vhsnunzip_utils_pkg.vhd \
  rtl/vhsnunzip_pkg.vhd \
  rtl/vhsnunzip_int_pkg.vhd \
  rtl/vhsnunzip_ram.syn.vhd \
  rtl/vhsnunzip_srl.vhd \
  rtl/vhsnunzip_fifo.vhd \
  rtl/vhsnunzip_pre_decoder.vhd \
  rtl/vhsnunzip_decoder.vhd \
  rtl/vhsnunzip_decoder_long.vhd \
  rtl/vhsnunzip_cmd_gen_1.vhd \
  rtl/vhsnunzip_cmd_gen_2.vhd \
  rtl/vhsnunzip_pipeline.vhd \
  rtl/vhsnunzip_unbuffered.vhd]
read_vhdl -vhdl2008 $files
read_xdc syn/constraints.xdc

synth_design -top $top -part $part -generic RAM_STYLE=$ramstyle
if {$mode eq "full"} {
  opt_design
  place_design
  route_design
}

report_timing_summary -file $outdir/timing.log -delay_type max -max_paths 10
report_utilization    -file $outdir/utilization.log

# ---- Machine-readable metrics ----
set paths [get_timing_paths -setup -max_paths 1 -nworst 1]
set wns 0.0
if {[llength $paths] > 0} { set wns [get_property SLACK [lindex $paths 0]] }
set fmax [expr {1000.0 / ($period - $wns)}]

proc ncells {pat} { return [llength [get_cells -hier -filter "REF_NAME =~ $pat"]] }

puts "METRIC top $top"
puts "METRIC mode $mode"
puts "METRIC part $part"
puts "METRIC ramstyle $ramstyle"
puts "METRIC period_ns $period"
puts "METRIC wns_ns $wns"
puts [format "METRIC fmax_mhz %.2f" $fmax]
puts "METRIC luts [ncells LUT*]"
puts "METRIC lutmem [expr {[ncells SRL*] + [ncells RAMD*] + [ncells RAMS*]}]"
puts "METRIC ffs [ncells FD*]"
puts "METRIC carry [ncells CARRY*]"
puts "METRIC bram36 [ncells RAMB36*]"
puts "METRIC bram18 [ncells RAMB18*]"
puts "METRIC uram [ncells URAM*]"
puts "METRIC dsp [ncells DSP*]"
puts "METRIC_DONE"
