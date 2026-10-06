-- SPDX-FileCopyrightText: 2026 Daniel J. Mazure
-- SPDX-License-Identifier: MIT
--
-- rr_rea_cdc — clock-domain crossing helpers for the REA IP.
--
-- Two reusable primitives:
--
--   rr_rea_sync_word    — two-flop synchronizer for a multi-bit
--                         "slow-changing" signal (config that's
--                         latched on arm, status flags). The user
--                         is responsible for keeping the source
--                         stable for ≥2 dest-clock periods around
--                         any sample point — typical for static
--                         config registers.
--
--   rr_rea_pulse_xfer   — toggle-pulse-coupled CDC. Source side
--                         flips a bit on each pulse; dest side
--                         two-flop-syncs the toggle and edge-
--                         detects to produce a single-cycle pulse.
--                         Survives arbitrary clock-ratio sampling.
--
-- Both pinned by REA-REQ-020 / 021.

library ieee;
    use ieee.std_logic_1164.all;
    use ieee.numeric_std.all;

-- ── Two-flop word synchronizer ────────────────────────────────────────

library ieee;
    use ieee.std_logic_1164.all;

entity rr_rea_sync_word is
    generic (
        G_WIDTH : positive := 32
    );
    port (
        dst_clk : in  std_logic;
        din     : in  std_logic_vector(G_WIDTH - 1 downto 0);
        dout    : out std_logic_vector(G_WIDTH - 1 downto 0)
    );
end entity;

architecture rtl of rr_rea_sync_word is
    signal s1 : std_logic_vector(G_WIDTH - 1 downto 0) := (others => '0');
    signal s2 : std_logic_vector(G_WIDTH - 1 downto 0) := (others => '0');
    -- Vendor-specific synthesis attributes. Vivado/Quartus both honor
    -- the canonical "ASYNC_REG" attribute on the destination flops to
    -- group them into the same slice and disable timing analysis on
    -- the source path. Harmless to other tools.
    attribute ASYNC_REG : string;
    attribute ASYNC_REG of s1 : signal is "TRUE";
    attribute ASYNC_REG of s2 : signal is "TRUE";
begin
    process (dst_clk)
    begin
        if rising_edge(dst_clk) then
            s1 <= din;
            s2 <= s1;
        end if;
    end process;
    dout <= s2;
end architecture;

-- ── Toggle-pulse cross-domain transfer ────────────────────────────────

library ieee;
    use ieee.std_logic_1164.all;

entity rr_rea_pulse_xfer is
    port (
        src_toggle : in  std_logic;     -- toggle level on src_clk
                                        -- (caller flips it on each event)
        dst_clk    : in  std_logic;
        dst_rst    : in  std_logic;
        dst_pulse  : out std_logic      -- 1-cycle pulse on dst_clk
                                        -- per source-side toggle edge
    );
end entity;

architecture rtl of rr_rea_pulse_xfer is
    signal s1, s2, s3 : std_logic := '0';
    attribute ASYNC_REG : string;
    attribute ASYNC_REG of s1 : signal is "TRUE";
    attribute ASYNC_REG of s2 : signal is "TRUE";
begin
    -- Destination: two-flop sync the toggle level, then one extra
    -- register for edge detect. Pulse out for one dst_clk per
    -- transition of the source toggle.
    process (dst_clk, dst_rst)
    begin
        if dst_rst = '1' then
            s1 <= '0'; s2 <= '0'; s3 <= '0';
        elsif rising_edge(dst_clk) then
            s1 <= src_toggle;
            s2 <= s1;
            s3 <= s2;
        end if;
    end process;

    dst_pulse <= s2 xor s3;
end architecture;
