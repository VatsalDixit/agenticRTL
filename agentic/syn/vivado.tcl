# Vivado synthesis, place and route for one candidate design.
#
# Run on the HACC build host by agentic/hacc.py, from a scratch directory
# holding rtl/ (the candidate's synthesisable VHDL plus the kit's own
# syn/ram_xilinx.vhd) and this file:
#
#   vivado -mode batch -source vivado.tcl -tclargs TOP PART PERIOD_NS
#
# Leaves timing.log, utilization.log and critical_paths.log in reports/.
# Frozen (agentic/freeze.py): this decides what f_max and area a candidate is
# credited with.

lassign $argv top part period_ns

create_project -force cand ./cand -part $part
set_property target_language VHDL [current_project]

# VHDL-2008 throughout, because that is the language the simulation (GHDL
# --std=08) verified. A candidate that only compiles as 2008 must be
# synthesised as 2008 too, or the two tools are judging different designs.
set files [lsort [glob rtl/*.vhd]]
add_files -norecurse $files
set_property file_type {VHDL 2008} [get_files $files]
set_property top $top [get_filesets sources_1]

set xdc [open clock.xdc w]
puts $xdc "create_clock -period $period_ns -name clk \[get_ports clk\]"
close $xdc
add_files -fileset constrs_1 -norecurse clock.xdc

# Out of context: this block decompresses a stream for other logic on the same
# die, and its buses never go near a package pin. In the default mode every
# port bit needs one, and a widened candidate in an earlier campaign ran out
# of them and died in placement with the design otherwise sound. Measured then,
# out of context cost -1.9% f_max and -2 LUTs against top-down.
synth_design -top $top -part $part -mode out_of_context
opt_design
place_design
route_design

file mkdir reports
report_timing_summary -file reports/timing.log
report_utilization -file reports/utilization.log
report_timing -max_paths 10 -file reports/critical_paths.log
