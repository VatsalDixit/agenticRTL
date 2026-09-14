library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library work;
use work.vhsnunzip_int_pkg.all;

-- Stand-in for the vhsnunzip history RAM, for the open-source synthesis flow
-- only. The real one instantiates Xilinx URAM/BRAM primitives (Vivado only);
-- the simulation one is behavioural and would become hundreds of thousands of
-- flip-flops. This keeps the ports and one register stage so paths are cut at
-- a register boundary, the same place the real memory registers them.
--
-- If a candidate changes the ram_command/ram_response records this still
-- compiles as long as the field names below survive. If it renames or replaces
-- the RAM entity, synthesis reports the failure and the candidate is dropped.
entity vhsnunzip_ram is
  generic (
    RAM_STYLE   : string := "ultra"
  );
  port (
    clk         : in  std_logic;
    reset       : in  std_logic;
    a_cmd       : in  ram_command;
    a_resp      : out ram_response;
    b_cmd       : in  ram_command;
    b_resp      : out ram_response
  );
end vhsnunzip_ram;

architecture blackbox of vhsnunzip_ram is
begin
  reg_proc: process (clk) is
  begin
    if rising_edge(clk) then
      a_resp.valid      <= a_cmd.valid and not a_cmd.wren;
      a_resp.valid_next <= a_cmd.valid and not a_cmd.wren;
      a_resp.rdat       <= a_cmd.wdat;
      a_resp.rctrl      <= a_cmd.wctrl;
      b_resp.valid      <= b_cmd.valid and not b_cmd.wren;
      b_resp.valid_next <= b_cmd.valid and not b_cmd.wren;
      b_resp.rdat       <= b_cmd.wdat;
      b_resp.rctrl      <= b_cmd.wctrl;
    end if;
  end process;
end blackbox;
