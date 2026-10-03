library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library work;
use work.vhsnunzip_dsw4_pkg.all;

-- DSW-4 block reader (SPEC 3.2, block reader part; build step B3).
--
-- Issues one 16-byte block per cycle from CBUF port C (RUN / PAD / NEXT,
-- retarget with epoch). PAD emits PAD_BLOCKS (= 3, see the package) blocks
-- with nvalid = 0. Window assembly is in vhsnunzip_pt (SPEC 7 table).
--
--   gb                 GA of the next CBUF beat.
--   csf_* / cef_*      chunk start / end FIFO heads from vhsnunzip_cbuf.
--   retgt_v/retgt_tgt  walker retarget: bk := tgt/16, epoch += 1, mode RUN.
--   wf_ok              the WFIFO has room for the window this block would
--                      complete (free >= windows in flight in PT1..PT3 + 1);
--                      computed where the WFIFO lives (vhsnunzip_core).
--   rowC / lanesC      CBUF port C: all 32 rows = bk(5:1) (registered here);
--                      the block is lanes 16*bk(0) .. 16*bk(0)+15.
--   blk                the block (registered), to vhsnunzip_pt.
entity vhsnunzip_blkrd is
  port (
    clk         : in  std_logic;
    reset       : in  std_logic;

    gb          : in  ga_t;
    csf_valid   : in  std_logic;
    csf_ga      : in  ga_t;
    csf_pop     : out std_logic;
    cef_valid   : in  std_logic;
    cef_ga      : in  ga_t;
    cef_pop     : out std_logic;

    retgt_v     : in  std_logic;
    retgt_tgt   : in  ga_t;
    wf_ok       : in  std_logic;

    rowC        : out u5_arr(0 to 31);
    lanesC      : in  byte_array(0 to 31);

    blk         : out blk_t
  );
end vhsnunzip_blkrd;

-- Interface stub: replace with the implementation.
architecture stub of vhsnunzip_blkrd is
begin
  csf_pop <= '0';
  cef_pop <= '0';
  rowC    <= (others => (others => '0'));
  blk     <= BLK_INIT;
end stub;
