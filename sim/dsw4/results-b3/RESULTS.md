# DSW-4 at commit 293c2e4: measured result

Real Parquet draws (the hacc-real200 scoring corpus), plain perf testbench, every
output byte checked by the frozen oracle; Vivado 2024.2 on hacc-build-02,
xcu55c-fsvh2892-2L-e, 4.0 ns constraint, OOC synth + place + route.

| table | B/cycle | i35 B/cycle | GB/s at 251.8 MHz |
|---|---|---|---|
| taxi (train) | 19.37 | 8.68 | 4.88 |
| lineitem | 21.25 | 9.38 | 5.35 |
| orders | 19.05 | 9.78 | 4.80 |
| customer | 20.61 | 9.72 | 5.19 |
| part | 18.79 | 10.08 | 4.73 |
| partsupp | 25.99 | 10.86 | 6.54 |
| supplier | 19.93 | 9.64 | 5.02 |
| **geomean** | **20.60** | 9.72 | **5.187** |

- f_max 251.8 MHz (WNS +0.029 ns at 4.0 ns, 0 failing endpoints); 29,050 LUTs,
  27,946 registers, 32 URAM, 0 BRAM/DSP.
- Throughput 5.187 GB/s = +228.7% over the original design (1.578 GB/s) and
  +88.1% over i35 (2.758 GB/s). Goal (+200%, 4.73 GB/s) met.
- nation, region, synth-512B and synth-8192B pass (unscored).
- Worst path: parse_inst.pt_inst.w3[valid] -> wf_mem CE, 1 LUT, 95% routing
  (fan-out); the constraint was met, so Vivado stopped optimising at 250 MHz.
