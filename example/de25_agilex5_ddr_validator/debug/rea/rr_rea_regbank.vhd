-- SPDX-FileCopyrightText: 2026 Daniel J. Mazure
-- SPDX-License-Identifier: MIT
--
-- rr_rea_regbank — memory-mapped register file for the REA IP.
--
-- Sits between the JTAG protocol decoder (rr_rea_jtag_iface) and the
-- capture FSM (rr_rea_capture_fsm). Implements the SW-interface
-- contract from SPEC.md (REA-REQ-010..012). Synchronous to jtag_clk;
-- CDC to/from sample_clk is the separate rr_rea_cdc block's job.
--
-- v0.1 register map (full table in SPEC.md):
--   0x00 RO  VERSION       0xC8: 0x52454101 ('REA' + v0.1)
--   0x04 WO  CTRL          arm_toggle/reset_toggle
--   0x08 RO  STATUS        armed/triggered/done/overflow
--   0x0C RO  SAMPLE_W      synth-time generic
--   0x10 RO  DEPTH         synth-time generic
--   0x14 RW  PRETRIG
--   0x18 RW  POSTTRIG
--   0x1C RO  CAPTURE_LEN   = pretrig + posttrig + 1
--   0x20 RW  TRIG_MODE     bit[0] = value_match
--   0x24 RW  TRIG_VALUE
--   0x28 RW  TRIG_MASK
--   0xA0 RW  CHAN_SEL      v0.1: must be 0
--   0xA4 RO  NUM_CHAN      v0.1: =1
--   0xC4 RO  TIMESTAMP_W   synth-time generic
--   0xC8 RO  START_PTR     captured address of oldest sample (post-done)
--
-- The CTRL register is "write-toggle": every write XORs the addressed
-- bit position into a sticky toggle register. Downstream rr_rea_cdc
-- edge-detects each toggle to produce a single sample_clk pulse.
-- This is the standard JTAG → fast-clock pulse-coupling pattern.

library ieee;
    use ieee.std_logic_1164.all;
    use ieee.numeric_std.all;

library work;
    use work.rr_rea_pkg.all;

entity rr_rea_regbank is
    generic (
        G_SAMPLE_W    : positive := 12;
        G_DEPTH       : positive := 4096;
        G_TIMESTAMP_W : natural  := 32;
        G_NUM_CHAN    : positive := 1;
        G_TRIG_CONDS  : positive := 4   -- v0.5 comparator-array slots (P3.647)
    );
    port (
        jtag_clk : in  std_logic;
        jtag_rst : in  std_logic;

        -- ── Register-port interface (from rr_rea_jtag_iface) ─────
        wr_en    : in  std_logic;
        wr_addr  : in  std_logic_vector(15 downto 0);
        wr_data  : in  std_logic_vector(31 downto 0);
        rd_addr  : in  std_logic_vector(15 downto 0);
        rd_data  : out std_logic_vector(31 downto 0);

        -- ── Status inputs (from sample-clk domain, sync'd) ───────
        armed_in     : in std_logic;
        triggered_in : in std_logic;
        done_in      : in std_logic;
        overflow_in  : in std_logic;
        start_ptr_in : in std_logic_vector(clog2(G_DEPTH) - 1 downto 0);

        -- ── Config outputs (to sample-clk domain, will be sync'd) ─
        pretrig_len_out  : out std_logic_vector(clog2(G_DEPTH) - 1 downto 0);
        posttrig_len_out : out std_logic_vector(clog2(G_DEPTH) - 1 downto 0);
        trig_value_out   : out std_logic_vector(G_SAMPLE_W - 1 downto 0);
        trig_mask_out    : out std_logic_vector(G_SAMPLE_W - 1 downto 0);
        trig_mode_out    : out std_logic_vector(31 downto 0);
        chan_sel_out     : out std_logic_vector(7 downto 0);
        decim_ratio_out  : out std_logic_vector(23 downto 0);

        -- ── Per-condition comparator array (RTL-P3.647) ──────────
        -- Expanded full-width per slot: the compact {valid,op,width,lsb}
        -- + 32-bit value written via COND_SEL/CFG/VAL becomes a shifted
        -- G_SAMPLE_W value + field mask + 4-bit op + valid, ready for the
        -- FSM's per-condition comparator. array_enable rides in
        -- trig_mode_out bit[2] (the top extracts it post-CDC).
        cond_values_out  : out std_logic_vector(
            G_TRIG_CONDS * G_SAMPLE_W - 1 downto 0);
        cond_masks_out   : out std_logic_vector(
            G_TRIG_CONDS * G_SAMPLE_W - 1 downto 0);
        cond_ops_out     : out std_logic_vector(G_TRIG_CONDS * 4 - 1 downto 0);
        cond_valid_out   : out std_logic_vector(G_TRIG_CONDS - 1 downto 0);

        -- ── Pulse toggles (to rr_rea_cdc → sample_clk pulses) ────
        arm_toggle_out   : out std_logic;
        reset_toggle_out : out std_logic
    );
end entity;

architecture rtl of rr_rea_regbank is

    constant C_PTR_W : positive := clog2(G_DEPTH);

    -- ── Storage for RW registers ─────────────────────────────────
    signal pretrig_r    : std_logic_vector(31 downto 0) := (others => '0');
    signal posttrig_r   : std_logic_vector(31 downto 0) := (others => '0');
    signal trig_mode_r  : std_logic_vector(31 downto 0) := (others => '0');
    -- RTL-P2.658(b): banked trigger value/mask. trig_words(G_SAMPLE_W) =
    -- ceil(W/32) 32-bit words held in a flat vector; the JTAG datapath
    -- pages through them via trig_word_sel_r (0x2C). For G_SAMPLE_W<=32
    -- there is exactly one word and trig_word_sel_r stays 0, so the
    -- 0x24/0x28 register behaviour is byte-identical to the legacy path.
    constant C_TRIG_WORDS : positive := trig_words(G_SAMPLE_W);
    signal trig_value_flat : std_logic_vector(C_TRIG_WORDS * 32 - 1 downto 0)
                                 := (others => '0');
    signal trig_mask_flat  : std_logic_vector(C_TRIG_WORDS * 32 - 1 downto 0)
                                 := (others => '0');
    signal trig_word_sel_r : unsigned(7 downto 0) := (others => '0');
    signal chan_sel_r   : std_logic_vector(31 downto 0) := (others => '0');
    signal decim_r      : std_logic_vector(31 downto 0) := (others => '0');

    -- ── Per-condition comparator array storage (RTL-P3.647) ──────
    -- Compact 32-bit slots paged via cond_sel_r (COND_SEL). cfg holds
    -- {valid[31],op[27:24],width[23:16],lsb[15:8]}; val holds the 32-bit
    -- compare value (right-aligned in the field). Expanded combinationally
    -- below into the full-width cond_*_out the FSM consumes.
    signal cond_cfg_flat : std_logic_vector(G_TRIG_CONDS * 32 - 1 downto 0)
                               := (others => '0');
    signal cond_val_flat : std_logic_vector(G_TRIG_CONDS * 32 - 1 downto 0)
                               := (others => '0');
    signal cond_sel_r    : unsigned(7 downto 0) := (others => '0');

    -- field_low_mask(w): w low bits set, in a G_SAMPLE_W vector (clamped).
    function field_low_mask(w : natural) return unsigned is
        variable ones : unsigned(G_SAMPLE_W - 1 downto 0) := (others => '1');
    begin
        if w = 0 then
            return (G_SAMPLE_W - 1 downto 0 => '0');
        elsif w >= G_SAMPLE_W then
            return ones;
        else
            return shift_right(ones, G_SAMPLE_W - w);  -- w low bits set
        end if;
    end function;

    -- ── Toggle bits — flipped on every write to CTRL.bit[N] ──────
    signal arm_toggle_r   : std_logic := '0';
    signal reset_toggle_r : std_logic := '0';

    -- ── RO computed: capture_len ─────────────────────────────────
    signal capture_len_w : std_logic_vector(31 downto 0);

    -- ── Sized constants for the RO informational regs ────────────
    function u32(v : natural) return std_logic_vector is
    begin
        return std_logic_vector(to_unsigned(v, 32));
    end function;

    constant C_REG_SAMPLE_W    : std_logic_vector(31 downto 0) := u32(G_SAMPLE_W);
    constant C_REG_DEPTH       : std_logic_vector(31 downto 0) := u32(G_DEPTH);
    constant C_REG_TIMESTAMP_W : std_logic_vector(31 downto 0) := u32(G_TIMESTAMP_W);
    constant C_REG_NUM_CHAN    : std_logic_vector(31 downto 0) := u32(G_NUM_CHAN);

begin

    -- capture_len = pretrig + posttrig + 1 (combinational)
    capture_len_w <= std_logic_vector(
        unsigned(pretrig_r) + unsigned(posttrig_r) + 1
    );

    -- Drive config outputs (lower bits sliced for the FSM's bus widths)
    pretrig_len_out  <= pretrig_r(C_PTR_W - 1 downto 0);
    posttrig_len_out <= posttrig_r(C_PTR_W - 1 downto 0);
    -- Full-width trigger value/mask to the FSM comparator. The flat
    -- vector is C_TRIG_WORDS*32 bits (>= G_SAMPLE_W), so the slice is
    -- always in range; bits above G_SAMPLE_W in the top word are unused.
    trig_value_out   <= trig_value_flat(G_SAMPLE_W - 1 downto 0);
    trig_mask_out    <= trig_mask_flat(G_SAMPLE_W - 1 downto 0);
    trig_mode_out    <= trig_mode_r;
    chan_sel_out     <= chan_sel_r(7 downto 0);
    decim_ratio_out  <= decim_r(23 downto 0);
    arm_toggle_out   <= arm_toggle_r;
    reset_toggle_out <= reset_toggle_r;

    -- ── Per-condition expansion (RTL-P3.647) ─────────────────────
    -- Decode each compact slot {valid,op,width,lsb}+val32 into a shifted
    -- full-width field value + field mask (op/valid passed through). The
    -- dynamic shift_left positions the field at its lsb; the FSM then does
    -- a plain masked-op compare per condition.
    g_cond_expand : for k in 0 to G_TRIG_CONDS - 1 generate
        signal cfg_k  : std_logic_vector(31 downto 0);
        signal val_k  : std_logic_vector(31 downto 0);
        signal wid_k  : natural range 0 to 255;
        signal lsb_k  : natural range 0 to 255;
        signal lowm_k : unsigned(G_SAMPLE_W - 1 downto 0);
    begin
        cfg_k <= cond_cfg_flat(k * 32 + 31 downto k * 32);
        val_k <= cond_val_flat(k * 32 + 31 downto k * 32);
        wid_k <= to_integer(unsigned(cfg_k(C_COND_WIDTH_LSB + 7 downto C_COND_WIDTH_LSB)));
        lsb_k <= to_integer(unsigned(cfg_k(C_COND_LSB_LSB + 7 downto C_COND_LSB_LSB)));
        lowm_k <= field_low_mask(wid_k);

        cond_valid_out(k) <= cfg_k(C_COND_VALID_BIT);
        cond_ops_out(k * 4 + 3 downto k * 4) <=
            cfg_k(C_COND_OP_LSB + 3 downto C_COND_OP_LSB);
        cond_masks_out(k * G_SAMPLE_W + G_SAMPLE_W - 1 downto k * G_SAMPLE_W) <=
            std_logic_vector(shift_left(lowm_k, lsb_k));
        cond_values_out(k * G_SAMPLE_W + G_SAMPLE_W - 1 downto k * G_SAMPLE_W) <=
            std_logic_vector(shift_left(
                resize(unsigned(val_k), G_SAMPLE_W) and lowm_k, lsb_k));
    end generate;

    -- ── Write port (jtag_clk-synchronous) ────────────────────────
    process (jtag_clk, jtag_rst)
    begin
        if jtag_rst = '1' then
            pretrig_r      <= (others => '0');
            posttrig_r     <= (others => '0');
            trig_mode_r    <= (others => '0');
            trig_value_flat <= (others => '0');
            trig_mask_flat  <= (others => '0');
            trig_word_sel_r <= (others => '0');
            cond_cfg_flat  <= (others => '0');
            cond_val_flat  <= (others => '0');
            cond_sel_r     <= (others => '0');
            chan_sel_r     <= (others => '0');
            decim_r        <= (others => '0');
            arm_toggle_r   <= '0';
            reset_toggle_r <= '0';

        elsif rising_edge(jtag_clk) then
            if wr_en = '1' then
                case unsigned(wr_addr) is
                    when C_ADDR_CTRL =>
                        -- Toggle bits — XOR each requested bit into
                        -- the sticky toggle register. The downstream
                        -- CDC edge-detects each toggle to make a
                        -- one-cycle sample_clk pulse.
                        if wr_data(C_CTRL_BIT_ARM) = '1' then
                            arm_toggle_r <= not arm_toggle_r;
                        end if;
                        if wr_data(C_CTRL_BIT_RESET) = '1' then
                            reset_toggle_r <= not reset_toggle_r;
                        end if;

                    when C_ADDR_PRETRIG =>
                        pretrig_r <= wr_data;
                    when C_ADDR_POSTTRIG =>
                        posttrig_r <= wr_data;
                    when C_ADDR_TRIG_MODE =>
                        trig_mode_r <= wr_data;
                    when C_ADDR_TRIG_VALUE =>
                        -- RTL-P2.658(b): write the 32-bit word selected by
                        -- trig_word_sel_r. Static-bound slice gated on a
                        -- dynamic compare — the nvc-safe paging idiom (no
                        -- dynamic vector slice, no array-element-from-port
                        -- latch). An out-of-range sel matches no word, so
                        -- the write is dropped (defensive, no aliasing).
                        for i in 0 to C_TRIG_WORDS - 1 loop
                            if to_integer(trig_word_sel_r) = i then
                                trig_value_flat((i + 1) * 32 - 1 downto i * 32)
                                    <= wr_data;
                            end if;
                        end loop;
                    when C_ADDR_TRIG_MASK =>
                        for i in 0 to C_TRIG_WORDS - 1 loop
                            if to_integer(trig_word_sel_r) = i then
                                trig_mask_flat((i + 1) * 32 - 1 downto i * 32)
                                    <= wr_data;
                            end if;
                        end loop;
                    when C_ADDR_TRIG_WORD_SEL =>
                        trig_word_sel_r <= unsigned(wr_data(7 downto 0));
                    when C_ADDR_COND_SEL =>
                        -- RTL-P3.647: select which comparator-array slot the
                        -- next COND_CFG/COND_VAL write targets (paged, like
                        -- TRIG_WORD_SEL).
                        cond_sel_r <= unsigned(wr_data(7 downto 0));
                    when C_ADDR_COND_CFG =>
                        -- Write the selected slot's {valid,op,width,lsb} word.
                        -- Static-bound slice gated on the dynamic sel (nvc-safe;
                        -- out-of-range sel matches no slot → write dropped).
                        for i in 0 to G_TRIG_CONDS - 1 loop
                            if to_integer(cond_sel_r) = i then
                                cond_cfg_flat((i + 1) * 32 - 1 downto i * 32)
                                    <= wr_data;
                            end if;
                        end loop;
                    when C_ADDR_COND_VAL =>
                        for i in 0 to G_TRIG_CONDS - 1 loop
                            if to_integer(cond_sel_r) = i then
                                cond_val_flat((i + 1) * 32 - 1 downto i * 32)
                                    <= wr_data;
                            end if;
                        end loop;
                    when C_ADDR_CHAN_SEL =>
                        chan_sel_r <= wr_data;
                    when C_ADDR_DECIM =>
                        decim_r <= wr_data;

                    when others =>
                        -- REA-REQ-012: writes to RO/unmapped addrs
                        -- are dropped on the floor.
                        null;
                end case;
            end if;
        end if;
    end process;

    -- ── Read port (combinational decode → registered driver) ─────
    --
    -- Pure-combinational read keeps the protocol simple for the JTAG
    -- iface; rd_addr stable for one jtag_clk → rd_data presented same
    -- cycle. The iface registers it on its TDO output.
    process (rd_addr,
             pretrig_r, posttrig_r, trig_mode_r, trig_value_flat,
             trig_mask_flat, trig_word_sel_r, chan_sel_r, decim_r,
             cond_cfg_flat, cond_val_flat, cond_sel_r,
             armed_in, triggered_in, done_in, overflow_in,
             start_ptr_in, capture_len_w)
        variable status : std_logic_vector(31 downto 0);
        variable spr    : std_logic_vector(31 downto 0);
    begin
        status := (others => '0');
        status(C_STATUS_BIT_ARMED)     := armed_in;
        status(C_STATUS_BIT_TRIGGERED) := triggered_in;
        status(C_STATUS_BIT_DONE)      := done_in;
        status(C_STATUS_BIT_OVERFLOW)  := overflow_in;

        spr := (others => '0');
        spr(C_PTR_W - 1 downto 0) := start_ptr_in;

        case unsigned(rd_addr) is
            when C_ADDR_VERSION     => rd_data <= C_REA_VERSION;
            when C_ADDR_STATUS      => rd_data <= status;
            when C_ADDR_SAMPLE_W    => rd_data <= C_REG_SAMPLE_W;
            when C_ADDR_DEPTH       => rd_data <= C_REG_DEPTH;
            when C_ADDR_PRETRIG     => rd_data <= pretrig_r;
            when C_ADDR_POSTTRIG    => rd_data <= posttrig_r;
            when C_ADDR_CAPTURE_LEN => rd_data <= capture_len_w;
            when C_ADDR_TRIG_MODE   => rd_data <= trig_mode_r;
            when C_ADDR_TRIG_VALUE  =>
                -- Read back the 32-bit word selected by trig_word_sel_r.
                rd_data <= (others => '0');
                for i in 0 to C_TRIG_WORDS - 1 loop
                    if to_integer(trig_word_sel_r) = i then
                        rd_data <= trig_value_flat(
                            (i + 1) * 32 - 1 downto i * 32);
                    end if;
                end loop;
            when C_ADDR_TRIG_MASK   =>
                rd_data <= (others => '0');
                for i in 0 to C_TRIG_WORDS - 1 loop
                    if to_integer(trig_word_sel_r) = i then
                        rd_data <= trig_mask_flat(
                            (i + 1) * 32 - 1 downto i * 32);
                    end if;
                end loop;
            when C_ADDR_TRIG_WORD_SEL =>
                rd_data <= (others => '0');
                rd_data(7 downto 0) <= std_logic_vector(trig_word_sel_r);
            when C_ADDR_COND_SEL =>
                rd_data <= (others => '0');
                rd_data(7 downto 0) <= std_logic_vector(cond_sel_r);
            when C_ADDR_COND_CFG =>
                -- Read back the selected slot's cfg word.
                rd_data <= (others => '0');
                for i in 0 to G_TRIG_CONDS - 1 loop
                    if to_integer(cond_sel_r) = i then
                        rd_data <= cond_cfg_flat((i + 1) * 32 - 1 downto i * 32);
                    end if;
                end loop;
            when C_ADDR_COND_VAL =>
                rd_data <= (others => '0');
                for i in 0 to G_TRIG_CONDS - 1 loop
                    if to_integer(cond_sel_r) = i then
                        rd_data <= cond_val_flat((i + 1) * 32 - 1 downto i * 32);
                    end if;
                end loop;
            when C_ADDR_CHAN_SEL    => rd_data <= chan_sel_r;
            when C_ADDR_NUM_CHAN    => rd_data <= C_REG_NUM_CHAN;
            when C_ADDR_DECIM       => rd_data <= decim_r;
            when C_ADDR_TIMESTAMP_W => rd_data <= C_REG_TIMESTAMP_W;
            when C_ADDR_START_PTR   => rd_data <= spr;
            when others             => rd_data <= (others => '0');
        end case;
    end process;

end architecture;
