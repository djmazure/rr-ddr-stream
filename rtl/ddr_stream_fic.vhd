-- SPDX-License-Identifier: LicenseRef-RouteRTL-1.0
-------------------------------------------------------------------------------
-- Title      : ddr_stream_fic — Microchip PolarFire SoC MSS FIC backend TOP
-- Project    : rr-ddr-stream
-------------------------------------------------------------------------------
-- File       : ddr_stream_fic.vhd
-- Standard   : VHDL'08
-------------------------------------------------------------------------------
-- Description:
--   Microchip PolarFire SoC backend for the rr-ddr-stream DMA core (DDR-P2.15).
--   Connects to MSS LPDDR4 memory subsystem via a Fabric Interface Controller
--   (FIC) AXI4 target port (FIC0 / FIC1).
--
--   Maintains identical core-side contract as ddr_stream_axi:
--     - Write-plane arming: chip_empty deasserts when data enters the ingress
--       FIFO, triggering uploader to issue AW bursts.
--     - Commit / wrap: wr_commit_addr_o tracks committed byte offsets relative
--       to base, wrapping at ring boundary 2**buffer_width_g.
--     - Idle signaling: write_idle_o / read_idle_o.
--     - Backpressure counters & CSR status: ddr_csr_o (fifoStatus, fifoCnt).
--     - Hardware reset status observability: dbg_reset_status_o.
--     - Version stamp: version_o (= C_DDR_STREAM_VERSION).
--
--   PolarFire SoC MSS FIC specifics:
--     - Native data width is 64 bits (C_FIC_DATA_WIDTH = 64).
--     - PolarFire SoC FIC target ports cap bursts at 64 beats
--       (max_burst_len_g defaults to C_PFSOC_FIC_MAX_BURST = 64).
-------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
library rr;
use rr.ddr_stream_pkg.all;

entity ddr_stream_fic is
  generic (
    axi_id_g             : integer                               := 0;
    fifo_depth_g         : integer                               := 9;
    user_data_width_g    : integer                               := 32;
    ddr_data_width_g     : integer                               := C_FIC_DATA_WIDTH;      -- 64
    ddr_addr_width_g     : integer                               := 32;
    ddr_base_addr_g      : std_logic_vector(31 downto 0)         := (others => '0');
    buffer_width_g       : integer                               := 31;
    fifo_impl_g          : string                                := "olo";
    enable_write_plane_g : boolean                               := true;
    enable_read_plane_g  : boolean                               := true;
    max_burst_len_g      : integer                               := C_PFSOC_FIC_MAX_BURST  -- 64
  );
  port (
    clk_sys_i          : in  std_logic;
    clk_ddr_i          : in  std_logic;
    rst_sys_i          : in  std_logic;
    rst_ddr_i          : in  std_logic;

    -- Streaming boundary
    s_axis_data_i      : in  std_logic_vector(user_data_width_g-1 downto 0);
    s_axis_valid_i     : in  std_logic;
    s_axis_ready_o     : out std_logic;
    m_axis_data_o      : out std_logic_vector(user_data_width_g-1 downto 0);
    m_axis_valid_o     : out std_logic;
    m_axis_ready_i     : in  std_logic;

    -- CSR & runtime base address
    csr_wr_base_addr_i : in  std_logic_vector(ddr_addr_width_g-1 downto 0) := (others => '0');
    csr_wr_base_load_i : in  std_logic := '0';
    csr_rd_base_addr_i : in  std_logic_vector(ddr_addr_width_g-1 downto 0) := (others => '0');
    csr_rd_base_load_i : in  std_logic := '0';

    -- Pointer reset
    csr_wr_ptr_reset_i : in  std_logic := '0';
    csr_rd_ptr_reset_i : in  std_logic := '0';

    -- Software pointers
    csr_rd_ptr_addr_i  : in  std_logic_vector(ddr_addr_width_g-1 downto 0) := (others => '0');
    csr_rd_ptr_load_i  : in  std_logic := '0';
    csr_wr_ptr_addr_i  : in  std_logic_vector(ddr_addr_width_g-1 downto 0) := (others => '0');
    csr_wr_ptr_load_i  : in  std_logic := '0';

    -- Status & telemetry
    write_idle_o       : out std_logic;
    read_idle_o        : out std_logic;
    ddr_csr_o          : out fifo_csr_t := C_INIT_FIFO_CSR;
    version_o          : out std_logic_vector(31 downto 0);
    wr_commit_addr_o   : out std_logic_vector(ddr_addr_width_g-1 downto 0) := (others => '0');
    dbg_reset_status_o : out std_logic_vector(3 downto 0) := (others => '0');

    -- FIC AXI4 target interface (master side driving FIC target)
    m_axi4_h2c_bus_o   : out axi4_h2c_bus_t;
    m_axi4_c2h_bus_i   : in  axi4_c2h_bus_t
  );
end entity ddr_stream_fic;

architecture RTL of ddr_stream_fic is

  function clamp_burst(val, cap : integer) return integer is
  begin
    if val > cap then
      return cap;
    else
      return val;
    end if;
  end function;

  -- Clamp max_burst_len_g to PolarFire SoC FIC hardware capability
  constant C_EFFECTIVE_MAX_BURST : integer := clamp_burst(max_burst_len_g, C_PFSOC_FIC_MAX_BURST);

begin

  -- Elaboration assertion: warn if max_burst_len_g exceeds PolarFire SoC FIC capability
  assert max_burst_len_g <= C_PFSOC_FIC_MAX_BURST
    report "ddr_stream_fic: max_burst_len_g (" & integer'image(max_burst_len_g) &
           ") exceeds PolarFire SoC FIC burst cap of " &
           integer'image(C_PFSOC_FIC_MAX_BURST) & " beats; clamped to " &
           integer'image(C_EFFECTIVE_MAX_BURST)
    severity warning;

  i_core : entity rr.ddr_stream_axi
    generic map (
      axi_id_g             => axi_id_g,
      fifo_depth_g         => fifo_depth_g,
      user_data_width_g    => user_data_width_g,
      ddr_data_width_g     => ddr_data_width_g,
      ddr_addr_width_g     => ddr_addr_width_g,
      ddr_base_addr_g      => ddr_base_addr_g,
      buffer_width_g       => buffer_width_g,
      fifo_impl_g          => fifo_impl_g,
      enable_write_plane_g => enable_write_plane_g,
      enable_read_plane_g  => enable_read_plane_g,
      max_burst_len_g      => C_EFFECTIVE_MAX_BURST
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
      csr_rd_ptr_addr_i  => csr_rd_ptr_addr_i,
      csr_rd_ptr_load_i  => csr_rd_ptr_load_i,
      csr_wr_ptr_addr_i  => csr_wr_ptr_addr_i,
      csr_wr_ptr_load_i  => csr_wr_ptr_load_i,
      write_idle_o       => write_idle_o,
      read_idle_o        => read_idle_o,
      ddr_csr_o          => ddr_csr_o,
      version_o          => version_o,
      wr_commit_addr_o   => wr_commit_addr_o,
      dbg_reset_status_o => dbg_reset_status_o,
      m_axi4_h2c_bus_o   => m_axi4_h2c_bus_o,
      m_axi4_c2h_bus_i   => m_axi4_c2h_bus_i
    );

end architecture RTL;
