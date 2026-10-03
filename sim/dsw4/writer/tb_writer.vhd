library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use ieee.math_real.all;
use std.textio.all;

library work;
use work.vhsnunzip_dsw4_pkg.all;

-- tb_writer: SPEC 12 B2b unit testbench for vhsnunzip_elq + vhsnunzip_writer.
--
-- The testbench plays the walker: it writes the elements of elements.txt into
-- the ELQ (up to 4 per cycle, under the 48-entry credit rule, credits returned
-- by cred_ret), drives de_credit_ok and a_r, and observes the writer's
-- commands. DIRLIST lists draw directories (gold.py trace dirs), one per line;
-- each draw runs after its own reset.
--
-- MODE 0 (A): feed as fast as credits allow, de_credit_ok held low for the
--   first 16 cycles of a draw (window fill) and then low on CRED_PCT % of
--   cycles; TEST_STALL_PCT may be on. With the window kept full, every decision
--   sees >= 4 usable entries, so the command stream must equal gold's
--   timing-free CMDF (cmds.txt or cmds_<variant>.txt) FIELD BY FIELD; checked
--   here. a_r is far ahead (every literal byte available, av = 63).
-- MODE 1 (B): random feed gaps (feed on FEED_PCT % of cycles, 1..4 elements)
--   and de-credit gaps. Visibility then shapes the commands, so the testbench
--   logs every decision cycle to LOGF: the visible-element bound vis (entries
--   that were in the writer window registers a full cycle: vis(t) = fp(t-1),
--   with fp the running sum of pf_adv) and the issued command; check_tb.py
--   replays gold.Writer.decide(vis) cycle by cycle and compares field by field.
-- MODE 2 (C): as B, plus a_r starts at 0 and advances by a random 0..AR_STEP-1 per
--   cycle (AR_STEP generic, default 48); check_tb.py checks the byte-exactness invariants (gold cannot be
--   compared: the RTL uses a one-cycle-old a_r, which is conservative).
--
-- Always checked here: rp_adv = cmd.c of the command issued in that cycle and
-- 0 otherwise; no command on a de-credit-low cycle or in the cycle after a
-- last command; no command after the last element; no deadlock.
entity tb_writer is
  generic (
    DIRLIST   : string   := "dirs.txt";
    CMDF      : string   := "cmds.txt";
    LOGF      : string   := "tb.log";
    MODE      : natural  := 0;
    SEED      : positive := 1;
    CRED_PCT  : natural  := 0;
    FEED_PCT  : natural  := 100;
    AR_STEP   : positive := 48;    -- MODE 2: a_r += random 0..AR_STEP-1
    SLOTS     : natural  := 4;
    CUT       : boolean  := false;
    NOREP     : boolean  := false;
    LITP1     : boolean  := false;
    STALL_PCT : natural  := 0
  );
end tb_writer;

architecture sim of tb_writer is

  signal clk          : std_logic := '0';
  signal reset        : std_logic := '1';
  signal running      : boolean := true;

  signal el           : element_arr(0 to 3) := (others => ELEMENT_INIT);
  signal wcnt         : unsigned(2 downto 0) := (others => '0');
  signal pf           : element_arr(0 to 3);
  signal nvis         : unsigned(6 downto 0);
  signal pf_adv       : unsigned(2 downto 0);
  signal rp_adv       : unsigned(2 downto 0);
  signal cred_ret     : unsigned(2 downto 0);
  signal a_r          : ga_t := X"40000000";
  signal de_credit_ok : std_logic := '0';
  signal cmd          : element_stream;

begin

  clk <= not clk after 5 ns when running else '0';

  elq: entity work.vhsnunzip_elq
    port map (
      clk => clk, reset => reset, el => el, wcnt => wcnt, pf => pf, nvis => nvis,
      pf_adv => pf_adv, rp_adv => rp_adv, cred_ret => cred_ret);

  wr: entity work.vhsnunzip_writer
    generic map (
      TEST_SLOTS => SLOTS, TEST_CUT => CUT, TEST_NOREP => NOREP,
      TEST_LITP1 => LITP1, TEST_STALL_PCT => STALL_PCT)
    port map (
      clk => clk, reset => reset, pf => pf, nvis => nvis, pf_adv => pf_adv,
      rp_adv => rp_adv, a_r => a_r, de_credit_ok => de_credit_ok, cmd => cmd);

  main: process
    type el_ptr is access element_arr;
    variable els      : el_ptr;
    variable nel      : natural;
    file     dl, ef, cf, lf : text;
    variable st       : file_open_status;
    variable ln, wl   : line;
    variable dir      : line;
    variable s1, s2   : positive := SEED;
    variable rnd      : real;
    variable v4       : std_logic_vector(3 downto 0);
    variable v8       : std_logic_vector(7 downto 0);
    variable v16      : std_logic_vector(15 downto 0);
    variable v24      : std_logic_vector(23 downto 0);
    variable v32      : std_logic_vector(31 downto 0);
    variable e        : element_t;
    -- per draw
    variable wp, rp, fp_cur, fp_old : natural;
    variable cred     : integer;
    variable t        : natural;
    variable idle     : natural;
    variable ncmd     : natural;
    variable s_rpadv, s_pfadv, s_cret : natural;
    variable s_cok    : std_logic;
    variable s_ar     : ga_t;
    variable prev_last: boolean;
    variable dec      : boolean;
    variable nw       : natural;
    variable ar_i     : unsigned(31 downto 0);
    variable errs     : natural := 0;
    variable derrs    : natural;
    variable tot_cmd  : natural := 0;
    variable tot_el   : natural := 0;
    variable tot_dec  : natural := 0;
    variable ndraw    : natural := 0;
    variable done_d   : boolean;
    variable tail     : natural;
    variable maxcyc   : natural;
    -- expected command fields
    variable x_last, x_rep, x_c, x_cut, x_d, x_lw : natural;
    variable x_lwv    : std_logic_vector(31 downto 0);
    variable good     : boolean;

    impure function urand return real is
    begin
      uniform(s1, s2, rnd);
      return rnd;
    end function;

    impure function pct(p : natural) return boolean is
    begin
      return urand * 100.0 < real(p);
    end function;

    procedure skip_comments(file f : text; variable l : inout line; variable eof : out boolean) is
    begin
      eof := false;
      loop
        if endfile(f) then
          eof := true;
          return;
        end if;
        readline(f, l);
        if l'length > 0 and l.all(l'low) /= '#' then
          return;
        end if;
      end loop;
    end procedure;

    procedure mism(name : string; exp, got : std_logic_vector) is
    begin
      good := false;
      if derrs < 12 then
        report "MISMATCH draw " & dir.all & " cmd " & integer'image(ncmd) & " field " & name
               & ": got " & to_hstring(got) & " expected " & to_hstring(exp)
          severity error;
      end if;
    end procedure;

    -- exp = the gold file's field, got = the RTL's.
    procedure cmp(name : string; exp, got : std_logic_vector) is
    begin
      if got /= exp then
        mism(name, exp, got);
      end if;
    end procedure;

    function sl1(b : std_logic) return std_logic_vector is
      variable v : std_logic_vector(3 downto 0) := "0000";
    begin
      v(0) := b;
      return v;
    end function;

    procedure write_cmd(variable l : inout line; c : element_stream) is
    begin
      write(l, to_hstring(sl1(c.last)) & " " & to_hstring(sl1(c.rep)) & " "
               & to_hstring('0' & std_logic_vector(c.c)) & " " & to_hstring(sl1(c.cut)) & " "
               & to_hstring("00" & std_logic_vector(c.d_total)) & " "
               & to_hstring(std_logic_vector(c.lw)));
      for k in 0 to 3 loop
        write(l, " " & to_hstring(sl1(c.slot(k).val)) & " "
                 & to_hstring("00" & c.slot(k).kind) & " " & to_hstring(sl1(c.slot(k).port_b)) & " "
                 & to_hstring("00" & std_logic_vector(c.slot(k).n)) & " "
                 & to_hstring("00" & std_logic_vector(c.slot(k).s)) & " "
                 & to_hstring(std_logic_vector(c.slot(k).q)) & " "
                 & to_hstring(std_logic_vector(c.slot(k).lptr)));
      end loop;
    end procedure;

    -- Compare cmd against the next line of CMDF (mode A).
    procedure check_cmd(c : element_stream) is
      variable eof : boolean;
      variable sv, sk, sp, sn, ss, sq, slp : std_logic_vector(31 downto 0);
      variable cpv : std_logic_vector(0 to 3);
      variable l0, l1 : std_logic;
    begin
      skip_comments(cf, ln, eof);
      if eof then
        good := false;
        report "MISMATCH draw " & dir.all & ": extra command " & integer'image(ncmd) severity error;
        return;
      end if;
      hread(ln, v24);
      cmp("idx", v24, std_logic_vector(to_unsigned(ncmd, 24)));
      hread(ln, v4); cmp("last", v4, sl1(c.last));
      hread(ln, v4); cmp("rep", v4, sl1(c.rep));
      hread(ln, v4); cmp("c", v4, '0' & std_logic_vector(c.c));
      hread(ln, v4); cmp("cut", v4, sl1(c.cut));
      hread(ln, v8); cmp("d_total", v8, "00" & std_logic_vector(c.d_total));
      hread(ln, v32); cmp("lw", v32, std_logic_vector(c.lw));
      cpv := (others => '0');
      l0 := '0';
      l1 := '0';
      for k in 0 to 3 loop
        hread(ln, v4); cmp("slot" & integer'image(k) & ".val", v4, sl1(c.slot(k).val));
        sv(3 downto 0) := v4;
        hread(ln, v4); cmp("slot" & integer'image(k) & ".kind", v4, "00" & c.slot(k).kind);
        if sv(0) = '1' and v4 = "0001" then cpv(k) := '1'; end if;
        sk(3 downto 0) := v4;
        hread(ln, v4); cmp("slot" & integer'image(k) & ".port_b", v4, sl1(c.slot(k).port_b));
        if sv(0) = '1' and sk(3 downto 0) = "0000" then
          if v4(0) = '1' then l1 := '1'; else l0 := '1'; end if;
        end if;
        hread(ln, v8); cmp("slot" & integer'image(k) & ".n", v8, "00" & std_logic_vector(c.slot(k).n));
        hread(ln, v8); cmp("slot" & integer'image(k) & ".s", v8, "00" & std_logic_vector(c.slot(k).s));
        hread(ln, v16); cmp("slot" & integer'image(k) & ".q", v16, std_logic_vector(c.slot(k).q));
        hread(ln, v32); cmp("slot" & integer'image(k) & ".lptr", v32, std_logic_vector(c.slot(k).lptr));
      end loop;
      cmp("cp_val", cpv, c.cp0_val & c.cp1_val & c.cp2_val & c.cp3_val);
      cmp("li0_val", sl1(l0), sl1(c.li0_val));
      cmp("li1_val", sl1(l1), sl1(c.li1_val));
    end procedure;

  begin
    file_open(st, dl, DIRLIST, read_mode);
    assert st = open_ok report "cannot open " & DIRLIST severity failure;
    if MODE /= 0 then
      file_open(st, lf, LOGF, write_mode);
      assert st = open_ok report "cannot open " & LOGF severity failure;
    end if;

    while not endfile(dl) loop
      readline(dl, dir);
      next when dir'length = 0;
      ndraw := ndraw + 1;
      derrs := 0;
      good := true;

      -- Load elements.txt.
      file_open(st, ef, dir.all & "/elements.txt", read_mode);
      assert st = open_ok report "cannot open " & dir.all & "/elements.txt" severity failure;
      nel := 0;
      while not endfile(ef) loop
        readline(ef, ln);
        if ln'length > 0 and ln.all(ln'low) /= '#' then
          nel := nel + 1;
        end if;
      end loop;
      file_close(ef);
      els := new element_arr(0 to nel - 1);
      file_open(st, ef, dir.all & "/elements.txt", read_mode);
      for i in 0 to nel - 1 loop
        loop
          readline(ef, ln);
          exit when ln'length > 0 and ln.all(ln'low) /= '#';
        end loop;
        hread(ln, v4);  e.kind := v4(1 downto 0);
        hread(ln, v32); e.len := unsigned(v32);
        hread(ln, v16); e.off := unsigned(v16);
        hread(ln, v32); e.lptr := unsigned(v32);
        e.valid := '1';
        els(i) := e;
      end loop;
      file_close(ef);
      -- Cycle cap (livelock guard): every command places >= 1 byte or
      -- completes an element.
      maxcyc := 100000 + 4 * nel;
      for i in 0 to nel - 1 loop
        maxcyc := maxcyc + 4 * to_integer(els(i).len(23 downto 0));
      end loop;

      if MODE = 0 then
        file_open(st, cf, dir.all & "/" & CMDF, read_mode);
        assert st = open_ok report "cannot open " & dir.all & "/" & CMDF severity failure;
      else
        write(wl, "DRAW " & dir.all & " " & integer'image(nel));
        writeline(lf, wl);
      end if;

      -- Reset.
      reset <= '1';
      wcnt <= (others => '0');
      de_credit_ok <= '0';
      if MODE = 2 then
        ar_i := (others => '0');
      else
        ar_i := X"40000000";
      end if;
      a_r <= ar_i;
      for i in 1 to 4 loop
        wait until rising_edge(clk);
      end loop;
      wait for 1 ns;
      reset <= '0';

      wp := 0; rp := 0; fp_cur := 0; fp_old := 0;
      cred := ELQ_CRED;
      t := 0; idle := 0; ncmd := 0;
      prev_last := false;
      done_d := false;
      tail := 0;
      s_cok := '0';

      loop
        -- Drive the inputs of cycle t.
        nw := 0;
        if wp < nel then
          if MODE = 0 or pct(FEED_PCT) then
            if MODE = 0 then
              nw := 4;
            else
              nw := 1 + integer(floor(urand * 4.0));
              if nw > 4 then nw := 4; end if;
            end if;
            if nw > cred then nw := cred; end if;
            if nw > nel - wp then nw := nel - wp; end if;
          end if;
        end if;
        for j in 0 to 3 loop
          if j < nw then
            el(j) <= els(wp + j);
          else
            el(j) <= ELEMENT_INIT;
          end if;
        end loop;
        wcnt <= to_unsigned(nw, 3);
        wp := wp + nw;
        cred := cred - nw;
        if MODE = 0 and t < 16 then
          s_cok := '0';
        elsif pct(CRED_PCT) then
          s_cok := '0';
        else
          s_cok := '1';
        end if;
        de_credit_ok <= s_cok;
        if MODE = 2 then
          ar_i := ar_i + to_unsigned(integer(floor(urand * real(AR_STEP))), 32);
          a_r <= ar_i;
        end if;
        s_ar := ar_i;

        -- Edge t -> t+1: sample the cycle-t combinational outputs first.
        wait until rising_edge(clk);
        s_rpadv := to_integer(rp_adv);
        s_pfadv := to_integer(pf_adv);
        s_cret  := to_integer(cred_ret);
        wait for 1 ns;

        -- cmd now holds the decision of cycle t.
        dec := s_cok = '1' and not prev_last;
        if dec then
          tot_dec := tot_dec + 1;
        end if;
        if cmd.valid = '1' then
          if not dec then
            good := false;
            report "ERROR draw " & dir.all & ": command issued at t=" & integer'image(t)
                   & " without credit or in a bubble" severity error;
          end if;
          if done_d then
            good := false;
            report "ERROR draw " & dir.all & ": command after the last element" severity error;
          end if;
          if s_rpadv /= to_integer(cmd.c) then
            good := false;
            report "ERROR draw " & dir.all & ": rp_adv " & integer'image(s_rpadv)
                   & " /= cmd.c " & integer'image(to_integer(cmd.c)) severity error;
          end if;
          if MODE = 0 then
            check_cmd(cmd);
          end if;
          ncmd := ncmd + 1;
          idle := 0;
        else
          if s_rpadv /= 0 then
            good := false;
            report "ERROR draw " & dir.all & ": rp_adv without a command" severity error;
          end if;
          idle := idle + 1;
        end if;
        if MODE /= 0 and dec then
          write(wl, integer'image(t) & " " & integer'image(fp_old) & " "
                    & to_hstring(std_logic_vector(s_ar)) & " ");
          if cmd.valid = '1' then
            write(wl, string'("1 "));
            write_cmd(wl, cmd);
          else
            write(wl, string'("0"));
          end if;
          writeline(lf, wl);
        end if;
        if not good then
          derrs := derrs + 1;
          good := true;
          errs := errs + 1;
        end if;

        prev_last := cmd.valid = '1' and cmd.last = '1';
        rp := rp + s_rpadv;
        fp_old := fp_cur;
        fp_cur := fp_cur + s_pfadv;
        cred := cred + s_cret;
        t := t + 1;

        if rp = nel then
          done_d := true;
        end if;
        if done_d then
          tail := tail + 1;
          exit when tail > 8;
        end if;
        if idle > 30000 then
          errs := errs + 1;
          report "ERROR draw " & dir.all & ": deadlock, no command for 30000 cycles at rp="
                 & integer'image(rp) & " of " & integer'image(nel) severity error;
          exit;
        end if;
        if t > maxcyc then
          errs := errs + 1;
          report "ERROR draw " & dir.all & ": cycle limit " & integer'image(maxcyc)
                 & " reached at rp=" & integer'image(rp) & " of " & integer'image(nel) severity error;
          exit;
        end if;
        exit when derrs > 12;
      end loop;

      if MODE = 0 then
        loop
          exit when endfile(cf);
          readline(cf, ln);
          if ln'length > 0 and ln.all(ln'low) /= '#' then
            errs := errs + 1;
            report "MISMATCH draw " & dir.all & ": " & CMDF & " has more commands than the RTL issued ("
                   & integer'image(ncmd) & ")" severity error;
            exit;
          end if;
        end loop;
        file_close(cf);
      else
        write(wl, string'("END ") & integer'image(t));
        writeline(lf, wl);
      end if;
      report "DRAW " & dir.all & " elements=" & integer'image(nel) & " commands="
             & integer'image(ncmd) & " cycles=" & integer'image(t)
             & " errors=" & integer'image(derrs);
      tot_cmd := tot_cmd + ncmd;
      tot_el := tot_el + nel;
      deallocate(els);
    end loop;

    if MODE /= 0 then
      file_close(lf);
    end if;
    if errs = 0 then
      report "TB_PASS mode=" & integer'image(MODE) & " draws=" & integer'image(ndraw)
             & " elements=" & integer'image(tot_el) & " commands=" & integer'image(tot_cmd)
             & " decision_cycles=" & integer'image(tot_dec);
    else
      report "TB_FAIL errors=" & integer'image(errs) & " draws=" & integer'image(ndraw);
    end if;
    running <= false;
    wait;
  end process;

end sim;
