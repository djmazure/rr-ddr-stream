-------------------------------------------------------------------------------
-- Title      : rr_fifo_async — vendor-agnostic asynchronous FIFO wrapper
-- Project    : rr-ddr-stream
-------------------------------------------------------------------------------
-- File       : rr_fifo_async.vhd
-- Author     : Daniel J. Mazure <dasjimaz@gmail.com>
-- Created    : 2026-05-15
-- Standard   : VHDL'08
-------------------------------------------------------------------------------
-- Description:
--   Asynchronous FIFO with an XPM-shape slim port set so existing
--   xpm_fifo_async consumers (the upload + download CDC FIFOs in
--   ddr_fifo_intf) drop in without churn. The fifo_impl_g generic
--   picks the underlying implementation:
--
--     "olo"  (default) : open-logic/olo_base_fifo_async — Quartus +
--                        Vivado portable, no vendor primitives.
--     "xpm"            : Xilinx Parameterized Macro xpm_fifo_async —
--                        the historical golden reference, Vivado-only.
--
--   Olo uses separate reset inputs for each clock domain; the wrapper
--   ties both to the single rst_i (caller is responsible for keeping
--   it asserted long enough for the slow side to register it). Olo's
--   In_RstOut / Out_RstOut provide the synced busy signals exposed on
--   wr_rst_busy_o / rd_rst_busy_o.
--
--   Output flag mapping when fifo_impl_g="olo":
--     PROG_FULL  = ALMOST_FULL  = In_AlmFull   (threshold = prog_full_thresh_g)
--     PROG_EMPTY = ALMOST_EMPTY = Out_AlmEmpty (threshold = prog_empty_thresh_g)
--     DATA_VALID = Out_Valid (FWFT semantic)
--
--   DDR-P3.6 (2026-05-15) — vendor-portability for the AVMM target.
-------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use ieee.math_real.all;

-- RR--T2.1: open-logic entities are referenced via their canonical
-- library `olo`, NOT `work` (see rr_fifo_sync.vhd for full note).
library olo;

-- RR--P3.3: file-scope `library xpm; use xpm.vcomponents.all;` removed.
-- The XPM body is delegated to rr_fifo_async_xpm_body (vendor-gated
-- under rtl/support/xilinx/) so this wrapper analyses cleanly on
-- Quartus / GHDL / NVC. fifo_impl_g = "olo" | "xpm" surface unchanged.

entity rr_fifo_async is
  generic (
    fifo_impl_g         : string  := "olo";  -- "olo" | "xpm"
    fifo_depth_g        : positive;          -- number of words (must be 2^N for olo)
    data_width_g        : positive;
    fwft_g              : boolean := true;
    cdc_sync_stages_g   : positive range 2 to 4 := 2;
    prog_full_thresh_g  : natural := 0;      -- 0 → fifo_depth_g - 16
    prog_empty_thresh_g : natural := 0       -- 0 → 8
  );
  port (
    rst_i           : in  std_logic;
    -- Write side (wr_clk_i domain)
    wr_clk_i        : in  std_logic;
    din_i           : in  std_logic_vector(data_width_g-1 downto 0);
    wr_en_i         : in  std_logic;
    full_o          : out std_logic;
    almost_full_o   : out std_logic;
    prog_full_o     : out std_logic;
    wr_data_count_o : out std_logic_vector(integer(ceil(log2(real(fifo_depth_g+1))))-1 downto 0);
    wr_rst_busy_o   : out std_logic;
    -- Read side (rd_clk_i domain)
    rd_clk_i        : in  std_logic;
    rd_en_i         : in  std_logic;
    dout_o          : out std_logic_vector(data_width_g-1 downto 0);
    empty_o         : out std_logic;
    almost_empty_o  : out std_logic;
    prog_empty_o    : out std_logic;
    data_valid_o    : out std_logic;
    rd_data_count_o : out std_logic_vector(integer(ceil(log2(real(fifo_depth_g+1))))-1 downto 0);
    rd_rst_busy_o   : out std_logic
  );
end entity rr_fifo_async;

architecture rtl of rr_fifo_async is

  function default_full_th return natural is
  begin
    if prog_full_thresh_g = 0 then
      return fifo_depth_g - 16;
    else
      return prog_full_thresh_g;
    end if;
  end function;

  function default_empty_th return natural is
  begin
    if prog_empty_thresh_g = 0 then
      return 8;
    else
      return prog_empty_thresh_g;
    end if;
  end function;

  constant C_PROG_FULL_TH  : natural := default_full_th;
  constant C_PROG_EMPTY_TH : natural := default_empty_th;
  constant C_COUNT_WIDTH   : positive := integer(ceil(log2(real(fifo_depth_g+1))));

  function fwft_str return string is
  begin
    if fwft_g then return "fwft"; else return "std"; end if;
  end function;

begin

  -----------------------------------------------------------------------------
  -- XPM backend.
  -- RR--P3.3: body delegated to rr_fifo_async_xpm_body (vendor-gated).
  -----------------------------------------------------------------------------
  GEN_XPM : if fifo_impl_g = "xpm" generate
    i_xpm_body : entity work.rr_fifo_async_xpm_body
      generic map (
        fifo_depth_g        => fifo_depth_g,
        data_width_g        => data_width_g,
        fwft_g              => fwft_g,
        cdc_sync_stages_g   => cdc_sync_stages_g,
        prog_full_thresh_g  => prog_full_thresh_g,
        prog_empty_thresh_g => prog_empty_thresh_g
      )
      port map (
        rst_i           => rst_i,
        wr_clk_i        => wr_clk_i,
        din_i           => din_i,
        wr_en_i         => wr_en_i,
        full_o          => full_o,
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
  end generate GEN_XPM;

  -----------------------------------------------------------------------------
  -- olo backend (default).
  -----------------------------------------------------------------------------
  GEN_OLO : if fifo_impl_g = "olo" generate
    signal in_full_s   : std_logic;
    signal in_almf_s   : std_logic;
    signal in_level_s  : std_logic_vector(C_COUNT_WIDTH-1 downto 0);
    signal in_rstout_s : std_logic;
    signal out_valid_s : std_logic;
    signal out_empty_s : std_logic;
    signal out_alme_s  : std_logic;
    signal out_level_s : std_logic_vector(C_COUNT_WIDTH-1 downto 0);
    signal out_rstout_s: std_logic;
  begin
    i_olo : entity olo.olo_base_fifo_async
      generic map (
        Width_g         => data_width_g,
        Depth_g         => fifo_depth_g,
        AlmFullOn_g     => true,
        AlmFullLevel_g  => C_PROG_FULL_TH,
        AlmEmptyOn_g    => true,
        AlmEmptyLevel_g => C_PROG_EMPTY_TH,
        RamStyle_g      => "auto",
        RamBehavior_g   => "RBW",
        ReadyRstState_g => '0',                 -- mirror XPM: full while in reset
        Optimization_g  => "SPEED",
        SyncStages_g    => cdc_sync_stages_g
      )
      port map (
        In_Clk       => wr_clk_i,
        In_Rst       => rst_i,
        In_RstOut    => in_rstout_s,
        In_Data      => din_i,
        In_Valid     => wr_en_i,
        In_Ready     => open,
        In_Full      => in_full_s,
        In_Empty     => open,
        In_AlmFull   => in_almf_s,
        In_AlmEmpty  => open,
        In_Level     => in_level_s,
        Out_Clk      => rd_clk_i,
        Out_Rst      => rst_i,
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
    almost_full_o   <= in_almf_s;
    prog_full_o     <= in_almf_s;
    wr_data_count_o <= in_level_s;
    wr_rst_busy_o   <= in_rstout_s;

    empty_o         <= out_empty_s;
    almost_empty_o  <= out_alme_s;
    prog_empty_o    <= out_alme_s;
    data_valid_o    <= out_valid_s;
    rd_data_count_o <= out_level_s;
    rd_rst_busy_o   <= out_rstout_s;
  end generate GEN_OLO;

  -----------------------------------------------------------------------------
  -- Compile-time guard.
  -----------------------------------------------------------------------------
  assert (fifo_impl_g = "olo") or (fifo_impl_g = "xpm")
    report "rr_fifo_async: fifo_impl_g must be ""olo"" or ""xpm"" (got """
           & fifo_impl_g & """)"
    severity failure;

end architecture rtl;
