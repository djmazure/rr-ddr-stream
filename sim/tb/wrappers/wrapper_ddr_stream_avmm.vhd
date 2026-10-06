-- File: wrapper_ddr_stream_avmm.vhd
-- Purpose: Flatten AVMM record ports for cocotb+NVC friendliness. Mirrors
--          wrapper_ddr_stream_axi.vhd; only the bus side differs.
-- Standard: VHDL-2008
--
-- Signal-name convention: lowercase suffixes after the M_AVMM_ prefix
-- match what AvmmBus.from_prefix(dut, "M_AVMM") expects in
-- sdk/cocotb_helpers/avmm_ram.py — VHDL is case-insensitive at the
-- handle level so the BFM resolves M_AVMM_address against this
-- M_AVMM_ADDRESS port unambiguously.

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library rr;
use rr.ddr_stream_pkg.all;

entity wrapper_ddr_stream_avmm is
  generic (
    G_FIFO_DEPTH     : integer                                 := 9;
    -- RR--P3.6: user-side / DDR-side widths independent (power-of-two ratio).
    G_USER_DATA_WIDTH : integer                                := 32;
    G_DDR_DATA_WIDTH : integer                                 := 128;
    G_DDR_ADDR_WIDTH : integer                                 := 32;
    -- DDR-P3.39: fixed-width like the public generic, preserving the full
    -- unsigned 32-bit base-address domain on 32-bit-INTEGER tools.
    G_DDR_BASE_ADDR  : std_logic_vector(31 downto 0)            := (others => '0');
    G_BUFFER_WIDTH   : integer                                 := 31;
    -- RR--P3.2: FIFO backend select. "olo" default | "xpm".
    G_FIFO_IMPL      : string                                  := "olo"
  );
  port (
    -- Clocks & resets
    CLK_SYS  : in  std_logic;
    CLK_DDR  : in  std_logic;
    RST_SYS  : in  std_logic;
    RST_DDR  : in  std_logic;

    -- DDR-P3.10: AXI-Stream handshake on both sides. Flat-port names
    -- map 1:1 to the DUT entity ports (s_axis_*, m_axis_*).
    S_AXIS_DATA  : in  std_logic_vector(G_USER_DATA_WIDTH-1 downto 0);
    S_AXIS_VALID : in  std_logic;
    S_AXIS_READY : out std_logic;
    M_AXIS_DATA  : out std_logic_vector(G_USER_DATA_WIDTH-1 downto 0);
    M_AXIS_VALID : out std_logic;
    M_AXIS_READY : in  std_logic;

    -- DDR-P3.9: runtime base address — see wrapper_ddr_stream_axi.vhd.
    CSR_WR_BASE_ADDR : in  std_logic_vector(G_DDR_ADDR_WIDTH-1 downto 0) := (others => '0');
    CSR_WR_BASE_LOAD : in  std_logic := '0';
    CSR_RD_BASE_ADDR : in  std_logic_vector(G_DDR_ADDR_WIDTH-1 downto 0) := (others => '0');
    CSR_RD_BASE_LOAD : in  std_logic := '0';
    -- DDR-P3.11: pointer-reset pulse — see wrapper_ddr_stream_axi.vhd.
    CSR_WR_PTR_RESET : in  std_logic := '0';
    CSR_RD_PTR_RESET : in  std_logic := '0';
    WRITE_IDLE       : out std_logic;
    READ_IDLE        : out std_logic;

    -- CSR / status
    DDR_CSR  : out fifo_csr_t := C_INIT_FIFO_CSR;

    --------------------------------------------------------------------------
    -- FLAT AVALON-MM MASTER (DUT master driving the slave memory model).
    -- Naming matches AvmmBus.from_prefix("M_AVMM").
    --------------------------------------------------------------------------
    M_AVMM_address    : out std_logic_vector(C_AVMM_ADDR_WIDTH-1       downto 0);
    M_AVMM_read       : out std_logic;
    M_AVMM_write      : out std_logic;
    -- RR--P3.6: wrapper-port widths track the active G_DDR_DATA_WIDTH.
    M_AVMM_writedata  : out std_logic_vector(G_DDR_DATA_WIDTH-1   downto 0);
    M_AVMM_byteenable : out std_logic_vector(G_DDR_DATA_WIDTH/8-1 downto 0);
    M_AVMM_burstcount : out std_logic_vector(C_AVMM_BURSTCOUNT_WIDTH-1 downto 0);

    --------------------------------------------------------------------------
    -- FLAT AVALON-MM SLAVE RESPONSE (memory model back to master/DUT)
    --------------------------------------------------------------------------
    M_AVMM_readdata      : in  std_logic_vector(G_DDR_DATA_WIDTH-1 downto 0);
    M_AVMM_readdatavalid : in  std_logic;
    M_AVMM_waitrequest   : in  std_logic
  );
end entity wrapper_ddr_stream_avmm;

architecture rtl of wrapper_ddr_stream_avmm is

  signal avmm_h2c : avmm_h2c_bus_t;
  signal avmm_c2h : avmm_c2h_bus_t;

begin

  -----------------------------------------------------------------------------
  -- RECORD -> FLAT
  -----------------------------------------------------------------------------
  M_AVMM_address    <= avmm_h2c.ADDRESS;
  M_AVMM_read       <= avmm_h2c.READ;
  M_AVMM_write      <= avmm_h2c.WRITE;
  -- RR--P3.6: slice down to the active ddr-side width on the way to
  -- the AvmmRam slave (avmm_h2c.* are C_AVMM_DATA_WIDTH wide).
  M_AVMM_writedata  <= avmm_h2c.WRITEDATA(G_DDR_DATA_WIDTH-1 downto 0);
  M_AVMM_byteenable <= avmm_h2c.BYTEENABLE(G_DDR_DATA_WIDTH/8-1 downto 0);
  M_AVMM_burstcount <= avmm_h2c.BURSTCOUNT;

  -----------------------------------------------------------------------------
  -- FLAT -> RECORD
  -----------------------------------------------------------------------------
  -- RR--P3.6: pad READDATA from the active ddr-side width up to
  -- C_AVMM_DATA_WIDTH (the record's slot width).
  avmm_c2h.READDATA(G_DDR_DATA_WIDTH-1 downto 0)               <= M_AVMM_readdata;
  avmm_c2h.READDATA(C_AVMM_DATA_WIDTH-1 downto G_DDR_DATA_WIDTH) <= (others => '0');
  avmm_c2h.READDATAVALID <= M_AVMM_readdatavalid;
  avmm_c2h.WAITREQUEST   <= M_AVMM_waitrequest;

  -----------------------------------------------------------------------------
  -- DUT
  -----------------------------------------------------------------------------
  u_ddr_stream_avmm : entity rr.ddr_stream_avmm
    generic map (
      fifo_depth_g      => G_FIFO_DEPTH,
      user_data_width_g => G_USER_DATA_WIDTH,
      ddr_data_width_g  => G_DDR_DATA_WIDTH,
      ddr_addr_width_g  => G_DDR_ADDR_WIDTH,
      ddr_base_addr_g  => G_DDR_BASE_ADDR,
      buffer_width_g   => G_BUFFER_WIDTH,
      fifo_impl_g      => G_FIFO_IMPL
    )
    port map (
      clk_sys_i        => CLK_SYS,
      clk_ddr_i        => CLK_DDR,
      rst_sys_i        => RST_SYS,
      rst_ddr_i        => RST_DDR,
      s_axis_data_i    => S_AXIS_DATA,
      s_axis_valid_i   => S_AXIS_VALID,
      s_axis_ready_o   => S_AXIS_READY,
      m_axis_data_o    => M_AXIS_DATA,
      m_axis_valid_o   => M_AXIS_VALID,
      m_axis_ready_i   => M_AXIS_READY,
      csr_wr_base_addr_i => CSR_WR_BASE_ADDR,
      csr_wr_base_load_i => CSR_WR_BASE_LOAD,
      csr_rd_base_addr_i => CSR_RD_BASE_ADDR,
      csr_rd_base_load_i => CSR_RD_BASE_LOAD,
      csr_wr_ptr_reset_i => CSR_WR_PTR_RESET,
      csr_rd_ptr_reset_i => CSR_RD_PTR_RESET,
      write_idle_o       => WRITE_IDLE,
      read_idle_o        => READ_IDLE,
      ddr_csr_o        => DDR_CSR,
      m_avmm_h2c_bus_o => avmm_h2c,
      m_avmm_c2h_bus_i => avmm_c2h
    );

end architecture rtl;
