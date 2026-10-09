library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library work;
use work.vhsnunzip_int_pkg.all;

-- DSW-4 shared records, constants and helper functions (SPEC.md section 7).
--
-- Why a separate package: SPEC section 7 places these declarations in
-- vhsnunzip_int_pkg.vhd, but two of the required names (element_stream and
-- decompressed_stream) are already declared there with i35's fields, and the
-- i35 core (vhsnunzip_pipeline and the decoders) still compiles from that
-- package until build step B2 deletes it. Declaring the DSW-4 versions in the
-- same package would break i35, so they live here. When B2 removes i35, the
-- integrator may move this content into vhsnunzip_int_pkg.vhd (agentic
-- analyse.py reads the element_stream / decompressed_stream advisory counts
-- from that file only).
--
-- How to use: DSW-4 modules write
--     use work.vhsnunzip_dsw4_pkg.all;
-- and NOT "use work.vhsnunzip_int_pkg.all" (both .all clauses together would
-- hide element_stream and decompressed_stream, which both packages declare).
-- The int_pkg names a DSW-4 module needs (byte_array, ram_command,
-- ram_response and their arrays, UNDEF) are re-exported below as aliases. The
-- RAM records themselves stay byte-identical in vhsnunzip_int_pkg.vhd.
--
-- Conventions: lengths are true lengths (not diminished-one). GA = 32-bit
-- global compressed byte address, compared only by modular difference
-- (ga_sdiff). W = 18-bit output position within the chunk.
package vhsnunzip_dsw4_pkg is

  -----------------------------------------------------------------------------
  -- Re-exported from vhsnunzip_int_pkg (same named entities, not copies).
  -----------------------------------------------------------------------------
  alias byte_array         is work.vhsnunzip_int_pkg.byte_array;
  alias ram_command        is work.vhsnunzip_int_pkg.ram_command;
  alias ram_command_array  is work.vhsnunzip_int_pkg.ram_command_array;
  alias ram_response       is work.vhsnunzip_int_pkg.ram_response;
  alias ram_response_array is work.vhsnunzip_int_pkg.ram_response_array;
  alias UNDEF              is work.vhsnunzip_int_pkg.UNDEF;

  -----------------------------------------------------------------------------
  -- Constants (SPEC 7, plus the depths SPEC 3 and 6 state in prose).
  -----------------------------------------------------------------------------
  subtype ga_t is unsigned(31 downto 0);           -- global compressed address

  -- Windowed GA (SPEC 3.1 credit). Every GA the element queue, the writer and
  -- the LWX path carry lives inside the CBUF credit window: all live element
  -- pointers are in [LWX, GB] with GB + 32 - LWX <= CBUF_WIN = 896, and A_r is
  -- GB two cycles old (GB - 64 at worst), so A_r - lptr is in [-64, +896] and
  -- lptr - LWX is in [0, 896]. GAW = 12 bits holds every one of those
  -- differences as a signed value (+-2048), so all of this arithmetic is exact
  -- modulo 2^GAW and the upper 20 bits of a GA are dead weight here.
  constant GAW : natural := 12;
  subtype gaw_t is unsigned(GAW - 1 downto 0);

  constant K_SLOTS    : natural := 4;    -- writer slots per command (KMAX)
  constant LINE_B     : natural := 32;   -- output line / command budget, bytes
  constant ELQ_CRED   : natural := 48;   -- walker -> writer element credits
  constant ELQ_DEPTH  : natural := 64;   -- ELQ physical entries (4 banks x 16)
  constant CBUF_WIN   : natural := 896;  -- CBUF credit: GB + 32 - LWX <= 896
  constant ST_LINES   : natural := 32;   -- ST tier iff D <= 32*ST_LINES - 1
  constant DE_CRED    : natural := 12;   -- writer issues iff de FIFO free >= 12
  constant DE_DEPTH_LOG2 : natural := 5; -- de FIFO 32 x (256 + 6 + 1)
  constant CO_DEPTH_LOG2 : natural := 4; -- CO FIFO 16 x (256 + 1 + 5)
  constant CSF_DEPTH  : natural := 4;    -- chunk start / end FIFOs
  -- Pad blocks per chunk in the block reader. SPEC 3.2 says 2; gold.py (B0)
  -- showed that 2 hangs the last chunk when its end is 16-aligned, so 3.
  constant PAD_BLOCKS : natural := 3;
  constant FAR_NXT    : natural := 127;  -- nxt value meaning "far literal"

  -- prec_t.kind / element_t.kind / wslot_t.kind codes.
  constant K_LIT : std_logic_vector(1 downto 0) := "00";
  constant K_CPY : std_logic_vector(1 downto 0) := "01";
  constant K_END : std_logic_vector(1 downto 0) := "10";  -- prec_t only
  constant K_EOC : std_logic_vector(1 downto 0) := "11";  -- element/wslot only

  -- agcmd_t.tier codes.
  constant T_NONE : std_logic_vector(1 downto 0) := "00";
  constant T_ST   : std_logic_vector(1 downto 0) := "01";
  constant T_LT   : std_logic_vector(1 downto 0) := "10";
  constant T_LIT  : std_logic_vector(1 downto 0) := "11";

  -- xcmd_t.ext_sel codes (S2a external preselect).
  constant X_LTE  : std_logic_vector(1 downto 0) := "00";
  constant X_LTO  : std_logic_vector(1 downto 0) := "01";
  constant X_LITA : std_logic_vector(1 downto 0) := "10";
  constant X_LITB : std_logic_vector(1 downto 0) := "11";

  -----------------------------------------------------------------------------
  -- Array helper types used by the records (named in SPEC 7, defined here).
  -----------------------------------------------------------------------------
  type ga_arr    is array (natural range <>) of ga_t;
  type slv2_arr  is array (natural range <>) of std_logic_vector(1 downto 0);
  type u3_arr    is array (natural range <>) of unsigned(2 downto 0);
  type u4_arr    is array (natural range <>) of unsigned(3 downto 0);
  type u5_arr    is array (natural range <>) of unsigned(4 downto 0);
  type u6_arr    is array (natural range <>) of unsigned(5 downto 0);
  type u7_arr    is array (natural range <>) of unsigned(6 downto 0);
  -- 2-D arrays, first index = slot k (0 to 3), second = lane j (0 to 31).
  type u4_arr2   is array (natural range <>, natural range <>) of unsigned(3 downto 0);
  type u5_arr2   is array (natural range <>, natural range <>) of unsigned(4 downto 0);
  type slv2_arr2 is array (natural range <>, natural range <>) of std_logic_vector(1 downto 0);
  -- Per-slot vector over the 16 pairs p (bit p = pair p).
  type slv_arr16 is array (natural range <>) of std_logic_vector(0 to 15);

  -----------------------------------------------------------------------------
  -- CBUF input beat (CO FIFO output). SPEC 3.1.
  -----------------------------------------------------------------------------
  type cbeat_t is record
    valid     : std_logic;
    last      : std_logic;                 -- last beat of a chunk
    data      : byte_array(0 to 31);       -- byte 0 = co_data(7:0)
    endi      : unsigned(4 downto 0);      -- index of the last valid byte
  end record;

  constant CBEAT_INIT : cbeat_t := (
    valid => '0', last => '0', data => (others => X"00"), endi => (others => '0'));

  -----------------------------------------------------------------------------
  -- Block reader output (one 16-byte block per cycle). SPEC 3.2.
  -----------------------------------------------------------------------------
  type blk_t is record
    valid     : std_logic;
    first     : std_logic;                 -- first block of a chunk
    epoch     : unsigned(1 downto 0);
    base      : ga_t;                      -- GA of byte 0 (16-aligned)
    data      : byte_array(0 to 15);
    nvalid    : unsigned(4 downto 0);      -- 0..16 bytes before the END
  end record;

  constant BLK_INIT : blk_t := (
    valid => '0', first => '0', epoch => (others => '0'), base => (others => '0'),
    data => (others => X"00"), nvalid => (others => '0'));

  -----------------------------------------------------------------------------
  -- PT1 per-position record. SPEC 3.3.
  -----------------------------------------------------------------------------
  type prec_t is record
    kind      : std_logic_vector(1 downto 0);  -- 00 LIT 01 CPY 10 END
    hdr       : unsigned(2 downto 0);          -- header bytes 1..5
    len       : unsigned(6 downto 0);          -- slen (near LIT) or copy len
    off       : unsigned(15 downto 0);         -- copy offset
    far       : std_logic;                     -- LIT with a 2..5-byte header
  end record;

  type prec_arr is array (natural range <>) of prec_t;

  constant PREC_INIT : prec_t := (
    kind => K_END, hdr => (others => '0'), len => (others => '0'),
    off => (others => '0'), far => '0');

  -----------------------------------------------------------------------------
  -- Window (WA + PT1..PT3 output, WFIFO entry). SPEC 3.2, 3.3.
  -----------------------------------------------------------------------------
  type win_t is record
    valid     : std_logic;
    first     : std_logic;                 -- block b is a chunk's first block
    epoch     : unsigned(1 downto 0);
    base      : ga_t;                      -- GA of block b (16-aligned)
    x         : byte_array(0 to 35);       -- bytes >= endrel forced to 0
    endrel    : unsigned(5 downto 0);      -- first END position, 63 = none
    vlen      : unsigned(2 downto 0);      -- varint length (first window)
    -- No per-position prec_arr: rec(i) = hdr_decode(x(i to i+2)) for every
    -- i < endrel, so the window carries x only and the walker decodes the
    -- (at most 4) positions it actually emits. Storing all 32 records cost
    -- 928 bits of WFIFO and a 32:1 29-bit mux per emission slot.
    -- nxt / nxt2 are the PT1 / PT2 hop tables, full 7-bit absolute positions.
    -- They live inside PT only (PT2 composes nxt2 from nxt, PT3 composes nxt4
    -- from nxt2), so the window carries nxt2 only for entries 0..15 and nxt
    -- not at all: see nxr / nok below.
    nxt       : u7_arr(0 to 31);
    nxt2      : u7_arr(0 to 31);
    -- nxr is the walker's hop table: hop6 form of nxt, bit 5 = "leaves this
    -- window" (the absolute value is then only needed for the exit, nxt4),
    -- bits 4..0 = the position inside the window. nok(j) = position nxt(j) is
    -- inside the window AND below endrel, i.e. exactly "the element after the
    -- one at j is still in this window", precomputed here because endrel is a
    -- window constant. The walker hops one level itself with these two tables
    -- (nxt3[e] = nxr[nxt2[e]]), which replaces PT3's 16 hop-3 muxes with one.
    nxr       : u6_arr(0 to 31);
    nok       : std_logic_vector(0 to 31);
    nxt4      : u7_arr(0 to 15);
  end record;

  constant WIN_INIT : win_t := (
    valid => '0', first => '0', epoch => (others => '0'), base => (others => '0'),
    x => (others => X"00"), endrel => (others => '0'), vlen => (others => '0'),
    nxt => (others => (others => '0')), nxt2 => (others => (others => '0')),
    nxr => (others => (others => '0')), nok => (others => '0'),
    nxt4 => (others => (others => '0')));

  -----------------------------------------------------------------------------
  -- Element (parser -> ELQ -> writer). SPEC 3.5, 3.6.
  --   LIT: len = payload bytes (32 b true length), lptr = GA of payload byte 0.
  --   CPY: len, off; lptr = GA of the header.
  --   EOC: len = 0, lptr = GA of the chunk end.
  --
  -- len and off share one 32-bit payload, because no element needs both: a
  -- copy length is at most 64 (so it fits in lo) and a copy has no high length,
  -- while a literal has no offset. Every element therefore carries
  --   lo = len(6 downto 0)                (both kinds)
  --   u  = len(31 downto 7)  for LIT      (zero for CPY / EOC above bit 15)
  --      = off               for CPY      (in u(15 downto 0))
  -- which is 46 stored bits instead of 62 -- the ELQ rotates this payload twice
  -- (write rotate by wp, read rotate by fp), so every bit saved here is two
  -- 4:1 mux bits, and 16 fewer flip-flops per walker element lane.
  -----------------------------------------------------------------------------
  type element_t is record
    valid     : std_logic;
    kind      : std_logic_vector(1 downto 0);  -- 00 LIT 01 CPY 11 EOC
    lo        : unsigned(6 downto 0);          -- len(6 downto 0)
    u         : unsigned(24 downto 0);         -- LIT len(31:7) / CPY off
    lptr      : gaw_t;                         -- GA, windowed (GAW bits)
  end record;

  type element_arr is array (natural range <>) of element_t;

  constant ELEMENT_INIT : element_t := (
    valid => '0', kind => K_LIT, lo => (others => '0'), u => (others => '0'),
    lptr => (others => '0'));

  -----------------------------------------------------------------------------
  -- Writer command (WR -> AG1). SPEC 3.7 "Command record out".
  --
  -- Name element_stream is kept so agentic/analyse.py counts the advisory
  -- slots from the cpK_val / liK_val fields (4 copy, 2 literal).
  --   cpK_val  = slot K holds a copy segment (val and kind = CPY).
  --   li0_val  = a literal segment uses port A (the command's 1st literal),
  --   li1_val  = a literal segment uses port B (the command's 2nd literal).
  -- Per slot (cmds.txt of gold.py, field for field):
  --   val    slot used (EOC included). Unused slots: all other fields 0.
  --   kind   00 LIT, 01 CPY, 11 EOC.
  --   port_b LIT: 1 = port B (2nd literal of the command).
  --   n      bytes placed by this slot (0..32; EOC 0).
  --   s      S_k, in-command start offset (unused/EOC: d_total).
  --   q      CPY effective offset (slot 0: q0; slot k >= 1: off_k), else 0.
  --   lptr   LIT: GA of the first byte placed by this command;
  --          CPY/EOC: the element's header GA.
  -- c and cut are the decision outputs (c = rp advance, EOC included; cut =
  -- slot c placed n > 0); the datapath does not need them, the tb does.
  -----------------------------------------------------------------------------
  type wslot_t is record
    val       : std_logic;
    kind      : std_logic_vector(1 downto 0);
    port_b    : std_logic;
    n         : unsigned(5 downto 0);
    s         : unsigned(5 downto 0);
    q         : unsigned(15 downto 0);
    lptr      : gaw_t;
  end record;

  type wslot_arr is array (0 to 3) of wslot_t;

  constant WSLOT_INIT : wslot_t := (
    val => '0', kind => K_LIT, port_b => '0', n => (others => '0'),
    s => (others => '0'), q => (others => '0'), lptr => (others => '0'));

  type element_stream is record  -- writer command (name kept for analyse.py advisory counts)
    valid     : std_logic;
    last      : std_logic;                 -- command ends the chunk (EOC)
    rep       : std_logic;                 -- slot 0 is a REP copy
    cp0_val   : std_logic;
    cp1_val   : std_logic;
    cp2_val   : std_logic;
    cp3_val   : std_logic;
    li0_val   : std_logic;
    li1_val   : std_logic;
    slot      : wslot_arr;
    d_total   : unsigned(5 downto 0);      -- 0..32
    lw        : gaw_t;                     -- low-water GA (SPEC 3.7, windowed)
    c         : unsigned(2 downto 0);      -- elements completed (0..4)
    cut       : std_logic;
  end record;

  constant ELEMENT_STREAM_INIT : element_stream := (
    valid => '0', last => '0', rep => '0',
    cp0_val => '0', cp1_val => '0', cp2_val => '0', cp3_val => '0',
    li0_val => '0', li1_val => '0',
    slot => (others => WSLOT_INIT), d_total => (others => '0'),
    lw => (others => '0'), c => (others => '0'), cut => '0');

  -----------------------------------------------------------------------------
  -- AG2 output (AG2 -> D1 -> S1). SPEC 3.8. Same fields as gold agcmd.txt,
  -- except adev/adod, which leave AG2 as the URAM read commands.
  -- Fields that do not apply to a slot's tier are 0.
  -----------------------------------------------------------------------------
  type agcmd_t is record  -- AG2 -> D1 -> S1
    valid     : std_logic;
    last      : std_logic;
    rep       : std_logic;
    wl        : unsigned(4 downto 0);      -- Wc(4:0)
    d_total   : unsigned(5 downto 0);
    s1        : unsigned(5 downto 0);      -- unused slots: d_total
    s2        : unsigned(5 downto 0);
    s3        : unsigned(5 downto 0);
    lw        : gaw_t;
    rep_q     : unsigned(4 downto 0);      -- q0(4:0), REP only
    rep_base  : unsigned(4 downto 0);      -- (Wc - q0) mod 32, REP only
    tier      : slv2_arr(0 to 3);          -- 00 none 01 ST 10 LT 11 LIT
    rot       : u5_arr(0 to 3);
    smod      : u5_arr(0 to 3);            -- src(4:0), ST and LT
    dhi       : u5_arr(0 to 3);            -- D(9:5), ST
    dlo       : u5_arr(0 to 3);            -- D(4:0), ST
    l0p       : std_logic_vector(0 to 3);  -- src(5) = L0(0), LT
    litb      : std_logic_vector(0 to 3);  -- LIT slot reads port B
    crowA     : unsigned(4 downto 0);      -- port A: ck(9:5), ck(4:0)
    cmodA     : unsigned(4 downto 0);
    crowB     : unsigned(4 downto 0);      -- port B
    cmodB     : unsigned(4 downto 0);
  end record;

  constant AGCMD_INIT : agcmd_t := (
    valid => '0', last => '0', rep => '0', wl => (others => '0'),
    d_total => (others => '0'), s1 => (others => '0'), s2 => (others => '0'),
    s3 => (others => '0'), lw => (others => '0'), rep_q => (others => '0'),
    rep_base => (others => '0'), tier => (others => T_NONE),
    rot => (others => (others => '0')), smod => (others => (others => '0')),
    dhi => (others => (others => '0')), dlo => (others => (others => '0')),
    l0p => (others => '0'), litb => (others => '0'),
    crowA => (others => '0'), cmodA => (others => '0'),
    crowB => (others => '0'), cmodB => (others => '0'));

  -----------------------------------------------------------------------------
  -- S1 output, per-lane controls (S1 -> S2a -> S2b). SPEC 3.9. Internal to
  -- vhsnunzip_dpath; declared here so the dpath sub-blocks share it.
  -- Two-dimensional fields: (slot k 0 to 3, lane j 0 to 31).
  -----------------------------------------------------------------------------
  type xcmd_t is record  -- S1 -> S2a -> S2b (all per-lane, registered)
    valid     : std_logic;
    last      : std_logic;
    cmpl      : std_logic;                 -- wl + d_total >= 32
    spill     : std_logic;                 -- last and wl + d_total > 32
    wl        : unsigned(4 downto 0);
    cnt_last  : unsigned(5 downto 0);      -- (wl + d_total) mod 32, or 32
    lw        : gaw_t;
    wr        : std_logic_vector(0 to 31); -- rel_j < d_total
    reg       : slv2_arr(0 to 31);         -- #{k in 1..3 : S_k <= rel_j}
    idx       : u4_arr2(0 to 3, 0 to 31);  -- 16:1 rotator index
    sta       : u5_arr2(0 to 3, 0 to 31);  -- short-term SRL address
    ext_sel   : slv2_arr2(0 to 3, 0 to 31);-- X_LTE / X_LTO / X_LITA / X_LITB
    hsa       : slv_arr16(0 to 3);         -- half select, dest group a
    hsb       : slv_arr16(0 to 3);         -- half select, dest group b
    srcst     : std_logic_vector(0 to 3);  -- slot k reads the ST SRLs
    rowA      : u5_arr(0 to 31);           -- CBUF port A row per lane
    rowB      : u5_arr(0 to 31);           -- CBUF port B row per lane
  end record;

  constant XCMD_INIT : xcmd_t := (
    valid => '0', last => '0', cmpl => '0', spill => '0', wl => (others => '0'),
    cnt_last => (others => '0'), lw => (others => '0'), wr => (others => '0'),
    reg => (others => "00"), idx => (others => (others => (others => '0'))),
    sta => (others => (others => (others => '0'))),
    ext_sel => (others => (others => "00")),
    hsa => (others => (others => '0')), hsb => (others => (others => '0')),
    srcst => (others => '0'),
    rowA => (others => (others => '0')), rowB => (others => (others => '0')));

  -----------------------------------------------------------------------------
  -- Decompressed output line (S4 PUSH -> de FIFO -> de port). SPEC 3.12.
  -- data stays byte_array(0 to 31) with literal digits (analyse.py advisory
  -- core_line_bytes). cnt is literal 0..32; de_dvalid = (cnt /= 0).
  -----------------------------------------------------------------------------
  type decompressed_stream is record
    valid     : std_logic;
    last      : std_logic;
    data      : byte_array(0 to 31);
    cnt       : unsigned(5 downto 0);
  end record;

  constant DECOMPRESSED_STREAM_INIT : decompressed_stream := (
    valid => '0', last => '0', data => (others => X"00"), cnt => (others => '0'));

  -----------------------------------------------------------------------------
  -- Helper functions (SPEC 11: hdr_decode, mod32, sat; plus ga_sdiff).
  -----------------------------------------------------------------------------

  -- x saturated to n bits (unsigned): result is unsigned(n-1 downto 0).
  function sat(x : unsigned; n : positive) return unsigned;

  -- x mod 32 as unsigned(4 downto 0) (the low 5 bits; x'length >= 5).
  function mod32(x : unsigned) return unsigned;

  -- Modular signed difference (a - b) mod 2^32 read in [-2^31, 2^31).
  -- Every GA comparison goes through this (SPEC 3 notation).
  function ga_sdiff(a, b : ga_t) return signed;

  -- PT1 header decode of the tag byte b(b'low) and the next two bytes
  -- (SPEC 3.3), without the END rule (positions >= endrel) and without nxt.
  --   tag 00, m = b0(7:2) < 60 : LIT, hdr 1, len = m + 1, far 0
  --   tag 00, m >= 60           : LIT, hdr m - 58 (2..5), len 0, far 1
  --   tag 01 : CPY, hdr 2, len 4 + b0(4:2), off = b0(7:5) & b1
  --   tag 10 : CPY, hdr 3, len 1 + b0(7:2), off = b1 | b2 << 8
  --   tag 11 : CPY, hdr 5, len 1 + b0(7:2), off = b1 | b2 << 8 (low 16 b)
  -- b must hold at least 3 bytes.
  function hdr_decode(b : byte_array) return prec_t;

  -----------------------------------------------------------------------------
  -- WFIFO storage format.
  --
  -- The WFIFO (vhsnunzip_parser) holds WF_DEPTH windows and the walker reads
  -- its head asynchronously. Held as a win_t register array, the head read is
  -- an 8:1 mux over ~1700 bits (~2 LUT/bit, and place-and-route bills them to
  -- the reader, walker_inst); held as one flat vector it is a distributed-RAM
  -- (LUTRAM) array, 1 LUT/bit, with the same asynchronous-read timing.
  --
  -- win_pack carries only the fields the walker reads: valid is regenerated
  -- from the FIFO count, the absolute nxt table is not stored at all (the
  -- walker needs only its hop6 / nok form, nxr / nok, which it also indexes
  -- with nxt2[e] for the hop-3 position), and nxt2 above entry 15 is never
  -- indexed (the walker's e is 4 bits, and PT composes nxt4 before the FIFO).
  -- win_unpack returns the unstored fields as zero. The stored hop state is
  -- 32*6 + 32*1 + 16*7 + 16*7 = 448 bits, the same as the four 16-entry 7-bit
  -- tables it replaces.
  -----------------------------------------------------------------------------
  constant WIN_BITS : natural := 1 + 2 + 32 + 36*8 + 6 + 3 + 32*6 + 32 + 2*16*7;

  function win_pack(w : win_t) return std_logic_vector;
  function win_unpack(v : std_logic_vector) return win_t;

end package vhsnunzip_dsw4_pkg;

package body vhsnunzip_dsw4_pkg is

  function sat(x : unsigned; n : positive) return unsigned is
    variable xn  : unsigned(x'length - 1 downto 0) := x;
    variable res : unsigned(n - 1 downto 0);
  begin
    if xn'length <= n then
      res := resize(xn, n);
    elsif xn(xn'high downto n) /= 0 then
      res := (others => '1');
    else
      res := xn(n - 1 downto 0);
    end if;
    return res;
  end function;

  function mod32(x : unsigned) return unsigned is
    variable xn : unsigned(x'length - 1 downto 0) := x;
  begin
    return xn(4 downto 0);
  end function;

  function ga_sdiff(a, b : ga_t) return signed is
  begin
    return signed(a - b);
  end function;

  function hdr_decode(b : byte_array) return prec_t is
    variable b0, b1, b2 : std_logic_vector(7 downto 0);
    variable m          : unsigned(5 downto 0);
    variable r          : prec_t;
  begin
    b0 := b(b'low);
    b1 := b(b'low + 1);
    b2 := b(b'low + 2);
    m  := unsigned(b0(7 downto 2));
    r  := PREC_INIT;
    case b0(1 downto 0) is
      when "00" =>
        r.kind := K_LIT;
        if m < 60 then
          r.hdr := to_unsigned(1, 3);
          r.len := resize(m, 7) + 1;
          r.far := '0';
        else
          r.hdr := resize(m - 58, 3);
          r.len := (others => '0');
          r.far := '1';
        end if;
      when "01" =>
        r.kind := K_CPY;
        r.hdr  := to_unsigned(2, 3);
        r.len  := resize(unsigned(b0(4 downto 2)), 7) + 4;
        r.off  := "00000" & unsigned(b0(7 downto 5)) & unsigned(b1);
      when "10" =>
        r.kind := K_CPY;
        r.hdr  := to_unsigned(3, 3);
        r.len  := resize(m, 7) + 1;
        r.off  := unsigned(b2) & unsigned(b1);
      when others =>
        r.kind := K_CPY;
        r.hdr  := to_unsigned(5, 3);
        r.len  := resize(m, 7) + 1;
        r.off  := unsigned(b2) & unsigned(b1);
    end case;
    return r;
  end function;

  function win_pack(w : win_t) return std_logic_vector is
    variable v : std_logic_vector(WIN_BITS - 1 downto 0) := (others => '0');
    variable i : natural := 0;
  begin
    v(i)              := w.first;                          i := i + 1;
    v(i + 1 downto i) := std_logic_vector(w.epoch);         i := i + 2;
    v(i + 31 downto i) := std_logic_vector(w.base);         i := i + 32;
    for k in 0 to 35 loop
      v(i + 7 downto i) := w.x(k);                          i := i + 8;
    end loop;
    v(i + 5 downto i) := std_logic_vector(w.endrel);        i := i + 6;
    v(i + 2 downto i) := std_logic_vector(w.vlen);          i := i + 3;
    for k in 0 to 31 loop
      v(i + 5 downto i) := std_logic_vector(w.nxr(k));      i := i + 6;
    end loop;
    for k in 0 to 31 loop
      v(i)              := w.nok(k);                        i := i + 1;
    end loop;
    for k in 0 to 15 loop
      v(i + 6 downto i) := std_logic_vector(w.nxt2(k));     i := i + 7;
    end loop;
    for k in 0 to 15 loop
      v(i + 6 downto i) := std_logic_vector(w.nxt4(k));     i := i + 7;
    end loop;
    return v;
  end function;

  function win_unpack(v : std_logic_vector) return win_t is
    variable w : win_t := WIN_INIT;
    variable i : natural := v'low;
  begin
    w.first  := v(i);                                       i := i + 1;
    w.epoch  := unsigned(v(i + 1 downto i));                i := i + 2;
    w.base   := unsigned(v(i + 31 downto i));               i := i + 32;
    for k in 0 to 35 loop
      w.x(k) := v(i + 7 downto i);                          i := i + 8;
    end loop;
    w.endrel := unsigned(v(i + 5 downto i));                i := i + 6;
    w.vlen   := unsigned(v(i + 2 downto i));                i := i + 3;
    for k in 0 to 31 loop
      w.nxr(k)  := unsigned(v(i + 5 downto i));             i := i + 6;
    end loop;
    for k in 0 to 31 loop
      w.nok(k)  := v(i);                                    i := i + 1;
    end loop;
    for k in 0 to 15 loop
      w.nxt2(k) := unsigned(v(i + 6 downto i));             i := i + 7;
    end loop;
    for k in 0 to 15 loop
      w.nxt4(k) := unsigned(v(i + 6 downto i));             i := i + 7;
    end loop;
    w.valid := '0';
    return w;
  end function;

end package body vhsnunzip_dsw4_pkg;
