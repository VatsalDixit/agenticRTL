library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library work;
use work.vhsnunzip_dsw4_pkg.all;

-- DSW-4 window assembly and hop tables PT1-PT3 (SPEC 3.2 WA, 3.3; build
-- step B3). Feed-forward, one window per cycle, no stall input (the WFIFO
-- credit throttles the block reader).
--
--   blk   block stream from vhsnunzip_blkrd (blk.valid = a block this cycle).
--   win   window(b), emitted 3 stages after PT1 when block b+2 has entered
--         WA (win.valid = a window this cycle), to the WFIFO.
entity vhsnunzip_pt is
  port (
    clk         : in  std_logic;
    reset       : in  std_logic;

    blk         : in  blk_t;
    win         : out win_t
  );
end vhsnunzip_pt;

-- Interface stub: replace with the implementation.
architecture stub of vhsnunzip_pt is
begin
  win <= WIN_INIT;
end stub;
