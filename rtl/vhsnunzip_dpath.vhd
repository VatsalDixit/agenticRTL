library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library work;
use work.vhsnunzip_dsw4_pkg.all;

-- DSW-4 datapath: D1, S1, S2a, S2b, S3, S4 and the history write
-- (SPEC 3.9-3.13). Fixed latency; never stalls.
--
--   ag          AG2 output (cycle C; the URAM read of an LT slot is presented
--               in the same cycle by vhsnunzip_agen).
--   ram_rd_resp port-b responses of the 32 history RAMs (valid and rdat at
--               C+3 for an LT read presented at C).
--   rowA / rowB CBUF literal-port rows per lane (S1 registers, visible C+3).
--   litA / litB CBUF literal-port bytes (async read of rowA/rowB; used in
--               S2a, C+3).
--   lwx         lw of the last command that was in S2a, registered (-> CBUF
--               credit, which registers it once more). Reset 0.
--   push        S4 PUSH register (visible C+6); push.valid = write the de
--               FIFO this cycle (cnt 0..32, last). Never pushes into a full
--               FIFO: the writer's de credit covers everything in flight.
--   ram_wr      port-a history writes of the 32 RAMs (registered, presented
--               C+7, the cycle after the push): for every mirror m, the 4
--               word instances of parity hw_ptr(0) get valid = 1, wren = 1,
--               addr = hw_ptr(12:1), wdat = PUSH bytes 8w .. 8w+7, wctrl = 0.
--
-- Pipeline (register visible in the cycle named):
--   C    ag (AG2)                       URAM read presented
--   C+1  D1  delay register
--   C+2  D2  delay register             (lines S1 up with the RAM latency)
--   C+3  S1  per-lane controls          URAM rdat valid, CBUF async read;
--                                       S2a computes EXT (4:1 preselect)
--   C+4  S2a EXT + controls             S2b: st-SRL read -> half select ->
--                                       16:1 pair rotator -> region 4:1 = NEW;
--                                       SRL write at the end of the cycle
--   C+5  S3  NEWr, wr_r                 OL <= NEWr where wr_r (end of cycle);
--                                       PUSH computed from NEWr / old OL
--   C+6  S4  PUSH                       de FIFO write at the end of the cycle
--   C+7  history write presented        executed at the end of C+8
-- A spill push (last command with wl + d_total > 32) takes the cycle after
-- the command's own push (C+7), with the data from OL; the writer's bubble
-- after a last command keeps that slot free.
entity vhsnunzip_dpath is
  port (
    clk         : in  std_logic;
    reset       : in  std_logic;

    ag          : in  agcmd_t;
    ram_rd_resp : in  ram_response_array(0 to 31);

    rowA        : out u5_arr(0 to 31);
    rowB        : out u5_arr(0 to 31);
    litA        : in  byte_array(0 to 31);
    litB        : in  byte_array(0 to 31);

    lwx         : out gaw_t;
    push        : out decompressed_stream;
    ram_wr      : out ram_command_array(0 to 31)
  );
end vhsnunzip_dpath;

architecture rtl of vhsnunzip_dpath is

  -- Per slot k (0 to 3), per lane (0 to 31) bytes.
  type bytes_k_t is array (0 to 3) of byte_array(0 to 31);
  -- Per slot k, per pair p (0 to 15) bytes.
  type pairs_k_t is array (0 to 3) of byte_array(0 to 15);

  constant RAM_IDLE : ram_command := (
    valid => '0', addr => (others => '0'), wren => '0',
    wdat => (others => X"00"), wctrl => (others => '0'));

  -- D1, D2.
  signal d1, d2       : agcmd_t := AGCMD_INIT;

  -- S1 registers (visible in S2a) and S2a registers (visible in S2b).
  signal s1           : xcmd_t := XCMD_INIT;
  signal s2           : xcmd_t := XCMD_INIT;
  signal ext          : bytes_k_t := (others => (others => X"00"));
  signal lwx_r        : gaw_t := (others => '0');

  -- S2b: short-term SRL outputs and the gathered line.
  signal st           : bytes_k_t;
  signal newb         : byte_array(0 to 31);

  -- S3.
  signal s3_valid     : std_logic := '0';
  signal s3_last      : std_logic := '0';
  signal s3_cmpl      : std_logic := '0';
  signal s3_spill     : std_logic := '0';
  signal s3_wl        : unsigned(4 downto 0) := (others => '0');
  signal s3_cnt       : unsigned(5 downto 0) := (others => '0');
  signal s3_new       : byte_array(0 to 31) := (others => X"00");
  signal s3_wr        : std_logic_vector(0 to 31) := (others => '0');
  signal ol           : byte_array(0 to 31) := (others => X"00");
  signal spill_pend   : std_logic := '0';
  signal spill_cnt    : unsigned(5 downto 0) := (others => '0');

  -- S4 and the history write.
  signal push_r       : decompressed_stream := DECOMPRESSED_STREAM_INIT;
  signal hw_ptr       : unsigned(12 downto 0) := (others => '0');
  signal wr_r         : ram_command_array(0 to 31) := (others => RAM_IDLE);

begin

  -----------------------------------------------------------------------------
  -- D1, D2: plain delay registers.
  -----------------------------------------------------------------------------
  d_proc: process (clk) is
  begin
    if rising_edge(clk) then
      d1 <= ag;
      d2 <= d1;
      if reset = '1' then
        d1.valid <= '0';
        d2.valid <= '0';
      end if;
    end if;
  end process;

  -----------------------------------------------------------------------------
  -- S1: per-lane controls (SPEC 3.9), registered.
  -----------------------------------------------------------------------------
  s1_proc: process (clk) is
    variable x     : xcmd_t;
    variable sum   : unsigned(6 downto 0);
    variable jj    : unsigned(4 downto 0);
    variable rel   : unsigned(4 downto 0);
    variable nreg  : unsigned(1 downto 0);
    variable msk   : unsigned(4 downto 0);
    variable lane  : unsigned(4 downto 0);
    variable t     : unsigned(4 downto 0);
    variable pr    : unsigned(3 downto 0);
    variable par   : std_logic;
    variable hs    : std_logic;
  begin
    if rising_edge(clk) then
      x := XCMD_INIT;
      x.valid := d2.valid;
      x.last  := d2.last;
      x.wl    := d2.wl;
      x.lw    := d2.lw;

      -- Per command.
      sum := resize(d2.wl, 7) + resize(d2.d_total, 7);
      if sum >= 32 then
        x.cmpl := '1';
      end if;
      if d2.last = '1' and sum > 32 then
        x.spill := '1';
      end if;
      x.cnt_last := resize(sum(4 downto 0), 6);

      msk := d2.rep_q - 1;

      -- Per destination lane j (and per slot/source lane j).
      for j in 0 to 31 loop
        jj  := to_unsigned(j, 5);
        rel := jj - d2.wl;
        if resize(rel, 6) < d2.d_total then
          x.wr(j) := d2.valid;
        end if;

        nreg := "00";
        if d2.s1 <= resize(rel, 6) then nreg := nreg + 1; end if;
        if d2.s2 <= resize(rel, 6) then nreg := nreg + 1; end if;
        if d2.s3 <= resize(rel, 6) then nreg := nreg + 1; end if;
        x.reg(j) := std_logic_vector(nreg);

        for k in 0 to 3 loop
          -- Rotator index.
          if k = 0 and d2.rep = '1' then
            lane := d2.rep_base + (rel and msk);
          else
            lane := jj + d2.rot(k);
          end if;
          x.idx(k, j) := lane(3 downto 0);

          -- Short-term SRL address.
          t := jj - d2.smod(k);
          if t > d2.dlo(k) then
            x.sta(k, j) := d2.dhi(k) - 1;
          else
            x.sta(k, j) := d2.dhi(k);
          end if;

          -- External preselect.
          if d2.tier(k) = T_LT then
            par := d2.l0p(k);
            if jj < d2.smod(k) then
              par := not par;
            end if;
            if par = '0' then
              x.ext_sel(k, j) := X_LTE;
            else
              x.ext_sel(k, j) := X_LTO;
            end if;
          elsif d2.litb(k) = '1' then
            x.ext_sel(k, j) := X_LITB;
          else
            x.ext_sel(k, j) := X_LITA;
          end if;
        end loop;

        -- CBUF literal-port rows.
        if jj < d2.cmodA then
          x.rowA(j) := d2.crowA + 1;
        else
          x.rowA(j) := d2.crowA;
        end if;
        if jj < d2.cmodB then
          x.rowB(j) := d2.crowB + 1;
        else
          x.rowB(j) := d2.crowB;
        end if;
      end loop;

      -- Per slot: tier flag and the per-pair half selects.
      for k in 0 to 3 loop
        if d2.tier(k) = T_ST then
          x.srcst(k) := '1';
        end if;
        for p in 0 to 15 loop
          if k = 0 and d2.rep = '1' then
            -- Both halves: bit 4 of the unique source lane of pair p, i.e.
            -- whether lane p+16 is one of the rep_q source lanes.
            lane := to_unsigned(p + 16, 5) - d2.rep_base;
            if lane < d2.rep_q then
              hs := '1';
            else
              hs := '0';
            end if;
            x.hsa(k)(p) := hs;
            x.hsb(k)(p) := hs;
          else
            pr   := to_unsigned(p, 4) - d2.rot(k)(3 downto 0);
            lane := resize(pr, 5) + d2.rot(k);
            x.hsa(k)(p) := lane(4);
            x.hsb(k)(p) := not lane(4);
          end if;
        end loop;
      end loop;

      s1 <= x;

      if reset = '1' then
        s1.valid <= '0';
        s1.wr    <= (others => '0');
      end if;
    end if;
  end process;

  rowA <= s1.rowA;
  rowB <= s1.rowB;

  -----------------------------------------------------------------------------
  -- S2a: external preselect (URAM even/odd, CBUF A/B), re-register controls.
  -----------------------------------------------------------------------------
  s2a_proc: process (clk) is
  begin
    if rising_edge(clk) then
      s2 <= s1;
      for k in 0 to 3 loop
        for j in 0 to 31 loop
          case s1.ext_sel(k, j) is
            when X_LTE  => ext(k)(j) <= ram_rd_resp(k*8 + j/8).rdat(j mod 8);
            when X_LTO  => ext(k)(j) <= ram_rd_resp(k*8 + 4 + j/8).rdat(j mod 8);
            when X_LITA => ext(k)(j) <= litA(j);
            when others => ext(k)(j) <= litB(j);
          end case;
        end loop;
      end loop;
      if s1.valid = '1' then
        lwx_r <= s1.lw;
      end if;
      if reset = '1' then
        s2.valid <= '0';
        s2.wr    <= (others => '0');
        lwx_r    <= (others => '0');
      end if;
    end if;
  end process;

  lwx <= lwx_r;

  -----------------------------------------------------------------------------
  -- S2b: the gather (LOOP L2 through the short-term SRLs).
  -----------------------------------------------------------------------------
  s2b_proc: process (s2, ext, st) is
    variable xa, xb : pairs_k_t;
    variable g      : byte_array(0 to 3);
  begin
    for k in 0 to 3 loop
      for p in 0 to 15 loop
        if s2.srcst(k) = '1' then
          if s2.hsa(k)(p) = '1' then xa(k)(p) := st(k)(p + 16); else xa(k)(p) := st(k)(p); end if;
          if s2.hsb(k)(p) = '1' then xb(k)(p) := st(k)(p + 16); else xb(k)(p) := st(k)(p); end if;
        else
          if s2.hsa(k)(p) = '1' then xa(k)(p) := ext(k)(p + 16); else xa(k)(p) := ext(k)(p); end if;
          if s2.hsb(k)(p) = '1' then xb(k)(p) := ext(k)(p + 16); else xb(k)(p) := ext(k)(p); end if;
        end if;
      end loop;
    end loop;
    for j in 0 to 31 loop
      for k in 0 to 3 loop
        if j < 16 then
          g(k) := xa(k)(to_integer(s2.idx(k, j)));
        else
          g(k) := xb(k)(to_integer(s2.idx(k, j)));
        end if;
      end loop;
      newb(j) <= g(to_integer(unsigned(s2.reg(j))));
    end loop;
  end process;

  -- 4 identical short-term SRL sets (one per slot), 32 lanes x 8 b x 32 deep.
  -- Lane j of every set shifts in NEW_j when wr_j (1-cycle write-to-read).
  srl_set_gen: for k in 0 to 3 generate
    srl_lane_gen: for j in 0 to 31 generate
      srl_inst: entity work.vhsnunzip_srl
        generic map (
          WIDTH       => 8,
          DEPTH_LOG2  => 5
        )
        port map (
          clk         => clk,
          wr_ena      => s2.wr(j),
          wr_data     => newb(j),
          rd_addr     => s2.sta(k, j),
          rd_data     => st(k)(j)
        );
    end generate;
  end generate;

  -----------------------------------------------------------------------------
  -- S3 (NEWr, OL) and S4 (PUSH, spill).
  -----------------------------------------------------------------------------
  s3_proc: process (clk) is
    variable pv : decompressed_stream;
  begin
    if rising_edge(clk) then
      -- S3 registers.
      s3_valid <= s2.valid;
      s3_last  <= s2.last;
      s3_cmpl  <= s2.cmpl;
      s3_spill <= s2.spill;
      s3_wl    <= s2.wl;
      s3_cnt   <= s2.cnt_last;
      s3_new   <= newb;
      s3_wr    <= s2.wr;

      -- OL update from the registered copy (end of S3).
      for j in 0 to 31 loop
        if s3_wr(j) = '1' then
          ol(j) <= s3_new(j);
        end if;
      end loop;

      -- PUSH (reads OL before this cycle's update).
      pv := push_r;
      pv.valid := '0';
      if s3_valid = '1' then
        for j in 0 to 31 loop
          if to_unsigned(j, 5) >= s3_wl then
            pv.data(j) := s3_new(j);
          else
            pv.data(j) := ol(j);
          end if;
        end loop;
        if s3_cmpl = '1' then
          pv.valid := '1';
          pv.cnt   := to_unsigned(32, 6);
          pv.last  := s3_last and not s3_spill;
        elsif s3_last = '1' then
          pv.valid := '1';
          pv.cnt   := s3_cnt;
          pv.last  := '1';
        end if;
      elsif spill_pend = '1' then
        pv.data  := ol;
        pv.valid := '1';
        pv.cnt   := spill_cnt;
        pv.last  := '1';
      end if;
      push_r <= pv;

      spill_pend <= s3_valid and s3_spill;
      spill_cnt  <= s3_cnt;

      if reset = '1' then
        s3_valid     <= '0';
        s3_wr        <= (others => '0');
        spill_pend   <= '0';
        push_r.valid <= '0';
      end if;
    end if;
  end process;

  push <= push_r;

  -----------------------------------------------------------------------------
  -- History write (SPEC 3.13), the cycle after the push.
  -----------------------------------------------------------------------------
  hw_proc: process (clk) is
    variable idx : natural;
  begin
    if rising_edge(clk) then
      for m in 0 to 3 loop
        for par in 0 to 1 loop
          for w in 0 to 3 loop
            idx := m*8 + par*4 + w;
            if push_r.valid = '1' and to_integer(hw_ptr(0 downto 0)) = par then
              wr_r(idx).valid <= '1';
            else
              wr_r(idx).valid <= '0';
            end if;
            wr_r(idx).wren  <= '1';
            wr_r(idx).addr  <= hw_ptr(12 downto 1);
            wr_r(idx).wdat  <= push_r.data(8*w to 8*w + 7);
            wr_r(idx).wctrl <= (others => '0');
          end loop;
        end loop;
      end loop;
      if push_r.valid = '1' then
        if push_r.last = '1' then
          hw_ptr <= (others => '0');
        else
          hw_ptr <= hw_ptr + 1;
        end if;
      end if;
      if reset = '1' then
        hw_ptr <= (others => '0');
        for i in 0 to 31 loop
          wr_r(i).valid <= '0';
        end loop;
      end if;
    end if;
  end process;

  ram_wr <= wr_r;

  -- pragma translate_off
  assert_proc: process (clk) is
    variable lt : boolean;
  begin
    if rising_edge(clk) then
      if reset = '0' then
        -- RAM data arrives exactly in S2a for every LT slot, and never
        -- otherwise (the ext_sel of an LT slot is X_LTE/X_LTO on every lane).
        for k in 0 to 3 loop
          lt := s1.valid = '1' and s1.ext_sel(k, 0)(1) = '0';
          for i in 0 to 7 loop
            if lt then
              assert ram_rd_resp(k*8 + i).valid = '1'
                report "dpath: URAM data of an LT read is not valid in S2a (slot "
                       & integer'image(k) & ")" severity failure;
            else
              assert ram_rd_resp(k*8 + i).valid /= '1'
                report "dpath: unexpected URAM read data in S2a (slot "
                       & integer'image(k) & ")" severity failure;
            end if;
          end loop;
        end loop;
        -- The spill push needs the bubble after a last command.
        assert not (s3_valid = '1' and spill_pend = '1')
          report "dpath: command in S3 in the spill cycle (no bubble after last)"
          severity failure;
        -- Pushed bytes are defined.
        if push_r.valid = '1' then
          for j in 0 to 31 loop
            if j < to_integer(push_r.cnt) then
              assert not is_x(push_r.data(j))
                report "dpath: undefined byte pushed in lane " & integer'image(j)
                severity error;
            end if;
          end loop;
        end if;
      end if;
    end if;
  end process;
  -- pragma translate_on

end rtl;
