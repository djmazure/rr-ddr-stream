-------------------------------------------------------------------------------
-- Title      : rr_fifo_async_mixed — mixed-width async FIFO wrapper
-- Project    : rr-ddr-stream
-------------------------------------------------------------------------------
-- File       : rr_fifo_async_mixed.vhd
-- Author     : Daniel J. Mazure <dasjimaz@gmail.com>
-- Created    : 2026-05-15
-- Standard   : VHDL'08
-------------------------------------------------------------------------------
-- Description:
--   Asynchronous FIFO with separate write- and read-side widths. The two
--   widths MUST be in an integer ratio (one is a multiple of the other);
--   the wrapper internally chains an open-logic width-converter with a
--   same-width async FIFO to satisfy both halves.
--
--   The fifo_impl_g generic picks the implementation:
--
--     "olo"  (default) : open-logic chain — Quartus + Vivado portable.
--                        - Upsize (wr<rd) : olo_base_wconv_n2xn (wr_clk) →
--                                           olo_base_fifo_async (CDC, rd width).
--                        - Downsize (wr>rd): olo_base_fifo_async (CDC, wr width) →
--                                            olo_base_wconv_xn2n (rd_clk).
--                        - Equal widths   : olo_base_fifo_async alone.
--     "xpm"            : Xilinx xpm_fifo_async with WRITE_DATA_WIDTH ≠
--                        READ_DATA_WIDTH. Vivado-only.
--
--   wr_data_count / rd_data_count semantics match XPM's "count in own
--   width" convention:
--     - Upsize:   wr_data_count = FIFO In_Level × ratio (write-side narrow
--                  units; off-by-up-to-(ratio-1) due to wconv residue,
--                  fine for observability).
--                  rd_data_count = FIFO Out_Level (wide units).
--     - Downsize: wr_data_count = FIFO In_Level (wide units).
--                  rd_data_count = FIFO Out_Level × ratio (narrow units;
--                  same residue caveat).
--
--   RR--P3.1 (2026-05-15) — DDR-P3.6 follow-up. Completes the AVMM
--   Quartus-portability story by replacing the upload + download
--   mixed-width XPM FIFOs in ddr_fifo_intf.
--
--   DDR-P2.2 (2026-05-26) — reset-CDC fix. The rd-domain pieces (the
--   downsize-path wconv_xn2n, and the async FIFO's Out_Rst port) were
--   wired to the raw wr-domain rst_i, an unsynchronised reset-deassert
--   crossing into rd_clk_i. The wconv now resets off the FIFO's
--   synchronised out_rstout_s; the FIFO's Out_Rst is tied '0' so olo's
--   internal olo_base_cc_reset crosses In_Rst→Out coherently (the
--   canonical single-reset usage). No functional change in zero-delay
--   sim; removes the slow-corner recovery violation on HW.
-------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use ieee.math_real.all;

-- RR--T2.1: open-logic entities are referenced via their canonical
-- library `olo`, NOT `work` (see rr_fifo_sync.vhd for full note).
library olo;

-- RR--P3.3: file-scope `library xpm; use xpm.vcomponents.all;` removed.
-- The XPM body is delegated to rr_fifo_async_mixed_xpm_body (vendor-
-- gated under rtl/support/xilinx/) so this wrapper analyses cleanly
-- on Quartus / GHDL / NVC. fifo_impl_g = "olo" | "xpm" unchanged.

entity rr_fifo_async_mixed is
  generic (
    fifo_impl_g         : string  := "olo";  -- "olo" | "xpm"
    -- FIFO sizing — expressed in WRITE-width units, matching XPM's
    -- FIFO_WRITE_DEPTH convention. Must be a power of two.
    wr_data_width_g     : positive;
    rd_data_width_g     : positive;
    fifo_wr_depth_g     : positive;
    fwft_g              : boolean := true;
    cdc_sync_stages_g   : positive range 2 to 4 := 2;
    prog_full_thresh_g  : natural := 0;      -- 0 → fifo_wr_depth_g - 16
    prog_empty_thresh_g : natural := 0;      -- 0 → 8
    wr_data_count_width_g : positive := 12;
    rd_data_count_width_g : positive := 12
  );
  port (
    rst_i           : in  std_logic;
    -- Write side (wr_clk_i domain) — width = wr_data_width_g
    wr_clk_i        : in  std_logic;
    din_i           : in  std_logic_vector(wr_data_width_g-1 downto 0);
    wr_en_i         : in  std_logic;
    full_o          : out std_logic;
    -- DDR-T2.14: backend-accurate "a write asserted THIS cycle will be
    -- accepted (lands)". This is NOT simply `not full_o`: the olo upsize path's
    -- full_o reflects the WIDE async-FIFO full, yet a narrow write is still
    -- absorbed by the wconv_n2xn packing stage when that wide FIFO is full — so
    -- a write can land while full_o='1'. xpm's asymmetric FIFO drops a write
    -- when full. Consumers that must guarantee a driven write is not silently
    -- dropped (e.g. the partial-word pad-flush in ddr_fifo_intf) gate on this
    -- instead of full_o, which is correct for BOTH backends.
    wr_ready_o      : out std_logic;
    almost_full_o   : out std_logic;
    prog_full_o     : out std_logic;
    wr_data_count_o : out std_logic_vector(wr_data_count_width_g-1 downto 0);
    wr_rst_busy_o   : out std_logic;
    -- Read side (rd_clk_i domain) — width = rd_data_width_g
    rd_clk_i        : in  std_logic;
    rd_en_i         : in  std_logic;
    dout_o          : out std_logic_vector(rd_data_width_g-1 downto 0);
    empty_o         : out std_logic;
    almost_empty_o  : out std_logic;
    prog_empty_o    : out std_logic;
    data_valid_o    : out std_logic;
    rd_data_count_o : out std_logic_vector(rd_data_count_width_g-1 downto 0);
    rd_rst_busy_o   : out std_logic
  );
end entity rr_fifo_async_mixed;

architecture rtl of rr_fifo_async_mixed is

  -- Strip surrounding double-quotes if the simulator's `-g` flag passed
  -- the string with literal quote chars (NVC behaviour as of 1.16).
  function strip_quotes(s : string) return string is
  begin
    if s'length >= 2 and s(s'left) = '"' and s(s'right) = '"' then
      return s(s'left+1 to s'right-1);
    end if;
    return s;
  end function;

  constant C_IMPL : string := strip_quotes(fifo_impl_g);

  -- RR--P3.3 (2026-05-16): local integer min/max — Quartus 23.1 Std
  -- doesn't recognise VHDL 2008's `maximum(integer, integer)` /
  -- `minimum(integer, integer)` even with `--std=2008`. Local helpers
  -- compile cleanly everywhere (Vivado, Quartus Std/Pro, GHDL, NVC).
  function imax(a, b : positive) return positive is
  begin
    if a > b then return a; else return b; end if;
  end function;
  function imin(a, b : positive) return positive is
  begin
    if a < b then return a; else return b; end if;
  end function;

  -- Geometry.
  constant C_RATIO        : positive := imax(wr_data_width_g, rd_data_width_g) /
                                        imin(wr_data_width_g, rd_data_width_g);
  constant C_WIDE_WIDTH   : positive := imax(wr_data_width_g, rd_data_width_g);
  -- FIFO depth at the WIDE side (XPM gives FIFO_WRITE_DEPTH at write side
  -- units; convert when wr is narrow):
  constant C_WIDE_DEPTH   : positive := fifo_wr_depth_g / (C_WIDE_WIDTH / wr_data_width_g);
  constant C_WIDE_LEVEL_W : positive := integer(ceil(log2(real(C_WIDE_DEPTH+1))));

  -- Threshold defaults. olo path uses wide-units (matches its level
  -- semantic); XPM path uses write-units (matches its native generic).
  function olo_full_th return natural is
  begin
    if prog_full_thresh_g = 0 then
      return C_WIDE_DEPTH - 16;
    else
      return prog_full_thresh_g / (C_WIDE_WIDTH / wr_data_width_g);
    end if;
  end function;

  function olo_empty_th return natural is
  begin
    if prog_empty_thresh_g = 0 then
      return 8;
    else
      return prog_empty_thresh_g / (C_WIDE_WIDTH / wr_data_width_g);
    end if;
  end function;

  function xpm_full_th return natural is
  begin
    if prog_full_thresh_g = 0 then
      return fifo_wr_depth_g - 16;
    else
      return prog_full_thresh_g;
    end if;
  end function;

  function xpm_empty_th return natural is
  begin
    if prog_empty_thresh_g = 0 then
      return 8;
    else
      return prog_empty_thresh_g;
    end if;
  end function;

  constant C_PROG_FULL_TH  : natural := olo_full_th;
  constant C_PROG_EMPTY_TH : natural := olo_empty_th;
  constant C_XPM_FULL_TH   : natural := xpm_full_th;
  constant C_XPM_EMPTY_TH  : natural := xpm_empty_th;

  function fwft_str return string is
  begin
    if fwft_g then return "fwft"; else return "std"; end if;
  end function;

  function ratio_left_shift(level : std_logic_vector; ratio : positive; out_w : positive)
    return std_logic_vector is
    variable v : unsigned(out_w-1 downto 0) := (others => '0');
  begin
    v := resize(unsigned(level) * to_unsigned(ratio, 8), out_w);
    return std_logic_vector(v);
  end function;

  function resize_count(level : std_logic_vector; out_w : positive)
    return std_logic_vector is
    variable v : unsigned(out_w-1 downto 0) := (others => '0');
  begin
    v := resize(unsigned(level), out_w);
    return std_logic_vector(v);
  end function;

begin

  -----------------------------------------------------------------------------
  -- XPM backend — native mixed-width async FIFO.
  -- RR--P3.3: body delegated to rr_fifo_async_mixed_xpm_body (vendor-gated).
  -----------------------------------------------------------------------------
  GEN_XPM : if C_IMPL = "xpm" generate
    signal full_xpm_s : std_logic;
  begin
    i_xpm_body : entity work.rr_fifo_async_mixed_xpm_body
      generic map (
        wr_data_width_g       => wr_data_width_g,
        rd_data_width_g       => rd_data_width_g,
        fifo_wr_depth_g       => fifo_wr_depth_g,
        fwft_g                => fwft_g,
        cdc_sync_stages_g     => cdc_sync_stages_g,
        prog_full_thresh_g    => prog_full_thresh_g,
        prog_empty_thresh_g   => prog_empty_thresh_g,
        wr_data_count_width_g => wr_data_count_width_g,
        rd_data_count_width_g => rd_data_count_width_g
      )
      port map (
        rst_i           => rst_i,
        wr_clk_i        => wr_clk_i,
        din_i           => din_i,
        wr_en_i         => wr_en_i,
        full_o          => full_xpm_s,
        almost_full_o   => almost_full_o,
        prog_full_o     => prog_full_o,
        wr_data_count_o => wr_data_count_o,
        wr_rst_busy_o   => wr_rst_busy_o,
        rd_clk_i        => rd_clk_i,
        rd_en_i         => rd_en_i,
        dout_o          => dout_o,
        empty_o         => empty_o,
        almost_empty_o  => almost_empty_o,
        prog_empty_o    => prog_empty_o,
        data_valid_o    => data_valid_o,
        rd_data_count_o => rd_data_count_o,
        rd_rst_busy_o   => rd_rst_busy_o
      );
    full_o     <= full_xpm_s;
    -- DDR-T2.14: xpm asymmetric FIFO drops a write when full → ready = not full.
    wr_ready_o <= not full_xpm_s;
  end generate GEN_XPM;

  -----------------------------------------------------------------------------
  -- olo backend — upsize path: wconv_n2xn (wr_clk) → fifo_async (CDC).
  -- Narrow user writes are packed by the converter (in wr_clk domain), then
  -- the wide stream crosses to rd_clk via the async FIFO.
  -----------------------------------------------------------------------------
  GEN_OLO_UPSIZE : if (C_IMPL = "olo") and (wr_data_width_g < rd_data_width_g) generate
    signal wconv_out_data  : std_logic_vector(C_WIDE_WIDTH-1 downto 0);
    signal wconv_out_valid : std_logic;
    signal wconv_out_ready : std_logic;
    signal wconv_in_ready  : std_logic;
    signal in_rstout_s     : std_logic;
    signal in_full_s       : std_logic;
    signal in_almf_s       : std_logic;
    signal in_level_s      : std_logic_vector(C_WIDE_LEVEL_W-1 downto 0);
    signal out_valid_s     : std_logic;
    signal out_empty_s     : std_logic;
    signal out_alme_s      : std_logic;
    signal out_level_s     : std_logic_vector(C_WIDE_LEVEL_W-1 downto 0);
    signal out_rstout_s    : std_logic;
  begin
    -- wconv_n2xn: pack wr_data_width_g writes into one wide word.
    i_wconv : entity olo.olo_base_wconv_n2xn
      generic map (
        InWidth_g  => wr_data_width_g,
        OutWidth_g => C_WIDE_WIDTH
      )
      port map (
        Clk         => wr_clk_i,
        Rst         => rst_i,
        In_Valid    => wr_en_i,
        -- DDR-T2.14: surface the converter's accept signal. The packing stage
        -- absorbs a narrow write even when the downstream wide FIFO is full, so
        -- this (not `not full_o`) is the accurate "this write lands" indication.
        In_Ready    => wconv_in_ready,
        In_Data     => din_i,
        In_Last     => '0',
        Out_Valid   => wconv_out_valid,
        Out_Ready   => wconv_out_ready,
        Out_Data    => wconv_out_data,
        Out_Last    => open,
        Out_WordEna => open
      );

    -- Async FIFO at the wide width.
    i_fifo : entity olo.olo_base_fifo_async
      generic map (
        Width_g         => C_WIDE_WIDTH,
        Depth_g         => C_WIDE_DEPTH,
        AlmFullOn_g     => true,
        AlmFullLevel_g  => C_PROG_FULL_TH,
        AlmEmptyOn_g    => true,
        AlmEmptyLevel_g => C_PROG_EMPTY_TH,
        RamStyle_g      => "auto",
        RamBehavior_g   => "RBW",
        ReadyRstState_g => '0',
        Optimization_g  => "SPEED",
        SyncStages_g    => cdc_sync_stages_g
      )
      port map (
        In_Clk       => wr_clk_i,
        In_Rst       => rst_i,
        In_RstOut    => in_rstout_s,
        In_Data      => wconv_out_data,
        In_Valid     => wconv_out_valid,
        In_Ready     => wconv_out_ready,
        In_Full      => in_full_s,
        In_Empty     => open,
        In_AlmFull   => in_almf_s,
        In_AlmEmpty  => open,
        In_Level     => in_level_s,
        Out_Clk      => rd_clk_i,
        Out_Rst      => '0',  -- DDR-P2.2: out side resets via olo's internal In_Rst→Out crossing; never sample write-domain rst_i in the rd_clk domain
        Out_RstOut   => out_rstout_s,
        Out_Data     => dout_o,
        Out_Valid    => out_valid_s,
        Out_Ready    => rd_en_i,
        Out_Full     => open,
        Out_Empty    => out_empty_s,
        Out_AlmFull  => open,
        Out_AlmEmpty => out_alme_s,
        Out_Level    => out_level_s
      );

    full_o          <= in_full_s;
    wr_ready_o      <= wconv_in_ready;
    almost_full_o   <= in_almf_s;
    prog_full_o     <= in_almf_s;
    wr_data_count_o <= ratio_left_shift(in_level_s, C_RATIO, wr_data_count_width_g);
    wr_rst_busy_o   <= in_rstout_s;

    empty_o         <= out_empty_s;
    almost_empty_o  <= out_alme_s;
    prog_empty_o    <= out_alme_s;
    data_valid_o    <= out_valid_s;
    rd_data_count_o <= resize_count(out_level_s, rd_data_count_width_g);
    rd_rst_busy_o   <= out_rstout_s;
  end generate GEN_OLO_UPSIZE;

  -----------------------------------------------------------------------------
  -- olo backend — downsize path: fifo_async (CDC, wr width) → wconv_xn2n
  -- (rd_clk). Wide writes cross domains, then narrow reads emerge after
  -- the converter (in rd_clk domain).
  -----------------------------------------------------------------------------
  GEN_OLO_DOWNSIZE : if (C_IMPL = "olo") and (wr_data_width_g > rd_data_width_g) generate
    signal fifo_out_data   : std_logic_vector(C_WIDE_WIDTH-1 downto 0);
    signal fifo_out_valid  : std_logic;
    signal fifo_out_ready  : std_logic;
    signal in_rstout_s     : std_logic;
    signal in_full_s       : std_logic;
    signal in_almf_s       : std_logic;
    signal in_ready_s      : std_logic;
    signal in_level_s      : std_logic_vector(C_WIDE_LEVEL_W-1 downto 0);
    signal out_empty_s     : std_logic;
    signal out_alme_s      : std_logic;
    signal out_level_s     : std_logic_vector(C_WIDE_LEVEL_W-1 downto 0);
    signal out_rstout_s    : std_logic;
    signal wconv_out_valid : std_logic;
  begin
    i_fifo : entity olo.olo_base_fifo_async
      generic map (
        Width_g         => C_WIDE_WIDTH,
        Depth_g         => C_WIDE_DEPTH,
        AlmFullOn_g     => true,
        AlmFullLevel_g  => C_PROG_FULL_TH,
        AlmEmptyOn_g    => true,
        AlmEmptyLevel_g => C_PROG_EMPTY_TH,
        RamStyle_g      => "auto",
        RamBehavior_g   => "RBW",
        ReadyRstState_g => '0',
        Optimization_g  => "SPEED",
        SyncStages_g    => cdc_sync_stages_g
      )
      port map (
        In_Clk       => wr_clk_i,
        In_Rst       => rst_i,
        In_RstOut    => in_rstout_s,
        In_Data      => din_i,
        In_Valid     => wr_en_i,
        In_Ready     => in_ready_s,
        In_Full      => in_full_s,
        In_Empty     => open,
        In_AlmFull   => in_almf_s,
        In_AlmEmpty  => open,
        In_Level     => in_level_s,
        Out_Clk      => rd_clk_i,
        Out_Rst      => '0',  -- DDR-P2.2: out side resets via olo's internal In_Rst→Out crossing; never sample write-domain rst_i in the rd_clk domain
        Out_RstOut   => out_rstout_s,
        Out_Data     => fifo_out_data,
        Out_Valid    => fifo_out_valid,
        Out_Ready    => fifo_out_ready,
        Out_Full     => open,
        Out_Empty    => out_empty_s,
        Out_AlmFull  => open,
        Out_AlmEmpty => out_alme_s,
        Out_Level    => out_level_s
      );

    -- wconv_xn2n: unpack wide reads into rd_data_width_g chunks.
    i_wconv : entity olo.olo_base_wconv_xn2n
      generic map (
        InWidth_g  => C_WIDE_WIDTH,
        OutWidth_g => rd_data_width_g
      )
      port map (
        Clk        => rd_clk_i,
        -- DDR-P2.2: this wconv is clocked by rd_clk_i, so it MUST reset off
        -- the FIFO's rd-domain synchronised reset (out_rstout_s), NOT raw
        -- rst_i. rst_i is in the wr_clk_i (In) domain; feeding it here was an
        -- unsynchronised reset-deassert CDC into r.DataVld[*]|sclr (STA:
        -- -4.0 ns recovery from the EMIF reset-sync anchor, slow corner).
        Rst        => out_rstout_s,
        In_Valid   => fifo_out_valid,
        In_Ready   => fifo_out_ready,
        In_Data    => fifo_out_data,
        In_Last    => '0',
        In_WordEna => (others => '1'),
        Out_Valid  => wconv_out_valid,
        Out_Ready  => rd_en_i,
        Out_Data   => dout_o,
        Out_Last   => open
      );

    full_o          <= in_full_s;
    wr_ready_o      <= in_ready_s;
    almost_full_o   <= in_almf_s;
    prog_full_o     <= in_almf_s;
    wr_data_count_o <= resize_count(in_level_s, wr_data_count_width_g);
    wr_rst_busy_o   <= in_rstout_s;

    -- Empty-tracks-wconv (not the wide FIFO): with the FIFO empty but
    -- the wconv still holding 1..ratio-1 narrow leftovers, the consumer
    -- must keep popping. Tracking only FIFO Out_Empty would cut off the
    -- last partial wide word's narrow tail.
    empty_o         <= not wconv_out_valid;
    almost_empty_o  <= out_alme_s;
    prog_empty_o    <= out_alme_s;
    data_valid_o    <= wconv_out_valid;
    rd_data_count_o <= ratio_left_shift(out_level_s, C_RATIO, rd_data_count_width_g);
    rd_rst_busy_o   <= out_rstout_s;
  end generate GEN_OLO_DOWNSIZE;

  -----------------------------------------------------------------------------
  -- olo backend — equal widths: just olo_base_fifo_async.
  -----------------------------------------------------------------------------
  GEN_OLO_EQUAL : if (C_IMPL = "olo") and (wr_data_width_g = rd_data_width_g) generate
    signal in_rstout_s  : std_logic;
    signal in_full_s    : std_logic;
    signal in_almf_s    : std_logic;
    signal in_ready_s   : std_logic;
    signal in_level_s   : std_logic_vector(C_WIDE_LEVEL_W-1 downto 0);
    signal out_valid_s  : std_logic;
    signal out_empty_s  : std_logic;
    signal out_alme_s   : std_logic;
    signal out_level_s  : std_logic_vector(C_WIDE_LEVEL_W-1 downto 0);
    signal out_rstout_s : std_logic;
  begin
    i_fifo : entity olo.olo_base_fifo_async
      generic map (
        Width_g         => wr_data_width_g,
        Depth_g         => C_WIDE_DEPTH,
        AlmFullOn_g     => true,
        AlmFullLevel_g  => C_PROG_FULL_TH,
        AlmEmptyOn_g    => true,
        AlmEmptyLevel_g => C_PROG_EMPTY_TH,
        RamStyle_g      => "auto",
        RamBehavior_g   => "RBW",
        ReadyRstState_g => '0',
        Optimization_g  => "SPEED",
        SyncStages_g    => cdc_sync_stages_g
      )
      port map (
        In_Clk       => wr_clk_i,
        In_Rst       => rst_i,
        In_RstOut    => in_rstout_s,
        In_Data      => din_i,
        In_Valid     => wr_en_i,
        In_Ready     => in_ready_s,
        In_Full      => in_full_s,
        In_Empty     => open,
        In_AlmFull   => in_almf_s,
        In_AlmEmpty  => open,
        In_Level     => in_level_s,
        Out_Clk      => rd_clk_i,
        Out_Rst      => '0',  -- DDR-P2.2: out side resets via olo's internal In_Rst→Out crossing; never sample write-domain rst_i in the rd_clk domain
        Out_RstOut   => out_rstout_s,
        Out_Data     => dout_o,
        Out_Valid    => out_valid_s,
        Out_Ready    => rd_en_i,
        Out_Full     => open,
        Out_Empty    => out_empty_s,
        Out_AlmFull  => open,
        Out_AlmEmpty => out_alme_s,
        Out_Level    => out_level_s
      );
    full_o          <= in_full_s;
    wr_ready_o      <= in_ready_s;
    almost_full_o   <= in_almf_s;
    prog_full_o     <= in_almf_s;
    wr_data_count_o <= resize_count(in_level_s, wr_data_count_width_g);
    wr_rst_busy_o   <= in_rstout_s;
    empty_o         <= out_empty_s;
    almost_empty_o  <= out_alme_s;
    prog_empty_o    <= out_alme_s;
    data_valid_o    <= out_valid_s;
    rd_data_count_o <= resize_count(out_level_s, rd_data_count_width_g);
    rd_rst_busy_o   <= out_rstout_s;
  end generate GEN_OLO_EQUAL;

  -----------------------------------------------------------------------------
  -- Compile-time guards.
  -----------------------------------------------------------------------------
  assert (C_IMPL = "olo") or (C_IMPL = "xpm")
    report "rr_fifo_async_mixed: fifo_impl_g must be ""olo"" or ""xpm"" (got """
           & fifo_impl_g & """)"
    severity failure;

end architecture rtl;
