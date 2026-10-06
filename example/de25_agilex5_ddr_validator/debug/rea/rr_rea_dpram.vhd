-- SPDX-FileCopyrightText: 2026 Daniel J. Mazure
-- SPDX-License-Identifier: MIT
--
-- rr_rea_dpram — true dual-port BRAM for the REA capture buffer.
--
-- Port A (sample_clk domain): synchronous write from the capture FSM.
-- Port B (jtag_clk domain): synchronous read for the JTAG burst slave.
-- Both ports also expose a synchronous read on their own clock so
-- the capture FSM can self-inspect (used by tests).
--
-- The intent is BRAM inference on both Xilinx (BRAM18/36) and Intel
-- (M9K/M20K). The pattern below — a single shared signal plus
-- per-port synchronous addr/data registers — is the canonical form
-- both Vivado and Quartus recognise.
--
-- Contracts (see requirements.yml):
--   REA-REQ-200: write A, read B at same addr → din_a appears on
--                dout_b on the next clk_b edge.
--   REA-REQ-201: write A and read B at DIFFERENT addrs in the same
--                cycle → port B sees the previously-written data.
--   REA-REQ-202: WIDTH and DEPTH generics drive the actual storage
--                shape (round-trip at addr=DEPTH-1, din=2^WIDTH-1).

library ieee;
    use ieee.std_logic_1164.all;
    use ieee.numeric_std.all;

library work;
    use work.rr_rea_pkg.all;

entity rr_rea_dpram is
    generic (
        G_WIDTH : positive := 12;
        G_DEPTH : positive := 4096
    );
    port (
        -- Port A (writer)
        clk_a   : in  std_logic;
        we_a    : in  std_logic;
        addr_a  : in  std_logic_vector(clog2(G_DEPTH) - 1 downto 0);
        din_a   : in  std_logic_vector(G_WIDTH - 1 downto 0);
        dout_a  : out std_logic_vector(G_WIDTH - 1 downto 0);
        -- Port B (reader)
        clk_b   : in  std_logic;
        addr_b  : in  std_logic_vector(clog2(G_DEPTH) - 1 downto 0);
        dout_b  : out std_logic_vector(G_WIDTH - 1 downto 0)
    );
end entity;

architecture rtl of rr_rea_dpram is
    type ram_t is array (0 to G_DEPTH - 1)
        of std_logic_vector(G_WIDTH - 1 downto 0);
    shared variable mem : ram_t := (others => (others => '0'));
begin

    -- Port A: synchronous write + synchronous read on clk_a
    process (clk_a)
    begin
        if rising_edge(clk_a) then
            if we_a = '1' then
                mem(to_integer(unsigned(addr_a))) := din_a;
            end if;
            dout_a <= mem(to_integer(unsigned(addr_a)));
        end if;
    end process;

    -- Port B: synchronous read on clk_b (read-only port)
    process (clk_b)
    begin
        if rising_edge(clk_b) then
            dout_b <= mem(to_integer(unsigned(addr_b)));
        end if;
    end process;

end architecture;
