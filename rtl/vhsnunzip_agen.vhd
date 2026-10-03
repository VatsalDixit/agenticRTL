library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library work;
use work.vhsnunzip_dsw4_pkg.all;

-- DSW-4 address generation AG1-AG2 (SPEC 3.8).
--
-- Two register stages, never stalls:
--   AG1 (cycle C-1): W loop (W := last ? 0 : W + d_total, 18 b), the command
--       start Wc, and per slot k: dest_k = Wc + S_k, D_k = q_k - S_k - 1
--       (signed 18 b, >= 0 by SPEC 5.1), the literal rotation
--       (ck - dest_k) mod 32 and ck(9:0).
--   AG2 (cycle C): src_k = dest_k - q_k, ST/LT tier from D_k, rot/smod,
--       Dhi/Dlo (ST), L0 parity and adev/adod (LT), the REP fields, the CBUF
--       row/lane bases per literal port; registers agcmd_t and presents the
--       URAM reads.
--
--   cmd     writer command (registered in vhsnunzip_writer, cycle C-2).
--           Per slot: val, kind (LIT/CPY/EOC), port_b, s, q, lptr are used
--           (n is implied by the S of the next slot / d_total). An unused
--           slot (val = 0) gets S = d_total whatever its s field holds.
--   ag      AG2 output (cycle C) -> vhsnunzip_dpath D1. Fields that do not
--           apply to a slot's tier are 0 (agcmd.txt of gold.py, field for
--           field).
--   ram_rd  port-b commands of the 32 history RAMs (registered, cycle C).
--           Instance index = m*8 + par*4 + w (mirror m = slot, par = line
--           parity, w = 8-byte word). For an LT slot m: valid = 1, wren = 0,
--           even-parity instances addr = adev_m, odd-parity addr = adod_m.
--           All other instances: valid = 0. Data returns at C+3.
--
-- TEST_ST_LINES (12..32, sim only): ST tier iff D <= 32*TEST_ST_LINES - 1.
entity vhsnunzip_agen is
  generic (
    TEST_ST_LINES : natural := ST_LINES
  );
  port (
    clk         : in  std_logic;
    reset       : in  std_logic;

    cmd         : in  element_stream;

    ag          : out agcmd_t;
    ram_rd      : out ram_command_array(0 to 31)
  );
end vhsnunzip_agen;

architecture rtl of vhsnunzip_agen is

  constant ST_MAX : natural := 32 * TEST_ST_LINES - 1;

  -- AG1 register stage, per slot.
  type ag1_slot_t is record
    used      : std_logic;                     -- val
    kind      : std_logic_vector(1 downto 0);  -- K_LIT / K_CPY / K_EOC
    port_b    : std_logic;
    q         : unsigned(15 downto 0);
    dest      : unsigned(17 downto 0);         -- Wc + S_k
    d         : signed(17 downto 0);           -- q_k - S_k - 1
    lrot      : unsigned(4 downto 0);          -- (ck - dest_k) mod 32
    ck        : unsigned(9 downto 0);          -- lptr(9:0)
  end record;
  type ag1_slot_arr is array (0 to 3) of ag1_slot_t;

  type ag1_t is record
    valid     : std_logic;
    last      : std_logic;
    rep       : std_logic;
    wc        : unsigned(17 downto 0);
    d_total   : unsigned(5 downto 0);
    s         : u6_arr(1 to 3);
    lw        : ga_t;
    slot      : ag1_slot_arr;
  end record;

  constant AG1_SLOT_INIT : ag1_slot_t := (
    used => '0', kind => K_EOC, port_b => '0', q => (others => '0'),
    dest => (others => '0'), d => (others => '0'), lrot => (others => '0'),
    ck => (others => '0'));

  constant AG1_INIT : ag1_t := (
    valid => '0', last => '0', rep => '0', wc => (others => '0'),
    d_total => (others => '0'), s => (others => (others => '0')),
    lw => (others => '0'), slot => (others => AG1_SLOT_INIT));

  constant RAM_IDLE : ram_command := (
    valid => '0', addr => (others => '0'), wren => '0',
    wdat => (others => X"00"), wctrl => (others => '0'));

  signal w_r   : unsigned(17 downto 0) := (others => '0');
  signal a1    : ag1_t := AG1_INIT;
  signal a2    : agcmd_t := AGCMD_INIT;
  signal rd_r  : ram_command_array(0 to 31) := (others => RAM_IDLE);

begin

  -----------------------------------------------------------------------------
  -- AG1: W loop, dest, D, literal rotation.
  -----------------------------------------------------------------------------
  ag1_proc: process (clk) is
    variable v    : ag1_t;
    variable s_k  : unsigned(5 downto 0);
  begin
    if rising_edge(clk) then
      v := AG1_INIT;
      v.valid   := cmd.valid;
      v.last    := cmd.last;
      v.rep     := cmd.rep;
      v.wc      := w_r;
      v.d_total := cmd.d_total;
      v.lw      := cmd.lw;
      for k in 0 to 3 loop
        if k = 0 then
          s_k := (others => '0');
        elsif cmd.slot(k).val = '1' then
          s_k := cmd.slot(k).s;
        else
          s_k := cmd.d_total;
        end if;
        if k > 0 then
          v.s(k) := s_k;
        end if;
        v.slot(k).used   := cmd.slot(k).val;
        v.slot(k).kind   := cmd.slot(k).kind;
        v.slot(k).port_b := cmd.slot(k).port_b;
        v.slot(k).q      := cmd.slot(k).q;
        v.slot(k).dest   := w_r + resize(s_k, 18);
        v.slot(k).d      := signed(resize(cmd.slot(k).q, 18))
                            - signed(resize(s_k, 18)) - 1;
        v.slot(k).lrot   := cmd.slot(k).lptr(4 downto 0) - w_r(4 downto 0)
                            - s_k(4 downto 0);
        v.slot(k).ck     := cmd.slot(k).lptr(9 downto 0);
      end loop;
      a1 <= v;

      if cmd.valid = '1' then
        if cmd.last = '1' then
          w_r <= (others => '0');
        else
          w_r <= w_r + resize(cmd.d_total, 18);
        end if;
      end if;

      if reset = '1' then
        a1.valid <= '0';
        w_r      <= (others => '0');
      end if;
    end if;
  end process;

  -----------------------------------------------------------------------------
  -- AG2: tier, src, URAM rows, CBUF bases; registered agcmd + URAM reads.
  -----------------------------------------------------------------------------
  ag2_proc: process (clk) is
    variable v    : agcmd_t;
    variable rd   : ram_command_array(0 to 31);
    variable src  : unsigned(17 downto 0);
    variable l0   : unsigned(12 downto 0);
    variable adev : unsigned(11 downto 0);
    variable adod : unsigned(11 downto 0);
    variable du   : unsigned(17 downto 0);
    variable sl   : ag1_slot_t;
  begin
    if rising_edge(clk) then
      v := AGCMD_INIT;
      rd := (others => RAM_IDLE);
      v.valid   := a1.valid;
      v.last    := a1.last;
      v.rep     := a1.rep;
      v.wl      := a1.wc(4 downto 0);
      v.d_total := a1.d_total;
      v.s1      := a1.s(1);
      v.s2      := a1.s(2);
      v.s3      := a1.s(3);
      v.lw      := a1.lw;
      for k in 0 to 3 loop
        sl  := a1.slot(k);
        src := sl.dest - resize(sl.q, 18);
        du  := unsigned(sl.d);
        if sl.used = '1' and sl.kind = K_CPY then
          v.rot(k)  := (others => '0');
          v.rot(k)  := v.rot(k) - sl.q(4 downto 0);     -- (-q) mod 32
          v.smod(k) := src(4 downto 0);
          if k = 0 and a1.rep = '1' then
            v.rep_q    := sl.q(4 downto 0);
            v.rep_base := src(4 downto 0);              -- dest_0 = Wc
          end if;
          if du(17 downto 10) = 0 and to_integer(du(9 downto 0)) <= ST_MAX then
            v.tier(k) := T_ST;
            v.dhi(k)  := du(9 downto 5);
            v.dlo(k)  := du(4 downto 0);
          else
            v.tier(k) := T_LT;
            l0   := src(17 downto 5);
            adod := l0(12 downto 1);
            if l0(0) = '1' then
              adev := l0(12 downto 1) + 1;
            else
              adev := l0(12 downto 1);
            end if;
            v.l0p(k) := l0(0);
            for w in 0 to 3 loop
              rd(k*8 + w).valid     := a1.valid;
              rd(k*8 + w).addr      := adev;
              rd(k*8 + 4 + w).valid := a1.valid;
              rd(k*8 + 4 + w).addr  := adod;
            end loop;
          end if;
        elsif sl.used = '1' and sl.kind = K_LIT then
          v.tier(k) := T_LIT;
          v.rot(k)  := sl.lrot;
          v.litb(k) := sl.port_b;
          if sl.port_b = '1' then
            v.crowB := sl.ck(9 downto 5);
            v.cmodB := sl.ck(4 downto 0);
          else
            v.crowA := sl.ck(9 downto 5);
            v.cmodA := sl.ck(4 downto 0);
          end if;
        end if;
      end loop;
      a2   <= v;
      rd_r <= rd;

      if reset = '1' then
        a2.valid <= '0';
        for i in 0 to 31 loop
          rd_r(i).valid <= '0';
        end loop;
      end if;
    end if;
  end process;

  ag     <= a2;
  ram_rd <= rd_r;

  -- pragma translate_off
  assert TEST_ST_LINES >= 1 and TEST_ST_LINES <= 32
    report "agen: TEST_ST_LINES must be 1..32 (Dhi is 5 bits)" severity failure;

  -- SPEC 7 asserts on every copy slot k (S_k, n_k = S_(k+1) - S_k, with
  -- S_0 = 0 and S_4 = d_total; unused slots carry S = d_total):
  --   D >= 0, i.e. q > S_k (the first source byte is below the command start);
  --   copy source < W unless REP: the LAST source byte W + S_k + n_k - 1 - q
  --   is below W, i.e. S_k + n_k <= q (SPEC 5.1: n <= q - d). REP (slot 0):
  --   q in {1,2,4,8,16}, sources W - q + (i mod q) < W by construction.
  assert_proc: process (clk) is
    variable sk, sn : natural;
    variable q      : natural;
  begin
    if rising_edge(clk) then
      if reset = '0' and a1.valid = '1' then
        for k in 0 to 3 loop
          if a1.slot(k).used = '1' and a1.slot(k).kind = K_CPY then
            assert a1.slot(k).d >= 0
              report "agen: copy slot " & integer'image(k) & " has D < 0 (q <= S)"
              severity failure;
            if k = 0 then sk := 0; else sk := to_integer(a1.s(k)); end if;
            if k = 3 then sn := to_integer(a1.d_total); else sn := to_integer(a1.s(k + 1)); end if;
            q := to_integer(a1.slot(k).q);
            if k = 0 and a1.rep = '1' then
              assert q = 1 or q = 2 or q = 4 or q = 8 or q = 16
                report "agen: REP copy with q = " & integer'image(q) severity failure;
            else
              assert sn <= q
                report "agen: copy slot " & integer'image(k) & " reads a byte of its own command (S "
                       & integer'image(sk) & " + n " & integer'image(sn - sk) & " > q "
                       & integer'image(q) & ")" severity failure;
            end if;
          end if;
        end loop;
      end if;
    end if;
  end process;
  -- pragma translate_on

end rtl;
