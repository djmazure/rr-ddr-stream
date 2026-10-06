-- =============================================================================
-- Title      : de25_nal_rom — synthetic H.264 NAL byte-ROM (AXI4-Lite read slave)
-- Project    : rr-ddr-stream / de25_agilex5_ddr_validator
-- -----------------------------------------------------------------------------
-- Description:
--   Phase 1b of the FPGA→Eth→RouteWave NALU end-goal: a SYNTHETIC NAL generator
--   in the fabric. A small read-only AXI4-Lite slave that holds a hand-crafted
--   H.264 Annex-B byte sequence (SPS + PPS + IDR-stub) in a ROM. The HPS reads
--   the ROM over the lwhps2fpga (lwh2f) bridge, ships the bytes over Ethernet,
--   and the host de25_wave_bridge --nal demuxes them into RouteWave Protocol-
--   Analyzer packets — proving the analyzer view end-to-end with FABRIC-sourced
--   NAL bytes, with NO dependency on a real bitstream or h264-dec (those are
--   Phase 2 / Phase 3).
--
--   This is DE25-specific (not the shared ddr_validator_csr/bist) — it does not
--   touch the validated f2h/BIST datapath. It is the lwh2f read-slave twin of
--   ddr_validator_csr: same AXI4-Lite handshake style, drop-in behind the same
--   lwh2f_axilite_bridge.
--
--   Register / ROM map (word-addressed, byte offset = 4 * index):
--     0x000  IDENT     = 0x0DD50A10  (magic — confirms the ROM is mapped)
--     0x004  NAL_LEN   = number of VALID Annex-B bytes (= 4*NAL_WORDS here)
--     0x008  NAL_WORDS = number of 32-bit ROM words
--     0x100  + 4*i     = NAL word i  (i = 0 .. NAL_WORDS-1)
--     (all other offsets read 0x00000000; writes are OKAY-acked but ignored)
--
--   Byte packing (CONTRACT for the host reader): BIG-ENDIAN within each 32-bit
--   word — word i carries Annex-B bytes [4i, 4i+1, 4i+2, 4i+3] MSB-first. So the
--   HPS reads word i and emits (w>>24, w>>16, w>>8, w) to reconstruct the stream.
--   word0 = 0x00000001 ⇒ start-code bytes 00 00 00 01; word1 = 0x67420028 ⇒
--   SPS header 0x67 (nal_unit_type 7) … This makes the on-wire byte order
--   independent of the HPS little-endian word view.
-- =============================================================================
library ieee;
  use ieee.std_logic_1164.all;
  use ieee.numeric_std.all;

entity de25_nal_rom is
  generic (
    addr_width_g : positive := 12   -- CSR-window address width (low bits decoded)
  );
  port (
    clk_i : in std_logic;
    rst_i : in std_logic;           -- active-high, synchronous

    -- AXI4-Lite slave (same port shape as ddr_validator_csr; single-beat)
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
    s_axi_rready_i  : in  std_logic
  );
end entity de25_nal_rom;

architecture rtl of de25_nal_rom is

  constant C_ADDR_LSB : natural := 2;  -- 32-bit word addressing

  -- Fixed-register word indices.
  constant C_IDENT_IDX : natural := 0;   -- 0x000
  constant C_LEN_IDX   : natural := 1;   -- 0x004
  constant C_WORDS_IDX : natural := 2;   -- 0x008
  -- ROM read window base word index: 0x100 / 4 = 64.
  constant C_ROM_BASE  : natural := 16#100# / 4;

  constant C_IDENT_MAGIC : std_logic_vector(31 downto 0) := x"0DD50A10";

  -- Synthetic Annex-B stream, big-endian-per-word (see header CONTRACT):
  --   SPS:  00 00 00 01 | 67 42 00 28 | f4 16 27 20   (nal_unit_type 7)
  --   PPS:  00 00 00 01 | 68 ce 38 80                 (nal_unit_type 8)
  --   IDR:  00 00 00 01 | 65 88 84 02   (IDR-slice stub, nal_unit_type 5)
  type rom_t is array (natural range <>) of std_logic_vector(31 downto 0);
  constant C_NAL_ROM : rom_t := (
    0 => x"00000001",   -- start code
    1 => x"67420028",   -- SPS  (0x67 = forbidden 0 / ref_idc 3 / type 7)
    2 => x"F4162720",
    3 => x"00000001",   -- start code
    4 => x"68CE3880",   -- PPS  (0x68 = type 8)
    5 => x"00000001",   -- start code
    6 => x"65888402"    -- IDR  (0x65 = type 5)
  );
  constant C_NAL_WORDS : natural := C_NAL_ROM'length;          -- 7
  constant C_NAL_LEN   : natural := C_NAL_WORDS * 4;           -- 28 bytes

  -- AXI-Lite write side (read-only ROM: writes are accepted + OKAY-acked, no-op).
  signal aw_taken_r : std_logic := '0';
  signal w_taken_r  : std_logic := '0';
  signal b_valid_r  : std_logic := '0';

  -- AXI-Lite read side.
  signal ar_ready_r : std_logic := '0';
  signal ar_addr_r  : unsigned(addr_width_g-1 downto 0) := (others => '0');
  signal r_valid_r  : std_logic := '0';
  signal r_data_r   : std_logic_vector(31 downto 0) := (others => '0');

  signal wr_commit_s : std_logic;

begin

  -- ── Write address / data accept (single-beat; both must arrive to commit) ──
  s_axi_awready_o <= '1' when (aw_taken_r = '0' and b_valid_r = '0') else '0';
  s_axi_wready_o  <= '1' when (w_taken_r  = '0' and b_valid_r = '0') else '0';
  wr_commit_s     <= aw_taken_r and w_taken_r;

  p_aw : process (clk_i)
  begin
    if rising_edge(clk_i) then
      if rst_i = '1' then
        aw_taken_r <= '0';
      elsif aw_taken_r = '0' and s_axi_awvalid_i = '1' and b_valid_r = '0' then
        aw_taken_r <= '1';
      elsif wr_commit_s = '1' then
        aw_taken_r <= '0';
      end if;
    end if;
  end process;

  p_w : process (clk_i)
  begin
    if rising_edge(clk_i) then
      if rst_i = '1' then
        w_taken_r <= '0';
      elsif w_taken_r = '0' and s_axi_wvalid_i = '1' and b_valid_r = '0' then
        w_taken_r <= '1';
      elsif wr_commit_s = '1' then
        w_taken_r <= '0';
      end if;
    end if;
  end process;

  -- ── Write response (ROM is read-only: commit just returns OKAY) ────────────
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

  -- ── Read address channel (one handshake per read; gate on r_valid_r='0') ───
  p_ar : process (clk_i)
  begin
    if rising_edge(clk_i) then
      if rst_i = '1' then
        ar_ready_r <= '0';
      elsif ar_ready_r = '0' and s_axi_arvalid_i = '1' and r_valid_r = '0' then
        ar_ready_r <= '1';
        ar_addr_r  <= unsigned(s_axi_araddr_i);
      else
        ar_ready_r <= '0';
      end if;
    end if;
  end process;
  s_axi_arready_o <= ar_ready_r;

  -- ── Read data channel (registered; decode IDENT/LEN/WORDS + ROM window) ────
  p_rd : process (clk_i)
    variable v_index : natural;
    variable v_rom   : natural;
  begin
    if rising_edge(clk_i) then
      if rst_i = '1' then
        r_valid_r <= '0';
        r_data_r  <= (others => '0');
      elsif ar_ready_r = '1' and s_axi_arvalid_i = '1' and r_valid_r = '0' then
        r_valid_r <= '1';
        v_index   := to_integer(ar_addr_r(addr_width_g-1 downto C_ADDR_LSB));
        if v_index = C_IDENT_IDX then
          r_data_r <= C_IDENT_MAGIC;
        elsif v_index = C_LEN_IDX then
          r_data_r <= std_logic_vector(to_unsigned(C_NAL_LEN, 32));
        elsif v_index = C_WORDS_IDX then
          r_data_r <= std_logic_vector(to_unsigned(C_NAL_WORDS, 32));
        elsif v_index >= C_ROM_BASE and v_index < C_ROM_BASE + C_NAL_WORDS then
          v_rom    := v_index - C_ROM_BASE;
          r_data_r <= C_NAL_ROM(v_rom);
        else
          r_data_r <= (others => '0');
        end if;
      elsif s_axi_rready_i = '1' and r_valid_r = '1' then
        r_valid_r <= '0';
      end if;
    end if;
  end process;
  s_axi_rvalid_o <= r_valid_r;
  s_axi_rdata_o  <= r_data_r;
  s_axi_rresp_o  <= "00";  -- OKAY

end architecture rtl;
