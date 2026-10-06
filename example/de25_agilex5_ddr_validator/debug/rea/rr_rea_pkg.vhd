-- SPDX-FileCopyrightText: 2026 Daniel J. Mazure
-- SPDX-License-Identifier: MIT
--
-- rr_rea_pkg — shared types and constants for the RouteRTL Embedded
-- Analyzer (REA) IP family. Pulled in by every block in the hierarchy.
--
-- The JTAG register addresses below are FROZEN — they form the SW-
-- interface contract for the REA host library (subset of the
-- frozen ELA register map). Adding registers is fine; renumbering
-- existing ones breaks the host SW.

library ieee;
    use ieee.std_logic_1164.all;
    use ieee.numeric_std.all;

package rr_rea_pkg is

    -- ── Magic + version ──────────────────────────────────────────
    -- 'REA' (0x524541) in the upper 24 bits + minor version in low 8.
    constant C_REA_VERSION : std_logic_vector(31 downto 0) := x"52454101";

    -- ── JTAG register map (host SW contract — DO NOT renumber) ───
    constant C_ADDR_VERSION     : unsigned(15 downto 0) := x"0000";  -- RO
    constant C_ADDR_CTRL        : unsigned(15 downto 0) := x"0004";  -- WO
    constant C_ADDR_STATUS      : unsigned(15 downto 0) := x"0008";  -- RO
    constant C_ADDR_SAMPLE_W    : unsigned(15 downto 0) := x"000C";  -- RO
    constant C_ADDR_DEPTH       : unsigned(15 downto 0) := x"0010";  -- RO
    constant C_ADDR_PRETRIG     : unsigned(15 downto 0) := x"0014";  -- RW
    constant C_ADDR_POSTTRIG    : unsigned(15 downto 0) := x"0018";  -- RW
    constant C_ADDR_CAPTURE_LEN : unsigned(15 downto 0) := x"001C";  -- RO
    constant C_ADDR_TRIG_MODE   : unsigned(15 downto 0) := x"0020";  -- RW
    constant C_ADDR_TRIG_VALUE  : unsigned(15 downto 0) := x"0024";  -- RW
    constant C_ADDR_TRIG_MASK   : unsigned(15 downto 0) := x"0028";  -- RW
    -- RTL-P2.658(b): word-select for the multiword (banked) TRIG_VALUE /
    -- TRIG_MASK when G_SAMPLE_W > 32. Writes/reads to 0x24/0x28 target
    -- the 32-bit word indexed by this register. Resets to 0, so a legacy
    -- host (or any G_SAMPLE_W<=32 probe) that never touches it always
    -- hits word 0 — byte-identical to the pre-v0.4 single-word behaviour.
    constant C_ADDR_TRIG_WORD_SEL : unsigned(15 downto 0) := x"002C";  -- RW v0.4
    -- RTL-P3.647: per-condition comparator array (mixed-op AND triggers).
    -- Paged like TRIG_WORD_SEL: COND_SEL picks slot k; COND_CFG/COND_VAL
    -- write that slot's {valid,op,width,field_lsb} + 32-bit value. Enabled by
    -- TRIG_MODE bit[2]. Inert (and the legacy single-comparator path
    -- unchanged) when array_enable=0 / no slots valid.
    constant C_ADDR_COND_SEL    : unsigned(15 downto 0) := x"0030";  -- RW v0.5
    constant C_ADDR_COND_CFG    : unsigned(15 downto 0) := x"0034";  -- RW v0.5
    constant C_ADDR_COND_VAL    : unsigned(15 downto 0) := x"0038";  -- RW v0.5
    constant C_ADDR_CHAN_SEL    : unsigned(15 downto 0) := x"00A0";  -- RW (=0 v0.1)
    constant C_ADDR_NUM_CHAN    : unsigned(15 downto 0) := x"00A4";  -- RO (=1 v0.1)
    constant C_ADDR_DECIM       : unsigned(15 downto 0) := x"00B0";  -- RW v0.3

    -- ── Sequencer registers (REA-REQ-607, v0.3) ──────────────────
    -- Per-stage block at ADDR_SEQ_BASE + N * SEQ_STRIDE:
    --   +0x00  cfg          (mode bits + count_target)
    --   +0x04  value_a
    --   +0x08  mask_a
    --   +0x0C  value_b      (reserved for v0.4 compound conditions)
    --   +0x10  mask_b       (reserved for v0.4 compound conditions)
    -- Layout is frozen (see SPEC.md) so any future host SW
    -- reuse keeps the same wire format.
    --
    -- WIDTH CONTRACT (RTL-P3.691, sibling of RTL-P2.658b): the per-stage
    -- value_a/mask_a (and value_b/mask_b) RW slots are NOT YET implemented
    -- in rr_rea_regbank. The capture-FSM already carries them full-width
    -- (G_SAMPLE_W bits per stage), so WHEN these JTAG slots are added they
    -- MUST be banked exactly like TRIG_VALUE/TRIG_MASK — a 32-bit window
    -- into trig_words(G_SAMPLE_W) words, paged by TRIG_WORD_SEL (0x2C) or a
    -- SEQ-local equivalent. SEQ_STRIDE stays 20 bytes (one 32-bit window per
    -- field) so the address map and wire format are preserved; the
    -- extra words are reached by paging, never by widening the slot. Do NOT
    -- reintroduce a single-32-bit value_a/mask_a — that is the exact cap
    -- P2.658b removed from the legacy path.
    constant C_ADDR_SEQ_BASE    : unsigned(15 downto 0) := x"0040";
    constant C_SEQ_STRIDE       : positive := 20;  -- bytes per stage
    constant C_ADDR_TIMESTAMP_W : unsigned(15 downto 0) := x"00C4";  -- RO
    constant C_ADDR_START_PTR   : unsigned(15 downto 0) := x"00C8";  -- RO
    constant C_ADDR_DATA_BASE   : unsigned(15 downto 0) := x"0100";  -- RO

    -- ── CTRL register bit assignments ────────────────────────────
    constant C_CTRL_BIT_ARM     : natural := 0;
    constant C_CTRL_BIT_RESET   : natural := 1;

    -- ── STATUS register bit assignments ──────────────────────────
    constant C_STATUS_BIT_ARMED      : natural := 0;
    constant C_STATUS_BIT_TRIGGERED  : natural := 1;
    constant C_STATUS_BIT_DONE       : natural := 2;
    constant C_STATUS_BIT_OVERFLOW   : natural := 3;

    -- ── TRIG_MODE values ─────────────────────────────────────────
    constant C_TRIG_MODE_VALUE_MATCH : std_logic_vector(31 downto 0) := x"00000001";

    -- bit[1] = enable multi-stage sequencer (v0.3, REA-REQ-601).
    -- When 0, the FSM uses the flat single-comparator path
    -- (TRIG_VALUE / TRIG_MASK at 0x24/0x28, REA-REQ-100..106).
    -- When 1, per-stage seq_value_k / seq_mask_k at ADDR_SEQ_BASE+
    -- drive the trigger; the final stage's match fires capture.
    constant C_TRIG_MODE_BIT_SEQ_EN : natural := 1;

    -- bit[2] = enable per-condition comparator array (RTL-P3.647). When 1, the
    -- FSM ANDs the per-condition {op,value,mask} slots (COND_SEL/CFG/VAL) into
    -- the trigger instead of the single-comparator path — this is how a
    -- mixed-op AND ('counter < 5 AND state == 1', two edges, …) is expressed.
    -- seq_en (bit[1]) takes precedence if both are set. 0 → legacy path.
    constant C_TRIG_MODE_BIT_ARRAY_EN : natural := 2;

    -- bit[3]  = enable external board-pin trigger (RTL-P3.266). When 1, the
    --           wrapper's `ext_trigger_in` package-pin input participates in
    --           the fire decision (the user routes a board pin → the ELA
    --           wrapper's ext_trigger_in in their HDL + XDC). When 0, the pin
    --           is ignored and behaviour is exactly the internal-only path.
    -- bit[8]  = external-trigger combine mode: 0 = OR with the internal
    --           trigger (fire on EITHER — cross-board "any of us trips"),
    --           1 = AND (fire only when the internal condition AND the pin are
    --           both asserted — scope-gated / armed-window captures).
    -- Distinct from the trig_xbar `trigger_in` CDC pulse (REA-REQ-400/401),
    -- which is an on-chip cross-core sync and always an independent OR.
    constant C_TRIG_MODE_BIT_EXT_EN  : natural := 3;
    constant C_TRIG_MODE_BIT_EXT_AND : natural := 8;

    -- ── Per-condition slot CFG word layout (COND_CFG, RTL-P3.647) ──
    -- {valid[31], op[27:24], field_width[23:16], field_lsb[15:8], rsvd[7:0]}.
    -- field_lsb addresses up to C_MAX_SAMPLE_W (256) bits; field_width is the
    -- contiguous field size (per-condition compare value is ≤32 bits — a
    -- wider full-width EQ uses the single-comparator path, RTL-P2.658b).
    constant C_COND_VALID_BIT  : natural := 31;
    constant C_COND_OP_LSB     : natural := 24;   -- 4 bits, reuses C_TRIG_OP_*
    constant C_COND_WIDTH_LSB  : natural := 16;   -- 8 bits
    constant C_COND_LSB_LSB    : natural := 8;    -- 8 bits

    -- bits[7:4] = comparator op for the single-comparator (legacy) path
    -- (RTL-P3.644/645/646). bit[0]=value_match stays set for back-compat;
    -- op 0 (EQ) == the historical masked-equality behaviour, so a host that
    -- writes the bare x"00000001" still gets EQ. The op applies to the masked
    -- field (probe_in and TRIG_MASK) vs (TRIG_VALUE and TRIG_MASK):
    --   EQ  ==   NE  /=   LT  <   GT  >  (unsigned, masked field)
    --   RISE/FALL — any masked bit transitioning 0->1 / 1->0 (needs prev sample)
    constant C_TRIG_OP_LSB   : natural := 4;
    constant C_TRIG_OP_WIDTH : natural := 4;
    constant C_TRIG_OP_EQ    : natural := 0;
    constant C_TRIG_OP_NE    : natural := 1;
    constant C_TRIG_OP_LT    : natural := 2;
    constant C_TRIG_OP_GT    : natural := 3;
    constant C_TRIG_OP_RISE  : natural := 4;
    constant C_TRIG_OP_FALL  : natural := 5;

    -- ── Trigger value/mask width support (RTL-P2.658) ────────────
    -- The trigger comparator covers the full G_SAMPLE_W; the JTAG
    -- datapath is 32-bit, so wide value/mask are banked into
    -- trig_words(G_SAMPLE_W) = ceil(W/32) words paged via TRIG_WORD_SEL.
    -- C_MAX_SAMPLE_W is the fail-fast elaboration ceiling (256 bits =
    -- 8 words) — aligned with the 256-bit dpram readback burst, so the
    -- trigger can cover anything the core can capture in one burst.
    constant C_TRIG_WORD_W  : positive := 32;
    constant C_MAX_SAMPLE_W : positive := 256;

    -- ── Helpers ──────────────────────────────────────────────────
    function clog2(n : natural) return natural;

    -- Number of 32-bit words needed to hold a w-bit value: ceil(w/32).
    function trig_words(w : positive) return positive;

end package;

package body rr_rea_pkg is

    function clog2(n : natural) return natural is
        variable r : natural := 0;
        variable v : natural := 1;
    begin
        while v < n loop
            v := v * 2;
            r := r + 1;
        end loop;
        return r;
    end function;

    function trig_words(w : positive) return positive is
    begin
        return (w + C_TRIG_WORD_W - 1) / C_TRIG_WORD_W;
    end function;

end package body;
