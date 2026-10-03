library std;
use std.textio.all;
use std.env.all;

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use ieee.math_real.all;

library work;
use work.vhsnunzip_dsw4_pkg.all;

-- tb_dpath (SPEC 12 B2a): unit testbench of vhsnunzip_dpath (+ agen, defifo,
-- 32 vhsnunzip_ram sim instances) against the gold traces of gold.py.
--
-- MODE 0: drives agcmd lines (AGF) straight into dpath.ag, with the URAM
--         read commands built from the line's adev/adod (as AG2 presents
--         them).
-- MODE 1: drives cmds lines (CMDF) into vhsnunzip_agen (TEST_ST_LINES =
--         ST_LINES_G) and checks every agen output (agcmd_t fields and the
--         32 URAM read commands) against AGF, field by field.
-- Both: the CBUF is a 32-row x 32-lane model written beat by beat from
-- MEMF (prep.py, the GA memory of cs.tv) under the real credit rule
-- GB + 32 - LWX_r <= 896 (LWX_r = dpath lwx registered once, as in
-- vhsnunzip_cbuf), async read at the dpath's rowA/rowB. A command issues only
-- when its literal bytes have arrived (A = GB two cycles old, as the writer
-- sees it), the de credit is ok, it is not the bubble cycle after a last
-- command, and a random draw < ISSUE_PCT. de_ready is random (DE_PCT).
-- The de stream is compared line by line with out.hex (cnt, bytes, last).
entity tb_dpath is
  generic (
    DIR        : string  := "";            -- gold draw directory, with '/'
    MEMF       : string  := "";            -- prep.py mem.hex
    AGF        : string  := "agcmd.txt";
    CMDF       : string  := "cmds.txt";
    OUTF       : string  := "out.hex";
    -- STRICT: every de transfer equals one out.hex line (cnt, bytes) and
    -- last=1 is followed by EOC. Not STRICT: per chunk byte stream equality
    -- only (a knob variant may frame the lines differently from out.hex,
    -- e.g. an extra cnt=0 last=1 transfer when the EOC rides alone).
    STRICT     : boolean := true;
    MODE       : natural := 0;
    ST_LINES_G : natural := 32;
    ISSUE_PCT  : natural := 100;
    DE_PCT     : natural := 100;
    SEED       : natural := 1;
    MAXCYC     : natural := 20000000
  );
end tb_dpath;

architecture tb of tb_dpath is

  type cbuf_t is array (0 to 31) of byte_array(0 to 31);
  type beats_t is array (natural range <>) of byte_array(0 to 31);
  type beats_ptr is access beats_t;

  constant RAM_IDLE : ram_command := (
    valid => '0', addr => (others => '0'), wren => '0',
    wdat => (others => X"00"), wctrl => (others => '0'));

  signal clk          : std_logic := '0';
  signal reset        : std_logic := '1';

  signal cmd          : element_stream := ELEMENT_STREAM_INIT;
  signal ag_tb        : agcmd_t := AGCMD_INIT;
  signal rd_tb        : ram_command_array(0 to 31) := (others => RAM_IDLE);
  signal ag_ag        : agcmd_t;
  signal rd_ag        : ram_command_array(0 to 31);
  signal ag           : agcmd_t;
  signal ram_rd       : ram_command_array(0 to 31);
  signal ram_rd_resp  : ram_response_array(0 to 31);
  signal ram_wr       : ram_command_array(0 to 31);

  signal rowA, rowB   : u5_arr(0 to 31);
  signal litA, litB   : byte_array(0 to 31);
  signal lwx          : ga_t;
  signal lwx_rr       : ga_t := (others => '0');
  signal push         : decompressed_stream;
  signal de           : decompressed_stream;
  signal de_ready     : std_logic := '0';
  signal de_credit_ok : std_logic;
  signal level        : unsigned(DE_DEPTH_LOG2 downto 0);

  signal cbuf         : cbuf_t := (others => (others => X"00"));
  signal gb           : ga_t := (others => '0');
  signal a1, a2       : ga_t := (others => '0');

  signal stim_done    : boolean := false;
  signal out_done     : boolean := false;
  signal n_issued     : natural := 0;
  signal n_lines      : natural := 0;
  signal n_err        : natural := 0;
  signal err_stim     : natural := 0;
  signal err_ag       : natural := 0;
  signal err_out      : natural := 0;
  signal cyc          : natural := 0;
  signal n_lt, n_st, n_lit : natural := 0;     -- MODE 0 (stim)
  signal m_lt, m_st, m_lit : natural := 0;     -- MODE 1 (agen checker)

  -- Read one hex field of ndig digits.
  procedure rx(l : inout line; ndig : in positive; v : out unsigned) is
    variable t    : std_logic_vector(4*ndig - 1 downto 0);
    variable good : boolean;
  begin
    hread(l, t, good);
    assert good report "tb_dpath: hex field parse error" severity failure;
    v := resize(unsigned(t), v'length);
  end procedure;

  procedure rb(l : inout line; v : out std_logic) is
    variable t : unsigned(3 downto 0);
  begin
    rx(l, 1, t);
    v := t(0);
  end procedure;

  -- Next non-comment line; false at end of file.
  procedure next_line(file f : text; l : inout line; ok : out boolean) is
  begin
    ok := false;
    while not endfile(f) loop
      readline(f, l);
      if l'length > 0 and l(l'low) = '#' then
        next;
      end if;
      ok := true;
      return;
    end loop;
  end procedure;

  type u12_arr is array (0 to 3) of unsigned(11 downto 0);

  -- One agcmd line: the record plus the URAM rows and the line index.
  procedure parse_ag(l : inout line; a : out agcmd_t; adev, adod : out u12_arr;
                     idx : out natural) is
    variable u24 : unsigned(23 downto 0);
    variable u4  : unsigned(3 downto 0);
    variable u8  : unsigned(7 downto 0);
    variable u20 : unsigned(19 downto 0);
    variable v   : agcmd_t;
  begin
    v := AGCMD_INIT;
    rx(l, 6, u24); idx := to_integer(u24);
    v.valid := '1';
    rb(l, v.last);
    rb(l, v.rep);
    rx(l, 2, u8); v.wl := u8(4 downto 0);
    rx(l, 2, u8); v.d_total := u8(5 downto 0);
    rx(l, 2, u8); v.s1 := u8(5 downto 0);
    rx(l, 2, u8); v.s2 := u8(5 downto 0);
    rx(l, 2, u8); v.s3 := u8(5 downto 0);
    rx(l, 8, v.lw);
    rx(l, 2, u8); v.rep_q := u8(4 downto 0);
    rx(l, 2, u8); v.rep_base := u8(4 downto 0);
    for k in 0 to 3 loop
      rx(l, 1, u4); v.tier(k) := std_logic_vector(u4(1 downto 0));
      rx(l, 2, u8); v.rot(k) := u8(4 downto 0);
      rx(l, 2, u8); v.smod(k) := u8(4 downto 0);
      rx(l, 2, u8); v.dhi(k) := u8(4 downto 0);
      rx(l, 2, u8); v.dlo(k) := u8(4 downto 0);
      rb(l, v.l0p(k));
      rb(l, v.litb(k));
      rx(l, 3, adev(k));
      rx(l, 3, adod(k));
    end loop;
    rx(l, 2, u8); v.crowA := u8(4 downto 0);
    rx(l, 2, u8); v.cmodA := u8(4 downto 0);
    rx(l, 2, u8); v.crowB := u8(4 downto 0);
    rx(l, 2, u8); v.cmodB := u8(4 downto 0);
    rx(l, 5, u20);   -- wc (informational)
    a := v;
  end procedure;

  -- One cmds line: the writer command plus the literal-arrival bound
  -- (max over LIT slots of lptr + n, 0 if none).
  procedure parse_cmd(l : inout line; c : out element_stream; need : out ga_t;
                      has_lit : out boolean; idx : out natural) is
    variable u24 : unsigned(23 downto 0);
    variable u4  : unsigned(3 downto 0);
    variable u8  : unsigned(7 downto 0);
    variable u16 : unsigned(15 downto 0);
    variable v   : element_stream;
    variable e   : ga_t;
    variable nd  : ga_t;
    variable hl  : boolean;
  begin
    v  := ELEMENT_STREAM_INIT;
    nd := (others => '0');
    hl := false;
    rx(l, 6, u24); idx := to_integer(u24);
    v.valid := '1';
    rb(l, v.last);
    rb(l, v.rep);
    rx(l, 1, u4); v.c := u4(2 downto 0);
    rb(l, v.cut);
    rx(l, 2, u8); v.d_total := u8(5 downto 0);
    rx(l, 8, v.lw);
    for k in 0 to 3 loop
      rb(l, v.slot(k).val);
      rx(l, 1, u4); v.slot(k).kind := std_logic_vector(u4(1 downto 0));
      rb(l, v.slot(k).port_b);
      rx(l, 2, u8); v.slot(k).n := u8(5 downto 0);
      rx(l, 2, u8); v.slot(k).s := u8(5 downto 0);
      rx(l, 4, v.slot(k).q);
      rx(l, 8, v.slot(k).lptr);
      if v.slot(k).val = '1' and v.slot(k).kind = K_LIT then
        e := v.slot(k).lptr + resize(v.slot(k).n, 32);
        if not hl or signed(e - nd) > 0 then
          nd := e;
        end if;
        hl := true;
      end if;
    end loop;
    v.cp0_val := v.slot(0).val when v.slot(0).kind = K_CPY else '0';
    v.cp1_val := v.slot(1).val when v.slot(1).kind = K_CPY else '0';
    v.cp2_val := v.slot(2).val when v.slot(2).kind = K_CPY else '0';
    v.cp3_val := v.slot(3).val when v.slot(3).kind = K_CPY else '0';
    for k in 0 to 3 loop
      if v.slot(k).val = '1' and v.slot(k).kind = K_LIT then
        if v.slot(k).port_b = '1' then v.li1_val := '1'; else v.li0_val := '1'; end if;
      end if;
    end loop;
    c := v;
    need := nd;
    has_lit := hl;
  end procedure;

  function hex2(b : std_logic_vector(7 downto 0)) return string is
    constant HX : string(1 to 16) := "0123456789ABCDEF";
    variable s  : string(1 to 2);
  begin
    if is_x(b) then
      return "XX";
    end if;
    s(1) := HX(to_integer(unsigned(b(7 downto 4))) + 1);
    s(2) := HX(to_integer(unsigned(b(3 downto 0))) + 1);
    return s;
  end function;

begin

  clk <= not clk after 5 ns;

  -----------------------------------------------------------------------------
  -- DUT: (agen) + dpath + defifo + 32 RAMs.
  -----------------------------------------------------------------------------
  agen_gen: if MODE = 1 generate
    agen_inst: entity work.vhsnunzip_agen
      generic map (TEST_ST_LINES => ST_LINES_G)
      port map (clk => clk, reset => reset, cmd => cmd, ag => ag_ag, ram_rd => rd_ag);
    ag     <= ag_ag;
    ram_rd <= rd_ag;
  end generate;
  direct_gen: if MODE /= 1 generate
    ag     <= ag_tb;
    ram_rd <= rd_tb;
  end generate;

  dpath_inst: entity work.vhsnunzip_dpath
    port map (
      clk => clk, reset => reset, ag => ag, ram_rd_resp => ram_rd_resp,
      rowA => rowA, rowB => rowB, litA => litA, litB => litB,
      lwx => lwx, push => push, ram_wr => ram_wr);

  defifo_inst: entity work.vhsnunzip_defifo
    port map (
      clk => clk, reset => reset, push => push, de => de, de_ready => de_ready,
      de_credit_ok => de_credit_ok, level => level);

  ram_gen: for i in 0 to 31 generate
    ram_inst: entity work.vhsnunzip_ram
      port map (
        clk => clk, reset => reset,
        a_cmd => ram_wr(i), a_resp => open,
        b_cmd => ram_rd(i), b_resp => ram_rd_resp(i));
  end generate;

  -----------------------------------------------------------------------------
  -- CBUF model.
  -----------------------------------------------------------------------------
  lit_proc: process (cbuf, rowA, rowB) is
  begin
    for j in 0 to 31 loop
      litA(j) <= cbuf(to_integer(rowA(j)))(j);
      litB(j) <= cbuf(to_integer(rowB(j)))(j);
    end loop;
  end process;

  infeed_proc: process is
    file f       : text;
    variable l   : line;
    variable nb  : integer;
    variable bp  : beats_ptr;
    variable t   : std_logic_vector(255 downto 0);
    variable gbv : ga_t := (others => '0');
    variable good : boolean;
  begin
    file_open(f, MEMF, read_mode);
    readline(f, l);
    read(l, nb);
    bp := new beats_t(0 to nb - 1);
    for i in 0 to nb - 1 loop
      readline(f, l);
      hread(l, t, good);
      assert good report "tb_dpath: mem.hex parse error" severity failure;
      for j in 0 to 31 loop
        bp(i)(j) := t(255 - 8*j downto 248 - 8*j);
      end loop;
    end loop;
    file_close(f);
    wait until rising_edge(clk) and reset = '0';
    loop
      wait until rising_edge(clk);
      lwx_rr <= lwx;
      a1 <= gb;
      a2 <= a1;
      if to_integer(gbv(31 downto 5)) < nb and
         unsigned(gbv + 32 - lwx_rr) <= CBUF_WIN then
        cbuf(to_integer(gbv(9 downto 5))) <= bp(to_integer(gbv(31 downto 5)));
        gbv := gbv + 32;
      end if;
      gb <= gbv;
    end loop;
  end process;

  -----------------------------------------------------------------------------
  -- Command issue.
  -----------------------------------------------------------------------------
  stim_proc: process is
    file fa      : text;
    file fc      : text;
    variable la, lc : line;
    variable oka, okc : boolean;
    variable av  : agcmd_t;
    variable adev, adod : u12_arr;
    variable cv  : element_stream;
    variable nd  : ga_t;
    variable hl  : boolean;
    variable ia, ic : natural;
    variable bubble : boolean := false;
    variable s1v, s2v : positive := SEED + 1;
    variable r   : real;
    variable go  : boolean;
    variable cnt : natural := 0;
    variable rd  : ram_command_array(0 to 31);
    variable lt, st, lit : natural := 0;
    variable errs : natural := 0;
  begin
    if MODE /= 1 then
      file_open(fa, DIR & AGF, read_mode);
    end if;
    file_open(fc, DIR & CMDF, read_mode);
    wait until rising_edge(clk) and reset = '0';
    for i in 1 to 20 loop
      wait until rising_edge(clk);
    end loop;
    loop
      next_line(fc, lc, okc);
      exit when not okc;
      parse_cmd(lc, cv, nd, hl, ic);
      if MODE /= 1 then
        next_line(fa, la, oka);
        assert oka report "tb_dpath: agcmd file shorter than cmds file" severity failure;
        parse_ag(la, av, adev, adod, ia);
        if ia /= ic then
          report "tb_dpath: agcmd idx " & integer'image(ia) & " /= cmds idx "
                 & integer'image(ic) severity error;
          errs := errs + 1;
        end if;
        for k in 0 to 3 loop
          if av.tier(k) = T_ST then st := st + 1;
          elsif av.tier(k) = T_LT then lt := lt + 1;
          elsif av.tier(k) = T_LIT then lit := lit + 1;
          end if;
        end loop;
      end if;
      -- Wait for the issue conditions (values before this edge).
      loop
        go := true;
        if bubble then go := false; end if;
        if de_credit_ok /= '1' then go := false; end if;
        if hl and signed(a2 - nd) < 0 then go := false; end if;
        if ISSUE_PCT < 100 then
          uniform(s1v, s2v, r);
          if r * 100.0 >= real(ISSUE_PCT) then go := false; end if;
        end if;
        if go then
          exit;
        end if;
        bubble := false;
        ag_tb <= AGCMD_INIT;      -- idle: no stale fields
        rd_tb <= (others => RAM_IDLE);
        cmd <= ELEMENT_STREAM_INIT;
        wait until rising_edge(clk);
      end loop;
      -- Issue at this edge.
      if MODE = 1 then
        cmd <= cv;
      else
        ag_tb <= av;
        rd := (others => RAM_IDLE);
        for k in 0 to 3 loop
          if av.tier(k) = T_LT then
            for w in 0 to 3 loop
              rd(k*8 + w).valid := '1';
              rd(k*8 + w).addr  := adev(k);
              rd(k*8 + 4 + w).valid := '1';
              rd(k*8 + 4 + w).addr  := adod(k);
            end loop;
          end if;
        end loop;
        rd_tb <= rd;
      end if;
      bubble := cv.last = '1';
      cnt := cnt + 1;
      n_issued <= cnt;
      wait until rising_edge(clk);
    end loop;
    ag_tb <= AGCMD_INIT;      -- idle: no stale fields
    rd_tb <= (others => RAM_IDLE);
    cmd <= ELEMENT_STREAM_INIT;
    if MODE /= 1 then
      next_line(fa, la, oka);
      if oka then
        report "tb_dpath: agcmd file longer than cmds file" severity error;
        errs := errs + 1;
      end if;
    end if;
    n_lt <= lt; n_st <= st; n_lit <= lit;
    err_stim <= errs;
    stim_done <= true;
    wait;
  end process;

  -----------------------------------------------------------------------------
  -- MODE 1: agen output against AGF.
  -----------------------------------------------------------------------------
  agchk_gen: if MODE = 1 generate
    agchk_proc: process is
      file fa     : text;
      variable la : line;
      variable ok : boolean;
      variable ev : agcmd_t;
      variable adev, adod : u12_arr;
      variable ia : natural;
      variable n  : natural := 0;
      variable errs : natural := 0;
      variable bad : boolean;
      variable lt, st, lit : natural := 0;
    begin
      file_open(fa, DIR & AGF, read_mode);
      loop
        wait until rising_edge(clk);
        if reset = '0' and ag_ag.valid = '1' then
          next_line(fa, la, ok);
          if not ok then
            report "tb_dpath: agen emitted more commands than " & AGF severity error;
            errs := errs + 1;
          else
            parse_ag(la, ev, adev, adod, ia);
            bad := ag_ag.last /= ev.last or ag_ag.rep /= ev.rep or ag_ag.wl /= ev.wl
                   or ag_ag.d_total /= ev.d_total or ag_ag.s1 /= ev.s1
                   or ag_ag.s2 /= ev.s2 or ag_ag.s3 /= ev.s3 or ag_ag.lw /= ev.lw
                   or ag_ag.rep_q /= ev.rep_q or ag_ag.rep_base /= ev.rep_base
                   or ag_ag.crowA /= ev.crowA or ag_ag.cmodA /= ev.cmodA
                   or ag_ag.crowB /= ev.crowB or ag_ag.cmodB /= ev.cmodB;
            for k in 0 to 3 loop
              if ag_ag.tier(k) /= ev.tier(k) or ag_ag.rot(k) /= ev.rot(k)
                 or ag_ag.smod(k) /= ev.smod(k) or ag_ag.dhi(k) /= ev.dhi(k)
                 or ag_ag.dlo(k) /= ev.dlo(k) or ag_ag.l0p(k) /= ev.l0p(k)
                 or ag_ag.litb(k) /= ev.litb(k) then
                bad := true;
              end if;
              if ev.tier(k) = T_ST then st := st + 1;
              elsif ev.tier(k) = T_LT then lt := lt + 1;
              elsif ev.tier(k) = T_LIT then lit := lit + 1;
              end if;
              for w in 0 to 3 loop
                if ev.tier(k) = T_LT then
                  if rd_ag(k*8 + w).valid /= '1' or rd_ag(k*8 + w).wren /= '0'
                     or rd_ag(k*8 + w).addr /= adev(k)
                     or rd_ag(k*8 + 4 + w).valid /= '1' or rd_ag(k*8 + 4 + w).wren /= '0'
                     or rd_ag(k*8 + 4 + w).addr /= adod(k) then
                    bad := true;
                  end if;
                else
                  if rd_ag(k*8 + w).valid /= '0' or rd_ag(k*8 + 4 + w).valid /= '0' then
                    bad := true;
                  end if;
                end if;
              end loop;
            end loop;
            if bad then
              errs := errs + 1;
              if errs <= 10 then
                report "tb_dpath: agen output differs from " & AGF & " line idx "
                       & integer'image(ia) severity error;
              end if;
            end if;
          end if;
          n := n + 1;
        end if;
        if stim_done and n = n_issued then
          -- Drain a few cycles to be sure no command is left in agen.
          for i in 1 to 4 loop
            wait until rising_edge(clk);
            if ag_ag.valid = '1' then
              report "tb_dpath: agen output after the last command" severity error;
              errs := errs + 1;
            end if;
          end loop;
          next_line(fa, la, ok);
          if ok then
            report "tb_dpath: " & AGF & " has more lines than agen emitted" severity error;
            errs := errs + 1;
          end if;
          err_ag <= errs;
          m_lt <= lt; m_st <= st; m_lit <= lit;
          wait;
        end if;
      end loop;
    end process;
  end generate;

  -----------------------------------------------------------------------------
  -- de port: random ready, compare with out.hex.
  -----------------------------------------------------------------------------
  ready_proc: process is
    variable s1v, s2v : positive := SEED + 7;
    variable r : real;
  begin
    wait until rising_edge(clk);
    if DE_PCT >= 100 then
      de_ready <= '1';
    else
      uniform(s1v, s2v, r);
      if r * 100.0 < real(DE_PCT) then de_ready <= '1'; else de_ready <= '0'; end if;
    end if;
  end process;

  out_proc: process is
    file fo      : text;
    variable l   : line;
    variable n   : natural := 0;
    variable errs : natural := 0;
    variable cntx : natural;
    variable eoc_pend : boolean := false;
    variable b   : std_logic_vector(7 downto 0) := (others => '0');
    variable good : boolean;
  begin
    file_open(fo, DIR & OUTF, read_mode);
    loop
      wait until rising_edge(clk);
      if reset = '0' and de.valid = '1' and de_ready = '1' and not STRICT then
        -- Byte stream per chunk.
        for j in 0 to to_integer(de.cnt) - 1 loop
          good := false;
          loop
            exit when eoc_pend;
            if l /= null and l'length >= 2 then
              hread(l, b, good);
              exit;
            end if;
            exit when endfile(fo);
            readline(fo, l);
            if l'length = 3 and l.all = "EOC" then
              eoc_pend := true;
              deallocate(l);
            end if;
          end loop;
          if not good or b /= de.data(j) then
            errs := errs + 1;
            if errs <= 10 then
              report "tb_dpath: de transfer " & integer'image(n) & " byte "
                     & integer'image(j) & " = " & hex2(de.data(j))
                     & " expected " & hex2(b) & " (stream)" severity error;
            end if;
            exit;
          end if;
        end loop;
        if de.last = '1' then
          loop
            if eoc_pend then
              eoc_pend := false;
              exit;
            end if;
            if l /= null and l'length > 0 then
              errs := errs + 1;
              report "tb_dpath: de transfer " & integer'image(n)
                     & " has last=1 before the expected chunk end" severity error;
              deallocate(l);
              exit;
            end if;
            if endfile(fo) then
              errs := errs + 1;
              report "tb_dpath: last=1 but " & OUTF & " has no EOC" severity error;
              exit;
            end if;
            readline(fo, l);
            if l'length = 3 and l.all = "EOC" then
              deallocate(l);
              exit;
            end if;
          end loop;
        end if;
        n := n + 1;
        n_lines <= n;
        if endfile(fo) and (l = null or l'length = 0) and not eoc_pend
           and de.last = '1' and not out_done then
          out_done <= true;
        end if;
        err_out <= errs;
      elsif reset = '0' and de.valid = '1' and de_ready = '1' then
        if endfile(fo) then
          if errs < 10 then
            report "tb_dpath: de transfer beyond the end of " & OUTF severity error;
          end if;
          errs := errs + 1;
        else
          readline(fo, l);
          cntx := l'length / 2;
          if l'length = 3 and l.all = "EOC" then
            cntx := 999;
          end if;
          if cntx /= to_integer(de.cnt) then
            errs := errs + 1;
            if errs <= 10 then
              report "tb_dpath: de line " & integer'image(n) & " cnt "
                     & integer'image(to_integer(de.cnt)) & " expected "
                     & integer'image(cntx) severity error;
            end if;
          else
            for j in 0 to cntx - 1 loop
              hread(l, b, good);
              if not good or b /= de.data(j) then
                errs := errs + 1;
                if errs <= 10 then
                  report "tb_dpath: de line " & integer'image(n) & " byte "
                         & integer'image(j) & " = " & hex2(de.data(j))
                         & " expected " & hex2(b) severity error;
                end if;
                exit;
              end if;
            end loop;
          end if;
          if de.last = '1' then
            if endfile(fo) then
              errs := errs + 1;
              report "tb_dpath: last=1 but " & OUTF & " has no EOC" severity error;
            else
              readline(fo, l);
              if not (l'length = 3 and l.all = "EOC") then
                errs := errs + 1;
                if errs <= 10 then
                  report "tb_dpath: de line " & integer'image(n)
                         & " has last=1, expected line is not EOC" severity error;
                end if;
              end if;
            end if;
          end if;
          n := n + 1;
          n_lines <= n;
          if endfile(fo) and not out_done then
            out_done <= true;
          end if;
        end if;
        err_out <= errs;
      end if;
    end loop;
  end process;

  -----------------------------------------------------------------------------
  -- Control: reset, watchdog, verdict.
  -----------------------------------------------------------------------------
  ctrl_proc: process is
    variable c : natural := 0;
    variable idle : natural := 0;
    variable last_iss, last_lines : natural := 0;
    variable total : natural;
    variable extra : natural := 0;
  begin
    reset <= '1';
    for i in 1 to 10 loop
      wait until rising_edge(clk);
    end loop;
    reset <= '0';
    loop
      wait until rising_edge(clk);
      c := c + 1;
      cyc <= c;
      if n_issued /= last_iss or n_lines /= last_lines then
        idle := 0;
        last_iss := n_issued;
        last_lines := n_lines;
      else
        idle := idle + 1;
      end if;
      if stim_done and out_done then
        for i in 1 to 64 loop
          wait until rising_edge(clk);
          if de.valid = '1' then
            report "tb_dpath: extra de output after the expected end" severity error;
            extra := extra + 1;
          end if;
        end loop;
        exit;
      end if;
      if c > MAXCYC or idle > 100000 then
        report "tb_dpath: TIMEOUT / no progress after " & integer'image(c)
               & " cycles, issued " & integer'image(n_issued) & ", lines "
               & integer'image(n_lines) severity error;
        report "TB_FAIL" severity failure;
      end if;
    end loop;
    wait until rising_edge(clk);
    total := err_stim + err_ag + err_out + extra;
    report "tb_dpath: cmds " & integer'image(n_issued) & " de lines "
           & integer'image(n_lines) & " cycles " & integer'image(c)
           & " slots ST/LT/LIT " & integer'image(n_st + m_st) & "/"
           & integer'image(n_lt + m_lt) & "/" & integer'image(n_lit + m_lit) & " errors stim/ag/out "
           & integer'image(err_stim) & "/" & integer'image(err_ag) & "/"
           & integer'image(err_out);
    if total = 0 then
      report "TB_PASS";
      finish;
    else
      report "TB_FAIL" severity failure;
    end if;
    wait;
  end process;

end tb;
