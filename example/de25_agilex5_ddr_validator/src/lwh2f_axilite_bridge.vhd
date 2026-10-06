-- =============================================================================
-- Title      : lwh2f_axilite_bridge — AXI4-full (lwh2f master) → AXI4-Lite (CSR)
-- Project    : rr-ddr-stream / de25_agilex5_ddr_validator
-- -----------------------------------------------------------------------------
-- Description:
--   Reusable extraction of the hand-written lwhps2fpga → ddr_validator_csr glue
--   that lives inline in top_de25_ddr_validator.vhd. The Agilex5 HPS lwh2f port
--   is a FULL AXI4 master (carries awid/arid, awlen/awsize/awburst, wlast, rlast);
--   the CSR is a single-beat AXI4-Lite slave (no IDs). This bridge:
--     • passes AW/W/B/AR/R straight through (address truncated to the CSR window),
--     • ECHOES the request id on bid/rid (latched on the address handshake),
--     • ties rlast = '1' (single-beat CSR).
--   Behaviourally identical to the inline glue (p_id_echo + lw_rlast_tie) — this
--   entity exists so the glue can be ATTACKED in sim by a full-AXI4 master BFM,
--   which the standalone ddr_validator_csr test (AXI-Lite BFM straight on the CSR)
--   never does. (DDR-T2.4 / RTL-P3.739 — on-silicon lwh2f CSR external abort.)
--
--   NOTE: burst inputs (awlen/awsize/awburst/awcache/wlast/arlen/…) are accepted
--   but IGNORED, exactly as the inline glue leaves them `open`. A real CSR access
--   from the HPS is always single-beat (awlen=0), so this is faithful; the test
--   drives single-beat only.
-- =============================================================================
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity lwh2f_axilite_bridge is
  generic (
    id_width_g   : positive := 4;    -- lwh2f awid/arid width
    addr_width_g : positive := 29;   -- lwh2f address width
    lite_addr_g  : positive := 12    -- CSR window (low bits decoded)
  );
  port (
    clk_i : in std_logic;
    rst_i : in std_logic;            -- active-high, sync

    -- ── full AXI4 SLAVE (faces the lwh2f master) ──────────────────────────
    s_awid    : in  std_logic_vector(id_width_g-1 downto 0);
    s_awaddr  : in  std_logic_vector(addr_width_g-1 downto 0);
    s_awprot  : in  std_logic_vector(2 downto 0);
    s_awvalid : in  std_logic;
    s_awready : out std_logic;
    s_wdata   : in  std_logic_vector(31 downto 0);
    s_wstrb   : in  std_logic_vector(3 downto 0);
    s_wvalid  : in  std_logic;
    s_wready  : out std_logic;
    s_bid     : out std_logic_vector(id_width_g-1 downto 0);
    s_bresp   : out std_logic_vector(1 downto 0);
    s_bvalid  : out std_logic;
    s_bready  : in  std_logic;
    s_arid    : in  std_logic_vector(id_width_g-1 downto 0);
    s_araddr  : in  std_logic_vector(addr_width_g-1 downto 0);
    s_arprot  : in  std_logic_vector(2 downto 0);
    s_arvalid : in  std_logic;
    s_arready : out std_logic;
    s_rid     : out std_logic_vector(id_width_g-1 downto 0);
    s_rdata   : out std_logic_vector(31 downto 0);
    s_rresp   : out std_logic_vector(1 downto 0);
    s_rlast   : out std_logic;
    s_rvalid  : out std_logic;
    s_rready  : in  std_logic;

    -- ── AXI4-Lite MASTER (drives the CSR) ─────────────────────────────────
    m_awaddr  : out std_logic_vector(lite_addr_g-1 downto 0);
    m_awprot  : out std_logic_vector(2 downto 0);
    m_awvalid : out std_logic;
    m_awready : in  std_logic;
    m_wdata   : out std_logic_vector(31 downto 0);
    m_wstrb   : out std_logic_vector(3 downto 0);
    m_wvalid  : out std_logic;
    m_wready  : in  std_logic;
    m_bresp   : in  std_logic_vector(1 downto 0);
    m_bvalid  : in  std_logic;
    m_bready  : out std_logic;
    m_araddr  : out std_logic_vector(lite_addr_g-1 downto 0);
    m_arprot  : out std_logic_vector(2 downto 0);
    m_arvalid : out std_logic;
    m_arready : in  std_logic;
    m_rdata   : in  std_logic_vector(31 downto 0);
    m_rresp   : in  std_logic_vector(1 downto 0);
    m_rvalid  : in  std_logic;
    m_rready  : out std_logic
  );
end entity lwh2f_axilite_bridge;

architecture rtl of lwh2f_axilite_bridge is
  signal bid_r : std_logic_vector(id_width_g-1 downto 0) := (others => '0');
  signal rid_r : std_logic_vector(id_width_g-1 downto 0) := (others => '0');
begin
  -- ── AW / W passthrough (address truncated to the CSR window) ────────────
  m_awaddr  <= s_awaddr(lite_addr_g-1 downto 0);
  m_awprot  <= s_awprot;
  m_awvalid <= s_awvalid;
  s_awready <= m_awready;

  m_wdata   <= s_wdata;
  m_wstrb   <= s_wstrb;
  m_wvalid  <= s_wvalid;
  s_wready  <= m_wready;

  -- ── B (echo bid) ────────────────────────────────────────────────────────
  s_bresp   <= m_bresp;
  s_bvalid  <= m_bvalid;
  m_bready  <= s_bready;
  s_bid     <= bid_r;

  -- ── AR passthrough ──────────────────────────────────────────────────────
  m_araddr  <= s_araddr(lite_addr_g-1 downto 0);
  m_arprot  <= s_arprot;
  m_arvalid <= s_arvalid;
  s_arready <= m_arready;

  -- ── R (echo rid, single-beat rlast) ─────────────────────────────────────
  s_rdata   <= m_rdata;
  s_rresp   <= m_rresp;
  s_rvalid  <= m_rvalid;
  m_rready  <= s_rready;
  s_rid     <= rid_r;
  s_rlast   <= '1';

  -- ── ID echo: latch awid/arid on their address handshake ─────────────────
  p_id_echo : process (clk_i)
  begin
    if rising_edge(clk_i) then
      if rst_i = '1' then
        bid_r <= (others => '0');
        rid_r <= (others => '0');
      else
        if s_awvalid = '1' and m_awready = '1' then
          bid_r <= s_awid;
        end if;
        if s_arvalid = '1' and m_arready = '1' then
          rid_r <= s_arid;
        end if;
      end if;
    end if;
  end process;
end architecture rtl;
