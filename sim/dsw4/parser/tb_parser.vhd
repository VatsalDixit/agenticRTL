-- tb_parser: DSW-4 B3b table parser unit testbench (adapted from tb_front).
--
--   cs.tv (8-byte granules) -> 32-byte beats (4 granules per beat, a chunk
--   starts on a new beat, endi = index of the last valid byte) -> random
--   source gaps -> vhsnunzip_cofifo -> vhsnunzip_cbuf -> vhsnunzip_parser
--   -> ELQ model (this tb).
--
-- The ELQ model queues every emitted element, compares it field by field
-- (kind, len, off, lptr) with the next line of elements.txt, and drains the
-- queue at a random 0..DRAIN_MAX elements per cycle (DRAIN_PCT = chance a
-- cycle drains at all). A literal at the queue head streams its payload like
-- the writer: at most 32 bytes per cycle, only bytes < a_r (A_r from cbuf),
-- and it retires when the payload is consumed. Drained elements return
-- credits one cycle later (the ELQ's cred_ret = rp_adv registered once).
-- lw = head literal ? its next payload byte : head lptr (held when the queue
-- is empty), fed to cbuf.lwx after LWLAT cycles (LWX of the command in S2a).
--
-- PASS: every element of elements.txt seen in order, nothing extra, queue
-- drained; prints "PARSER_PASS". Any mismatch / timeout: "PARSER_FAIL" and
-- severity failure.
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use ieee.math_real.all;
use std.textio.all;

library work;
use work.vhsnunzip_dsw4_pkg.all;

entity tb_parser is
  generic (
    DIR        : string  := ".";
    SEED       : positive := 1;
    SRC_PCT    : natural := 100;   -- chance per cycle that the source offers a beat
    DRAIN_PCT  : natural := 100;   -- chance per cycle that the ELQ model drains
    DRAIN_MAX  : natural := 4;     -- max elements retired per draining cycle
    LWLAT      : positive := 10;   -- cycles from lw to cbuf.lwx
    TIMEOUT    : natural := 200000; -- cycles without an emitted element
    GRP        : string  := "-";    -- walker group trace file ("-" = none)
    RETGT      : boolean := false;  -- TEST_RETGT
    DRAIN_FULL : boolean := false;  -- drain DRAIN_MAX every cycle (unthrottled)
    WFD        : positive := 8      -- WFIFO depth (WF_DEPTH; B3c default 8)
  );
end tb_parser;

architecture sim of tb_parser is

  signal clk     : std_logic := '0';
  signal reset   : std_logic := '1';
  signal done    : boolean := false;
  signal done_d  : boolean := false;

  signal co_in   : cbeat_t := CBEAT_INIT;
  signal co_in_ready : std_logic;
  signal co      : cbeat_t;
  signal co_ready: std_logic;
  signal lwx     : ga_t := (others => '0');
  signal gb, a_r : ga_t;
  signal csf_valid, cef_valid, csf_pop, cef_pop : std_logic;
  signal csf_ga, cef_ga : ga_t;
  signal rowA, rowB, rowC : u5_arr(0 to 31) := (others => (others => '0'));
  signal litA, litB, lanesC : byte_array(0 to 31);
  signal el      : element_arr(0 to 3);
  signal wcnt    : unsigned(2 downto 0);
  signal cred_ret: unsigned(2 downto 0) := "000";
  signal cred    : unsigned(6 downto 0);

begin

  done_d <= done after 20 ns;
  clk <= not clk after 5 ns when not done_d else '0';

  fifo_inst: entity work.vhsnunzip_cofifo
    port map (clk => clk, reset => reset, wr => co_in, wr_ready => co_in_ready,
              rd => co, rd_ready => co_ready, level => open);

  cbuf_inst: entity work.vhsnunzip_cbuf
    port map (clk => clk, reset => reset, co => co, co_ready => co_ready,
              lwx => lwx, gb => gb, a_r => a_r,
              csf_valid => csf_valid, csf_ga => csf_ga, csf_pop => csf_pop,
              cef_valid => cef_valid, cef_ga => cef_ga, cef_pop => cef_pop,
              rowA => rowA, litA => litA, rowB => rowB, litB => litB,
              rowC => rowC, lanesC => lanesC);

  parse_inst: entity work.vhsnunzip_parser
    generic map (TEST_RETGT => RETGT, WF_DEPTH => WFD, GRP_FILE => GRP)
    port map (clk => clk, reset => reset, gb => gb,
              csf_valid => csf_valid, csf_ga => csf_ga, csf_pop => csf_pop,
              cef_valid => cef_valid, cef_ga => cef_ga, cef_pop => cef_pop,
              rowC => rowC, lanesC => lanesC,
              el => el, wcnt => wcnt, cred_ret => cred_ret, cred => cred);

  -- Source: cs.tv -> beats.
  src_proc: process is
    file fin       : text;
    variable ln    : line;
    variable s     : string(1 to 68);
    variable ng    : natural;
    variable beat  : cbeat_t;
    variable gend  : natural;
    variable glast : boolean;
    variable s1, s2: positive := 7;
    variable rnd   : real;
    variable nbeat : natural := 0;
    variable gstat : file_open_status;
  begin
    s1 := SEED; s2 := SEED * 7 + 3;
    co_in <= CBEAT_INIT;
    file_open(gstat, fin, DIR & "/cs.tv", read_mode);
    assert gstat = open_ok report "PARSER_FAIL cannot open " & DIR & "/cs.tv" severity failure;
    wait until rising_edge(clk) and reset = '0';
    while not endfile(fin) loop
      -- Assemble one beat.
      beat := CBEAT_INIT;
      ng := 0;
      glast := false;
      while ng < 4 and not glast and not endfile(fin) loop
        readline(fin, ln);
        if ln'length < 68 then
          next;
        end if;
        read(ln, s);
        for i in 0 to 7 loop
          for k in 0 to 7 loop
            if s(i * 8 + k + 1) = '1' then
              beat.data(ng * 8 + i)(7 - k) := '1';
            else
              beat.data(ng * 8 + i)(7 - k) := '0';
            end if;
          end loop;
        end loop;
        gend := 0;
        for k in 0 to 2 loop
          gend := gend * 2;
          if s(66 + k) = '1' then gend := gend + 1; end if;
        end loop;
        glast := s(65) = '1';
        if not glast then
          assert gend = 7 report "PARSER_FAIL non-last granule with endi /= 7" severity failure;
        end if;
        beat.endi := to_unsigned(ng * 8 + gend, 5);
        ng := ng + 1;
      end loop;
      exit when ng = 0;
      assert ng = 4 or glast report "PARSER_FAIL cs.tv ends mid-chunk" severity failure;
      beat.valid := '1';
      if glast then beat.last := '1'; end if;
      -- Random source gaps.
      loop
        uniform(s1, s2, rnd);
        exit when rnd * 100.0 < real(SRC_PCT);
        co_in <= CBEAT_INIT;
        wait until rising_edge(clk);
      end loop;
      co_in <= beat;
      wait until rising_edge(clk) and co_in_ready = '1';
      nbeat := nbeat + 1;
    end loop;
    co_in <= CBEAT_INIT;
    report "source done: " & integer'image(nbeat) & " beats";
    wait;
  end process;

  -- ELQ model, element checker, lw / credit return.
  sink_proc: process is
    file fel       : text;
    variable ln    : line;
    variable gstat : file_open_status;
    variable kind4 : std_logic_vector(3 downto 0);
    variable len32 : std_logic_vector(31 downto 0);
    variable off16 : std_logic_vector(15 downto 0);
    variable lptr32: std_logic_vector(31 downto 0);
    variable pos32 : std_logic_vector(31 downto 0);
    variable hdr4  : std_logic_vector(3 downto 0);
    variable chk16 : std_logic_vector(15 downto 0);
    variable exp_n : natural := 0;
    variable exp_total : natural := 0;
    variable got_n : natural := 0;
    variable more  : boolean;
    type q_t is array (0 to 127) of element_t;
    variable q     : q_t;
    variable qh, qt, qn : natural := 0;
    variable lp    : ga_t := (others => '0');   -- head literal next byte
    variable rem_v : unsigned(31 downto 0) := (others => '0');
    variable hload : boolean := false;
    variable lw    : ga_t := (others => '0');
    variable lw_ret: ga_t := (others => '0');
    type lw_pipe_t is array (0 to LWLAT - 1) of ga_t;
    variable lwp   : lw_pipe_t := (others => (others => '0'));
    variable s1, s2: positive := 1;
    variable rnd   : real;
    variable budget, ndrain : natural;
    variable avail : integer;
    variable take  : natural;
    variable cyc, idle : natural := 0;
    variable e     : element_t;
    variable ok    : boolean;
    variable fail  : boolean := false;
    variable stop_drain : boolean;

    -- Advance fel to the next data line; false at end of file.
    procedure next_line(variable found : out boolean) is
    begin
      found := false;
      while not endfile(fel) loop
        readline(fel, ln);
        if ln'length > 0 and ln(ln'low) /= '#' then
          found := true;
          return;
        end if;
      end loop;
    end procedure;
  begin
    s1 := SEED * 13 + 5; s2 := SEED * 3 + 11;
    file_open(gstat, fel, DIR & "/elements.txt", read_mode);
    assert gstat = open_ok report "PARSER_FAIL cannot open elements.txt" severity failure;
    -- First pass: count the expected elements.
    exp_total := 0;
    loop
      next_line(more);
      exit when not more;
      exp_total := exp_total + 1;
    end loop;
    file_close(fel);
    file_open(gstat, fel, DIR & "/elements.txt", read_mode);
    reset <= '1';
    for i in 0 to 4 loop
      wait until rising_edge(clk);
    end loop;
    reset <= '0';
    loop
      wait until rising_edge(clk);
      cyc := cyc + 1;
      idle := idle + 1;

      -- 1. Elements emitted this cycle (wcnt/el registered in the parser).
      for k in 0 to 3 loop
        if k < to_integer(wcnt) then
          e := el(k);
          got_n := got_n + 1;
          idle := 0;
          next_line(more);
          if not more then
            report "PARSER_FAIL extra element #" & integer'image(got_n) & " kind "
                   & to_hstring(e.kind) & " lptr " & to_hstring(e.lptr) severity error;
            fail := true;
          else
            hread(ln, kind4); hread(ln, len32); hread(ln, off16); hread(ln, lptr32);
            hread(ln, pos32); hread(ln, hdr4); hread(ln, chk16);
            exp_n := exp_n + 1;
            ok := e.valid = '1' and e.kind = kind4(1 downto 0) and e.len = unsigned(len32)
                  and e.off = unsigned(off16) and e.lptr = unsigned(lptr32);
            if not ok then
              report "PARSER_FAIL element #" & integer'image(got_n) & " (chunk "
                     & to_hstring(chk16) & " pos " & to_hstring(pos32) & ")"
                     & " got kind " & to_hstring(e.kind) & " len " & to_hstring(e.len)
                     & " off " & to_hstring(e.off) & " lptr " & to_hstring(e.lptr)
                     & " valid " & std_logic'image(e.valid)
                     & " expected kind " & to_hstring(kind4) & " len " & to_hstring(len32)
                     & " off " & to_hstring(off16) & " lptr " & to_hstring(lptr32)
                     severity error;
              fail := true;
            end if;
          end if;
          assert qn < 128 report "PARSER_FAIL tb queue overflow" severity failure;
          q(qt) := e; qt := (qt + 1) mod 128; qn := qn + 1;
          if qn > ELQ_CRED then
            report "PARSER_FAIL more than ELQ_CRED elements outstanding" severity error;
            fail := true;
          end if;
        end if;
      end loop;
      exit when fail;

      -- 2. Drain (the writer side).
      ndrain := 0;
      uniform(s1, s2, rnd);
      if rnd * 100.0 < real(DRAIN_PCT) then
        uniform(s1, s2, rnd);
        budget := integer(floor(rnd * real(DRAIN_MAX + 1)));
        if DRAIN_FULL then budget := DRAIN_MAX; end if;
        if budget > DRAIN_MAX then budget := DRAIN_MAX; end if;
        stop_drain := false;
        while ndrain < budget and qn > 0 and not stop_drain loop
          if not hload then
            lp := q(qh).lptr;
            rem_v := q(qh).len;
            hload := true;
          end if;
          if q(qh).kind = K_LIT and rem_v /= 0 then
            avail := to_integer(ga_sdiff(a_r, lp));
            if avail > 0 then
              take := 32;
              if avail < take then take := avail; end if;
              if rem_v < take then take := to_integer(rem_v); end if;
              lp := lp + take;
              rem_v := rem_v - take;
            end if;
            stop_drain := true;   -- one literal piece per cycle
          end if;
          if q(qh).kind /= K_LIT or rem_v = 0 then
            -- lw of a retired element (used while the queue is empty):
            -- literal: its payload end; copy / EOC: its header GA.
            if q(qh).kind = K_LIT then lw_ret := lp; else lw_ret := q(qh).lptr; end if;
            qh := (qh + 1) mod 128; qn := qn - 1; ndrain := ndrain + 1;
            hload := false;
          end if;
        end loop;
      end if;
      cred_ret <= to_unsigned(ndrain, 3);

      -- 3. Low-water mark.
      if qn > 0 then
        if hload and q(qh).kind = K_LIT then
          lw := lp;
        else
          lw := q(qh).lptr;
        end if;
      else
        lw := lw_ret;   -- tb_front held a stale lw here when every element drained at once
      end if;
      lwx <= lwp(LWLAT - 1);
      for i in LWLAT - 1 downto 1 loop
        lwp(i) := lwp(i - 1);
      end loop;
      lwp(0) := lw;

      -- 4. Done / timeout.
      if got_n >= exp_total and qn = 0 then
        wait until rising_edge(clk);
        cred_ret <= "000";
        for i in 0 to 50 loop
          wait until rising_edge(clk);
          if wcnt /= 0 then
            report "PARSER_FAIL extra element after the last expected one" severity error;
            fail := true;
          end if;
        end loop;
        exit;
      end if;
      if idle > TIMEOUT then
        report "PARSER_FAIL timeout: no element for " & integer'image(TIMEOUT)
               & " cycles after " & integer'image(got_n) & " elements; gb "
               & to_hstring(gb) & " lwx " & to_hstring(lwx) & " cred "
               & integer'image(to_integer(cred)) severity error;
        fail := true;
        exit;
      end if;
    end loop;
    if fail then
      report "PARSER_FAIL " & DIR severity failure;
    else
      report "PARSER_PASS elements " & integer'image(got_n) & " cycles " & integer'image(cyc)
             & " " & DIR;
    end if;
    done <= true;
    wait;
  end process;

  -- Coverage from port-level signals: stalls (beat offered, CBUF not
  -- ready) split by whether 4 chunks were pending in CSF (pushed on each
  -- first-of-chunk pop, popped by the parser) or not (CBUF credit), max CSF
  -- occupancy, cycles with the parser out of credits. Reported at the end.
  cov_proc: process (clk) is
    variable n_cred, n_csf, n_pcred, pend, pend_max : natural := 0;
    variable first : boolean := true;
    variable reported : boolean := false;
  begin
    if rising_edge(clk) then
      if reset = '0' then
        if co.valid = '1' and co_ready = '0' then
          if pend >= CSF_DEPTH then n_csf := n_csf + 1; else n_cred := n_cred + 1; end if;
        end if;
        if cred = 0 then n_pcred := n_pcred + 1; end if;
        if co.valid = '1' and co_ready = '1' then
          if first then pend := pend + 1; end if;
          first := co.last = '1';
        end if;
        if csf_pop = '1' then pend := pend - 1; end if;
        if pend > pend_max then pend_max := pend; end if;
      end if;
      if done and not reported then
        reported := true;
        report "PARSER_COV cbuf_credit_stall " & integer'image(n_cred)
               & " csf_full_stall " & integer'image(n_csf)
               & " csf_max " & integer'image(pend_max)
               & " parser_no_credit " & integer'image(n_pcred);
      end if;
    end if;
  end process;

end sim;
