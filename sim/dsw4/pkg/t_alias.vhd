library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.vhsnunzip_dsw4_pkg.all;
entity t_only is end entity;
architecture a of t_only is
  signal b1, b2 : byte_array(0 to 3) := (others => X"00");
  signal rc : ram_command;
  signal rr : ram_response_array(0 to 31);
  signal es : element_stream := ELEMENT_STREAM_INIT;
  signal ds : decompressed_stream := DECOMPRESSED_STREAM_INIT;
  signal eq : boolean;
begin
  eq <= b1 = b2;
  process
    variable p : prec_t;
    variable bb : byte_array(0 to 4) := (X"F0", X"01", X"02", X"03", X"04");
  begin
    p := hdr_decode(bb);
    assert p.kind = K_LIT and p.far = '1' and p.hdr = 2 report "far lit" severity failure;
    bb(0) := X"0A";   -- tag 10, m=2: CPY hdr 3 len 3
    p := hdr_decode(bb);
    assert p.kind = K_CPY and p.hdr = 3 and p.len = 3 and p.off = X"0201" report "tag10" severity failure;
    bb(0) := X"E5";   -- tag 01: len 4+1=5, off = 7<<8|01
    p := hdr_decode(bb);
    assert p.kind = K_CPY and p.hdr = 2 and p.len = 5 and p.off = X"0701" report "tag01" severity failure;
    bb(0) := X"EC";   -- tag 00, m=59: LIT len 60
    p := hdr_decode(bb);
    assert p.kind = K_LIT and p.far = '0' and p.hdr = 1 and p.len = 60 report "lit60" severity failure;
    bb(0) := X"FF";   -- tag 11 m=63 len 64 hdr 5
    p := hdr_decode(bb);
    assert p.kind = K_CPY and p.hdr = 5 and p.len = 64 report "tag11" severity failure;
    assert sat(to_unsigned(200, 9), 7) = 127 and sat(to_unsigned(100, 9), 7) = 100 report "sat" severity failure;
    assert sat(to_unsigned(5, 3), 6) = 5 report "sat widen" severity failure;
    assert mod32(to_unsigned(70, 18)) = 6 report "mod32" severity failure;
    assert ga_sdiff(to_unsigned(5, 32), X"FFFFFFFE") = 7 report "sdiff" severity failure;
    assert ga_sdiff(X"FFFFFFFE", to_unsigned(5, 32)) = -7 report "sdiff2" severity failure;
    report "FUNCS_OK";
    wait;
  end process;
end architecture;

library ieee;
use ieee.std_logic_1164.all;
use work.vhsnunzip_int_pkg.all;
use work.vhsnunzip_dsw4_pkg.all;
entity t_both is end entity;
architecture a of t_both is
  signal b1 : byte_array(0 to 3);
  signal rc : ram_command;
begin
end architecture;
