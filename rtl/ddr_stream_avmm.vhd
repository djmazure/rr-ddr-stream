-------------------------------------------------------------------------------
-- Title      : ddr_stream_avmm — Avalon-MM TOP for the rr-ddr-stream DMA core
-- Project    : rr-ddr-stream
-------------------------------------------------------------------------------
-- File       : ddr_stream_avmm.vhd
-- Author     : Daniel J. Mazure <dasjimaz@gmail.com>
-- Standard   : VHDL'08
-------------------------------------------------------------------------------
-- Description:
--   Avalon-MM master TOP for the DMA-shaped ring-buffered-DDR primitive
--   (DDR-P3.1). Composes the same bus-agnostic policy entities as
--   ddr_stream_axi (ddr_fifo_intf / ptr_mgr / ddr_sync_cdc), wired to
--   uploader_avmm + downloader_avmm instead of the AXI uploader /
--   downloader.
--
--   Bus-arbitration: the AVMM bus shares ADDRESS, BURSTCOUNT, and
--   WAITREQUEST between the read and write sub-masters. A real Avalon
--   topology would route both into a Platform Designer arbiter; here
--   uploader_avmm and downloader_avmm operate on disjoint cycles
--   (gated by the per-master burst_setter rate-limiting + the
--   reset-init guard), so the TOP combines them via OR-mux on
--   ADDRESS / BURSTCOUNT and tees WAITREQUEST to both. WRITE and
--   READ are mutually exclusive on the bus by construction (each
--   master only drives its own request line).
--
--   DDR-P3.10 (2026-05-18) — producer/consumer boundary speaks AXI-Stream
--   valid/ready instead of the legacy wr_en/busy/rd_en/dout_valid surface.
--   2-deep skid_buffer on both sides decouples back-pressure and the FWFT
--   FIFO read latency from the user-facing handshake.
-------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use IEEE.MATH_REAL.all;
library rr;
use rr.ddr_stream_pkg.all;
use rr.functions_pkg.all;

entity ddr_stream_avmm is
  generic (
    fifo_depth_g     : integer                                 := 9;   -- 512-word depth
    -- RR--P3.6 (2026-05-16): user-side / DDR-side widths independent.
    -- ratio = ddr_data_width_g / user_data_width_g must be a power
    -- of two ≥ 1 (1, 2, 4, 8, 16). Default 32-in / 128-out preserves
    -- the historical shape. 32/512 is the immediate Intel-EMIF case.
    user_data_width_g : integer                                := 32;
    ddr_data_width_g : integer                                 := 128;
    ddr_addr_width_g : integer                                 := 32;  -- 4G
    -- DDR-P3.39 / DS-T3.32: a fixed-width vector avoids Quartus 23.1 Std
    -- Error 10556 while covering the complete unsigned 32-bit address space.
    ddr_base_addr_g  : std_logic_vector(31 downto 0) := (others => '0');
    buffer_width_g   : integer                                 := 31;  -- DDR window, 2G
    fifo_impl_g      : string                                  := "olo"
    );
  port (
    clk_sys_i        : in  std_logic;
    clk_ddr_i        : in  std_logic;
    rst_sys_i        : in  std_logic;
    rst_ddr_i        : in  std_logic;
    -- DDR-P3.10 (2026-05-18): AXI-Stream handshake on both boundary
    -- sides. Mirrors ddr_stream_axi.vhd's port surface for symmetric
    -- consumer experience across both backends.
    s_axis_data_i    : in  std_logic_vector(user_data_width_g-1 downto 0);
    s_axis_valid_i   : in  std_logic;
    s_axis_ready_o   : out std_logic;
    m_axis_data_o    : out std_logic_vector(user_data_width_g-1 downto 0);
    m_axis_valid_o   : out std_logic;
    m_axis_ready_i   : in  std_logic;
    -- DDR-P3.9 (2026-05-18): runtime base address override —
    -- mirrors ddr_stream_axi. All four inputs have safe defaults so
    -- existing consumers can leave them unwired.
    csr_wr_base_addr_i : in  std_logic_vector(ddr_addr_width_g-1 downto 0) := (others => '0');
    csr_wr_base_load_i : in  std_logic := '0';
    csr_rd_base_addr_i : in  std_logic_vector(ddr_addr_width_g-1 downto 0) := (others => '0');
    csr_rd_base_load_i : in  std_logic := '0';
    -- DDR-P3.11 (2026-08-23): pointer-reset pulse — see ddr_stream_axi.vhd.
    csr_wr_ptr_reset_i : in  std_logic := '0';
    csr_rd_ptr_reset_i : in  std_logic := '0';
    write_idle_o       : out std_logic;
    read_idle_o        : out std_logic;
    ddr_csr_o        : out fifo_csr_t := C_INIT_FIFO_CSR;
    m_avmm_h2c_bus_o : out avmm_h2c_bus_t;
    m_avmm_c2h_bus_i : in  avmm_c2h_bus_t
    );
end entity ddr_stream_avmm;

architecture RTL of ddr_stream_avmm is

  -----------------------------------------------------------------------------
  -- Constants — same flavour as ddr_stream_axi.vhd.
  -----------------------------------------------------------------------------
  constant C_FWFT               : integer := 1;
  constant C_CDC_NUM_STAGES     : integer := 2;
  constant C_MSGS_PER_WORD      : integer := ddr_data_width_g / user_data_width_g;
  constant C_MSGS_PER_WORD_LOG2 : integer := integer(ceil(log2(real(C_MSGS_PER_WORD))));
  constant C_RESET_INIT_TIMEOUT : integer := 64;
  constant C_EXTEND_EMPTY_PIPE  : integer := 6;

  -----------------------------------------------------------------------------
  -- Signals.
  -----------------------------------------------------------------------------
  signal uploader_addr : std_logic_vector(ddr_addr_width_g - 1 downto 0);
  signal uploader_cs_r : std_logic;

  signal downloader_cs_r : std_logic;
  signal downloader_addr : std_logic_vector(ddr_addr_width_g - 1 downto 0);

  signal rstTimeout_r   : integer := 0;
  signal busWrError     : std_logic;
  signal busRdError     : std_logic;
  signal upBusBusy      : std_logic;
  signal downBusBusy    : std_logic;
  signal upBusBusy_r    : std_logic;
  signal downBusBusy_r  : std_logic;
  signal upBusBusyCdc   : std_logic;
  signal downBusBusyCdc : std_logic;
  -- DDR-P3.25: clk_sys-synced bus-error flags for the CSR. Avalon-MM has no
  -- per-transfer error response, so busWrError/busRdError are tied-low parity
  -- stubs here — these stay '0', but the wiring mirrors the AXI variant so
  -- ddr_csr_o.fifoStatus.{write,read}_error are always driven.
  signal busWrError_r   : std_logic;
  signal busRdError_r   : std_logic;
  signal wrErrCdc       : std_logic;
  signal rdErrCdc       : std_logic;

  signal fifoUpDataCnt    : std_logic_vector(fifo_depth_g + C_FWFT - 1 downto 0);
  signal fifoUpDin        : std_logic_vector(ddr_data_width_g - 1 downto 0);
  signal fifoUpEmpty      : std_logic;
  signal fifoUpRdEn       : std_logic;
  signal fifoDownDataRoom : std_logic_vector(fifo_depth_g + C_FWFT - 1 downto 0);
  signal fifoDownFull     : std_logic;
  signal fifoDownDin      : std_logic_vector(ddr_data_width_g - 1 downto 0);
  signal fifoDownWrEn     : std_logic;
  --
  signal fifoUserDin      : std_logic_vector(user_data_width_g - 1 downto 0);
  signal fifoUserDout     : std_logic_vector(user_data_width_g - 1 downto 0);
  signal fifoUserWrEn     : std_logic;
  signal fifoUserFull     : std_logic;
  signal fifoUserRdEn     : std_logic;
  signal fifoUserEmpty    : std_logic;
  signal fifoDDRPipeEmpty : std_logic;
  signal fifoDdrPipeCnt   : std_logic_vector(fifo_depth_g + C_FWFT + C_MSGS_PER_WORD_LOG2 - 1 downto 0);
  signal pipeBusy         : std_logic;
  signal ddrEmpty         : std_logic;
  signal ddrPipeIsEmpty   : std_logic;
  signal ddrFull          : std_logic;
  signal ddrAlmostFull    : std_logic;
  signal ddrChipIsEmpty   : std_logic;
  signal ddrEmptyCdc      : std_logic;
  signal ddrFullCdc       : std_logic;
  signal ddrAlmostFullCdc : std_logic;
  signal ddrPipeCnt       : unsigned(ddr_csr_o.fifoCnt'range);
  signal ddrWordCnt       : unsigned(buffer_width_g - 1 downto 0);
  --
  signal ddrCSR_tp        : fifo_csr_t := C_INIT_FIFO_CSR;
  -- DDR-T3.5: KEEP the CSR status register so synthesis does not constant-fold
  -- and remove fifoStatus.underflow (Synth 8-6014). underflow is unreachable by
  -- construction (DDR-P3.10 handshake gating), so synth proves it constant '0',
  -- but it is a named CSR status bit exposed via ddr_csr_o and must stay
  -- observable and consistent with overflow. Mirrors the AXI variant's keep.
  attribute keep : string;
  attribute keep of ddrCSR_tp : signal is "true";

  -----------------------------------------------------------------------------
  -- AVMM sub-master records (write-only and read-only).
  -----------------------------------------------------------------------------
  signal m_avmm_write_h2c : avmm_write_h2c_t;
  signal m_avmm_write_c2h : avmm_write_c2h_t;
  signal m_avmm_read_h2c  : avmm_read_h2c_t;
  signal m_avmm_read_c2h  : avmm_read_c2h_t;

  -- DDR-P3.10 / VFIFO-T9 (2026-05-18): only producer-side skid stays
  -- (back-pressure decoupling); consumer side is combinational so
  -- the FIFO empty signal cleanly accounts for all buffered state.
  signal skidInValid      : std_logic;
  signal skidInReady      : std_logic;

  -----------------------------------------------------------------------------
  -- DDR-T2.8 (2026-06-15): command-phase ARBITER for the shared AVMM port.
  -- The two sub-masters formerly composed via a bare OR-mux that did not
  -- enforce read/write mutual exclusion (the "disjoint by construction"
  -- claim was an unproven timing assumption). Under concurrent full-duplex
  -- load against a zero-wait slave, both planes asserted their command in
  -- the SAME cycle → illegal Avalon-MM. This arbiter grants the bus to ONE
  -- plane per command phase (round-robin, anti-starvation) and stalls the
  -- other by holding its WAITREQUEST high, so neither sub-master ever
  -- believes a transfer completed that the bus did not actually carry.
  --
  -- An Avalon master samples acceptance on (command & !waitrequest). We
  -- gate the NON-granted master's WAITREQUEST to '1' so it stays in its
  -- command phase without committing, AND mask its READ/WRITE off the
  -- external port. The granted master sees the real slave WAITREQUEST and
  -- drives all shared lines (ADDRESS / BURSTCOUNT / WRITEDATA / BYTEENABLE).
  type arb_grant_t is (GRANT_NONE, GRANT_WRITE, GRANT_READ);
  signal arb_grant_r   : arb_grant_t := GRANT_NONE;
  signal arb_last_r    : arb_grant_t := GRANT_READ;  -- so first tie favours WRITE
  signal arb_want_w    : std_logic;
  signal arb_want_r    : std_logic;
  signal arb_grant_w   : std_logic;
  signal arb_grant_rd  : std_logic;
  -- DDR-P3.9: runtime base-address registers — see ddr_stream_axi.vhd.
  constant C_DDR_BASE_RST  : std_logic_vector(ddr_addr_width_g-1 downto 0)
                              := std_logic_vector(resize(unsigned(ddr_base_addr_g), ddr_addr_width_g));
  signal wr_base_addr_sys  : std_logic_vector(ddr_addr_width_g-1 downto 0) := C_DDR_BASE_RST;
  signal rd_base_addr_sys  : std_logic_vector(ddr_addr_width_g-1 downto 0) := C_DDR_BASE_RST;
  signal wr_base_addr_meta : std_logic_vector(ddr_addr_width_g-1 downto 0) := C_DDR_BASE_RST;
  signal wr_base_addr_ddr  : std_logic_vector(ddr_addr_width_g-1 downto 0) := C_DDR_BASE_RST;
  signal rd_base_addr_meta : std_logic_vector(ddr_addr_width_g-1 downto 0) := C_DDR_BASE_RST;
  signal rd_base_addr_ddr  : std_logic_vector(ddr_addr_width_g-1 downto 0) := C_DDR_BASE_RST;
  -- DDR-T1.3 (2026-09-04): the AVMM top serves the SAME shipped scoped SDC as
  -- the AXI top, and section C (constraints/ddr_stream_cdc_scoped.sdc) targets
  -- *_base_addr_sys -> *_base_addr_meta here too. On a static-base build these
  -- fold to the ddr_base_addr_g constant and are removed (TMC-20026 empty
  -- collection) unless pinned. KEEP (Vivado string form) + PRESERVE (Quartus
  -- register-retention) the crossing endpoints so the section-C selector binds
  -- >=1 on BOTH tops and BOTH toolchains. Mirrors ddr_stream_axi.vhd §4a.
  attribute keep of wr_base_addr_sys  : signal is "true";
  attribute keep of rd_base_addr_sys  : signal is "true";
  attribute keep of wr_base_addr_meta : signal is "true";
  attribute keep of rd_base_addr_meta : signal is "true";
  attribute keep of rd_base_addr_ddr  : signal is "true";
  attribute preserve : boolean;
  attribute preserve of wr_base_addr_sys  : signal is true;
  attribute preserve of rd_base_addr_sys  : signal is true;
  attribute preserve of wr_base_addr_meta : signal is true;
  attribute preserve of rd_base_addr_meta : signal is true;
  attribute preserve of rd_base_addr_ddr  : signal is true;
  -- DDR-P3.11: ptr_reset toggle CDC — see ddr_stream_axi.vhd.
  signal wr_ptr_reset_tgl_sys   : std_logic := '0';
  signal rd_ptr_reset_tgl_sys   : std_logic := '0';
  signal wr_ptr_reset_tgl_ddr   : std_logic;
  signal rd_ptr_reset_tgl_ddr   : std_logic;
  signal wr_ptr_reset_tgl_ddr_d : std_logic := '0';
  signal rd_ptr_reset_tgl_ddr_d : std_logic := '0';
  signal wr_ptr_reset_ddr       : std_logic := '0';
  signal rd_ptr_reset_ddr       : std_logic := '0';

begin
  -- DDR-P3.10: AXI-Stream skid buffers on both boundary sides.
  -- See ddr_stream_axi.vhd for the design rationale — same pattern.
  i_skid_in : entity rr.skid_buffer
    generic map (
      data_width_g => user_data_width_g
      )
    port map (
      clk_i     => clk_sys_i,
      rst_i     => rst_sys_i,
      s_data_i  => s_axis_data_i,
      s_valid_i => s_axis_valid_i,
      s_ready_o => s_axis_ready_o,
      m_data_o  => fifoUserDin,
      m_valid_o => skidInValid,
      m_ready_i => skidInReady
      );
  skidInReady  <= not fifoUserFull;
  fifoUserWrEn <= skidInValid and skidInReady;

  -- Consumer side: combinational pass-through (no skid). See
  -- ddr_stream_axi.vhd for the VFIFO-T9 rationale.
  m_axis_data_o  <= fifoUserDout;
  -- DDR-T2.11: gate VALID low while in sys reset — a bare `not fifoUserEmpty`
  -- leaves TVALID high through a mid-stream reset (AMBA AXIS §2.7.1 violation).
  m_axis_valid_o <= (not fifoUserEmpty) and not rst_sys_i;
  fifoUserRdEn   <= m_axis_valid_o and m_axis_ready_i;

  -- Reset-init guard: hold uploader/downloader CS low until the
  -- empty/full signals have stabilised post-reset.
  process(clk_ddr_i)
  begin
    if rising_edge(clk_ddr_i) then
      if rstTimeout_r < C_RESET_INIT_TIMEOUT then
        rstTimeout_r <= rstTimeout_r + 1;
      else
        uploader_cs_r   <= '1';
        downloader_cs_r <= '1';
      end if;
      if rst_ddr_i = '1' then
        uploader_cs_r   <= '0';
        downloader_cs_r <= '0';
        rstTimeout_r    <= 0;
      end if;
    end if;
  end process;

  i_uploader : entity rr.uploader_avmm
    generic map(
      ddr_data_width_g => ddr_data_width_g,
      ddr_addr_width_g => ddr_addr_width_g,
      fifo_depth_g     => fifo_depth_g,
      fwft_g           => C_FWFT,
      buffer_width_g   => buffer_width_g
      )
    port map (
      clk_ddr_i      => clk_ddr_i,
      rst_ddr_i      => rst_ddr_i,
      cs_i           => uploader_cs_r,
      fifo_cnt_i     => fifoUpDataCnt,
      fifo_empty_i   => fifoUpEmpty,
      fifo_rd_en_o   => fifoUpRdEn,
      fifo_data_i    => fifoUpDin,
      ptr_write_i    => uploader_addr,
      ptr_read_i     => downloader_addr,
      ptr_end_o      => uploader_addr,
      ddr_base_addr_i => wr_base_addr_ddr,
      ptr_reset_i    => wr_ptr_reset_ddr,
      bus_busy_o     => upBusBusy,
      bus_error_o    => busWrError,
      h2c_o          => m_avmm_write_h2c,
      c2h_i          => m_avmm_write_c2h
      );

  i_downloader : entity rr.downloader_avmm
    generic map(
      ddr_data_width_g => ddr_data_width_g,
      ddr_addr_width_g => ddr_addr_width_g,
      buffer_width_g   => buffer_width_g,
      fifo_depth_g     => fifo_depth_g,
      fwft_g           => C_FWFT
      )
    port map (
      clk_ddr_i      => clk_ddr_i,
      rst_ddr_i      => rst_ddr_i,
      cs_i           => downloader_cs_r,
      fifo_room_i    => fifoDownDataRoom,
      fifo_full_i    => fifoDownFull,
      fifo_wr_en_o   => fifoDownWrEn,
      fifo_data_o    => fifoDownDin,
      ptr_read_i     => downloader_addr,
      ptr_write_i    => uploader_addr,
      ptr_end_o      => downloader_addr,
      ddr_base_addr_i => rd_base_addr_ddr,
      ptr_reset_i    => rd_ptr_reset_ddr,
      bus_busy_o     => downBusBusy,
      bus_error_o    => busRdError,
      h2c_o          => m_avmm_read_h2c,
      c2h_i          => m_avmm_read_c2h
      );

  -----------------------------------------------------------------------------
  -- DDR-T2.8: Command-phase ARBITER + bus composition.
  --
  -- A plane is "in its command phase" while it asserts READ/WRITE and the
  -- slave has not yet accepted it (WAITREQUEST high). Acceptance happens on
  -- the cycle (command & !WAITREQUEST). The arbiter grants ONE plane per
  -- command phase; the non-granted plane is stalled by forcing its
  -- WAITREQUEST to '1' (so it never sees an acceptance) and masked off the
  -- external port. This guarantees READ and WRITE are never both '1' on the
  -- shared port.
  --
  -- Grant lifecycle (registered, no combinational loops):
  --   * Re-arbitrate when idle (GRANT_NONE) or when the currently-granted
  --     plane's command phase ends (it deasserted its request, i.e. its
  --     transfer was accepted last cycle or it had nothing to send).
  --   * On a tie (both want the bus) grant round-robin via arb_last_r.
  --   * Hold the grant across WAITREQUEST stalls so a stalled-but-granted
  --     transfer is never abandoned mid-command-phase.
  -----------------------------------------------------------------------------
  arb_want_w   <= m_avmm_write_h2c.WRITE;
  arb_want_r   <= m_avmm_read_h2c.READ;
  arb_grant_w  <= '1' when arb_grant_r = GRANT_WRITE else '0';
  arb_grant_rd <= '1' when arb_grant_r = GRANT_READ  else '0';

  arb_proc : process(clk_ddr_i)
    variable v_grant : arb_grant_t;
  begin
    if rising_edge(clk_ddr_i) then
      if rst_ddr_i = '1' then
        arb_grant_r <= GRANT_NONE;
        arb_last_r  <= GRANT_READ;
      else
        v_grant := arb_grant_r;

        -- Drop a grant whose plane is no longer requesting (command phase
        -- over: accepted, or nothing more to drive).
        if (arb_grant_r = GRANT_WRITE and arb_want_w = '0') or
           (arb_grant_r = GRANT_READ  and arb_want_r = '0') then
          v_grant := GRANT_NONE;
        end if;

        -- (Re)arbitrate whenever no plane is currently granted.
        if v_grant = GRANT_NONE then
          if arb_want_w = '1' and arb_want_r = '1' then
            -- Tie → round-robin: serve whichever was NOT served last.
            if arb_last_r = GRANT_WRITE then
              v_grant := GRANT_READ;
            else
              v_grant := GRANT_WRITE;
            end if;
          elsif arb_want_w = '1' then
            v_grant := GRANT_WRITE;
          elsif arb_want_r = '1' then
            v_grant := GRANT_READ;
          end if;
        end if;

        arb_grant_r <= v_grant;
        if v_grant = GRANT_WRITE or v_grant = GRANT_READ then
          arb_last_r <= v_grant;
        end if;
      end if;
    end if;
  end process arb_proc;

  -- Shared-port drivers: only the granted plane reaches the bus. The
  -- non-granted plane's address/data/burst lines are forced idle so a stale
  -- value can never alias onto a granted transfer.
  m_avmm_h2c_bus_o.READ  <= m_avmm_read_h2c.READ  and arb_grant_rd;
  m_avmm_h2c_bus_o.WRITE <= m_avmm_write_h2c.WRITE and arb_grant_w;

  m_avmm_h2c_bus_o.ADDRESS    <= m_avmm_write_h2c.ADDRESS    when arb_grant_w = '1'
                                 else m_avmm_read_h2c.ADDRESS when arb_grant_rd = '1'
                                 else (others => '0');
  m_avmm_h2c_bus_o.BURSTCOUNT <= m_avmm_write_h2c.BURSTCOUNT    when arb_grant_w = '1'
                                 else m_avmm_read_h2c.BURSTCOUNT when arb_grant_rd = '1'
                                 else (others => '0');
  m_avmm_h2c_bus_o.WRITEDATA  <= m_avmm_write_h2c.WRITEDATA  when arb_grant_w = '1'
                                 else (others => '0');
  m_avmm_h2c_bus_o.BYTEENABLE <= m_avmm_write_h2c.BYTEENABLE when arb_grant_w = '1'
                                 else (others => '0');

  -- Tee slave responses to both sub-masters, BUT gate the non-granted
  -- plane's WAITREQUEST high so it stalls cleanly in its command phase and
  -- never registers an acceptance the bus did not carry. Once granted, the
  -- plane sees the real slave WAITREQUEST and commits normally.
  --   * Write plane: stalled unless granted. (When granted but the slave
  --     stalls, it still sees the real WAITREQUEST.)
  --   * Read plane: the READ command phase (ADDR_ACK) is gated the same way;
  --     once accepted it leaves ADDR_ACK and the subsequent READDATAVALID
  --     drain (DATA_READ) does not assert READ, so it needs no gating.
  m_avmm_write_c2h.WAITREQUEST <= m_avmm_c2h_bus_i.WAITREQUEST when arb_grant_w = '1'
                                  else '1';
  m_avmm_read_c2h.WAITREQUEST  <= m_avmm_c2h_bus_i.WAITREQUEST when arb_grant_rd = '1'
                                  else '1';
  -- READDATA / READDATAVALID are response-side: they belong to an
  -- already-accepted read burst and must always reach the read plane,
  -- independent of the current command-phase grant.
  m_avmm_read_c2h.READDATA      <= m_avmm_c2h_bus_i.READDATA;
  m_avmm_read_c2h.READDATAVALID <= m_avmm_c2h_bus_i.READDATAVALID;

  -- DDR-T2.8: ENFORCE the invariant the OR-mux comment merely claimed.
  arb_invariant_proc : process(clk_ddr_i)
  begin
    if rising_edge(clk_ddr_i) then
      if rst_ddr_i = '0' then
        assert not (m_avmm_h2c_bus_o.READ = '1' and m_avmm_h2c_bus_o.WRITE = '1')
          report "AVMM read/write overlap on shared master port (DDR-T2.8)"
          severity failure;
      end if;
    end if;
  end process arb_invariant_proc;

  -----------------------------------------------------------------------------
  -- Bus-agnostic policy + glue (identical to ddr_stream_axi).
  -----------------------------------------------------------------------------
  i_ptr_mgr : entity rr.ptr_mgr
    generic map (
      ptr_width_g => buffer_width_g,
      ptr_size_g  => ddr_data_width_g/8,
      prog_full_g => 2
      )
    port map (
      clk_i          => clk_ddr_i,
      rst_i          => rst_ddr_i,
      in_ptr_wr_i    => uploader_addr(buffer_width_g-1 downto 0),
      in_ptr_rd_i    => downloader_addr(buffer_width_g-1 downto 0),
      empty_o        => ddrEmpty,
      full_o         => ddrFull,
      almost_empty_o => open,
      almost_full_o  => ddrAlmostFull,
      diff_o         => ddrWordCnt
      );

  i_sync_cdc_empty : entity rr.ddr_sync_cdc
    generic map(
      num_stages_g => C_CDC_NUM_STAGES,
      rst_val_g    => '1'
      )
    port map (
      clk_i => clk_sys_i,
      set_i => rst_sys_i,
      rst_i => '0',
      d_i   => ddrEmpty,
      q_o   => ddrEmptyCdc
      );

  i_sync_cdc_full : entity rr.ddr_sync_cdc
    generic map(
      num_stages_g => C_CDC_NUM_STAGES
      )
    port map (
      clk_i => clk_sys_i,
      set_i => '0',
      rst_i => '0',
      d_i   => ddrAlmostFull,
      q_o   => ddrAlmostFullCdc
      );

  upBusBusy_r <= upBusBusy when rising_edge(clk_ddr_i);

  i_sync_cdc_up_busy : entity rr.ddr_sync_cdc
    generic map(
      num_stages_g => C_CDC_NUM_STAGES
      )
    port map (
      clk_i => clk_sys_i,
      set_i => '0',
      rst_i => '0',
      d_i   => upBusBusy_r,
      q_o   => upBusBusyCdc
      );

   downBusBusy_r <= downBusBusy when rising_edge(clk_ddr_i);

  i_sync_cdc_down_busy : entity rr.ddr_sync_cdc
    generic map(
      num_stages_g => C_CDC_NUM_STAGES+C_EXTEND_EMPTY_PIPE,
      or_reduce_g  => TRUE
      )
    port map (
      clk_i => clk_sys_i,
      set_i => '0',
      rst_i => '0',
      d_i   => downBusBusy_r,
      q_o   => downBusBusyCdc
      );

  -- DDR-P3.25: sync the (tied-low) bus-error stubs into clk_sys for the CSR,
  -- mirroring the AXI variant (Avalon-MM has no error response → stays '0').
  busWrError_r <= busWrError when rising_edge(clk_ddr_i);
  busRdError_r <= busRdError when rising_edge(clk_ddr_i);

  i_sync_cdc_wr_err : entity rr.ddr_sync_cdc
    generic map(
      num_stages_g => C_CDC_NUM_STAGES
      )
    port map (
      clk_i => clk_sys_i,
      set_i => '0',
      rst_i => '0',
      d_i   => busWrError_r,
      q_o   => wrErrCdc
      );

  i_sync_cdc_rd_err : entity rr.ddr_sync_cdc
    generic map(
      num_stages_g => C_CDC_NUM_STAGES
      )
    port map (
      clk_i => clk_sys_i,
      set_i => '0',
      rst_i => '0',
      d_i   => busRdError_r,
      q_o   => rdErrCdc
      );

  i_ddr_fifo_intf : entity rr.ddr_fifo_intf
    generic map(
      fifo_depth_g => fifo_depth_g,
      fifo_width_g => ddr_data_width_g,
      msg_width_g  => user_data_width_g,
      fifo_impl_g  => fifo_impl_g
      )
    port map (
      clk_sys_i         => clk_sys_i,
      clk_ddr_i         => clk_ddr_i,
      rst_sys_i         => rst_sys_i,
      rst_ddr_i         => rst_ddr_i,
      busy_o            => pipeBusy,
      fifo_in_data_i    => fifoUserDin,
      fifo_in_wren_i    => fifoUserWrEn,
      fifo_in_full_o    => fifoUserFull,
      fifo_out_data_o   => fifoUserDout,
      fifo_out_rden_i   => fifoUserRdEn,
      fifo_out_empty_o  => fifoUserEmpty,
      fifo_pipe_empty_o => fifoDDRPipeEmpty,
      fifo_pipe_cnt_o   => fifoDdrPipeCnt,
      fifo_up_cnt_o     => fifoUpDataCnt,
      fifo_up_empty_o   => fifoUpEmpty,
      fifo_up_rd_en_i   => fifoUpRdEn,
      fifo_up_data_o    => fifoUpDin,
      fifo_down_room_o  => fifoDownDataRoom,
      fifo_down_full_o  => fifoDownFull,
      fifo_down_wr_en_i => fifoDownWrEn,
      fifo_down_data_i  => fifoDownDin
      );

  ddrChipIsEmpty <= ddrEmptyCdc;
  ddrPipeIsEmpty <= ddrEmptyCdc and fifoDDRPipeEmpty and (not upBusBusyCdc) and (not downBusBusyCdc);
  ddrPipeCnt     <= assign_with_padding(ddrPipeCnt, unsigned(fifoDdrPipeCnt));

  -----------------------------------------------------------------------------
  -- Outputs (registered with reset clause — DDR-T1 hygiene).
  -----------------------------------------------------------------------------
  ddrCSR_tp.fifoStatus.full       <= fifoUserFull;
  ddrCSR_tp.fifoStatus.empty      <= ddrPipeIsEmpty;
  ddrCSR_tp.fifoStatus.chip_empty <= ddrChipIsEmpty;
  ddrCSR_tp.fifoStatus.chip_full  <= ddrAlmostFullCdc;
  ddrCSR_tp.fifoStatus.write_error <= wrErrCdc;   -- DDR-P3.25 (AVMM: always '0')
  ddrCSR_tp.fifoStatus.read_error  <= rdErrCdc;   -- DDR-P3.25 (AVMM: always '0')
  ddrCSR_tp.fifoCnt               <= std_logic_vector(ddrPipeCnt);

  -- DDR-P3.10: dout_o / dout_valid_o / busy_o moved into skid_buffer
  -- registers on the m_axis_* / s_axis_* boundaries. Only ddr_csr_o
  -- stays in the output register process.
  output_reg_proc : process(clk_sys_i) is
  begin
    if rising_edge(clk_sys_i) then
      if rst_sys_i = '1' then
        ddr_csr_o <= C_INIT_FIFO_CSR;
      else
        ddr_csr_o <= ddrCSR_tp;
      end if;
    end if;
  end process;

  ---------------------------------------------------------------------------
  -- DDR-P3.9: runtime base-address registers + idle outputs.
  -- See ddr_stream_axi.vhd for the design rationale, and for the
  -- quasi-static protocol (DDR-REQ-211, DDR-P2.29) that the RR-CDC-005
  -- waivers on the two *_base_addr_meta captures below name.
  ---------------------------------------------------------------------------
  write_idle_o <= not upBusBusyCdc;
  read_idle_o  <= not downBusBusyCdc;

  csr_base_addr_sys_proc : process(clk_sys_i) is
  begin
    if rising_edge(clk_sys_i) then
      if rst_sys_i = '1' then
        wr_base_addr_sys     <= C_DDR_BASE_RST;
        rd_base_addr_sys     <= C_DDR_BASE_RST;
        wr_ptr_reset_tgl_sys <= '0';
        rd_ptr_reset_tgl_sys <= '0';
      else
        if csr_wr_base_load_i = '1' and upBusBusyCdc = '0' then
          wr_base_addr_sys <= csr_wr_base_addr_i;
        end if;
        if csr_rd_base_load_i = '1' and downBusBusyCdc = '0' then
          rd_base_addr_sys <= csr_rd_base_addr_i;
        end if;
        -- DDR-P3.11: latch a ptr_reset pulse as a toggle edge, gated by the
        -- same idle contract as base_load — see ddr_stream_axi.vhd.
        if csr_wr_ptr_reset_i = '1' and upBusBusyCdc = '0' then
          wr_ptr_reset_tgl_sys <= not wr_ptr_reset_tgl_sys;
        end if;
        if csr_rd_ptr_reset_i = '1' and downBusBusyCdc = '0' then
          rd_ptr_reset_tgl_sys <= not rd_ptr_reset_tgl_sys;
        end if;
      end if;
    end if;
  end process;

  csr_base_addr_ddr_proc : process(clk_ddr_i) is
  begin
    if rising_edge(clk_ddr_i) then
      if rst_ddr_i = '1' then
        wr_base_addr_meta <= C_DDR_BASE_RST;
        wr_base_addr_ddr  <= C_DDR_BASE_RST;
        rd_base_addr_meta <= C_DDR_BASE_RST;
        rd_base_addr_ddr  <= C_DDR_BASE_RST;
      else
        wr_base_addr_meta <= wr_base_addr_sys;  -- rr-lint-disable-line RR-CDC-005 quasi-static: wr_base_addr_sys loads only while write_idle_o=1 and the plane holds no data until the capture settles (DDR-REQ-211)
        wr_base_addr_ddr  <= wr_base_addr_meta;
        rd_base_addr_meta <= rd_base_addr_sys;  -- rr-lint-disable-line RR-CDC-005 quasi-static: rd_base_addr_sys loads only while read_idle_o=1 and the plane holds no data until the capture settles (DDR-REQ-211)
        rd_base_addr_ddr  <= rd_base_addr_meta;
      end if;
    end if;
  end process;

  ---------------------------------------------------------------------------
  -- DDR-P3.11: ptr_reset toggle CDC, clk_sys -> clk_ddr — see
  -- ddr_stream_axi.vhd for the design rationale.
  ---------------------------------------------------------------------------
  i_sync_cdc_wr_ptr_reset : entity rr.ddr_sync_cdc
    generic map (
      num_stages_g => C_CDC_NUM_STAGES,
      or_reduce_g  => false
      )
    port map (
      clk_i => clk_ddr_i,
      rst_i => '0',
      set_i => '0',
      d_i   => wr_ptr_reset_tgl_sys,
      q_o   => wr_ptr_reset_tgl_ddr
      );

  i_sync_cdc_rd_ptr_reset : entity rr.ddr_sync_cdc
    generic map (
      num_stages_g => C_CDC_NUM_STAGES,
      or_reduce_g  => false
      )
    port map (
      clk_i => clk_ddr_i,
      rst_i => '0',
      set_i => '0',
      d_i   => rd_ptr_reset_tgl_sys,
      q_o   => rd_ptr_reset_tgl_ddr
      );

  csr_ptr_reset_ddr_proc : process(clk_ddr_i) is
  begin
    if rising_edge(clk_ddr_i) then
      if rst_ddr_i = '1' then
        wr_ptr_reset_tgl_ddr_d <= wr_ptr_reset_tgl_ddr;
        rd_ptr_reset_tgl_ddr_d <= rd_ptr_reset_tgl_ddr;
        wr_ptr_reset_ddr       <= '0';
        rd_ptr_reset_ddr       <= '0';
      else
        wr_ptr_reset_tgl_ddr_d <= wr_ptr_reset_tgl_ddr;
        rd_ptr_reset_tgl_ddr_d <= rd_ptr_reset_tgl_ddr;
        wr_ptr_reset_ddr <= wr_ptr_reset_tgl_ddr xor wr_ptr_reset_tgl_ddr_d;
        rd_ptr_reset_ddr <= rd_ptr_reset_tgl_ddr xor rd_ptr_reset_tgl_ddr_d;
      end if;
    end if;
  end process;

  ----- Overflow / underflow assertions (DDR-P3.10: handshake-gated, so
  ----- these are unreachable by construction — defensive only).
  process(clk_sys_i)
  begin
    if rising_edge(clk_sys_i) then
      ddrCSR_tp.fifoStatus.overflow  <= '0';
      ddrCSR_tp.fifoStatus.underflow <= '0';

      if fifoUserWrEn = '1' and fifoUserFull = '1' then
        report "Internal: write attempt with FIFO full (skid guard breached)";
        ddrCSR_tp.fifoStatus.overflow <= '1';
      end if;
      if fifoUserWrEn = '1' and ddrFullCdc = '1' then
        report "Internal: write attempt with DDR full";
        ddrCSR_tp.fifoStatus.overflow <= '1';
      end if;
      if fifoUserRdEn = '1' and fifoDDRPipeEmpty = '1' then
        report "Internal: read attempt with pipe empty (skid guard breached)";
        ddrCSR_tp.fifoStatus.underflow <= '1';
      end if;
    end if;
  end process;

end architecture RTL;
