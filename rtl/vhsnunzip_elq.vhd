library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library work;
use work.vhsnunzip_dsw4_pkg.all;

-- DSW-4 element queue ELQ (SPEC 3.6).
--
-- 64 entries in 4 banks (entry i in bank i mod 4), 16 per bank, LUTRAM
-- (RAM32M simple dual port: one write and one asynchronous read per bank per
-- cycle). An entry stores kind, len, off, lptr (82 b); valid is implied by the
-- pointers.
--
-- Pointers, 7 b (mod 128, so that 64 entries in flight differ from 0; the
-- bank address is bits 5:2):
--   wp  write: the next entry the walker writes;
--   fp  fetch: the next entry the writer has not yet loaded into its window;
--   rp  retire: elements the writer has completed (credit return, asserts).
--   rp <= fp <= wp, wp - rp <= ELQ_CRED (the walker's credit rule).
--
--   el / wcnt  write el(0 to wcnt-1) at wp .. wp+wcnt-1. Bank b takes el(j)
--              with j = (b - wp) mod 4 when j < wcnt (a 4:1 per bit),
--              wp += wcnt.
--   pf         entries fp .. fp+3, read asynchronously at the REGISTERED fp
--              and rotated by fp mod 4 (each bank is read exactly once).
--              pf(j).valid = 1 iff j < nvis.
--   nvis       wp - fp (0..64), from registers only. An entry written at a
--              clock edge is presented from the next cycle on (LUTRAM write at
--              the edge, asynchronous read after it).
--   pf_adv     entries of pf the writer loads into its window at this clock
--              edge (0..4, <= nvis); fp += pf_adv. Late (depends on the
--              writer decision): a 5:1 select of the precomputed fp + 0..4.
--   rp_adv     elements the writer retired this cycle (its decision c, 0..4);
--              rp += rp_adv.
--   cred_ret   rp_adv registered once, returned to the walker / parser.
--
-- This is SPEC 3.6 with one fix (reported in B-pkg): the SPEC presents PF at
-- rp+8 and validates window entries by a compare against nvis, so an entry
-- that is written after the writer loaded its window slot would never be
-- re-read, and an empty queue at chunk start would never load at all. Here the
-- writer only loads valid entries, from fp, and fp = rp + 8 whenever its
-- window is full, which is the SPEC case.
entity vhsnunzip_elq is
  port (
    clk         : in  std_logic;
    reset       : in  std_logic;

    el          : in  element_arr(0 to 3);
    wcnt        : in  unsigned(2 downto 0);

    pf          : out element_arr(0 to 3);
    nvis        : out unsigned(6 downto 0);
    pf_adv      : in  unsigned(2 downto 0);
    rp_adv      : in  unsigned(2 downto 0);

    cred_ret    : out unsigned(2 downto 0)
  );
end vhsnunzip_elq;

architecture behavior of vhsnunzip_elq is

  -- Stored entry: kind 2 + u 25 + lo 7 + lptr GAW (windowed GA) bits. len and
  -- off share the (u, lo) payload (dsw4_pkg element_t), so the entry is 46 b
  -- instead of 62: 16 bits less through BOTH the write rotate by wp and the
  -- read rotate by fp, and 16 LUTRAM bits less per entry.
  constant EW : natural := 2 + 25 + 7 + GAW;
  subtype ent_t is std_logic_vector(EW - 1 downto 0);
  type bank_t is array (0 to 15) of ent_t;

  signal bank0, bank1, bank2, bank3 : bank_t := (others => (others => '0'));

  signal wp, fp, rp  : unsigned(6 downto 0) := (others => '0');
  signal cret        : unsigned(2 downto 0) := (others => '0');

  signal nvis_i      : unsigned(6 downto 0);
  type fpc_arr is array (0 to 4) of unsigned(6 downto 0);
  signal fpc         : fpc_arr;            -- fp + 0..4 (registers only)
  type ent_arr is array (0 to 3) of ent_t;
  signal rd          : ent_arr;            -- per bank read data
  signal wd          : ent_arr;            -- per bank write data
  signal we          : std_logic_vector(0 to 3);
  type addr_arr is array (0 to 3) of unsigned(3 downto 0);
  signal wa          : addr_arr;
  -- Bank read addresses, registered with fp (bank b holds entry
  -- fp + ((b - fp) mod 4)), so the LUTRAM read address is a register.
  signal ra          : addr_arr := (others => (others => '0'));
  -- fp(5:2) + 0, 1, 2: the only three values any bank read address can take
  -- (see racode).
  type qp_arr is array (0 to 2) of unsigned(3 downto 0);
  signal qp          : qp_arr;

  function rdaddr(f : unsigned(6 downto 0)) return addr_arr is
    variable j   : unsigned(1 downto 0);
    variable idx : unsigned(6 downto 0);
    variable res : addr_arr;
  begin
    for b in 0 to 3 loop
      j := to_unsigned(b, 2) - f(1 downto 0);
      idx := f + resize(j, 7);
      res(b) := idx(5 downto 2);
    end loop;
    return res;
  end function;

  -- rdaddr(f)(b) = f + ((b - f(1:0)) mod 4) rounds f up to the next index
  -- congruent to b modulo 4, so its bank address is just
  --   f(5:2) + (1 when b < f(1:0) else 0)
  -- -- only the carry out of the two low bits ever reaches it. With
  -- f = fp + pf_adv (pf_adv <= 4) that carry is at most 2, so all 20 candidate
  -- addresses are fp(5:2) + 0, 1 or 2: one 4-bit incrementer pair and a 3:1
  -- select per bank, instead of five 7-bit pointer adders feeding twenty more.
  -- racode(m, adv, b) is the 2-bit select, a single LUT of (m, adv, b).
  function racode(m : unsigned(1 downto 0); adv : unsigned(2 downto 0);
                  b : natural) return unsigned is
    variable s : unsigned(3 downto 0);
    variable c : unsigned(1 downto 0);
  begin
    s := resize(m, 4) + resize(adv, 4);
    c := "00";
    if s >= 4 then
      c := c + 1;
    end if;
    if b < to_integer(s(1 downto 0)) then
      c := c + 1;
    end if;
    return c;
  end function;

  function pack(e : element_t) return ent_t is
  begin
    return e.kind & std_logic_vector(e.u) & std_logic_vector(e.lo)
         & std_logic_vector(e.lptr);
  end function;

  function unpack(v : ent_t) return element_t is
    variable e : element_t;
  begin
    e.valid := '1';
    e.kind  := v(GAW + 33 downto GAW + 32);
    e.u     := unsigned(v(GAW + 31 downto GAW + 7));
    e.lo    := unsigned(v(GAW + 6 downto GAW));
    e.lptr  := unsigned(v(GAW - 1 downto 0));
    return e;
  end function;

begin

  nvis_i <= wp - fp;

  fpc_g: for i in 0 to 4 generate
    fpc(i) <= fp + i;
  end generate;
  qp_g: for i in 0 to 2 generate
    qp(i) <= fp(5 downto 2) + i;
  end generate;
  nvis   <= nvis_i;

  -- Write side: bank b takes el((b - wp) mod 4), at address
  -- wp(5:2) + (1 when b < wp(1:0) else 0) (the rdaddr identity, see above).
  wr_map: process (wp, wcnt, el) is
    variable j : unsigned(1 downto 0);
  begin
    for b in 0 to 3 loop
      j := to_unsigned(b, 2) - wp(1 downto 0);
      if b < to_integer(wp(1 downto 0)) then
        wa(b) <= wp(5 downto 2) + 1;
      else
        wa(b) <= wp(5 downto 2);
      end if;
      wd(b) <= pack(el(to_integer(j)));
      if resize(j, 3) < wcnt then
        we(b) <= '1';
      else
        we(b) <= '0';
      end if;
    end loop;
  end process;

  rd(0) <= bank0(to_integer(ra(0)));
  rd(1) <= bank1(to_integer(ra(1)));
  rd(2) <= bank2(to_integer(ra(2)));
  rd(3) <= bank3(to_integer(ra(3)));

  -- PF(j) = entry fp + j, in bank (fp + j) mod 4.
  pf_map: process (fp, rd, nvis_i) is
    variable b : unsigned(1 downto 0);
    variable e : element_t;
  begin
    for j in 0 to 3 loop
      b := fp(1 downto 0) + to_unsigned(j, 2);
      e := unpack(rd(to_integer(b)));
      if to_unsigned(j, 7) < nvis_i then
        e.valid := '1';
      else
        e.valid := '0';
      end if;
      pf(j) <= e;
    end loop;
  end process;

  -- LUTRAM banks: write only, no reset.
  ram_p: process (clk) is
  begin
    if rising_edge(clk) then
      if we(0) = '1' then bank0(to_integer(wa(0))) <= wd(0); end if;
      if we(1) = '1' then bank1(to_integer(wa(1))) <= wd(1); end if;
      if we(2) = '1' then bank2(to_integer(wa(2))) <= wd(2); end if;
      if we(3) = '1' then bank3(to_integer(wa(3))) <= wd(3); end if;
    end if;
  end process;

  ptr_p: process (clk) is
  begin
    if rising_edge(clk) then
      wp   <= wp + resize(wcnt, 7);
      -- fp + pf_adv as a select of precomputed fp + 0..4 (pf_adv is the
      -- writer's late one-hot select; no adder after it).
      case pf_adv is
        when "001"  => fp <= fpc(1);
        when "010"  => fp <= fpc(2);
        when "011"  => fp <= fpc(3);
        when "100"  => fp <= fpc(4);
        when others => fp <= fpc(0);
      end case;
      -- ra = rdaddr(fp + pf_adv), as a 3:1 select of fp(5:2) + 0..2.
      for b in 0 to 3 loop
        ra(b) <= qp(to_integer(racode(fp(1 downto 0), pf_adv, b)));
      end loop;
      rp   <= rp + resize(rp_adv, 7);
      cret <= rp_adv;
      if reset = '1' then
        wp   <= (others => '0');
        fp   <= (others => '0');
        ra   <= rdaddr(to_unsigned(0, 7));
        rp   <= (others => '0');
        cret <= (others => '0');
      end if;
    end if;
  end process;

  cred_ret <= cret;

  -- pragma translate_off
  chk_p: process (clk) is
    variable inflight : unsigned(6 downto 0);
  begin
    if rising_edge(clk) and reset = '0' then
      inflight := (wp + resize(wcnt, 7)) - rp;
      assert inflight <= to_unsigned(ELQ_DEPTH, 7)
        report "ELQ overflow: " & integer'image(to_integer(inflight)) & " entries in flight"
        severity failure;
      assert wcnt <= 4 and pf_adv <= 4 and rp_adv <= 4
        report "ELQ: count above 4" severity failure;
      assert resize(pf_adv, 7) <= nvis_i
        report "ELQ: writer fetched an entry that is not written" severity failure;
      assert resize(rp_adv, 7) <= fp - rp
        report "ELQ: writer retired an entry it has not fetched" severity failure;
      assert ra = rdaddr(fp)
        report "ELQ: registered read addresses do not match fp" severity failure;
      assert wa = rdaddr(wp)
        report "ELQ: write addresses do not match wp" severity failure;
    end if;
  end process;
  -- pragma translate_on

end behavior;
