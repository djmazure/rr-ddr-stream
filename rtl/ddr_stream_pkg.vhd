-------------------------------------------------------------------------------
-- Title      : rr_ddr_stream internal package
-- Project    : rr-ddr-stream — RouteRTL DDR-backed streaming-DMA primitive
-------------------------------------------------------------------------------
-- File       : ddr_stream_pkg.vhd
-- Author     : Daniel J. Mazure <dasjimaz@gmail.com>
-- Standard   : VHDL'93/02 (compatible with NVC --std=2008)
-------------------------------------------------------------------------------
-- Description:
--   Bus-record types + sizing constants for rr-ddr-stream's AXI4 backend.
--   Carved out of rr-vfifo/rtl/vfifo_pkg.vhd 2026-05-15 (decision
--   rr-vfifo D.1). The FIFO CSR / data-valid types stayed in rr-vfifo
--   since they belong to the FIFO-product surface, not the DMA core.
--
--   AVMM record types (avmm_h2c_t / avmm_c2h_t) land here under DDR-P3.1
--   alongside the AXI ones. Both backends share this package; VHDL
--   elaboration prunes unused entities at synth, so no LUT cost for
--   single-backend users.
-------------------------------------------------------------------------------

library IEEE;
use IEEE.STD_LOGIC_1164.all;
use IEEE.NUMERIC_STD.all;
use IEEE.MATH_REAL.all;

package ddr_stream_pkg is

  -----------------------------------------------------------------------------
  -- IP version stamp (DDR-P3.14). A 32-bit packed identity word a consumer can
  -- pull into its own identity regbank at ELABORATION (no read-plane needed —
  -- write-only-plane integrators, e.g. enable_read_plane_g=false, still get it).
  -- Also exposed as ddr_stream_axi's always-on `version_o` output port.
  -- Layout: [31:24]=major, [23:16]=minor, [15:8]=patch, [7:0]=reserved(0x00).
  -- KEEP IN SYNC with ip.yml `version:` on every release (a generator could
  -- enforce this later — same drift risk as a regbank schema).
  -----------------------------------------------------------------------------
  constant C_DDR_STREAM_VER_MAJOR : natural := 0;
  constant C_DDR_STREAM_VER_MINOR : natural := 3;
  constant C_DDR_STREAM_VER_PATCH : natural := 6;   -- ip.yml: version "0.3.6"

  constant C_DDR_STREAM_VERSION : std_logic_vector(31 downto 0) :=
      std_logic_vector(to_unsigned(C_DDR_STREAM_VER_MAJOR, 8))
    & std_logic_vector(to_unsigned(C_DDR_STREAM_VER_MINOR, 8))
    & std_logic_vector(to_unsigned(C_DDR_STREAM_VER_PATCH, 8))
    & x"00";

  -----------------------------------------------------------------------------
  -- Endpoint-FIFO sizing — referenced by the AXI4 record + array types.
  -- NUM_EP_FIFOS is the count of distinct endpoint FIFOs a multi-tier
  -- consumer (rr-vfifo) would serve; the DMA core itself uses one.
  -----------------------------------------------------------------------------
  constant C_NUM_EP_FIFOS         : integer := 2;
  constant C_FIFO_BURST_INDEX     : natural := 1;
  constant C_FIFO_STREAM_INDEX    : natural := 0;
  constant C_EP_FIFO_WIDTH        : integer := 32;
  constant C_FP_BUS_WIDTH         : integer := 32;
  -- granularity of a logical message at the user-facing boundary. Renamed
  -- from C_SNIFFER_MSG_WIDTH 2026-05-28 — the legacy name carried over from
  -- a sniffer-domain origin; rr-ddr-stream is not a sniffer.
  constant C_USER_MSG_WIDTH       : integer := 32;

  -----------------------------------------------------------------------------
  -- AXI4 plumbing — bus width, channel sub-field widths, the H2C / C2H record
  -- pair, and the corresponding init constants. Defines the bus surface that
  -- axi4r / axi4w drive on the memory side of the IP.
  -----------------------------------------------------------------------------
  -- RR--P3.6: bus-width ceiling — 512 covers every supported
  -- ddr_data_width_g (32, 64, 128, 256, 512). Narrower DUT
  -- instantiations drive only bits [ddr_data_width_g-1 : 0] of WDATA
  -- / RDATA / WRITEDATA / READDATA; the upper bits are constant-
  -- zero filler at synth time. AXI / AVMM masters indicate the
  -- effective beat width via AWSIZE / ARSIZE; slaves respect that.
  constant C_AXI_DATA_WIDTH  : integer := 512;
  constant C_AXI_ADDR_WIDTH  : integer := 32;
  constant C_AXI_LEN_WIDTH   : integer := 8;
  constant C_AXI_SIZE_WIDTH  : integer := 3;
  constant C_AXI_BURST_WIDTH : integer := 2;
  constant C_AXI_LOCK_WIDTH  : integer := 1;
  constant C_AXI_CACHE_WIDTH : integer := 4;
  constant C_AXI_PROT_WIDTH  : integer := 3;
  constant C_AXI_STRB_WIDTH  : integer := C_AXI_DATA_WIDTH/8;
  constant C_AXI_RESP_WIDTH  : integer := 2;
  constant C_AXI_ID_WIDTH    : integer := 2;
  constant C_AXI_QOS_WIDTH   : integer := 4;

  -----------------------------------------------------------------------------
  -- PolarFire SoC Fabric Interface Controller (FIC) constants (DDR-P2.15).
  -- PolarFire SoC MSS exposes LPDDR4 to the FPGA fabric via FIC AXI4 target
  -- ports (FIC0 / FIC1). The native fabric data width is 64 bits, and the
  -- FIC AXI4 interconnect caps bursts at 64 beats (AWLEN <= 63).
  -----------------------------------------------------------------------------
  constant C_FIC_DATA_WIDTH      : integer := 64;
  constant C_PFSOC_FIC_MAX_BURST : integer := 64;

  -- DDR-offset and per-FIFO config arrays, keyed by C_NUM_EP_FIFOS.
  type ddr_offset_array_t                 is array (natural range 0 to C_NUM_EP_FIFOS-1) of unsigned(C_AXI_ADDR_WIDTH-1 downto 0);
  type ddr_min_burst_wr_width_array_t     is array (natural range 0 to C_NUM_EP_FIFOS-1) of integer;
  type ddr_burst_wr_timeout_ratio_array_t is array (natural range 0 to C_NUM_EP_FIFOS-1) of integer;

  -- H2C ("Host-to-Card") — master-side outputs into the memory subsystem.
  type axi4_h2c_bus_t is record
    AWID    : std_logic_vector(C_AXI_ID_WIDTH-1 downto 0);
    AWADDR  : std_logic_vector(C_AXI_ADDR_WIDTH-1 downto 0);
    AWLEN   : std_logic_vector(C_AXI_LEN_WIDTH-1 downto 0);
    AWSIZE  : std_logic_vector(C_AXI_SIZE_WIDTH-1 downto 0);
    AWBURST : std_logic_vector(C_AXI_BURST_WIDTH-1 downto 0);
    AWLOCK  : std_logic_vector(C_AXI_LOCK_WIDTH-1 downto 0);
    AWCACHE : std_logic_vector(C_AXI_CACHE_WIDTH-1 downto 0);
    AWPROT  : std_logic_vector(C_AXI_PROT_WIDTH-1 downto 0);
    AWQOS   : std_logic_vector(C_AXI_QOS_WIDTH-1 downto 0);
    AWVALID : std_logic;
    WDATA   : std_logic_vector(C_AXI_DATA_WIDTH-1 downto 0);
    WSTRB   : std_logic_vector(C_AXI_STRB_WIDTH-1 downto 0);
    WLAST   : std_logic;
    WVALID  : std_logic;
    BREADY  : std_logic;
    ARID    : std_logic_vector(C_AXI_ID_WIDTH-1 downto 0);
    ARADDR  : std_logic_vector(C_AXI_ADDR_WIDTH-1 downto 0);
    ARLEN   : std_logic_vector(C_AXI_LEN_WIDTH-1 downto 0);
    ARSIZE  : std_logic_vector(C_AXI_SIZE_WIDTH-1 downto 0);
    ARBURST : std_logic_vector(C_AXI_BURST_WIDTH-1 downto 0);
    ARLOCK  : std_logic_vector(C_AXI_LOCK_WIDTH-1 downto 0);
    ARCACHE : std_logic_vector(C_AXI_CACHE_WIDTH-1 downto 0);
    ARPROT  : std_logic_vector(C_AXI_PROT_WIDTH-1 downto 0);
    ARQOS   : std_logic_vector(C_AXI_QOS_WIDTH-1 downto 0);
    ARVALID : std_logic;
    RREADY  : std_logic;
  end record axi4_h2c_bus_t;

  -- C2H ("Card-to-Host") — slave-side responses back to the master.
  type axi4_c2h_bus_t is record
    AWREADY : std_logic;
    WREADY  : std_logic;
    BID     : std_logic_vector(C_AXI_ID_WIDTH-1 downto 0);
    BRESP   : std_logic_vector(C_AXI_RESP_WIDTH-1 downto 0);
    BVALID  : std_logic;
    ARREADY : std_logic;
    RID     : std_logic_vector(C_AXI_ID_WIDTH-1 downto 0);
    RDATA   : std_logic_vector(C_AXI_DATA_WIDTH-1 downto 0);
    RRESP   : std_logic_vector(C_AXI_RESP_WIDTH-1 downto 0);
    RLAST   : std_logic;
    RVALID  : std_logic;
  end record axi4_c2h_bus_t;

  -- RR--P3.6: array types + global init constants removed — WDATA/RDATA
  -- are now unconstrained so the package can no longer carry a default-
  -- width record. Consumers that want a zero-initialised bus record
  -- can declare it locally constrained to their target width with
  -- per-field defaults; no external site referenced the old constants.

  -----------------------------------------------------------------------------
  -- AXI4 sub-record types — write-only and read-only views of axi4_h2c_bus_t
  -- and axi4_c2h_bus_t. A write-only master (axi4w / uploader) and a
  -- read-only master (axi4r / downloader) each take a pair of sub-record
  -- ports that NEVER overlap on the same field of the parent record. The
  -- TOP composes the two sub-records into the full bus by direct field
  -- assignment.
  -----------------------------------------------------------------------------

  -- Write-master → slave (host→card, write path).
  type axi4_write_h2c_t is record
    AWID    : std_logic_vector(C_AXI_ID_WIDTH-1 downto 0);
    AWADDR  : std_logic_vector(C_AXI_ADDR_WIDTH-1 downto 0);
    AWLEN   : std_logic_vector(C_AXI_LEN_WIDTH-1 downto 0);
    AWSIZE  : std_logic_vector(C_AXI_SIZE_WIDTH-1 downto 0);
    AWBURST : std_logic_vector(C_AXI_BURST_WIDTH-1 downto 0);
    AWLOCK  : std_logic_vector(C_AXI_LOCK_WIDTH-1 downto 0);
    AWCACHE : std_logic_vector(C_AXI_CACHE_WIDTH-1 downto 0);
    AWPROT  : std_logic_vector(C_AXI_PROT_WIDTH-1 downto 0);
    AWQOS   : std_logic_vector(C_AXI_QOS_WIDTH-1 downto 0);
    AWVALID : std_logic;
    WDATA   : std_logic_vector(C_AXI_DATA_WIDTH-1 downto 0);
    WSTRB   : std_logic_vector(C_AXI_STRB_WIDTH-1 downto 0);
    WLAST   : std_logic;
    WVALID  : std_logic;
    BREADY  : std_logic;
  end record axi4_write_h2c_t;

  -- Slave → write-master (card→host, write path).
  type axi4_write_c2h_t is record
    AWREADY : std_logic;
    WREADY  : std_logic;
    BID     : std_logic_vector(C_AXI_ID_WIDTH-1 downto 0);
    BRESP   : std_logic_vector(C_AXI_RESP_WIDTH-1 downto 0);
    BVALID  : std_logic;
  end record axi4_write_c2h_t;

  -- Read-master → slave (host→card, read path).
  type axi4_read_h2c_t is record
    ARID    : std_logic_vector(C_AXI_ID_WIDTH-1 downto 0);
    ARADDR  : std_logic_vector(C_AXI_ADDR_WIDTH-1 downto 0);
    ARLEN   : std_logic_vector(C_AXI_LEN_WIDTH-1 downto 0);
    ARSIZE  : std_logic_vector(C_AXI_SIZE_WIDTH-1 downto 0);
    ARBURST : std_logic_vector(C_AXI_BURST_WIDTH-1 downto 0);
    ARLOCK  : std_logic_vector(C_AXI_LOCK_WIDTH-1 downto 0);
    ARCACHE : std_logic_vector(C_AXI_CACHE_WIDTH-1 downto 0);
    ARPROT  : std_logic_vector(C_AXI_PROT_WIDTH-1 downto 0);
    ARQOS   : std_logic_vector(C_AXI_QOS_WIDTH-1 downto 0);
    ARVALID : std_logic;
    RREADY  : std_logic;
  end record axi4_read_h2c_t;

  -- Slave → read-master (card→host, read path).
  type axi4_read_c2h_t is record
    ARREADY : std_logic;
    RID     : std_logic_vector(C_AXI_ID_WIDTH-1 downto 0);
    RDATA   : std_logic_vector(C_AXI_DATA_WIDTH-1 downto 0);
    RRESP   : std_logic_vector(C_AXI_RESP_WIDTH-1 downto 0);
    RLAST   : std_logic;
    RVALID  : std_logic;
  end record axi4_read_c2h_t;

  -- Array aggregates indexed by literal positions (NVC --std=2008 requires
  -- locally-static choices — named constants like C_FIFO_*_INDEX aren't
  -- treated as locally-static even though they have defined integer values).
  -- Index map: 0 = C_FIFO_STREAM_INDEX, 1 = C_FIFO_BURST_INDEX
  constant C_MIN_BURST_WR_WIDTH     : ddr_min_burst_wr_width_array_t     := (0 => 9, 1 => 4);
  constant C_BURST_WR_TIMEOUT_RATIO : ddr_burst_wr_timeout_ratio_array_t := (0 => 2, 1 => 4);
  constant C_MIN_BURST_RD_WIDTH     : ddr_min_burst_wr_width_array_t     := (0 => 6, 1 => 6);
  constant C_BURST_RD_TIMEOUT_RATIO : ddr_burst_wr_timeout_ratio_array_t := (0 => 4, 1 => 4);

  -----------------------------------------------------------------------------
  -- User-facing streaming + status types. These belong with the DMA core
  -- because the DMA core's input/output surface uses them. rr-vfifo
  -- inherits them transitively via its rr-ddr-stream dependency.
  -----------------------------------------------------------------------------
  type fifo_status_t is record
    full         : std_logic;
    empty        : std_logic;
    almost_empty : std_logic;
    almost_full  : std_logic;
    prog_empty   : std_logic;
    prog_full    : std_logic;
    chip_empty   : std_logic;
    chip_full    : std_logic;
    overflow     : std_logic;
    underflow    : std_logic;
    -- DDR-P3.25: sticky DDR bus-error flags promoted from the AXI write/read
    -- response (BRESP/RRESP /= OKAY). Latched in clk_ddr, sync'd to clk_sys,
    -- held until reset. Always '0' on the AVMM variant (no error response).
    write_error  : std_logic;
    read_error   : std_logic;
  end record fifo_status_t;

  constant C_INIT_FIFO_STATUS : fifo_status_t := (
    full         => '0',
    empty        => '1',
    almost_empty => '0',
    almost_full  => '0',
    prog_empty   => '0',
    prog_full    => '0',
    chip_empty   => '1',
    chip_full    => '1',
    overflow     => '0',
    underflow    => '0',
    write_error  => '0',
    read_error   => '0'
  );

  type fifo_csr_t is record
    fifoStatus     : fifo_status_t;
    fifoFreeCnt    : std_logic_vector(C_FP_BUS_WIDTH-1 downto 0);
    fifoCnt        : std_logic_vector(C_FP_BUS_WIDTH-1 downto 0);
    fifoFullCnt    : std_logic_vector(C_FP_BUS_WIDTH-1 downto 0);
    fifoEmptyCnt   : std_logic_vector(C_FP_BUS_WIDTH-1 downto 0);
    blockStrobeCnt : std_logic_vector(C_FP_BUS_WIDTH-1 downto 0);
  end record fifo_csr_t;

  constant C_INIT_FIFO_CSR : fifo_csr_t := (
    fifoStatus     => C_INIT_FIFO_STATUS,
    fifoFreeCnt    => (others => '0'),
    fifoCnt        => (others => '0'),
    fifoFullCnt    => (others => '0'),
    fifoEmptyCnt   => (others => '0'),
    blockStrobeCnt => (others => '0')
  );

  type fifo_csr_array_t is array (natural range 0 to C_NUM_EP_FIFOS-1) of fifo_csr_t;

  -- data_valid_record_t / data_valid_record_array_t retired 2026-05-15
  -- (DDR-P3.4 + VFIFO-P3.7). User-data ports are now flat
  -- (data : std_logic_vector(C_EP_FIFO_WIDTH-1 downto 0); valid : std_logic).
  -- The CSR record types stay — they carry too many sub-fields to flatten
  -- profitably.

  -----------------------------------------------------------------------------
  -- Avalon-MM plumbing — bus width, BURSTCOUNT width, and the H2C / C2H
  -- record pair for the Intel-target backend (DDR-P3.1). The AVMM master
  -- here drives the same C_AXI_DATA_WIDTH / C_AXI_ADDR_WIDTH widths as the
  -- AXI4 backend so user-data and pointer math stay symmetric across the
  -- two TOPs. BURSTCOUNT is 1-based on Avalon (vs AXI's 0-based AWLEN);
  -- 9 bits gives a 256-beat ceiling matching AXI's max burst.
  -----------------------------------------------------------------------------
  constant C_AVMM_DATA_WIDTH       : integer := C_AXI_DATA_WIDTH;
  constant C_AVMM_ADDR_WIDTH       : integer := C_AXI_ADDR_WIDTH;
  constant C_AVMM_BYTEEN_WIDTH     : integer := C_AVMM_DATA_WIDTH/8;
  constant C_AVMM_BURSTCOUNT_WIDTH : integer := 9;

  -- H2C ("Host-to-Card") — master-side outputs into the memory subsystem.
  -- Avalon-MM has a single combined channel: address, read/write, burstcount,
  -- writedata, byteenable share the bus; the slave returns readdata,
  -- readdatavalid, and a per-cycle waitrequest.
  type avmm_h2c_bus_t is record
    ADDRESS    : std_logic_vector(C_AVMM_ADDR_WIDTH-1 downto 0);
    READ       : std_logic;
    WRITE      : std_logic;
    WRITEDATA  : std_logic_vector(C_AVMM_DATA_WIDTH-1 downto 0);
    BYTEENABLE : std_logic_vector(C_AVMM_BYTEEN_WIDTH-1 downto 0);
    BURSTCOUNT : std_logic_vector(C_AVMM_BURSTCOUNT_WIDTH-1 downto 0);
  end record avmm_h2c_bus_t;

  -- C2H ("Card-to-Host") — slave-side responses back to the master.
  type avmm_c2h_bus_t is record
    READDATA      : std_logic_vector(C_AVMM_DATA_WIDTH-1 downto 0);
    READDATAVALID : std_logic;
    WAITREQUEST   : std_logic;
  end record avmm_c2h_bus_t;

  -----------------------------------------------------------------------------
  -- AVMM sub-record types — write-only and read-only views of the shared
  -- avmm bus. avmm_w / uploader_avmm take avmm_write_*; avmm_r / downloader_avmm
  -- take avmm_read_*. The TOP composes the two sub-masters onto the shared
  -- address/burstcount lines via OR-mux (only one master is asserting
  -- write/read at any given cycle, gated by the per-master burst_setter).
  -----------------------------------------------------------------------------

  -- Write-master → slave.
  type avmm_write_h2c_t is record
    ADDRESS    : std_logic_vector(C_AVMM_ADDR_WIDTH-1 downto 0);
    WRITE      : std_logic;
    WRITEDATA  : std_logic_vector(C_AVMM_DATA_WIDTH-1 downto 0);
    BYTEENABLE : std_logic_vector(C_AVMM_BYTEEN_WIDTH-1 downto 0);
    BURSTCOUNT : std_logic_vector(C_AVMM_BURSTCOUNT_WIDTH-1 downto 0);
  end record avmm_write_h2c_t;

  -- Slave → write-master. Only waitrequest matters; the slave acks the
  -- write commit by lowering waitrequest on the cycle it accepts the beat.
  type avmm_write_c2h_t is record
    WAITREQUEST : std_logic;
  end record avmm_write_c2h_t;

  -- Read-master → slave.
  type avmm_read_h2c_t is record
    ADDRESS    : std_logic_vector(C_AVMM_ADDR_WIDTH-1 downto 0);
    READ       : std_logic;
    BURSTCOUNT : std_logic_vector(C_AVMM_BURSTCOUNT_WIDTH-1 downto 0);
  end record avmm_read_h2c_t;

  -- Slave → read-master.
  type avmm_read_c2h_t is record
    READDATA      : std_logic_vector(C_AVMM_DATA_WIDTH-1 downto 0);
    READDATAVALID : std_logic;
    WAITREQUEST   : std_logic;
  end record avmm_read_c2h_t;

end package ddr_stream_pkg;
