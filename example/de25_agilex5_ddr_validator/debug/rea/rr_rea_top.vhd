-- SPDX-FileCopyrightText: 2026 Daniel J. Mazure
-- SPDX-License-Identifier: MIT
--
-- rr_rea_top — RouteRTL Embedded Analyzer top-level integration.
--
-- Vendor-neutral: takes JTAG TAP signals as ports rather than
-- instantiating BSCANE2 / sld_virtual_jtag / etc. The Xilinx wrapper
-- (rr_rea_jtag_xilinx7.vhd) is a separate thin shim that connects
-- BSCANE2 to this top. In simulation, the cocotb testbench drives
-- the TAP signals directly — zero vendor primitive needed.
--
-- Wiring:
--   JTAG iface     ◄──► regbank          (reg-bus)
--   regbank        ──── CDCs ────►       capture_fsm config inputs
--   capture_fsm    ──── CDCs ────►       regbank status mirror
--   capture_fsm    ──────────────►       dpram (port A: write)
--   dpram (port B: read) ────────►       JTAG iface (when reg_addr in DPRAM window)
--
-- See SPEC.md and requirements.yml REA-REQ-300 for the contract.

library ieee;
    use ieee.std_logic_1164.all;
    use ieee.numeric_std.all;

library work;
    use work.rr_rea_pkg.all;

entity rr_rea_top is
    generic (
        G_SAMPLE_W    : positive := 12;
        G_DEPTH       : positive := 4096;
        G_TIMESTAMP_W : natural  := 32;
        G_NUM_CHAN    : positive := 1;
        G_TRIG_CONDS  : positive := 4   -- v0.5 comparator-array slots (P3.647)
    );
    port (
        -- ── Sample-clock domain ──────────────────────────────────
        sample_clk : in  std_logic;
        sample_rst : in  std_logic;
        probe_in   : in  std_logic_vector(G_SAMPLE_W - 1 downto 0);

        -- ── External board-pin trigger (RTL-P3.266) ─────────────
        -- Async package-pin input the user routes from a board pin (scope
        -- trigger-out, another FPGA's trigger_out, a button). Synced inside
        -- and folded into the fire decision per TRIG_MODE ext_en[3]/ext_and[8].
        -- Defaults '0' so existing instantiations that don't drive it are
        -- unaffected (internal-only trigger).
        ext_trigger_in : in std_logic := '0';

        -- ── Local trigger pulse (1-cycle on sample_clk) ─────────
        -- Exposed so the design can route it to an LED / external
        -- pin / cross-domain trigger crossbar (v0.2). Doubles as a
        -- "Vivado optimizer anchor" — without an observable output
        -- the whole REA hierarchy gets pruned in synthesis.
        trigger_out : out std_logic;

        -- ── JTAG TAP (jtag_clk domain) — driven by external wrapper
        --    in synth, driven by testbench in sim ─────────────────
        arst       : in  std_logic;
        tck        : in  std_logic;
        tdi        : in  std_logic;
        tdo        : out std_logic;
        capture    : in  std_logic;
        shift_en   : in  std_logic;
        update     : in  std_logic;
        sel        : in  std_logic
    );
end entity;

architecture rtl of rr_rea_top is

    constant C_PTR_W : positive := clog2(G_DEPTH);

    -- ── Reg-bus wires (jtag_clk domain) ──────────────────────────
    signal reg_clk    : std_logic;
    signal reg_rst    : std_logic;
    signal reg_wr_en  : std_logic;
    signal reg_rd_en  : std_logic;
    signal reg_addr   : std_logic_vector(15 downto 0);
    signal reg_wdata  : std_logic_vector(31 downto 0);
    signal reg_rdata  : std_logic_vector(31 downto 0);

    -- ── Regbank → CDC → FSM config (jtag_clk → sample_clk) ──────
    signal pretrig_jclk  : std_logic_vector(C_PTR_W - 1 downto 0);
    signal posttrig_jclk : std_logic_vector(C_PTR_W - 1 downto 0);
    signal trig_value_jclk : std_logic_vector(G_SAMPLE_W - 1 downto 0);
    signal trig_mask_jclk  : std_logic_vector(G_SAMPLE_W - 1 downto 0);
    signal arm_toggle_jclk   : std_logic;
    signal reset_toggle_jclk : std_logic;
    signal trig_mode_jclk    : std_logic_vector(31 downto 0);
    signal chan_sel_jclk     : std_logic_vector(7 downto 0);
    signal decim_ratio_jclk  : std_logic_vector(23 downto 0);
    signal decim_ratio_sclk  : std_logic_vector(23 downto 0);

    signal pretrig_sclk    : std_logic_vector(C_PTR_W - 1 downto 0);
    signal posttrig_sclk   : std_logic_vector(C_PTR_W - 1 downto 0);
    signal trig_value_sclk : std_logic_vector(G_SAMPLE_W - 1 downto 0);
    signal trig_mask_sclk  : std_logic_vector(G_SAMPLE_W - 1 downto 0);
    -- Low 16 bits of TRIG_MODE crossed to sample_clk: [7:0] op byte
    -- (P3.644/645) + the enable bits seq[1]/array[2]/ext_en[3], and [8] =
    -- ext_and combine mode (RTL-P3.266). Widened from 8 → 16 to carry bit 8.
    signal trig_mode_sclk  : std_logic_vector(15 downto 0);
    signal ext_trig_sclk   : std_logic_vector(0 downto 0);  -- synced board pin
    signal arm_pulse_sclk   : std_logic;
    signal reset_pulse_sclk : std_logic;

    -- ── Comparator-array config (RTL-P3.647): regbank → CDC → FSM ──
    -- array_enable rides in trig_mode bit[2] (already CDC'd); only the
    -- per-condition value/mask/op/valid arrays need their own sync.
    signal cond_values_jclk : std_logic_vector(G_TRIG_CONDS * G_SAMPLE_W - 1 downto 0);
    signal cond_masks_jclk  : std_logic_vector(G_TRIG_CONDS * G_SAMPLE_W - 1 downto 0);
    signal cond_ops_jclk    : std_logic_vector(G_TRIG_CONDS * 4 - 1 downto 0);
    signal cond_valid_jclk  : std_logic_vector(G_TRIG_CONDS - 1 downto 0);
    signal cond_values_sclk : std_logic_vector(G_TRIG_CONDS * G_SAMPLE_W - 1 downto 0);
    signal cond_masks_sclk  : std_logic_vector(G_TRIG_CONDS * G_SAMPLE_W - 1 downto 0);
    signal cond_ops_sclk    : std_logic_vector(G_TRIG_CONDS * 4 - 1 downto 0);
    signal cond_valid_sclk  : std_logic_vector(G_TRIG_CONDS - 1 downto 0);

    -- ── FSM outputs (sample_clk domain) ──────────────────────────
    signal armed_sclk     : std_logic;
    signal triggered_sclk : std_logic;
    signal done_sclk      : std_logic;
    signal overflow_sclk  : std_logic;
    signal trigger_out_sclk : std_logic;
    signal trigger_sticky_r : std_logic := '0';
    signal dpram_we_sclk    : std_logic;
    signal dpram_addr_sclk  : std_logic_vector(C_PTR_W - 1 downto 0);
    signal dpram_din_sclk   : std_logic_vector(G_SAMPLE_W - 1 downto 0);
    signal wr_ptr_sclk      : std_logic_vector(C_PTR_W - 1 downto 0);
    signal trig_ptr_sclk    : std_logic_vector(C_PTR_W - 1 downto 0);
    signal start_ptr_sclk   : std_logic_vector(C_PTR_W - 1 downto 0);

    -- ── CDC: sample_clk → jtag_clk (status mirror) ───────────────
    signal armed_jclk     : std_logic_vector(0 downto 0);
    signal triggered_jclk : std_logic_vector(0 downto 0);
    signal done_jclk      : std_logic_vector(0 downto 0);
    signal overflow_jclk  : std_logic_vector(0 downto 0);
    signal start_ptr_jclk : std_logic_vector(C_PTR_W - 1 downto 0);

    -- ── DPRAM read-port (jtag_clk domain) ────────────────────────
    signal dpram_addr_b : std_logic_vector(C_PTR_W - 1 downto 0);
    signal dpram_dout_b : std_logic_vector(G_SAMPLE_W - 1 downto 0);

    -- ── reg_rdata mux: regbank vs dpram window ───────────────────
    signal regbank_rdata : std_logic_vector(31 downto 0);
    signal dpram_rdata   : std_logic_vector(31 downto 0);
    signal in_dpram_window : std_logic;

    -- Forward declarations of CDC entities are not strictly needed in
    -- VHDL-93+ (entity instantiation works directly), but we keep
    -- them here for clarity.
    component rr_rea_sync_word is
        generic (G_WIDTH : positive);
        port (
            dst_clk : in  std_logic;
            din     : in  std_logic_vector(G_WIDTH - 1 downto 0);
            dout    : out std_logic_vector(G_WIDTH - 1 downto 0)
        );
    end component;

    component rr_rea_pulse_xfer is
        port (
            src_toggle : in  std_logic;
            dst_clk    : in  std_logic;
            dst_rst    : in  std_logic;
            dst_pulse  : out std_logic
        );
    end component;

begin

    -- ── JTAG protocol decoder ────────────────────────────────────
    u_jtag : entity work.rr_rea_jtag_iface
        port map (
            arst      => arst,
            tck       => tck,
            tdi       => tdi,
            tdo       => tdo,
            capture   => capture,
            shift_en  => shift_en,
            update    => update,
            sel       => sel,
            reg_clk   => reg_clk,
            reg_rst   => reg_rst,
            reg_wr_en => reg_wr_en,
            reg_rd_en => reg_rd_en,
            reg_addr  => reg_addr,
            reg_wdata => reg_wdata,
            reg_rdata => reg_rdata
        );

    -- ── Register file ────────────────────────────────────────────
    u_regbank : entity work.rr_rea_regbank
        generic map (
            G_SAMPLE_W    => G_SAMPLE_W,
            G_DEPTH       => G_DEPTH,
            G_TIMESTAMP_W => G_TIMESTAMP_W,
            G_NUM_CHAN    => G_NUM_CHAN,
            G_TRIG_CONDS  => G_TRIG_CONDS
        )
        port map (
            jtag_clk => reg_clk,
            jtag_rst => reg_rst,
            wr_en    => reg_wr_en,
            wr_addr  => reg_addr,
            wr_data  => reg_wdata,
            rd_addr  => reg_addr,
            rd_data  => regbank_rdata,
            armed_in     => armed_jclk(0),
            triggered_in => triggered_jclk(0),
            done_in      => done_jclk(0),
            overflow_in  => overflow_jclk(0),
            start_ptr_in => start_ptr_jclk,
            pretrig_len_out  => pretrig_jclk,
            posttrig_len_out => posttrig_jclk,
            trig_value_out   => trig_value_jclk,
            trig_mask_out    => trig_mask_jclk,
            trig_mode_out    => trig_mode_jclk,
            chan_sel_out     => chan_sel_jclk,
            decim_ratio_out  => decim_ratio_jclk,
            cond_values_out  => cond_values_jclk,
            cond_masks_out   => cond_masks_jclk,
            cond_ops_out     => cond_ops_jclk,
            cond_valid_out   => cond_valid_jclk,
            arm_toggle_out   => arm_toggle_jclk,
            reset_toggle_out => reset_toggle_jclk
        );

    -- ── reg_rdata mux: dpram window vs regbank ───────────────────
    -- dpram_window: addr in [0x0100 .. 0x0100 + DEPTH*4)
    -- (each dpram cell occupies 4 bytes / 1 word in the JTAG map)
    in_dpram_window <= '1' when
        unsigned(reg_addr) >= unsigned(C_ADDR_DATA_BASE) and
        unsigned(reg_addr) < (unsigned(C_ADDR_DATA_BASE) +
                              to_unsigned(G_DEPTH * 4, 16))
        else '0';

    -- Address into dpram: (reg_addr - DATA_BASE) >> 2
    dpram_addr_b <= std_logic_vector(resize(
        shift_right(unsigned(reg_addr) - unsigned(C_ADDR_DATA_BASE), 2),
        C_PTR_W));

    -- Zero-extend dpram cell to 32 bits.
    dpram_rdata <= std_logic_vector(resize(unsigned(dpram_dout_b), 32));

    reg_rdata <= dpram_rdata when in_dpram_window = '1' else regbank_rdata;

    -- ── CDC: jtag_clk config words → sample_clk ──────────────────
    u_cdc_pretrig : rr_rea_sync_word
        generic map (G_WIDTH => C_PTR_W)
        port map (dst_clk => sample_clk, din => pretrig_jclk,
                  dout => pretrig_sclk);

    u_cdc_posttrig : rr_rea_sync_word
        generic map (G_WIDTH => C_PTR_W)
        port map (dst_clk => sample_clk, din => posttrig_jclk,
                  dout => posttrig_sclk);

    u_cdc_trig_value : rr_rea_sync_word
        generic map (G_WIDTH => G_SAMPLE_W)
        port map (dst_clk => sample_clk, din => trig_value_jclk,
                  dout => trig_value_sclk);

    u_cdc_trig_mask : rr_rea_sync_word
        generic map (G_WIDTH => G_SAMPLE_W)
        port map (dst_clk => sample_clk, din => trig_mask_jclk,
                  dout => trig_mask_sclk);

    -- Comparator-op + enable-bits CDC: low 16 bits of TRIG_MODE carry
    -- value_match[0]/seq_en[1]/array_en[2]/ext_en[3], the op nibble [7:4]
    -- (RTL-P3.644/645/646), and ext_and[8] (RTL-P3.266). Quasi-static —
    -- the host writes TRIG_MODE before pulsing arm.
    u_cdc_trig_mode : rr_rea_sync_word
        generic map (G_WIDTH => 16)
        port map (dst_clk => sample_clk, din => trig_mode_jclk(15 downto 0),
                  dout => trig_mode_sclk);

    -- RTL-P3.266: external board-pin trigger, synced into the sample_clk
    -- domain (the pin is fully asynchronous to sample_clk — the user routes
    -- it from a package pin). A double-flop level synchronizer is correct for
    -- a level / wide-pulse external trigger; sub-sample-period pulses are not
    -- guaranteed to be seen (documented in SPEC).
    u_cdc_ext_trig : rr_rea_sync_word
        generic map (G_WIDTH => 1)
        port map (dst_clk => sample_clk, din => (0 => ext_trigger_in),
                  dout => ext_trig_sclk);

    -- v0.3 decimation ratio CDC
    u_cdc_decim : rr_rea_sync_word
        generic map (G_WIDTH => 24)
        port map (dst_clk => sample_clk, din => decim_ratio_jclk,
                  dout => decim_ratio_sclk);

    -- RTL-P3.647 comparator-array config CDC. Quasi-static (the host writes
    -- all slots before pulsing arm; the FSM latches on the arm pulse), so the
    -- per-bit double-flop sync — same as trig_value/mask — is sufficient.
    u_cdc_cond_values : rr_rea_sync_word
        generic map (G_WIDTH => G_TRIG_CONDS * G_SAMPLE_W)
        port map (dst_clk => sample_clk, din => cond_values_jclk,
                  dout => cond_values_sclk);
    u_cdc_cond_masks : rr_rea_sync_word
        generic map (G_WIDTH => G_TRIG_CONDS * G_SAMPLE_W)
        port map (dst_clk => sample_clk, din => cond_masks_jclk,
                  dout => cond_masks_sclk);
    u_cdc_cond_ops : rr_rea_sync_word
        generic map (G_WIDTH => G_TRIG_CONDS * 4)
        port map (dst_clk => sample_clk, din => cond_ops_jclk,
                  dout => cond_ops_sclk);
    u_cdc_cond_valid : rr_rea_sync_word
        generic map (G_WIDTH => G_TRIG_CONDS)
        port map (dst_clk => sample_clk, din => cond_valid_jclk,
                  dout => cond_valid_sclk);

    -- ── CDC: jtag_clk pulse toggles → sample_clk pulses ─────────
    u_cdc_arm : rr_rea_pulse_xfer
        port map (
            src_toggle => arm_toggle_jclk,
            dst_clk => sample_clk, dst_rst => sample_rst,
            dst_pulse => arm_pulse_sclk
        );

    u_cdc_reset : rr_rea_pulse_xfer
        port map (
            src_toggle => reset_toggle_jclk,
            dst_clk => sample_clk, dst_rst => sample_rst,
            dst_pulse => reset_pulse_sclk
        );

    -- ── Capture FSM ──────────────────────────────────────────────
    u_fsm : entity work.rr_rea_capture_fsm
        generic map (G_SAMPLE_W => G_SAMPLE_W, G_DEPTH => G_DEPTH,
                     G_TRIG_CONDS => G_TRIG_CONDS)
        port map (
            sample_clk    => sample_clk,
            sample_rst    => sample_rst,
            probe_in      => probe_in,
            arm_pulse     => arm_pulse_sclk,
            reset_pulse   => reset_pulse_sclk,
            pretrig_len_in  => pretrig_sclk,
            posttrig_len_in => posttrig_sclk,
            trig_value_in   => trig_value_sclk,
            trig_mask_in    => trig_mask_sclk,
            trig_mode_in    => trig_mode_sclk(7 downto 0),
            decim_ratio_in  => decim_ratio_sclk,
            -- RTL-P3.647: array_enable rides in trig_mode bit[2] (CDC'd above);
            -- the per-condition arrays come from their own sync.
            array_enable_in => trig_mode_sclk(C_TRIG_MODE_BIT_ARRAY_EN),
            cond_values_in  => cond_values_sclk,
            cond_masks_in   => cond_masks_sclk,
            cond_ops_in     => cond_ops_sclk,
            cond_valid_in   => cond_valid_sclk,
            -- RTL-P3.266: external board-pin trigger (synced) + its enable
            -- (TRIG_MODE bit[3]) and OR/AND combine mode (bit[8]).
            ext_trigger_in  => ext_trig_sclk(0),
            ext_enable_in   => trig_mode_sclk(C_TRIG_MODE_BIT_EXT_EN),
            ext_and_in      => trig_mode_sclk(C_TRIG_MODE_BIT_EXT_AND),
            armed       => armed_sclk,
            triggered   => triggered_sclk,
            done        => done_sclk,
            overflow    => overflow_sclk,
            trigger_out => trigger_out_sclk,
            dpram_we    => dpram_we_sclk,
            dpram_addr  => dpram_addr_sclk,
            dpram_din   => dpram_din_sclk,
            wr_ptr_out    => wr_ptr_sclk,
            trig_ptr_out  => trig_ptr_sclk,
            start_ptr_out => start_ptr_sclk
        );

    -- ── CDC: sample_clk status → jtag_clk ────────────────────────
    u_cdc_armed : rr_rea_sync_word
        generic map (G_WIDTH => 1)
        port map (dst_clk => reg_clk,
                  din(0) => armed_sclk,
                  dout => armed_jclk);

    u_cdc_triggered : rr_rea_sync_word
        generic map (G_WIDTH => 1)
        port map (dst_clk => reg_clk,
                  din(0) => triggered_sclk,
                  dout => triggered_jclk);

    u_cdc_done : rr_rea_sync_word
        generic map (G_WIDTH => 1)
        port map (dst_clk => reg_clk,
                  din(0) => done_sclk,
                  dout => done_jclk);

    u_cdc_overflow : rr_rea_sync_word
        generic map (G_WIDTH => 1)
        port map (dst_clk => reg_clk,
                  din(0) => overflow_sclk,
                  dout => overflow_jclk);

    u_cdc_start_ptr : rr_rea_sync_word
        generic map (G_WIDTH => C_PTR_W)
        port map (dst_clk => reg_clk,
                  din => start_ptr_sclk,
                  dout => start_ptr_jclk);

    -- ── trigger_out: pulse the external port + maintain a sticky
    --    flag flipped on each trigger so an LED can dance and the
    --    Vivado optimizer can't prune the hierarchy. ─────────────
    trigger_out <= trigger_sticky_r;
    process (sample_clk, sample_rst)
    begin
        if sample_rst = '1' then
            trigger_sticky_r <= '0';
        elsif rising_edge(sample_clk) then
            if trigger_out_sclk = '1' then
                trigger_sticky_r <= not trigger_sticky_r;
            end if;
        end if;
    end process;

    -- ── Sample DPRAM ─────────────────────────────────────────────
    u_dpram : entity work.rr_rea_dpram
        generic map (G_WIDTH => G_SAMPLE_W, G_DEPTH => G_DEPTH)
        port map (
            clk_a  => sample_clk,
            we_a   => dpram_we_sclk,
            addr_a => dpram_addr_sclk,
            din_a  => dpram_din_sclk,
            dout_a => open,
            clk_b  => reg_clk,
            addr_b => dpram_addr_b,
            dout_b => dpram_dout_b
        );

end architecture;
