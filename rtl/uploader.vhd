-------------------------------------------------------------------------------
-- Title      : uploader
-- Project    : 
-------------------------------------------------------------------------------
-- File       : uploader.vhd
-- Author     :   <dasji@RIGTICH>
-- Company    : 
-- Created    : 2024-05-29
-- Last update: 2024-07-06
-- Platform   : 
-- Standard   : VHDL'08
-------------------------------------------------------------------------------
-- Description: uploads the ethernet packets to the DDR via AXI4
-------------------------------------------------------------------------------
-- Copyright (c) 2024 
-------------------------------------------------------------------------------
-- Revisions  :
-- Date        Version  Author  Description
-- 2024-05-29  1.0      dmazure Created
-------------------------------------------------------------------------------

library IEEE;
use IEEE.STD_LOGIC_1164.all;
use IEEE.NUMERIC_STD.all;
use IEEE.MATH_REAL.all;
use ieee.std_logic_textio.all;
use std.textio.all;
library rr;
use rr.ddr_stream_pkg.all;

-- Style: esa-vhdl-provisional. VFIFO-P3.4 record-typed cleanup landed
-- 2026-05-15 — the 21 flat M0_AXI_* ports were collapsed into two
-- record-typed ports (h2c_o + c2h_i) using axi4_write_h2c_t /
-- axi4_write_c2h_t from vfifo_pkg. For cocotb test stimulus that needs
-- flat AXI signals (cocotbext-axi, Xilinx VIP), use the test wrapper at
-- sim/tb/wrappers/wrapper_uploader.vhd.
entity uploader is
  generic (
    ddr_data_width_g     : integer := 512;
    ddr_addr_width_g     : integer := 32; -- 4G
    fifo_depth_g         : integer := 12; -- 4096 words
    fwft_g               : integer := 1;
    buffer_width_g       : integer := 31; -- DDR window, 2G
    axi_id_writeport_g   : integer := 0;
    max_burst_len_g      : integer := 256 -- AXI4=256; AXI3 (Zynq-7 HP)=16 (VFIFO-T2.6/IDP-T2.5)
  );
  port (
    ---------------------------------------------------------------------------
    -- Boundary ports. The FIFO-read (clk_ddr) -> DDR-write datapath is realized
    -- by i_burstw_setter (burst sizing) + i_axi4w (AXI write engine) below.
    ---------------------------------------------------------------------------
    clk_ddr_i    : in std_logic;
    rst_ddr_i    : in std_logic;
    --
    cs_i         : in std_logic; -- controls the burst setter
    fifo_cnt_i   : in std_logic_vector(fifo_depth_g+fwft_g - 1 downto 0);
    fifo_empty_i : in std_logic;
    fifo_rd_en_o : out std_logic;
    fifo_data_i  : in std_logic_vector(ddr_data_width_g - 1 downto 0);
    ptr_write_i  : in std_logic_vector(ddr_addr_width_g - 1 downto 0);
    ptr_read_i   : in std_logic_vector(ddr_addr_width_g - 1 downto 0);
    ptr_end_o    : out std_logic_vector(ddr_addr_width_g - 1 downto 0);
    -- DDR-P3.9: runtime base address — was a generic until 2026-05-18.
    -- See axi4w.vhd for the propagation rationale.
    ddr_base_addr_i : in std_logic_vector(ddr_addr_width_g - 1 downto 0);
    -- DDR-P3.11 (2026-08-23): pointer-reset pulse — see axi4w.vhd.
    ptr_reset_i  : in std_logic := '0';
    bus_busy_o   : out std_logic;
    bus_error_o  : out std_logic;
    ---------------------------------------------------------------------------
    -- AXI4 write-only master interface (VFIFO-P3.4 record-typed cleanup):
    -- collapsed from 21 flat M0_AXI_* ports into two record-typed ports
    -- using axi4_write_h2c_t / axi4_write_c2h_t from vfifo_pkg.
    ---------------------------------------------------------------------------
    h2c_o : out axi4_write_h2c_t;
    c2h_i : in  axi4_write_c2h_t
    );
end uploader;

architecture RTL of uploader is

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

  -----------------------------------------------------------------------------
  -- Calculates the burst based on the input pointers, avoiding crossing the 4K
  -- boundary, writing a burst bigger than the remaining space or waiting some
  -- cycles in case there are not enough words to create a burst.
  -- Inputs:
  -- 1. DDR pointer, which is the physicall address of the last DDR access
  -- 2. DDR ADDR, which is the pointer to the address where the following write
  -- will occur
  -- 3. BUS READY, signals that the axi4w module is ready to accept new transfers.
  -- Outputs:
  -- 1. CMD_VALID and CMD_LEN, signal a new valid burst
  -----------------------------------------------------------------------------

  i_burstw_setter : entity rr.burstw_setter
    generic map(
      ddr_data_width_g => ddr_data_width_g,
      ddr_addr_width_g => ddr_addr_width_g,
      buffer_width_g   => buffer_width_g,
      fifo_depth_g     => fifo_depth_g+fwft_g,
      max_burst_len_g  => max_burst_len_g
    )
    port map
    (
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

  -----------------------------------------------------------------------------
  -- Instance the Write Module (DDR_CLK domain)
  -- Inputs:
  -- 1. The length of the burst (CMD_WLEN)
  -- 2. The data (CMD_WDATA) to be written in to the DDR,
  -- 3. The starting DDR address for the burst (CMD_AWADDR)
  -- Ouptus:
  -- 1. CMD_RDEN, to read new data from the Upstream FIFO
  -- 2. CMD_READY, signals the bus is ready to new transfers
  -- 2. DDR_POINTER, with the new CMD_AWADDR to use
  -- 3. BUS_WERROR, signals the BRESP AXI result.
  -----------------------------------------------------------------------------

  arstn     <= not rst_ddr_i;
  cmdAwaddr <= ptr_write_i;
  cmdWdata  <= fifo_data_i;

  i_axi4w : entity rr.axi4w
    generic map(
      ddr_data_width_g   => ddr_data_width_g,
      ddr_addr_width_g   => ddr_addr_width_g,
      buffer_width_g     => buffer_width_g,
      fifo_depth_g       => fifo_depth_g,
      axi_id_writeport_g => axi_id_writeport_g
    )
    port map
    (
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


  -----------------------------------------------------------------------------
  -- Outputs
  -----------------------------------------------------------------------------

  fifo_rd_en_o <= cmdRden;
  ptr_end_o    <= ddrPtr;
  bus_error_o  <= busError;
  bus_busy_o   <= not cmdReady;

end rtl;
