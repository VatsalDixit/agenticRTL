library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library work;
use work.vhsnunzip_dsw4_pkg.all;

-- DSW-4 window assembly and hop tables PT1-PT3 (SPEC 3.2 WA, 3.3; build
-- step B3). Feed-forward, one window per cycle, no stall input (the block
-- reader's window credit throttles it).
--
--   WA   2-block shift register s0, s1 (blocks b, b+1). When block b+2 (blk)
--        enters and s0, s1 hold blocks, window(b) is formed: x = s0 & s1 &
--        blk(0..3); endrel = 16k + nvalid of the first of the three blocks
--        with nvalid < 16, else 63; bytes at positions >= endrel are 0;
--        first / epoch / base are block b's. Every block entering after the
--        first two completes one window (the block reader counts the same).
--   PT1  per position i = 0..31 (bytes x[i..i+2]): END if i >= endrel
--        (nxt = i, absorbing); else hdr_decode; nxt = i + 1 + slen (near
--        literal, <= 92), 127 (FAR literal), i + hdr (copy). vlen = 1 + number
--        of leading x[0..3] with bit 7 set.
--   PT2  nxt2[i] = nxt[i] < 32 ? nxt[nxt[i]] : nxt[i]          (i = 0..31)
--   PT3  nxt3[i] = nxt2[i] < 32 ? nxt[nxt2[i]] : nxt2[i]        (i = 0..15)
--        nxt4[i] = nxt2[i] < 32 ? nxt2[nxt2[i]] : nxt2[i]
--
--   blk   block stream from vhsnunzip_blkrd (blk.valid = a block this cycle).
--   win   window(b), registered at PT3, 3 cycles after block b+2 is on blk
--         (win.valid = a window this cycle), to the WFIFO.
entity vhsnunzip_pt is
  port (
    clk         : in  std_logic;
    reset       : in  std_logic;

    blk         : in  blk_t;
    win         : out win_t
  );
end vhsnunzip_pt;

architecture behavior of vhsnunzip_pt is

  signal s0, s1   : blk_t := BLK_INIT;
  signal w1, w2, w3 : win_t := WIN_INIT;

begin

  seq_proc: process (clk) is
    variable x      : byte_array(0 to 35);
    variable endrel : unsigned(5 downto 0);
    variable pr     : prec_t;
    variable nx     : u7_arr(0 to 31);
    variable n2     : u7_arr(0 to 31);
    variable w      : win_t;
    variable v      : unsigned(2 downto 0);
    variable t      : unsigned(6 downto 0);
  begin
    if rising_edge(clk) then
      ---------------------------------------------------------------- WA + PT1
      if blk.valid = '1' then
        s0 <= s1;
        s1 <= blk;
      end if;

      for i in 0 to 15 loop
        x(i)      := s0.data(i);
        x(16 + i) := s1.data(i);
      end loop;
      for i in 0 to 3 loop
        x(32 + i) := blk.data(i);
      end loop;
      if s0.nvalid(4) = '0' then
        endrel := resize(s0.nvalid(3 downto 0), 6);
      elsif s1.nvalid(4) = '0' then
        endrel := "01" & s1.nvalid(3 downto 0);
      elsif blk.nvalid(4) = '0' then
        endrel := "10" & blk.nvalid(3 downto 0);
      else
        endrel := to_unsigned(63, 6);
      end if;
      for i in 0 to 35 loop
        if to_unsigned(i, 6) >= endrel then
          x(i) := X"00";
        end if;
      end loop;

      w := WIN_INIT;
      w.valid  := blk.valid and s0.valid and s1.valid;
      w.first  := s0.first;
      w.epoch  := s0.epoch;
      w.base   := s0.base;
      w.x      := x;
      w.endrel := endrel;
      if x(0)(7) = '0' then    v := "001";
      elsif x(1)(7) = '0' then v := "010";
      elsif x(2)(7) = '0' then v := "011";
      elsif x(3)(7) = '0' then v := "100";
      else                     v := "101";
      end if;
      w.vlen := v;
      for i in 0 to 31 loop
        pr := hdr_decode(x(i to i + 2));
        if to_unsigned(i, 6) >= endrel then
          w.rec(i) := PREC_INIT;              -- kind END
          w.nxt(i) := to_unsigned(i, 7);
        else
          w.rec(i) := pr;
          if pr.kind = K_LIT then
            if pr.far = '1' then
              w.nxt(i) := to_unsigned(FAR_NXT, 7);
            else
              w.nxt(i) := to_unsigned(i + 1, 7) + pr.len;
            end if;
          else
            w.nxt(i) := to_unsigned(i, 7) + pr.hdr;
          end if;
        end if;
      end loop;
      w1 <= w;

      ---------------------------------------------------------------- PT2
      w := w1;
      nx := w1.nxt;
      for i in 0 to 31 loop
        if nx(i)(6 downto 5) = "00" then
          w.nxt2(i) := nx(to_integer(nx(i)(4 downto 0)));
        else
          w.nxt2(i) := nx(i);
        end if;
      end loop;
      w2 <= w;

      ---------------------------------------------------------------- PT3
      w := w2;
      nx := w2.nxt;
      n2 := w2.nxt2;
      for i in 0 to 15 loop
        t := n2(i);
        if t(6 downto 5) = "00" then
          w.nxt3(i) := nx(to_integer(t(4 downto 0)));
          w.nxt4(i) := n2(to_integer(t(4 downto 0)));
        else
          w.nxt3(i) := t;
          w.nxt4(i) := t;
        end if;
      end loop;
      w3 <= w;

      if reset = '1' then
        s0.valid <= '0';
        s1.valid <= '0';
        w1.valid <= '0';
        w2.valid <= '0';
        w3.valid <= '0';
      end if;
    end if;
  end process;

  win <= w3;

end behavior;
