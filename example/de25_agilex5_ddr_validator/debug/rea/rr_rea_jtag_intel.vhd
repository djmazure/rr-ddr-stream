-- SPDX-FileCopyrightText: 2026 Daniel J. Mazure
-- SPDX-License-Identifier: MIT
--
-- rr_rea_intel — Intel/Altera sld_virtual_jtag wrapper for rr_rea_top.
--
-- Mirrors rr_rea_xilinx7 (BSCANE2 variant). Instantiates Quartus's
-- sld_virtual_jtag megafunction with the given G_CTRL_CHAIN as its
-- sld_instance_index, then wires its virtual TAP signals straight to
-- rr_rea_top. This is the only block in the IP that depends on an
-- Intel vendor primitive.
--
-- Ported to VHDL from fpgacapZero v0.3.0 rtl/jtag_tap/jtag_tap_intel.v
-- (Apache-2.0, Leonardo Capossio / bard0). Same pattern, adapted to
-- rr_rea_top's port shape.
--
-- Chain selection note: unlike BSCANE2 (which is selected by the
-- USERn IR), each sld_virtual_jtag instance gets its own virtual IR
-- index automatically managed by the sld_node. The G_CTRL_CHAIN
-- generic maps to sld_instance_index (1-based), matching the
-- IR_TABLE_INTEL_VJTAG host-side convention in
-- sdk/cli/rea/transport.py.

library ieee;
    use ieee.std_logic_1164.all;

entity rr_rea_intel is
    generic (
        G_SAMPLE_W    : positive := 12;
        G_DEPTH       : positive := 4096;
        G_TIMESTAMP_W : natural  := 32;
        G_NUM_CHAN    : positive := 1;
        G_CTRL_CHAIN  : positive := 1   -- sld_instance_index (1-based)
    );
    port (
        sample_clk  : in  std_logic;
        sample_rst  : in  std_logic;
        probe_in    : in  std_logic_vector(G_SAMPLE_W - 1 downto 0);
        trigger_out : out std_logic
    );
end entity;

architecture rtl of rr_rea_intel is

    component sld_virtual_jtag is
        generic (
            sld_auto_instance_index : string  := "NO";
            sld_instance_index      : integer := 0;
            sld_ir_width            : integer := 1
        );
        port (
            tck               : out std_logic;
            tdi               : out std_logic;
            tdo               : in  std_logic;
            virtual_state_cdr : out std_logic;
            virtual_state_sdr : out std_logic;
            virtual_state_udr : out std_logic;
            ir_in             : out std_logic_vector(sld_ir_width - 1 downto 0);
            ir_out            : in  std_logic_vector(sld_ir_width - 1 downto 0)
        );
    end component;

    signal tck      : std_logic;
    signal tdi      : std_logic;
    signal tdo      : std_logic;
    signal capture  : std_logic;
    signal shift_en : std_logic;
    signal update   : std_logic;
    -- sld_virtual_jtag is selected automatically when its IR matches —
    -- there is no per-instance SEL output. Tie sel high so rr_rea_top's
    -- TAP-side logic always observes "selected" while the virtual JTAG
    -- node is active. The capture/shift/update strobes are themselves
    -- only asserted by the sld_node when this instance is targeted.
    signal sel      : std_logic := '1';
    -- sld_virtual_jtag exposes no reset; rely on iface FSM's natural init.
    signal arst     : std_logic := '0';

    signal ir_in_unused : std_logic_vector(0 downto 0);

begin

    u_vjtag : sld_virtual_jtag
        generic map (
            sld_auto_instance_index => "NO",
            sld_instance_index      => G_CTRL_CHAIN,
            sld_ir_width            => 1
        )
        port map (
            tck               => tck,
            tdi               => tdi,
            tdo               => tdo,
            virtual_state_cdr => capture,
            virtual_state_sdr => shift_en,
            virtual_state_udr => update,
            ir_in             => ir_in_unused,
            ir_out            => "0"
        );

    u_top : entity work.rr_rea_top
        generic map (
            G_SAMPLE_W    => G_SAMPLE_W,
            G_DEPTH       => G_DEPTH,
            G_TIMESTAMP_W => G_TIMESTAMP_W,
            G_NUM_CHAN    => G_NUM_CHAN
        )
        port map (
            sample_clk  => sample_clk,
            sample_rst  => sample_rst,
            probe_in    => probe_in,
            trigger_out => trigger_out,
            arst        => arst,
            tck         => tck,
            tdi         => tdi,
            tdo         => tdo,
            capture     => capture,
            shift_en    => shift_en,
            update      => update,
            sel         => sel
        );

end architecture;
