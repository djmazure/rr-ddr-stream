--------------------------------------------------------------------------------
-- File       : axi_wirethrough.vhd
-- Project    : rr-ddr-stream — sim/models self-test harness
-- Purpose    : A dependency-FREE AXI4 wire-through top for unit-testing the
--              realistic DDR slave model (sim/models/realistic_ddr_axi.py). A
--              cocotbext AxiMaster drives the S_AXI_* slave port; the top wires
--              it straight onto the M_AXI_* master port, where the Python model
--              sits. No IP, no packages, no library deps — elaborates standalone
--              under nvc so the model self-test exercises ONLY the model.
-- Standard   : VHDL-2008
-- Licence    : MIT
--------------------------------------------------------------------------------
library ieee;
  use ieee.std_logic_1164.all;

entity axi_wirethrough is
  generic (
    G_ID_W   : positive := 4;
    G_ADDR_W : positive := 32;
    G_DATA_W : positive := 128
  );
  port (
    CLK : in std_logic;
    RST : in std_logic;

    -- ── AXI4 slave (driven by the cocotb AxiMaster) ──────────────────────────
    S_AXI_AWID    : in  std_logic_vector(G_ID_W-1 downto 0);
    S_AXI_AWADDR  : in  std_logic_vector(G_ADDR_W-1 downto 0);
    S_AXI_AWLEN   : in  std_logic_vector(7 downto 0);
    S_AXI_AWSIZE  : in  std_logic_vector(2 downto 0);
    S_AXI_AWBURST : in  std_logic_vector(1 downto 0);
    S_AXI_AWVALID : in  std_logic;
    S_AXI_AWREADY : out std_logic;
    S_AXI_WDATA   : in  std_logic_vector(G_DATA_W-1 downto 0);
    S_AXI_WSTRB   : in  std_logic_vector(G_DATA_W/8-1 downto 0);
    S_AXI_WLAST   : in  std_logic;
    S_AXI_WVALID  : in  std_logic;
    S_AXI_WREADY  : out std_logic;
    S_AXI_BID     : out std_logic_vector(G_ID_W-1 downto 0);
    S_AXI_BRESP   : out std_logic_vector(1 downto 0);
    S_AXI_BVALID  : out std_logic;
    S_AXI_BREADY  : in  std_logic;
    S_AXI_ARID    : in  std_logic_vector(G_ID_W-1 downto 0);
    S_AXI_ARADDR  : in  std_logic_vector(G_ADDR_W-1 downto 0);
    S_AXI_ARLEN   : in  std_logic_vector(7 downto 0);
    S_AXI_ARSIZE  : in  std_logic_vector(2 downto 0);
    S_AXI_ARBURST : in  std_logic_vector(1 downto 0);
    S_AXI_ARVALID : in  std_logic;
    S_AXI_ARREADY : out std_logic;
    S_AXI_RID     : out std_logic_vector(G_ID_W-1 downto 0);
    S_AXI_RDATA   : out std_logic_vector(G_DATA_W-1 downto 0);
    S_AXI_RRESP   : out std_logic_vector(1 downto 0);
    S_AXI_RLAST   : out std_logic;
    S_AXI_RVALID  : out std_logic;
    S_AXI_RREADY  : in  std_logic;

    -- ── AXI4 master (the realistic DDR model sits here) ──────────────────────
    M_AXI_AWID    : out std_logic_vector(G_ID_W-1 downto 0);
    M_AXI_AWADDR  : out std_logic_vector(G_ADDR_W-1 downto 0);
    M_AXI_AWLEN   : out std_logic_vector(7 downto 0);
    M_AXI_AWSIZE  : out std_logic_vector(2 downto 0);
    M_AXI_AWBURST : out std_logic_vector(1 downto 0);
    M_AXI_AWVALID : out std_logic;
    M_AXI_AWREADY : in  std_logic;
    M_AXI_WDATA   : out std_logic_vector(G_DATA_W-1 downto 0);
    M_AXI_WSTRB   : out std_logic_vector(G_DATA_W/8-1 downto 0);
    M_AXI_WLAST   : out std_logic;
    M_AXI_WVALID  : out std_logic;
    M_AXI_WREADY  : in  std_logic;
    M_AXI_BID     : in  std_logic_vector(G_ID_W-1 downto 0);
    M_AXI_BRESP   : in  std_logic_vector(1 downto 0);
    M_AXI_BVALID  : in  std_logic;
    M_AXI_BREADY  : out std_logic;
    M_AXI_ARID    : out std_logic_vector(G_ID_W-1 downto 0);
    M_AXI_ARADDR  : out std_logic_vector(G_ADDR_W-1 downto 0);
    M_AXI_ARLEN   : out std_logic_vector(7 downto 0);
    M_AXI_ARSIZE  : out std_logic_vector(2 downto 0);
    M_AXI_ARBURST : out std_logic_vector(1 downto 0);
    M_AXI_ARVALID : out std_logic;
    M_AXI_ARREADY : in  std_logic;
    M_AXI_RID     : in  std_logic_vector(G_ID_W-1 downto 0);
    M_AXI_RDATA   : in  std_logic_vector(G_DATA_W-1 downto 0);
    M_AXI_RRESP   : in  std_logic_vector(1 downto 0);
    M_AXI_RLAST   : in  std_logic;
    M_AXI_RVALID  : in  std_logic;
    M_AXI_RREADY  : out std_logic
  );
end entity axi_wirethrough;

architecture rtl of axi_wirethrough is
begin
  -- Pure combinational wire-through (one-master-one-slave, no arbitration).
  M_AXI_AWID    <= S_AXI_AWID;
  M_AXI_AWADDR  <= S_AXI_AWADDR;
  M_AXI_AWLEN   <= S_AXI_AWLEN;
  M_AXI_AWSIZE  <= S_AXI_AWSIZE;
  M_AXI_AWBURST <= S_AXI_AWBURST;
  M_AXI_AWVALID <= S_AXI_AWVALID;
  S_AXI_AWREADY <= M_AXI_AWREADY;

  M_AXI_WDATA   <= S_AXI_WDATA;
  M_AXI_WSTRB   <= S_AXI_WSTRB;
  M_AXI_WLAST   <= S_AXI_WLAST;
  M_AXI_WVALID  <= S_AXI_WVALID;
  S_AXI_WREADY  <= M_AXI_WREADY;

  S_AXI_BID     <= M_AXI_BID;
  S_AXI_BRESP   <= M_AXI_BRESP;
  S_AXI_BVALID  <= M_AXI_BVALID;
  M_AXI_BREADY  <= S_AXI_BREADY;

  M_AXI_ARID    <= S_AXI_ARID;
  M_AXI_ARADDR  <= S_AXI_ARADDR;
  M_AXI_ARLEN   <= S_AXI_ARLEN;
  M_AXI_ARSIZE  <= S_AXI_ARSIZE;
  M_AXI_ARBURST <= S_AXI_ARBURST;
  M_AXI_ARVALID <= S_AXI_ARVALID;
  S_AXI_ARREADY <= M_AXI_ARREADY;

  S_AXI_RID     <= M_AXI_RID;
  S_AXI_RDATA   <= M_AXI_RDATA;
  S_AXI_RRESP   <= M_AXI_RRESP;
  S_AXI_RLAST   <= M_AXI_RLAST;
  S_AXI_RVALID  <= M_AXI_RVALID;
  M_AXI_RREADY  <= S_AXI_RREADY;
end architecture rtl;
