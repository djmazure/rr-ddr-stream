-------------------------------------------------------------------------------
-- rr_fifo_sync_xpm_body — Xilinx-only xpm_fifo_sync wrapper (RR--P3.3).
--
-- Carved out of rr_fifo_sync.vhd's GEN_XPM body so the file-scope
-- `library xpm; use xpm.vcomponents.all;` decl no longer leaks into
-- the vendor-neutral wrapper. The wrapper instantiates this entity in
-- its GEN_XPM branch via `entity work.rr_fifo_sync_xpm_body`.
--
-- Compile gate: this file MUST NOT be fed to Quartus / GHDL standalone /
-- any non-Xilinx analyse step — `library xpm` is Xilinx-private and
-- will fail to resolve. ip.yml's `synthesis.sources_per_vendor.xilinx:`
-- gates it for Vivado targets only.
-------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use ieee.math_real.all;

library xpm;
use xpm.vcomponents.all;

entity rr_fifo_sync_xpm_body is
  generic (
    fifo_depth_g        : positive;
    data_width_g        : positive;
    fwft_g              : boolean := true;
    prog_full_thresh_g  : natural := 0;
    prog_empty_thresh_g : natural := 0
  );
  port (
    clk_i           : in  std_logic;
    rst_i           : in  std_logic;
    din_i           : in  std_logic_vector(data_width_g-1 downto 0);
    wr_en_i         : in  std_logic;
    full_o          : out std_logic;
    almost_full_o   : out std_logic;
    prog_full_o     : out std_logic;
    overflow_o      : out std_logic;
    rd_en_i         : in  std_logic;
    dout_o          : out std_logic_vector(data_width_g-1 downto 0);
    empty_o         : out std_logic;
    almost_empty_o  : out std_logic;
    prog_empty_o    : out std_logic;
    underflow_o     : out std_logic;
    data_valid_o    : out std_logic;
    data_cnt_o      : out std_logic_vector(integer(ceil(log2(real(fifo_depth_g+1))))-1 downto 0);
    rst_busy_o      : out std_logic
  );
end entity rr_fifo_sync_xpm_body;

architecture rtl of rr_fifo_sync_xpm_body is

  function default_full_th return natural is
  begin
    if prog_full_thresh_g = 0 then
      return fifo_depth_g - 5;
    else
      return prog_full_thresh_g;
    end if;
  end function;

  function default_empty_th return natural is
  begin
    if prog_empty_thresh_g = 0 then
      return 5;
    else
      return prog_empty_thresh_g;
    end if;
  end function;

  constant C_PROG_FULL_TH  : natural  := default_full_th;
  constant C_PROG_EMPTY_TH : natural  := default_empty_th;
  constant C_COUNT_WIDTH   : positive := integer(ceil(log2(real(fifo_depth_g+1))));

  function fwft_str return string is
  begin
    if fwft_g then return "fwft"; else return "std"; end if;
  end function;

  signal full_s, empty_s : std_logic;

begin

  i_xpm : xpm_fifo_sync
    generic map (
      FIFO_MEMORY_TYPE    => "auto",
      FIFO_WRITE_DEPTH    => fifo_depth_g,
      WRITE_DATA_WIDTH    => data_width_g,
      READ_DATA_WIDTH     => data_width_g,
      READ_MODE           => fwft_str,
      USE_ADV_FEATURES    => "1F1F",
      PROG_FULL_THRESH    => C_PROG_FULL_TH,
      PROG_EMPTY_THRESH   => C_PROG_EMPTY_TH,
      RD_DATA_COUNT_WIDTH => C_COUNT_WIDTH,
      WR_DATA_COUNT_WIDTH => C_COUNT_WIDTH,
      DOUT_RESET_VALUE    => "0",
      FULL_RESET_VALUE    => 0
    )
    port map (
      sleep         => '0',
      rst           => rst_i,
      wr_clk        => clk_i,
      wr_en         => wr_en_i,
      din           => din_i,
      full          => full_s,
      prog_full     => prog_full_o,
      wr_data_count => open,
      almost_full   => almost_full_o,
      overflow      => overflow_o,
      wr_ack        => open,
      wr_rst_busy   => rst_busy_o,
      rd_en         => rd_en_i,
      dout          => dout_o,
      empty         => empty_s,
      prog_empty    => prog_empty_o,
      rd_data_count => data_cnt_o,
      almost_empty  => almost_empty_o,
      underflow     => underflow_o,
      data_valid    => data_valid_o,
      rd_rst_busy   => open,
      injectsbiterr => '0',
      injectdbiterr => '0',
      sbiterr       => open,
      dbiterr       => open
    );

  full_o  <= full_s;
  empty_o <= empty_s;

end architecture rtl;
