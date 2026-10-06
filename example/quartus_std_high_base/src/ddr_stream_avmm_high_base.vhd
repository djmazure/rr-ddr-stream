-------------------------------------------------------------------------------
-- Title      : ddr_stream_avmm_high_base — Quartus Std 23.1 high-base smoke
-- Project    : rr-ddr-stream
-------------------------------------------------------------------------------
-- File       : ddr_stream_avmm_high_base.vhd
-- Standard   : VHDL'08
-------------------------------------------------------------------------------
-- Description:
--   Thin consumer of ddr_stream_avmm that maps ddr_base_addr_g as a
--   fixed-width hex vector x"80000000".  That value is outside the
--   32-bit VHDL INTEGER range, so an integer generic (the pre-DDR-P3.39
--   surface) cannot carry it on Quartus 23.1 Std / NVC.  Baking the
--   literal into VHDL — rather than YAML build_options.generics —
--   avoids the integer parser on the tool-flow side as well.
-------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library rr;
use rr.ddr_stream_pkg.all;

entity ddr_stream_avmm_high_base is
  generic (
    fifo_depth_g      : integer := 9;
    user_data_width_g : integer := 32;
    ddr_data_width_g  : integer := 128;
    ddr_addr_width_g  : integer := 32;
    buffer_width_g    : integer := 31;
    fifo_impl_g       : string  := "olo"
  );
  port (
    clk_sys_i          : in  std_logic;
    clk_ddr_i          : in  std_logic;
    rst_sys_i          : in  std_logic;
    rst_ddr_i          : in  std_logic;
    s_axis_data_i      : in  std_logic_vector(user_data_width_g-1 downto 0);
    s_axis_valid_i     : in  std_logic;
    s_axis_ready_o     : out std_logic;
    m_axis_data_o      : out std_logic_vector(user_data_width_g-1 downto 0);
    m_axis_valid_o     : out std_logic;
    m_axis_ready_i     : in  std_logic;
    csr_wr_base_addr_i : in  std_logic_vector(ddr_addr_width_g-1 downto 0) := (others => '0');
    csr_wr_base_load_i : in  std_logic := '0';
    csr_rd_base_addr_i : in  std_logic_vector(ddr_addr_width_g-1 downto 0) := (others => '0');
    csr_rd_base_load_i : in  std_logic := '0';
    csr_wr_ptr_reset_i : in  std_logic := '0';
    csr_rd_ptr_reset_i : in  std_logic := '0';
    write_idle_o       : out std_logic;
    read_idle_o        : out std_logic;
    ddr_csr_o          : out fifo_csr_t := C_INIT_FIFO_CSR;
    m_avmm_h2c_bus_o   : out avmm_h2c_bus_t;
    m_avmm_c2h_bus_i   : in  avmm_c2h_bus_t
  );
end entity ddr_stream_avmm_high_base;

architecture rtl of ddr_stream_avmm_high_base is
begin
  u_ddr_stream_avmm : entity rr.ddr_stream_avmm
    generic map (
      fifo_depth_g      => fifo_depth_g,
      user_data_width_g => user_data_width_g,
      ddr_data_width_g  => ddr_data_width_g,
      ddr_addr_width_g  => ddr_addr_width_g,
      -- DDR-P3.39 / DS-T3.32: unsigned upper-half 32-bit base.  Must stay a
      -- std_logic_vector hex literal, never 16#80000000# / 2147483648.
      ddr_base_addr_g   => x"80000000",
      buffer_width_g    => buffer_width_g,
      fifo_impl_g       => fifo_impl_g
    )
    port map (
      clk_sys_i          => clk_sys_i,
      clk_ddr_i          => clk_ddr_i,
      rst_sys_i          => rst_sys_i,
      rst_ddr_i          => rst_ddr_i,
      s_axis_data_i      => s_axis_data_i,
      s_axis_valid_i     => s_axis_valid_i,
      s_axis_ready_o     => s_axis_ready_o,
      m_axis_data_o      => m_axis_data_o,
      m_axis_valid_o     => m_axis_valid_o,
      m_axis_ready_i     => m_axis_ready_i,
      csr_wr_base_addr_i => csr_wr_base_addr_i,
      csr_wr_base_load_i => csr_wr_base_load_i,
      csr_rd_base_addr_i => csr_rd_base_addr_i,
      csr_rd_base_load_i => csr_rd_base_load_i,
      csr_wr_ptr_reset_i => csr_wr_ptr_reset_i,
      csr_rd_ptr_reset_i => csr_rd_ptr_reset_i,
      write_idle_o       => write_idle_o,
      read_idle_o        => read_idle_o,
      ddr_csr_o          => ddr_csr_o,
      m_avmm_h2c_bus_o   => m_avmm_h2c_bus_o,
      m_avmm_c2h_bus_i   => m_avmm_c2h_bus_i
    );
end architecture rtl;
