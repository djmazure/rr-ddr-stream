-- SPDX-FileCopyrightText: 2026 Daniel J. Mazure
-- SPDX-License-Identifier: MIT
--
-- rr_rea_jtag_iface — vendor-neutral BSCAN → register-bus bridge.
--
-- Implements the frozen 49-bit DR JTAG register protocol so the
-- existing REA host SW (Analyzer / XilinxHwServerTransport) connects
-- unmodified. The protocol is the SW interface contract:
--
--   49-bit DR (LSB-first on the wire):
--     bits[31:0]  — wdata / rdata
--     bits[47:32] — addr[15:0]
--     bits[48]    — rnw (1 = write, 0 = read)
--
--   Phases driven by the vendor-specific TAP wrapper:
--     CAPTURE   — load reg_rdata into sr[31:0]
--     SHIFT     — sr <= {tdi, sr[48:1]}
--     UPDATE    — decode sr[48]: write → reg_wr_en; read → reg_rd_en
--                 (with reg_addr / reg_wdata committed from the sr)
--
-- v0.1 implements only the USER1 control chain — the host's
-- `read_block` falls back to USER1 reads when USER2 burst isn't
-- present, so dpram readout works at full speed/cycle even without
-- the burst path. USER2 burst is a v0.2 feature (rr_rea_jtag_burst).
--
-- Tested via REA-REQ-001..003. The TAP signals are driven directly
-- by the cocotb testbench, mocking the BSCAN hard macro.

library ieee;
    use ieee.std_logic_1164.all;
    use ieee.numeric_std.all;

entity rr_rea_jtag_iface is
    port (
        arst       : in  std_logic;

        -- ── TAP signals (from vendor-specific BSCAN wrapper) ─────
        tck        : in  std_logic;
        tdi        : in  std_logic;
        tdo        : out std_logic;
        capture    : in  std_logic;
        shift_en   : in  std_logic;
        update     : in  std_logic;
        sel        : in  std_logic;

        -- ── Register bus (drives rr_rea_regbank) ─────────────────
        reg_clk    : out std_logic;
        reg_rst    : out std_logic;
        reg_wr_en  : out std_logic;
        reg_rd_en  : out std_logic;
        reg_addr   : out std_logic_vector(15 downto 0);
        reg_wdata  : out std_logic_vector(31 downto 0);
        reg_rdata  : in  std_logic_vector(31 downto 0)
    );
end entity;

architecture rtl of rr_rea_jtag_iface is

    signal sr        : std_logic_vector(48 downto 0) := (others => '0');
    signal reg_wr_en_r : std_logic := '0';
    signal reg_rd_en_r : std_logic := '0';
    signal reg_addr_r  : std_logic_vector(15 downto 0) := (others => '0');
    signal reg_wdata_r : std_logic_vector(31 downto 0) := (others => '0');

begin

    tdo       <= sr(0);
    reg_clk   <= tck;
    reg_rst   <= arst;
    reg_wr_en <= reg_wr_en_r;
    reg_rd_en <= reg_rd_en_r;
    reg_addr  <= reg_addr_r;
    reg_wdata <= reg_wdata_r;

    process (tck, arst)
    begin
        if arst = '1' then
            sr          <= (others => '0');
            reg_wr_en_r <= '0';
            reg_rd_en_r <= '0';
            reg_addr_r  <= (others => '0');
            reg_wdata_r <= (others => '0');

        elsif rising_edge(tck) then
            -- Default: pulse signals are 1-cycle.
            reg_wr_en_r <= '0';
            reg_rd_en_r <= '0';

            if sel = '1' then
                if capture = '1' then
                    -- CAPTURE: load current rdata into low half of sr.
                    sr(31 downto 0) <= reg_rdata;
                elsif shift_en = '1' then
                    -- SHIFT: shift LSB-first toward TDO.
                    sr <= tdi & sr(48 downto 1);
                elsif update = '1' then
                    -- UPDATE: decode rnw bit and pulse the bus.
                    if sr(48) = '1' then
                        reg_addr_r  <= sr(47 downto 32);
                        reg_wdata_r <= sr(31 downto 0);
                        reg_wr_en_r <= '1';
                    else
                        reg_addr_r  <= sr(47 downto 32);
                        reg_rd_en_r <= '1';
                    end if;
                end if;
            end if;
        end if;
    end process;

end architecture;
