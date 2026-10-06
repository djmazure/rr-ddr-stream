-- =============================================================================
-- Title      : de25_axil_demux2 — 1→2 AXI4-Lite demux (address-bit select)
-- Project    : rr-ddr-stream / de25_agilex5_ddr_validator
-- -----------------------------------------------------------------------------
-- Description:
--   Splits one AXI4-Lite slave port (driven by lwh2f_axilite_bridge) onto TWO
--   AXI4-Lite master ports, selected by a single address bit `sel_bit_g`:
--     • bit = 0 → m0  (ddr_validator_csr,  lwh2f offset 0x000..0xFFF)
--     • bit = 1 → m1  (de25_nal_rom,       lwh2f offset 0x1000..0x1FFF)
--   This is how the synthetic fabric NAL generator (de25_nal_rom) is mapped
--   into the lwh2f window alongside the validator CSR WITHOUT touching either
--   shared slave. The bridge's lite_addr_g widens to sel_bit_g+1 so the select
--   bit reaches the demux; each slave still decodes its own low sel_bit_g bits.
--
--   SINGLE-OUTSTANDING by design — matches the lwhps2fpga master's single-beat,
--   non-pipelined CSR cadence (the bridge issues one AW/W or one AR at a time).
--   Write- and read-side selects are latched at the AW / AR handshake so W/B and
--   R route to the same slave the address picked. Response channels are muxed
--   from the in-flight slave. No bursts, no IDs (AXI-Lite).
-- =============================================================================
library ieee;
  use ieee.std_logic_1164.all;

entity de25_axil_demux2 is
  generic (
    addr_width_g : positive := 13;  -- includes the select bit
    sel_bit_g    : natural  := 12   -- 0 → m0, 1 → m1
  );
  port (
    clk_i : in std_logic;
    rst_i : in std_logic;           -- active-high, synchronous

    -- Upstream AXI4-Lite slave (from the lwh2f bridge)
    s_awaddr_i  : in  std_logic_vector(addr_width_g-1 downto 0);
    s_awprot_i  : in  std_logic_vector(2 downto 0);
    s_awvalid_i : in  std_logic;
    s_awready_o : out std_logic;
    s_wdata_i   : in  std_logic_vector(31 downto 0);
    s_wstrb_i   : in  std_logic_vector(3 downto 0);
    s_wvalid_i  : in  std_logic;
    s_wready_o  : out std_logic;
    s_bresp_o   : out std_logic_vector(1 downto 0);
    s_bvalid_o  : out std_logic;
    s_bready_i  : in  std_logic;
    s_araddr_i  : in  std_logic_vector(addr_width_g-1 downto 0);
    s_arprot_i  : in  std_logic_vector(2 downto 0);
    s_arvalid_i : in  std_logic;
    s_arready_o : out std_logic;
    s_rdata_o   : out std_logic_vector(31 downto 0);
    s_rresp_o   : out std_logic_vector(1 downto 0);
    s_rvalid_o  : out std_logic;
    s_rready_i  : in  std_logic;

    -- Downstream AXI4-Lite master 0 (sel = '0')
    m0_awaddr_o  : out std_logic_vector(addr_width_g-2 downto 0);
    m0_awprot_o  : out std_logic_vector(2 downto 0);
    m0_awvalid_o : out std_logic;
    m0_awready_i : in  std_logic;
    m0_wdata_o   : out std_logic_vector(31 downto 0);
    m0_wstrb_o   : out std_logic_vector(3 downto 0);
    m0_wvalid_o  : out std_logic;
    m0_wready_i  : in  std_logic;
    m0_bresp_i   : in  std_logic_vector(1 downto 0);
    m0_bvalid_i  : in  std_logic;
    m0_bready_o  : out std_logic;
    m0_araddr_o  : out std_logic_vector(addr_width_g-2 downto 0);
    m0_arprot_o  : out std_logic_vector(2 downto 0);
    m0_arvalid_o : out std_logic;
    m0_arready_i : in  std_logic;
    m0_rdata_i   : in  std_logic_vector(31 downto 0);
    m0_rresp_i   : in  std_logic_vector(1 downto 0);
    m0_rvalid_i  : in  std_logic;
    m0_rready_o  : out std_logic;

    -- Downstream AXI4-Lite master 1 (sel = '1')
    m1_awaddr_o  : out std_logic_vector(addr_width_g-2 downto 0);
    m1_awprot_o  : out std_logic_vector(2 downto 0);
    m1_awvalid_o : out std_logic;
    m1_awready_i : in  std_logic;
    m1_wdata_o   : out std_logic_vector(31 downto 0);
    m1_wstrb_o   : out std_logic_vector(3 downto 0);
    m1_wvalid_o  : out std_logic;
    m1_wready_i  : in  std_logic;
    m1_bresp_i   : in  std_logic_vector(1 downto 0);
    m1_bvalid_i  : in  std_logic;
    m1_bready_o  : out std_logic;
    m1_araddr_o  : out std_logic_vector(addr_width_g-2 downto 0);
    m1_arprot_o  : out std_logic_vector(2 downto 0);
    m1_arvalid_o : out std_logic;
    m1_arready_i : in  std_logic;
    m1_rdata_i   : in  std_logic_vector(31 downto 0);
    m1_rresp_i   : in  std_logic_vector(1 downto 0);
    m1_rvalid_i  : in  std_logic;
    m1_rready_o  : out std_logic
  );
end entity de25_axil_demux2;

architecture rtl of de25_axil_demux2 is
  -- Per-channel select: combinational for the address phase, latched for the
  -- data/response phase (single outstanding).
  signal aw_sel_s : std_logic;                       -- s_awaddr[sel] (this cycle)
  signal ar_sel_s : std_logic;                       -- s_araddr[sel] (this cycle)

  signal wr_busy_r : std_logic := '0';               -- AW accepted, B pending
  signal wr_sel_r  : std_logic := '0';               -- latched write-side slave
  signal rd_busy_r : std_logic := '0';               -- AR accepted, R pending
  signal rd_sel_r  : std_logic := '0';               -- latched read-side slave

  -- Low address bits forwarded to the slaves (sel bit stripped).
  signal s_awlow_s : std_logic_vector(addr_width_g-2 downto 0);
  signal s_arlow_s : std_logic_vector(addr_width_g-2 downto 0);
begin
  aw_sel_s  <= s_awaddr_i(sel_bit_g);
  ar_sel_s  <= s_araddr_i(sel_bit_g);
  s_awlow_s <= s_awaddr_i(addr_width_g-2 downto 0);
  s_arlow_s <= s_araddr_i(addr_width_g-2 downto 0);

  -- ── Latch the in-flight slave per channel (single outstanding) ─────────────
  p_track : process (clk_i)
  begin
    if rising_edge(clk_i) then
      if rst_i = '1' then
        wr_busy_r <= '0';
        rd_busy_r <= '0';
        wr_sel_r  <= '0';
        rd_sel_r  <= '0';
      else
        -- Write: capture select when AW is accepted; clear when B completes.
        if wr_busy_r = '0' and s_awvalid_i = '1' and
           ((aw_sel_s = '0' and m0_awready_i = '1') or
            (aw_sel_s = '1' and m1_awready_i = '1')) then
          wr_busy_r <= '1';
          wr_sel_r  <= aw_sel_s;
        elsif wr_busy_r = '1' and s_bvalid_o = '1' and s_bready_i = '1' then
          wr_busy_r <= '0';
        end if;
        -- Read: capture select when AR is accepted; clear when R completes.
        if rd_busy_r = '0' and s_arvalid_i = '1' and
           ((ar_sel_s = '0' and m0_arready_i = '1') or
            (ar_sel_s = '1' and m1_arready_i = '1')) then
          rd_busy_r <= '1';
          rd_sel_r  <= ar_sel_s;
        elsif rd_busy_r = '1' and s_rvalid_o = '1' and s_rready_i = '1' then
          rd_busy_r <= '0';
        end if;
      end if;
    end if;
  end process;

  -- ── Write address: route AW to the addressed slave (only when not busy) ────
  m0_awaddr_o  <= s_awlow_s;
  m1_awaddr_o  <= s_awlow_s;
  m0_awprot_o  <= s_awprot_i;
  m1_awprot_o  <= s_awprot_i;
  m0_awvalid_o <= '1' when (wr_busy_r = '0' and s_awvalid_i = '1' and aw_sel_s = '0') else '0';
  m1_awvalid_o <= '1' when (wr_busy_r = '0' and s_awvalid_i = '1' and aw_sel_s = '1') else '0';
  s_awready_o  <= m0_awready_i when (wr_busy_r = '0' and aw_sel_s = '0') else
                  m1_awready_i when (wr_busy_r = '0' and aw_sel_s = '1') else '0';

  -- ── Write data: follow the latched (or this-cycle) write-side slave ────────
  --   Before AW is latched, W steers by the current AW address; after, by the
  --   latched select. The lwh2f master presents AW before/with W, so aw_sel_s is
  --   valid whenever W is offered for the same transaction.
  m0_wdata_o  <= s_wdata_i;
  m1_wdata_o  <= s_wdata_i;
  m0_wstrb_o  <= s_wstrb_i;
  m1_wstrb_o  <= s_wstrb_i;
  m0_wvalid_o <= '1' when (s_wvalid_i = '1' and
                           ((wr_busy_r = '1' and wr_sel_r = '0') or
                            (wr_busy_r = '0' and aw_sel_s = '0'))) else '0';
  m1_wvalid_o <= '1' when (s_wvalid_i = '1' and
                           ((wr_busy_r = '1' and wr_sel_r = '1') or
                            (wr_busy_r = '0' and aw_sel_s = '1'))) else '0';
  s_wready_o  <= m0_wready_i when ((wr_busy_r = '1' and wr_sel_r = '0') or
                                   (wr_busy_r = '0' and aw_sel_s = '0')) else
                 m1_wready_i;

  -- ── Write response: mux from the in-flight write slave ─────────────────────
  m0_bready_o <= s_bready_i when wr_sel_r = '0' else '0';
  m1_bready_o <= s_bready_i when wr_sel_r = '1' else '0';
  s_bvalid_o  <= m0_bvalid_i when wr_sel_r = '0' else m1_bvalid_i;
  s_bresp_o   <= m0_bresp_i  when wr_sel_r = '0' else m1_bresp_i;

  -- ── Read address: route AR to the addressed slave (only when not busy) ─────
  m0_araddr_o  <= s_arlow_s;
  m1_araddr_o  <= s_arlow_s;
  m0_arprot_o  <= s_arprot_i;
  m1_arprot_o  <= s_arprot_i;
  m0_arvalid_o <= '1' when (rd_busy_r = '0' and s_arvalid_i = '1' and ar_sel_s = '0') else '0';
  m1_arvalid_o <= '1' when (rd_busy_r = '0' and s_arvalid_i = '1' and ar_sel_s = '1') else '0';
  s_arready_o  <= m0_arready_i when (rd_busy_r = '0' and ar_sel_s = '0') else
                  m1_arready_i when (rd_busy_r = '0' and ar_sel_s = '1') else '0';

  -- ── Read data: mux from the in-flight read slave ──────────────────────────
  m0_rready_o <= s_rready_i when rd_sel_r = '0' else '0';
  m1_rready_o <= s_rready_i when rd_sel_r = '1' else '0';
  s_rvalid_o  <= m0_rvalid_i when rd_sel_r = '0' else m1_rvalid_i;
  s_rdata_o   <= m0_rdata_i  when rd_sel_r = '0' else m1_rdata_i;
  s_rresp_o   <= m0_rresp_i  when rd_sel_r = '0' else m1_rresp_i;

end architecture rtl;
