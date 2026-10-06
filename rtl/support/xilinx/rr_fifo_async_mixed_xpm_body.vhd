-------------------------------------------------------------------------------
-- rr_fifo_async_mixed_xpm_body — Xilinx-only xpm_fifo_async wrapper
-- for mixed-width async FIFO (RR--P3.3).
--
-- Carved out of rr_fifo_async_mixed.vhd's GEN_XPM body. Same gating
-- as the sibling xpm_body files — file-scope `library xpm` lives here,
-- not in the vendor-neutral wrapper.
-------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use ieee.math_real.all;

library xpm;
use xpm.vcomponents.all;

entity rr_fifo_async_mixed_xpm_body is
  generic (
    wr_data_width_g       : positive;
    rd_data_width_g       : positive;
    fifo_wr_depth_g       : positive;
    fwft_g                : boolean := true;
    cdc_sync_stages_g     : positive range 2 to 4 := 2;
    prog_full_thresh_g    : natural := 0;
    prog_empty_thresh_g   : natural := 0;
    wr_data_count_width_g : positive := 12;
    rd_data_count_width_g : positive := 12
  );
  port (
    rst_i           : in  std_logic;
    wr_clk_i        : in  std_logic;
    din_i           : in  std_logic_vector(wr_data_width_g-1 downto 0);
    wr_en_i         : in  std_logic;
    full_o          : out std_logic;
    almost_full_o   : out std_logic;
    prog_full_o     : out std_logic;
    wr_data_count_o : out std_logic_vector(wr_data_count_width_g-1 downto 0);
    wr_rst_busy_o   : out std_logic;
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
end entity rr_fifo_async_mixed_xpm_body;

architecture rtl of rr_fifo_async_mixed_xpm_body is

  function xpm_full_th return natural is
  begin
    if prog_full_thresh_g = 0 then
      -- RR--P3.6 (2026-05-16): XPM legal range for PROG_FULL_THRESH is
      -- [5, FIFO_WRITE_DEPTH - 35] (Xilinx UG953 + xpm_fifo_async
      -- runtime DRC). depth-16 was the old default; that's outside
      -- the legal window for any deep FIFO. Use depth-40 to keep a
      -- margin without crowding the upper bound.
      return fifo_wr_depth_g - 40;
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

  constant C_XPM_FULL_TH  : natural := xpm_full_th;
  constant C_XPM_EMPTY_TH : natural := xpm_empty_th;

  function fwft_str return string is
  begin
    if fwft_g then return "fwft"; else return "std"; end if;
  end function;

begin

  i_xpm : xpm_fifo_async
    generic map (
      CASCADE_HEIGHT      => 0,
      CDC_SYNC_STAGES     => cdc_sync_stages_g,
      DOUT_RESET_VALUE    => "0",
      ECC_MODE            => "no_ecc",
      FIFO_MEMORY_TYPE    => "auto",
      FIFO_READ_LATENCY   => 1,
      FIFO_WRITE_DEPTH    => fifo_wr_depth_g,
      FULL_RESET_VALUE    => 0,
      PROG_EMPTY_THRESH   => C_XPM_EMPTY_TH,
      PROG_FULL_THRESH    => C_XPM_FULL_TH,
      RD_DATA_COUNT_WIDTH => rd_data_count_width_g,
      READ_DATA_WIDTH     => rd_data_width_g,
      read_mode           => fwft_str,
      RELATED_CLOCKS      => 0,
      SIM_ASSERT_CHK      => 0,
      USE_ADV_FEATURES    => "1F1F",
      WAKEUP_TIME         => 0,
      WRITE_DATA_WIDTH    => wr_data_width_g,
      WR_DATA_COUNT_WIDTH => wr_data_count_width_g
    )
    port map (
      sleep         => '0',
      rst           => rst_i,
      wr_clk        => wr_clk_i,
      wr_en         => wr_en_i,
      din           => din_i,
      full          => full_o,
      prog_full     => prog_full_o,
      wr_data_count => wr_data_count_o,
      almost_full   => almost_full_o,
      overflow      => open,
      wr_ack        => open,
      wr_rst_busy   => wr_rst_busy_o,
      rd_clk        => rd_clk_i,
      rd_en         => rd_en_i,
      dout          => dout_o,
      empty         => empty_o,
      prog_empty    => prog_empty_o,
      rd_data_count => rd_data_count_o,
      almost_empty  => almost_empty_o,
      underflow     => open,
      data_valid    => data_valid_o,
      rd_rst_busy   => rd_rst_busy_o,
      injectsbiterr => '0',
      injectdbiterr => '0',
      sbiterr       => open,
      dbiterr       => open
    );

end architecture rtl;
