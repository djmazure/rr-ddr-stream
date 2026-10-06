-------------------------------------------------------------------------------
-- Title      : ddr_validator_csr — AXI4-Lite control/status register file
-- Project    : rr-ddr-stream / zybo_z7_20_ddr_validator
-------------------------------------------------------------------------------
-- File       : ddr_validator_csr.vhd
-- Author     : Daniel J. Mazure <dasjimaz@gmail.com>
-- Standard   : VHDL-2008
-------------------------------------------------------------------------------
-- Description:
--   AXI4-Lite slave exposing the validator CSR map (docs/DESIGN.md §4) to
--   the Linux userspace app over the PS M_AXI_GP0 port. Word-addressed,
--   32-bit, 4 KiB window @ 0x4000_0000.
--
--   The AXI handshake is the house single-beat AXI4-Lite slave pattern
--   (mirrors deepskopion/hw/rtl/regbank/system_regbank.vhd): AW+W accepted
--   together, single-cycle B, registered read data. No bursts, no IDs,
--   no outstanding transactions — AXI4-Lite by construction.
--
--   `W1P` (write-1-to-pulse) bits emit a 1-cycle strobe on the write-commit
--   cycle and self-clear: CTRL.start, CTRL.soft_reset, BASE_LOAD.[0,1],
--   PTR_RESET.[0,1]. Everything else is plain RW (control) or R (status
--   latched combinationally from the BIST / IP).
--
--   Reset is active-high, synchronous (clk_sys domain). The top converts
--   the BD's active-low fclk_aresetn to this polarity.
-------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity ddr_validator_csr is
  generic (
    -- AXI-Lite address width seen by the slave (offset within the 4 KiB
    -- window the smartconnect routes from 0x4000_0000). 12 bits = 4 KiB.
    addr_width_g : positive := 12
  );
  port (
    clk_i : in std_logic;                       -- clk_sys
    rst_i : in std_logic;                        -- active-high, sync

    -- ── AXI4-Lite slave (from PS M_AXI_GP0 via smartconnect) ──────────────
    s_axi_awaddr_i  : in  std_logic_vector(addr_width_g-1 downto 0);
    s_axi_awprot_i  : in  std_logic_vector(2 downto 0);
    s_axi_awvalid_i : in  std_logic;
    s_axi_awready_o : out std_logic;
    s_axi_wdata_i   : in  std_logic_vector(31 downto 0);
    s_axi_wstrb_i   : in  std_logic_vector(3 downto 0);
    s_axi_wvalid_i  : in  std_logic;
    s_axi_wready_o  : out std_logic;
    s_axi_bresp_o   : out std_logic_vector(1 downto 0);
    s_axi_bvalid_o  : out std_logic;
    s_axi_bready_i  : in  std_logic;
    s_axi_araddr_i  : in  std_logic_vector(addr_width_g-1 downto 0);
    s_axi_arprot_i  : in  std_logic_vector(2 downto 0);
    s_axi_arvalid_i : in  std_logic;
    s_axi_arready_o : out std_logic;
    s_axi_rdata_o   : out std_logic_vector(31 downto 0);
    s_axi_rresp_o   : out std_logic_vector(1 downto 0);
    s_axi_rvalid_o  : out std_logic;
    s_axi_rready_i  : in  std_logic;

    -- ── Control (to BIST + IP) ────────────────────────────────────────────
    start_o        : out std_logic;             -- CTRL[0]  W1P
    soft_reset_o   : out std_logic;             -- CTRL[1]  W1P
    continuous_o   : out std_logic;             -- CTRL[8]  RW (reserved — single-shot today)
    scenario_o     : out std_logic_vector(3 downto 0);   -- SCENARIO[3:0]
    n_words_o      : out std_logic_vector(31 downto 0);  -- N_WORDS
    wr_base_o      : out std_logic_vector(31 downto 0);  -- WR_BASE
    rd_base_o      : out std_logic_vector(31 downto 0);  -- RD_BASE
    wr_base_load_o : out std_logic;             -- BASE_LOAD[0] W1P
    rd_base_load_o : out std_logic;             -- BASE_LOAD[1] W1P
    wr_ptr_reset_o : out std_logic;             -- PTR_RESET[0] W1P (DDR-P3.11)
    rd_ptr_reset_o : out std_logic;             -- PTR_RESET[1] W1P (DDR-P3.11)

    -- ── Status (from BIST + IP) ───────────────────────────────────────────
    running_i         : in std_logic;
    done_i            : in std_logic;
    pass_i            : in std_logic;
    fail_i            : in std_logic;
    write_idle_i      : in std_logic;
    read_idle_i       : in std_logic;
    error_count_i     : in std_logic_vector(31 downto 0);
    words_written_i   : in std_logic_vector(31 downto 0);
    words_read_i      : in std_logic_vector(31 downto 0);
    first_err_index_i : in std_logic_vector(31 downto 0);
    first_err_exp_i   : in std_logic_vector(31 downto 0);
    first_err_got_i   : in std_logic_vector(31 downto 0);
    wr_commit_addr_i  : in std_logic_vector(31 downto 0)
  );
end entity ddr_validator_csr;

architecture rtl of ddr_validator_csr is

  -- Byte→word address conversion (32-bit registers).
  constant C_ADDR_LSB : natural := 2;

  -- Word indices (byte offset / 4) — must match docs/DESIGN.md §4.
  constant C_CTRL        : natural := 16#00# / 4;  -- 0x00
  constant C_SCENARIO    : natural := 16#08# / 4;  -- 0x08
  constant C_N_WORDS     : natural := 16#0C# / 4;  -- 0x0C
  constant C_WR_BASE     : natural := 16#28# / 4;  -- 0x28
  constant C_RD_BASE     : natural := 16#2C# / 4;  -- 0x2C
  constant C_BASE_LOAD   : natural := 16#30# / 4;  -- 0x30
  constant C_PTR_RESET   : natural := 16#34# / 4;  -- 0x34
  constant C_WR_COMMIT   : natural := 16#38# / 4;  -- 0x38, DDR-T1.3 durable pointer

  -- Read-only status word indices.
  constant C_STATUS          : natural := 16#04# / 4;  -- 0x04
  constant C_ERROR_COUNT     : natural := 16#10# / 4;  -- 0x10
  constant C_WORDS_WRITTEN   : natural := 16#14# / 4;  -- 0x14
  constant C_WORDS_READ      : natural := 16#18# / 4;  -- 0x18
  constant C_FIRST_ERR_INDEX : natural := 16#1C# / 4;  -- 0x1C
  constant C_FIRST_ERR_EXP   : natural := 16#20# / 4;  -- 0x20
  constant C_FIRST_ERR_GOT   : natural := 16#24# / 4;  -- 0x24
  constant C_IDENT           : natural := 16#3C# / 4;  -- 0x3C

  -- Host sanity probe — magic 'DD5' + IP version 0.2.0 (docs/DESIGN.md §4).
  constant C_IDENT_MAGIC : std_logic_vector(31 downto 0) := x"0DD50200";

  -- ── AXI handshake state ──────────────────────────────────────────────────
  -- DDR-T2.5: AW and W are accepted INDEPENDENTLY (AXI channels are independent).
  -- `aw_taken_r`/`w_taken_r` hold from acceptance until the write commits; WDATA/
  -- WSTRB are latched at W acceptance so the commit doesn't depend on W still
  -- being presented. The earlier coupled "awvalid AND wvalid" handshake deadlocked
  -- a master that drives AW before W.
  signal aw_taken_r : std_logic := '0';
  signal w_taken_r  : std_logic := '0';
  signal wr_commit_s : std_logic;                                    -- comb: both taken, no resp in flight
  signal wdata_r    : std_logic_vector(31 downto 0) := (others => '0');
  signal wstrb_r    : std_logic_vector(3 downto 0)  := (others => '0');
  signal b_valid_r  : std_logic := '0';
  signal ar_ready_r : std_logic := '0';
  signal r_valid_r  : std_logic := '0';
  signal r_data_r   : std_logic_vector(31 downto 0) := (others => '0');

  signal aw_addr_r : unsigned(addr_width_g-1 downto 0) := (others => '0');
  signal ar_addr_r : unsigned(addr_width_g-1 downto 0) := (others => '0');

  -- ── Backing registers for the RW control fields ──────────────────────────
  signal scenario_r   : std_logic_vector(3 downto 0)  := (others => '0');
  signal continuous_r : std_logic                     := '0';
  signal n_words_r    : std_logic_vector(31 downto 0) := std_logic_vector(to_unsigned(64, 32));
  signal wr_base_r    : std_logic_vector(31 downto 0) := (others => '0');
  signal rd_base_r    : std_logic_vector(31 downto 0) := (others => '0');

  -- ── W1P strobes (1-cycle, registered on write commit) ─────────────────────
  signal start_p_r        : std_logic := '0';
  signal soft_reset_p_r   : std_logic := '0';
  signal wr_base_load_p_r : std_logic := '0';
  signal rd_base_load_p_r : std_logic := '0';
  signal wr_ptr_reset_p_r : std_logic := '0';
  signal rd_ptr_reset_p_r : std_logic := '0';

begin

  -- ── Write commit ───────────────────────────────────────────────────────
  -- One commit per write: both channels accepted, no B response in flight. The
  -- b_valid_r gate is the DDR-T2.1 re-fire guard (a master holding AWVALID across
  -- the BVALID/BREADY round-trip — Cadence PS-AXI / Xilinx smartconnect burst-
  -- collapse — must not re-pulse W1P registers).
  wr_commit_s <= '1' when (aw_taken_r = '1' and w_taken_r = '1' and b_valid_r = '0')
                 else '0';

  -- ── Write address channel (accepted independently of W — DDR-T2.5) ──────
  -- AWREADY is asserted whenever no AW is held and no response is in flight; it
  -- does NOT wait for WVALID, so an AW-then-W master cannot deadlock.
  s_axi_awready_o <= '1' when (aw_taken_r = '0' and b_valid_r = '0') else '0';
  p_aw : process (clk_i)
  begin
    if rising_edge(clk_i) then
      if rst_i = '1' then
        aw_taken_r <= '0';
      elsif aw_taken_r = '0' and s_axi_awvalid_i = '1' and b_valid_r = '0' then
        aw_taken_r <= '1';
        aw_addr_r  <= unsigned(s_axi_awaddr_i);
      elsif wr_commit_s = '1' then
        aw_taken_r <= '0';
      end if;
    end if;
  end process;

  -- ── Write data channel (accepted independently of AW; WDATA/WSTRB latched) ─
  s_axi_wready_o <= '1' when (w_taken_r = '0' and b_valid_r = '0') else '0';
  p_w : process (clk_i)
  begin
    if rising_edge(clk_i) then
      if rst_i = '1' then
        w_taken_r <= '0';
      elsif w_taken_r = '0' and s_axi_wvalid_i = '1' and b_valid_r = '0' then
        w_taken_r <= '1';
        wdata_r   <= s_axi_wdata_i;
        wstrb_r   <= s_axi_wstrb_i;
      elsif wr_commit_s = '1' then
        w_taken_r <= '0';
      end if;
    end if;
  end process;

  -- ── Register write + W1P pulse generation ──────────────────────────────
  -- A write commits when wr_commit_s is high (both AW and W accepted) for one
  -- cycle. WSTRB is honoured per byte lane for the multi-byte RW registers.
  -- W1P strobes are decoded from the freshly presented WDATA bits and
  -- asserted for exactly that commit cycle.
  p_reg_wr : process (clk_i)
    variable v_index : natural;
  begin
    if rising_edge(clk_i) then
      -- Default: all strobes low (1-cycle pulse semantics).
      start_p_r        <= '0';
      soft_reset_p_r   <= '0';
      wr_base_load_p_r <= '0';
      rd_base_load_p_r <= '0';
      wr_ptr_reset_p_r <= '0';
      rd_ptr_reset_p_r <= '0';

      if rst_i = '1' then
        scenario_r   <= (others => '0');
        continuous_r <= '0';
        n_words_r    <= std_logic_vector(to_unsigned(64, 32));
        wr_base_r    <= (others => '0');
        rd_base_r    <= (others => '0');
      elsif wr_commit_s = '1' then
        v_index := to_integer(aw_addr_r(addr_width_g-1 downto C_ADDR_LSB));
        case v_index is

          when C_CTRL =>
            -- CTRL: [0] start (W1P) · [1] soft_reset (W1P) · [8] continuous
            if wstrb_r(0) = '1' then
              start_p_r      <= wdata_r(0);
              soft_reset_p_r <= wdata_r(1);
            end if;
            if wstrb_r(1) = '1' then
              continuous_r <= wdata_r(8);
            end if;

          when C_SCENARIO =>
            if wstrb_r(0) = '1' then
              scenario_r <= wdata_r(3 downto 0);
            end if;

          when C_N_WORDS =>
            for byte_i in 0 to 3 loop
              if wstrb_r(byte_i) = '1' then
                n_words_r(byte_i*8+7 downto byte_i*8) <= wdata_r(byte_i*8+7 downto byte_i*8);
              end if;
            end loop;

          when C_WR_BASE =>
            for byte_i in 0 to 3 loop
              if wstrb_r(byte_i) = '1' then
                wr_base_r(byte_i*8+7 downto byte_i*8) <= wdata_r(byte_i*8+7 downto byte_i*8);
              end if;
            end loop;

          when C_RD_BASE =>
            for byte_i in 0 to 3 loop
              if wstrb_r(byte_i) = '1' then
                rd_base_r(byte_i*8+7 downto byte_i*8) <= wdata_r(byte_i*8+7 downto byte_i*8);
              end if;
            end loop;

          when C_BASE_LOAD =>
            -- BASE_LOAD: [0] load WR_BASE (W1P) · [1] load RD_BASE (W1P)
            if wstrb_r(0) = '1' then
              wr_base_load_p_r <= wdata_r(0);
              rd_base_load_p_r <= wdata_r(1);
            end if;

          when C_PTR_RESET =>
            -- PTR_RESET: [0] wr ptr reset · [1] rd ptr reset (W1P, DDR-P3.11)
            if wstrb_r(0) = '1' then
              wr_ptr_reset_p_r <= wdata_r(0);
              rd_ptr_reset_p_r <= wdata_r(1);
            end if;

          when others =>
            null;  -- read-only / unmapped — writes ignored (still OKAY-acked)
        end case;
      end if;
    end if;
  end process;

  -- ── Write response ─────────────────────────────────────────────────────
  p_b : process (clk_i)
  begin
    if rising_edge(clk_i) then
      if rst_i = '1' then
        b_valid_r <= '0';
      elsif wr_commit_s = '1' then
        b_valid_r <= '1';
      elsif s_axi_bready_i = '1' and b_valid_r = '1' then
        b_valid_r <= '0';
      end if;
    end if;
  end process;
  s_axi_bvalid_o <= b_valid_r;
  s_axi_bresp_o  <= "00";  -- OKAY

  -- ── Read address channel ───────────────────────────────────────────────
  -- DDR-T2.1 / RTL-P2.651: gate the AR re-arm on r_valid_r='0' (read-side
  -- counterpart of the AW/W gates above). Without it, a master pipelining
  -- ARADDR before RREADY drains the previous response can have the second
  -- read silently merged into the held response.
  p_ar : process (clk_i)
  begin
    if rising_edge(clk_i) then
      if rst_i = '1' then
        ar_ready_r <= '0';
      elsif ar_ready_r = '0' and s_axi_arvalid_i = '1'
            and r_valid_r = '0' then  -- DDR-T2.1: one handshake per read
        ar_ready_r <= '1';
        ar_addr_r  <= unsigned(s_axi_araddr_i);
      else
        ar_ready_r <= '0';
      end if;
    end if;
  end process;
  s_axi_arready_o <= ar_ready_r;

  -- ── Read data channel ──────────────────────────────────────────────────
  p_rd : process (clk_i)
    variable v_index : natural;
  begin
    if rising_edge(clk_i) then
      if rst_i = '1' then
        r_valid_r <= '0';
        r_data_r  <= (others => '0');
      elsif ar_ready_r = '1' and s_axi_arvalid_i = '1' and r_valid_r = '0' then
        r_valid_r <= '1';
        v_index   := to_integer(ar_addr_r(addr_width_g-1 downto C_ADDR_LSB));
        case v_index is
          when C_CTRL =>
            r_data_r        <= (others => '0');
            r_data_r(8)     <= continuous_r;
          when C_STATUS =>
            r_data_r        <= (others => '0');
            r_data_r(0)     <= running_i;
            r_data_r(1)     <= done_i;
            r_data_r(2)     <= pass_i;
            r_data_r(3)     <= fail_i;
            r_data_r(4)     <= write_idle_i;
            r_data_r(5)     <= read_idle_i;
          when C_SCENARIO        => r_data_r <= x"0000000" & scenario_r;
          when C_N_WORDS         => r_data_r <= n_words_r;
          when C_ERROR_COUNT     => r_data_r <= error_count_i;
          when C_WORDS_WRITTEN   => r_data_r <= words_written_i;
          when C_WORDS_READ      => r_data_r <= words_read_i;
          when C_FIRST_ERR_INDEX => r_data_r <= first_err_index_i;
          when C_FIRST_ERR_EXP   => r_data_r <= first_err_exp_i;
          when C_FIRST_ERR_GOT   => r_data_r <= first_err_got_i;
          when C_WR_BASE         => r_data_r <= wr_base_r;
          when C_RD_BASE         => r_data_r <= rd_base_r;
          when C_WR_COMMIT       => r_data_r <= wr_commit_addr_i;
          when C_IDENT           => r_data_r <= C_IDENT_MAGIC;
          when others            => r_data_r <= (others => '0');
        end case;
      elsif s_axi_rready_i = '1' and r_valid_r = '1' then
        r_valid_r <= '0';
      end if;
    end if;
  end process;
  s_axi_rvalid_o <= r_valid_r;
  s_axi_rdata_o  <= r_data_r;
  s_axi_rresp_o  <= "00";  -- OKAY

  -- ── Control outputs ────────────────────────────────────────────────────
  start_o        <= start_p_r;
  soft_reset_o   <= soft_reset_p_r;
  continuous_o   <= continuous_r;
  scenario_o     <= scenario_r;
  n_words_o      <= n_words_r;
  wr_base_o      <= wr_base_r;
  rd_base_o      <= rd_base_r;
  wr_base_load_o <= wr_base_load_p_r;
  rd_base_load_o <= rd_base_load_p_r;
  wr_ptr_reset_o <= wr_ptr_reset_p_r;
  rd_ptr_reset_o <= rd_ptr_reset_p_r;

end architecture rtl;
