-- SPDX-FileCopyrightText: 2026 Daniel J. Mazure
-- SPDX-License-Identifier: MIT
--
-- rr_rea_jtag_microchip.vhd — Microchip PolarFire / PolarFire SoC UJTAG wrapper for rr_rea_top.
--
-- Adapts rr_rea_top's JTAG datapath to Microchip's UJTAG primitive (PF-T10,
-- docs/backends/libreila_evaluation.md §PF-T10 / RTL-P3.1706).
--
-- In Microchip devices (PolarFire, PolarFire SoC, RTG4, SmartFusion2), the UJTAG
-- macro provides direct fabric access to the dedicated on-chip JTAG TAP controller.
-- Instructions in the range 16..127 (0x10..0x7F) are user-defined.
--
-- Port mapping from UJTAG:
--   UDRCK   -> tck_s (JTAG clock)
--   UTDI    -> tdi_s (JTAG data in)
--   UTDO    <- tdo_s (JTAG data out)
--   UDRCAP  -> capture_s (asserted during Capture-DR state)
--   UDRSH   -> shift_en_s (asserted during Shift-DR state)
--   UDRUPD  -> update_s (asserted during Update-DR state)
--   UIREG   -> uireg_s (current 8-bit instruction register contents)
--   URSTB   -> active-low TAP reset (Test-Logic-Reset), inverted for arst_s
--
-- Selection:
--   sel_s is asserted when UIREG matches the configured user instruction opcode.
--   G_CTRL_CHAIN maps to the user opcode: values >= 16 specify the raw opcode directly;
--   values 1..4 map to standard default user opcodes (1 -> 0x55, 2 -> 0x56, etc.).
--
-- JTAG pass-through pins (TCK, TMS, TDI, TRSTB, TDO) on UJTAG MUST be driven from
-- top-level ports in a real build. The port defaults below (TCK='0', TMS='0', TDI='0',
-- TRSTB='1') exist only so the simulation BFM can leave them unconnected: Libero
-- rejects a tied-off UJTAG pass-through with CMPPF_073 ("net tied off to GND/VCC ...
-- must have a fanout of 1"), measured on the first PolarFire compile of this
-- wrapper (RTL-P2.1279). Promote them to top-level ports (see
-- examples/polarfire_soc_rea_demo); Libero binds ports wired straight to the UJTAG
-- to the dedicated JTAG pads, so no user package pin is assigned. The demo is the
-- on-silicon witness: rr ila capture over the embedded FlashPro5, no extra pins.

library ieee;
    use ieee.std_logic_1164.all;
    use ieee.numeric_std.all;

entity rr_rea_microchip is
    generic (
        G_SAMPLE_W    : positive := 12;
        G_DEPTH       : positive := 4096;
        G_TIMESTAMP_W : natural  := 32;
        G_NUM_CHAN    : positive := 1;
        G_NUM_SOURCE  : positive := 1;  -- RTL-P2.837 write-side source bits
        G_CTRL_CHAIN  : integer  := 1   -- UJTAG user IR opcode or chain index
    );
    port (
        sample_clk_i  : in  std_logic;
        sample_rst_i  : in  std_logic;
        probe_i       : in  std_logic_vector(G_SAMPLE_W - 1 downto 0);
        ext_trigger_i : in  std_logic := '0';
        source_o      : out std_logic_vector(G_NUM_SOURCE - 1 downto 0);
        trigger_o     : out std_logic;
        -- JTAG boundary ports for simulation BFM or optional external routing
        jtag_tck_i    : in  std_logic := '0';
        jtag_tms_i    : in  std_logic := '0';
        jtag_tdi_i    : in  std_logic := '0';
        jtag_trstb_i  : in  std_logic := '1';
        jtag_tdo_o    : out std_logic
    );
end entity rr_rea_microchip;

architecture rtl of rr_rea_microchip is

    component UJTAG is
        port (
            UTDO   : in  std_logic;
            UDRCAP : out std_logic;
            UDRSH  : out std_logic;
            UDRUPD : out std_logic;
            UIREG  : out std_logic_vector(7 downto 0);
            URSTB  : out std_logic;
            UTDI   : out std_logic;
            TCK    : in  std_logic;
            TRSTB  : in  std_logic;
            TDI    : in  std_logic;
            TDO    : out std_logic;
            TMS    : in  std_logic;
            UDRCK  : out std_logic
        );
    end component UJTAG;

    component rr_rea_top is
        generic (
            G_SAMPLE_W    : positive := 12;
            G_DEPTH       : positive := 4096;
            G_TIMESTAMP_W : natural  := 32;
            G_NUM_CHAN    : positive := 1;
            G_NUM_SOURCE  : positive := 1
        );
        port (
            sample_clk_i  : in  std_logic;
            sample_rst_i  : in  std_logic;
            probe_i       : in  std_logic_vector(G_SAMPLE_W - 1 downto 0);
            ext_trigger_i : in  std_logic := '0';
            source_o      : out std_logic_vector(G_NUM_SOURCE - 1 downto 0);
            trigger_o     : out std_logic;
            arst_i        : in  std_logic;
            tck_i         : in  std_logic;
            tdi_i         : in  std_logic;
            tdo_o         : out std_logic;
            capture_i     : in  std_logic;
            shift_en_i    : in  std_logic;
            update_i      : in  std_logic;
            sel_i         : in  std_logic
        );
    end component rr_rea_top;

    function resolve_opcode(chain : integer) return std_logic_vector is
    begin
        if chain >= 16 and chain <= 127 then
            return std_logic_vector(to_unsigned(chain, 8));
        elsif chain = 1 then
            return x"55";
        elsif chain = 2 then
            return x"56";
        elsif chain = 3 then
            return x"57";
        elsif chain = 4 then
            return x"58";
        else
            return x"55";
        end if;
    end function;

    constant C_USER_OPCODE : std_logic_vector(7 downto 0) := resolve_opcode(G_CTRL_CHAIN);

    signal tck_s      : std_logic;
    signal tdi_s      : std_logic;
    signal tdo_s      : std_logic;
    signal capture_s  : std_logic;
    signal shift_en_s : std_logic;
    signal update_s   : std_logic;
    signal sel_s      : std_logic;
    signal arst_s     : std_logic;
    signal uireg_s    : std_logic_vector(7 downto 0);
    signal urstb_s    : std_logic;

    -- Synthesis attribute anchors to prevent pruning
    attribute DONT_TOUCH     : string;
    attribute KEEP_HIERARCHY : string;
    attribute DONT_TOUCH     of u_ujtag : label is "TRUE";
    attribute KEEP_HIERARCHY of u_top   : label is "TRUE";
    attribute DONT_TOUCH     of u_top   : label is "TRUE";

begin

    u_ujtag : UJTAG
        port map (
            UTDO   => tdo_s,
            UDRCAP => capture_s,
            UDRSH  => shift_en_s,
            UDRUPD => update_s,
            UIREG  => uireg_s,
            URSTB  => urstb_s,
            UTDI   => tdi_s,
            TCK    => jtag_tck_i,
            TRSTB  => jtag_trstb_i,
            TDI    => jtag_tdi_i,
            TDO    => jtag_tdo_o,
            TMS    => jtag_tms_i,
            UDRCK  => tck_s
        );

    sel_s  <= '1' when (uireg_s = C_USER_OPCODE) else '0';
    arst_s <= not urstb_s;

    u_top : rr_rea_top
        generic map (
            G_SAMPLE_W    => G_SAMPLE_W,
            G_DEPTH       => G_DEPTH,
            G_TIMESTAMP_W => G_TIMESTAMP_W,
            G_NUM_CHAN    => G_NUM_CHAN,
            G_NUM_SOURCE  => G_NUM_SOURCE
        )
        port map (
            sample_clk_i  => sample_clk_i,
            sample_rst_i  => sample_rst_i,
            probe_i       => probe_i,
            ext_trigger_i => ext_trigger_i,
            source_o      => source_o,
            trigger_o     => trigger_o,
            arst_i        => arst_s,
            tck_i         => tck_s,
            tdi_i         => tdi_s,
            tdo_o         => tdo_s,
            capture_i     => capture_s,
            shift_en_i    => shift_en_s,
            update_i      => update_s,
            sel_i         => sel_s
        );

end architecture rtl;

library ieee;
    use ieee.std_logic_1164.all;

-- Drop-in entity alias matching the filename convention (rr_rea_jtag_microchip)
entity rr_rea_jtag_microchip is
    generic (
        G_SAMPLE_W    : positive := 12;
        G_DEPTH       : positive := 4096;
        G_TIMESTAMP_W : natural  := 32;
        G_NUM_CHAN    : positive := 1;
        G_NUM_SOURCE  : positive := 1;
        G_CTRL_CHAIN  : integer  := 1
    );
    port (
        sample_clk_i  : in  std_logic;
        sample_rst_i  : in  std_logic;
        probe_i       : in  std_logic_vector(G_SAMPLE_W - 1 downto 0);
        ext_trigger_i : in  std_logic := '0';
        source_o      : out std_logic_vector(G_NUM_SOURCE - 1 downto 0);
        trigger_o     : out std_logic;
        jtag_tck_i    : in  std_logic := '0';
        jtag_tms_i    : in  std_logic := '0';
        jtag_tdi_i    : in  std_logic := '0';
        jtag_trstb_i  : in  std_logic := '1';
        jtag_tdo_o    : out std_logic
    );
end entity rr_rea_jtag_microchip;

architecture rtl of rr_rea_jtag_microchip is
begin
    u_impl : entity work.rr_rea_microchip
        generic map (
            G_SAMPLE_W    => G_SAMPLE_W,
            G_DEPTH       => G_DEPTH,
            G_TIMESTAMP_W => G_TIMESTAMP_W,
            G_NUM_CHAN    => G_NUM_CHAN,
            G_NUM_SOURCE  => G_NUM_SOURCE,
            G_CTRL_CHAIN  => G_CTRL_CHAIN
        )
        port map (
            sample_clk_i  => sample_clk_i,
            sample_rst_i  => sample_rst_i,
            probe_i       => probe_i,
            ext_trigger_i => ext_trigger_i,
            source_o      => source_o,
            trigger_o     => trigger_o,
            jtag_tck_i    => jtag_tck_i,
            jtag_tms_i    => jtag_tms_i,
            jtag_tdi_i    => jtag_tdi_i,
            jtag_trstb_i  => jtag_trstb_i,
            jtag_tdo_o    => jtag_tdo_o
        );
end architecture rtl;
