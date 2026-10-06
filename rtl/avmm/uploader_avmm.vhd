-------------------------------------------------------------------------------
-- Title      : uploader_avmm — Avalon-MM peer of uploader.vhd
-- Project    : rr-ddr-stream
-------------------------------------------------------------------------------
-- File       : uploader_avmm.vhd
-- Author     : Daniel J. Mazure <dasjimaz@gmail.com>
-- Standard   : VHDL'08
-------------------------------------------------------------------------------
-- Description:
--   Composes burstw_setter (bus-agnostic burst-length policy) with avmm_w
--   (Avalon-MM write-master FSM). Surface and timing match uploader.vhd
--   bit-for-bit on the FIFO side; only the bus-record port shape
--   differs.
-------------------------------------------------------------------------------

library IEEE;
use IEEE.STD_LOGIC_1164.all;
use IEEE.NUMERIC_STD.all;
library rr;
use rr.ddr_stream_pkg.all;

entity uploader_avmm is
  generic (
    ddr_data_width_g : integer                                 := 128;
    ddr_addr_width_g : integer                                 := 32;
    fifo_depth_g     : integer                                 := 12;
    fwft_g           : integer                                 := 1;
    buffer_width_g   : integer                                 := 31
  );
  port (
    clk_ddr_i    : in  std_logic;
    rst_ddr_i    : in  std_logic;
    --
    cs_i         : in  std_logic;        -- controls the burst setter
    fifo_cnt_i   : in  std_logic_vector(fifo_depth_g+fwft_g - 1 downto 0);
    fifo_empty_i : in  std_logic;
    fifo_rd_en_o : out std_logic;
    fifo_data_i  : in  std_logic_vector(ddr_data_width_g - 1 downto 0);
    ptr_write_i  : in  std_logic_vector(ddr_addr_width_g - 1 downto 0);
    ptr_read_i   : in  std_logic_vector(ddr_addr_width_g - 1 downto 0);
    ptr_end_o    : out std_logic_vector(ddr_addr_width_g - 1 downto 0);
    -- DDR-P3.9: runtime base address — see avmm_w.vhd.
    ddr_base_addr_i : in std_logic_vector(ddr_addr_width_g - 1 downto 0);
    -- DDR-P3.11 (2026-08-23): pointer-reset pulse — see axi4w.vhd.
    ptr_reset_i  : in std_logic := '0';
    bus_busy_o   : out std_logic;
    bus_error_o  : out std_logic;
    ---------------------------------------------------------------------------
    -- Avalon-MM master interface (write-only sub-record).
    ---------------------------------------------------------------------------
    h2c_o : out avmm_write_h2c_t;
    c2h_i : in  avmm_write_c2h_t
  );
end uploader_avmm;

architecture RTL of uploader_avmm is

  signal cmdValid  : std_logic;
  signal cmdWlen   : std_logic_vector(7 downto 0);
  signal cmdAwaddr : std_logic_vector(ddr_addr_width_g - 1 downto 0);
  signal cmdWdata  : std_logic_vector(ddr_data_width_g - 1 downto 0);
  signal cmdRden   : std_logic;
  signal cmdReady  : std_logic;
  --
  signal ddrPtr   : std_logic_vector(ddr_addr_width_g - 1 downto 0);
  signal arstn    : std_logic;
  signal busError : std_logic;

begin

  i_burstw_setter : entity rr.burstw_setter
    generic map(
      ddr_data_width_g => ddr_data_width_g,
      ddr_addr_width_g => ddr_addr_width_g,
      buffer_width_g   => buffer_width_g,
      fifo_depth_g     => fifo_depth_g+fwft_g
    )
    port map (
      clk_ddr_i    => clk_ddr_i,
      rst_ddr_i    => rst_ddr_i,
      cs_i         => cs_i,
      fifo_cnt_i   => fifo_cnt_i,
      fifo_empty_i => fifo_empty_i,
      ptr_write_i  => ptr_write_i,
      ptr_read_i   => ptr_read_i,
      bus_ready_i  => cmdReady,
      cmd_valid_o  => cmdValid,
      cmd_len_o    => cmdWlen
    );

  arstn     <= not rst_ddr_i;
  cmdAwaddr <= ptr_write_i;
  cmdWdata  <= fifo_data_i;

  i_avmm_w : entity rr.avmm_w
    generic map(
      ddr_data_width_g => ddr_data_width_g,
      ddr_addr_width_g => ddr_addr_width_g,
      buffer_width_g   => buffer_width_g,
      fifo_depth_g     => fifo_depth_g
    )
    port map (
      ddr_base_addr_i => ddr_base_addr_i,
      ptr_reset_i   => ptr_reset_i,
      cmd_valid_i   => cmdValid,
      cmd_wlen_i    => cmdWlen,
      cmd_awaddr_i  => cmdAwaddr,
      cmd_wdata_i   => cmdWdata,
      cmd_rden_o    => cmdRden,
      cmd_ready_o   => cmdReady,
      aclk_i        => clk_ddr_i,
      arstn_i       => arstn,
      h2c_o         => h2c_o,
      c2h_i         => c2h_i,
      bus_werror_o  => busError,
      ddr_pointer_o => ddrPtr
    );

  fifo_rd_en_o <= cmdRden;
  ptr_end_o    <= ddrPtr;
  bus_error_o  <= busError;
  bus_busy_o   <= not cmdReady;

end RTL;
