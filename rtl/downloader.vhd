-------------------------------------------------------------------------------
-- Title      : downloader
-- Project    : 
-------------------------------------------------------------------------------
-- File       : downloader.vhd
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

-------------------------------------------------------------------------------
-- The entity is divided in three interfaces:
-- 1. A FIFO interface where the data downloaded from the DDR lands
-- 2. A DDR pointer management interface, to dertemine the DDR address to read
-- 3. An AXI4 interface to the DDR
-------------------------------------------------------------------------------

entity downloader is
  generic (
    ddr_data_width_g     : integer := 512;
    ddr_addr_width_g     : integer := 32; -- 4G
    buffer_width_g       : integer := 31; -- DDR window, 2G
    fifo_depth_g         : integer := 12; -- 4096 words
    fwft_g               : integer := 1;
    axi_id_readport_g    : integer := 0;
    -- VFIFO-T2.7: max AXI read-burst length in BEATS (AXI4=256; AXI3 Zynq-7
    -- S_AXI_HP=16). Threaded to burstr_setter. Default 256 = AXI4 no-op.
    max_burst_len_g      : integer := 256
  );
  port (
    clk_ddr_i     : in std_logic;
    rst_ddr_i     : in std_logic;
    --
    cs_i          : in std_logic; -- controls the burst setter
    fifo_full_i   : in std_logic;
    fifo_room_i   : in std_logic_vector(fifo_depth_g+fwft_g-1 downto 0);
    fifo_wr_en_o  : out std_logic;
    fifo_data_o   : out std_logic_vector(ddr_data_width_g - 1 downto 0);
    --
    ptr_write_i   : in std_logic_vector(ddr_addr_width_g - 1 downto 0);
    ptr_read_i    : in std_logic_vector(ddr_addr_width_g - 1 downto 0);
    ptr_end_o     : out std_logic_vector(ddr_addr_width_g - 1 downto 0);
    -- DDR-P3.9: runtime base address — see axi4r.vhd.
    ddr_base_addr_i : in std_logic_vector(ddr_addr_width_g - 1 downto 0);
    -- DDR-P3.11 (2026-08-23): pointer-reset pulse — see axi4w.vhd.
    ptr_reset_i   : in std_logic := '0';
    bus_busy_o    : out std_logic;
    bus_error_o   : out std_logic;
    ---------------------------------------------------------------------------
    -- AXI4 read-only master interface (VFIFO-P3.4 record-typed cleanup):
    -- collapsed from 17 flat M0_AXI_* ports into two record-typed ports
    -- using axi4_read_h2c_t / axi4_read_c2h_t from vfifo_pkg.
    ---------------------------------------------------------------------------
    h2c_o : out axi4_read_h2c_t;
    c2h_i : in  axi4_read_c2h_t
  );
end downloader;

architecture RTL of downloader is

  signal cmdValid  : std_logic;
  signal cmdRlen   : std_logic_vector(7 downto 0);
  signal cmdAraddr : std_logic_vector(ddr_addr_width_g - 1 downto 0);
  signal cmdRdata  : std_logic_vector(ddr_data_width_g - 1 downto 0);
  signal cmdWrEn   : std_logic;
  signal cmdReady  : std_logic;
  --
  signal ddrPtr   : std_logic_vector(ddr_addr_width_g - 1 downto 0);
  signal arstn    : std_logic;
  signal busError : std_logic;
begin

  -----------------------------------------------------------------------------
  -- Calculates the burst based on the input pointers, avoiding crossing the 4K
  -- boundary, generating a burst not bigger than the remaining data or waiting some
  -- cycles in case there are not enough words to create a burst.
  -- Inputs:
  -- 1. DDR pointer, which is the physicall address of the last DDR access
  -- 2. DDR ADDR, which is the pointer to the address where the following read
  -- will occur
  -- 3. BUS READY, signals that the axi4r module is ready to accept new transfers.
  -- Outputs:
  -- 1. CMD_VALID and CMD_LEN, signal a new valid burst
  -----------------------------------------------------------------------------

  i_burstr_setter : entity rr.burstr_setter
    generic map(
      ddr_data_width_g => ddr_data_width_g,
      ddr_addr_width_g => ddr_addr_width_g,
      buffer_width_g   => buffer_width_g,
      fifo_depth_g     => fifo_depth_g+fwft_g,
      max_burst_len_g  => max_burst_len_g
    )
    port map
    (
      clk_ddr_i   => clk_ddr_i,
      rst_ddr_i   => rst_ddr_i,
      cs_i        => cs_i,
      fifo_room_i => fifo_room_i,
      fifo_full_i => fifo_full_i,
      ptr_write_i => ptr_write_i,
      ptr_read_i  => ptr_read_i,
      bus_ready_i => cmdReady,
      cmd_valid_o => cmdValid,
      cmd_len_o   => cmdRlen
    );

  -----------------------------------------------------------------------------
  -- Instance the Read Module (DDR_CLK domain)
  -- Inputs:
  -- 1. The length of the burst (CMD_WLEN)
  -- 2. The DDR read data (CMD_RDATA) to be written in to the downstream FIFO
  -- 3. The starting DDR address for the reading burst (CMD_ARADDR)
  -- Ouptus:
  -- 1. CMD_WREN, to write new data from the DDR to the downstream  FIFO
  -- 2. CMD_READY, signals the bus is ready to new transfers
  -- 2. DDR_POINTER, where the last access has been made
  -- 3. BUS_ERROR (bus_error_o): axi4r decodes a read RRESP /= OKAY into
  --    readError_r -> bus_rerror_o -> busError; surfaced to the read_error CSR
  --    bit (DDR-P3.25). Covered end-to-end by
  --    test_demo_axi_error_response_conformance (axi_read_error_response).
  -----------------------------------------------------------------------------

  arstn     <= not rst_ddr_i;
  cmdAraddr <= ptr_read_i;
  fifo_data_o <= cmdRdata;

  i_axi4r : entity rr.axi4r
    generic map(
      ddr_data_width_g  => ddr_data_width_g,
      ddr_addr_width_g  => ddr_addr_width_g,
      buffer_width_g    => buffer_width_g,
      axi_id_readport_g => axi_id_readport_g
    )
    port map
    (
      ddr_base_addr_i => ddr_base_addr_i,
      ptr_reset_i   => ptr_reset_i,
      cmd_valid_i   => cmdValid,
      cmd_rlen_i    => cmdRlen,
      cmd_araddr_i  => cmdAraddr,
      cmd_rdata_o   => cmdRdata,
      cmd_wren_o    => cmdWrEn,
      cmd_ready_o   => cmdReady,
      aclk_i        => clk_ddr_i,
      arstn_i       => arstn,
      h2c_o         => h2c_o,
      c2h_i         => c2h_i,
      bus_rerror_o  => busError,
      ddr_pointer_o => ddrPtr
    );

  -----------------------------------------------------------------------------
  -- Outputs
  -----------------------------------------------------------------------------
  fifo_wr_en_o  <= cmdWrEn;
  ptr_end_o     <= ddrPtr;
  bus_error_o   <= busError;
  bus_busy_o    <= not cmdReady;

end RTL;
