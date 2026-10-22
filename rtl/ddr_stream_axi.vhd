-------------------------------------------------------------------------------
-- Title      : ddr_stream_axi — AXI4 TOP for the rr-ddr-stream DMA core
-- Project    : rr-ddr-stream
-------------------------------------------------------------------------------
-- File       : ddr_stream_axi.vhd
-- Author     : Daniel J. Mazure <dasjimaz@gmail.com>
-- Standard   : VHDL'08
-------------------------------------------------------------------------------
-- Description:
--   AXI4 master TOP for the DMA-shaped ring-buffered-DDR primitive carved out
--   of rr-vfifo on 2026-05-15 (decision rr-vfifo D.1). Composes:
--     - ddr_fifo_intf : user-side CDC FIFO between streaming domain (clk_sys)
--                       and DDR domain (clk_ddr)
--     - uploader      : writes from the upstream FIFO into DDR via AXI4
--     - downloader    : reads from DDR via AXI4 into the downstream FIFO
--     - ptr_mgr       : DDR-space pointer ring (FULL/EMPTY/DIFF math)
--     - ddr_sync_cdc      : empty/full/busy CDC primitives across the two clocks
--
--   Derived from rr-vfifo's virtual_fifo.vhd. Differences:
--     - Cache-anticipation signals (asymetric_empty_r, sync_event_r,
--       ddr_word_cnt_sync_r) are gone — those were preparing for v0.4's
--       mux_ep_ddr cache arbiter, which now lives in rr-vfifo on top of
--       this TOP.
--
--   DDR-P3.10 (2026-05-18) — producer/consumer boundary speaks AXI-Stream
--   valid/ready instead of the legacy wr_en/busy/rd_en/dout_valid surface.
--   The boundary uses skid_buffer (2-deep, registered) on both sides:
--     - s_axis_*  : ingress (user → DDR), back-pressured on internal FIFO full
--     - m_axis_*  : egress  (DDR → user), data + valid stay until handshake
--   No combinational path from m_axis_ready_i to s_axis_ready_o (skid breaks
--   it). Internal pop/push handshakes are gated to be unreachable-on-overflow
--   by construction.
-------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use IEEE.MATH_REAL.all;
library rr;
use rr.ddr_stream_pkg.all;
use rr.functions_pkg.all;

entity ddr_stream_axi is
  generic (
    axi_id_g         : integer                               := 0;
    fifo_depth_g     : integer                               := 9;  -- 512-word depth
    -- RR--P3.6 (2026-05-16): user-side / DDR-side widths independent.
    -- ratio = ddr_data_width_g / user_data_width_g must be a power
    -- of two ≥ 1 (1, 2, 4, 8, 16). Default 32-in / 128-out preserves
    -- the historical shape. 32/512 is the immediate Intel-EMIF case.
    user_data_width_g : integer                              := 32;
    ddr_data_width_g  : integer                              := 128;
    ddr_addr_width_g  : integer                              := 32;  -- 4G
    -- DDR-P3.39 / DS-T3.32: fixed-width vector retains Quartus 23.1 Std
    -- compatibility without restricting the base to INTEGER'high.  In
    -- particular, addresses in the upper half of the 32-bit DDR space are
    -- legal on tools whose INTEGER is only 32 bits wide.
    ddr_base_addr_g  : std_logic_vector(31 downto 0)          := (others => '0');
    buffer_width_g   : integer                               := 31; -- DDR window, 2G
    fifo_impl_g      : string                                := "olo";
    -- DDR-P1.1 (2026-05-18): plane-elision generics. Set false to
    -- skip elaboration of one plane; the disabled side's outputs are
    -- driven to safe defaults (s_axis_ready_o='0' or m_axis_valid_o='0'
    -- plus *_idle_o='1' plus zeroed AXI4 record fields). At least one
    -- plane must be enabled (elaboration assert in architecture body).
    -- First consumer: Deepskopion DS-P1.9 instantiates with
    -- enable_read_plane_g=false — PS drains DDR via its own HP master.
    enable_write_plane_g : boolean                           := true;
    enable_read_plane_g  : boolean                           := true;
    -- VFIFO-T2.6 / VFIFO-T2.7 / IDP-T2.5: max AXI burst length in BEATS,
    -- applied to BOTH the write (AW) and read (AR) planes. AXI4 = 256; an
    -- AXI3 slave (Zynq-7 S_AXI_HP, A*LEN 4-bit) MUST be 16, else >16-beat
    -- bursts are illegal and the AXI4->AXI3 SmartConnect mishandles them
    -- (writes: dropped excess beats — VFIFO-T2.6; reads: the read plane wedges
    -- with the readback frozen just past the cache — the silicon RD=61 stall,
    -- VFIFO-T2.7). Threaded into uploader -> burstw_setter AND downloader ->
    -- burstr_setter. Default 256 = AXI4 no-op.
    max_burst_len_g      : integer                           := 256
    );
  port (
    clk_sys_i        : in  std_logic;
    clk_ddr_i        : in  std_logic;
    rst_sys_i        : in  std_logic;
    rst_ddr_i        : in  std_logic;
    -- DDR-P3.10 (2026-05-18): AXI-Stream handshake on both boundary
    -- sides. 2-deep skid buffers decouple back-pressure and the FWFT
    -- FIFO 1-cycle read latency from the user-facing handshake.
    s_axis_data_i    : in  std_logic_vector(user_data_width_g-1 downto 0);
    s_axis_valid_i   : in  std_logic;
    s_axis_ready_o   : out std_logic;
    m_axis_data_o    : out std_logic_vector(user_data_width_g-1 downto 0);
    m_axis_valid_o   : out std_logic;
    m_axis_ready_i   : in  std_logic;
    -- DDR-P3.9 (2026-05-18): runtime base address override for the
    -- write- (uploader) and read- (downloader) side address rings.
    -- All four ports have safe defaults so existing consumers that
    -- don't need runtime control can leave them unwired — base
    -- address stays at the ddr_base_addr_g generic for the lifetime
    -- of the bitstream. Host must honor write_idle_o / read_idle_o
    -- before pulsing the load (loads while not idle are ignored).
    csr_wr_base_addr_i : in  std_logic_vector(ddr_addr_width_g-1 downto 0) := (others => '0');
    csr_wr_base_load_i : in  std_logic := '0';
    csr_rd_base_addr_i : in  std_logic_vector(ddr_addr_width_g-1 downto 0) := (others => '0');
    csr_rd_base_load_i : in  std_logic := '0';
    -- DDR-P3.11 (2026-08-23): pointer-reset pulse — independent of base_load,
    -- so a host can (a) pulse load+ptr_reset together for a fresh ping-pong
    -- buffer at offset 0, (b) pulse ptr_reset alone to rewind the CURRENT
    -- buffer without moving its base, or (c) pulse load alone to keep today's
    -- DDR-P3.9 "shift the ring by a constant" diagnostic behaviour. Same idle
    -- contract as csr_wr_base_load_i / csr_rd_base_load_i: the host must honor
    -- write_idle_o / read_idle_o before pulsing (a reset while not idle is
    -- ignored). ONE-CYCLE clk_sys pulse.
    csr_wr_ptr_reset_i : in  std_logic := '0';
    csr_rd_ptr_reset_i : in  std_logic := '0';
    -- DDR-P2.4 / DS-P2.59 (2026-06-01): software-managed ring read pointer.
    -- When the read plane is elided (enable_read_plane_g=false) there is no HW
    -- downloader to advance the ring's read side, so the ring fills once and
    -- wedges FULL. This port lets a PS consumer that drains DDR via its own HP
    -- master re-instate the SPSC drain: write the byte offset (mod ring size =
    -- 2**buffer_width_g) it has CONSUMED-up-to. HW feeds it into ptr_mgr's read
    -- input so freed space lets the uploader auto-wrap — continuous, lossless,
    -- no double-read. Same units as wr_commit_addr_o (the commit pointer).
    -- Default load='0' => identical to today's tied-low behaviour until the PS
    -- pulses it (safe, opt-in). Ignored when enable_read_plane_g=true (the HW
    -- downloader owns the read pointer in that configuration).
    -- csr_rd_ptr_load_i is a ONE-CYCLE clk_sys pulse per update (e.g. a regbank
    -- write-strobe): it drives the multi-bit MCP CDC's enable. Hold
    -- csr_rd_ptr_addr_i stable from the pulse through the crossing — a stable
    -- host register (held between writes) satisfies this. Do NOT tie load high.
    csr_rd_ptr_addr_i  : in  std_logic_vector(ddr_addr_width_g-1 downto 0) := (others => '0');
    csr_rd_ptr_load_i  : in  std_logic := '0';
    -- DDR-P3.16 (2026-06-02): software-managed ring WRITE pointer — the exact
    -- mirror of csr_rd_ptr above. When the WRITE plane is elided
    -- (enable_write_plane_g=false) there is no HW uploader to advance the ring's
    -- write side, so the ring reports EMPTY forever and the HW downloader has
    -- nothing to drain. This port lets a PS PRODUCER that fills DDR via its own
    -- HP master re-instate the SPSC source: write the byte offset (mod ring size
    -- = 2**buffer_width_g) it has PRODUCED-up-to. HW feeds it into the read
    -- side's ptr_mgr write input so the downloader sees available data and drains
    -- it. Same units as wr_commit_addr_o. Default load='0' => identical to the
    -- legacy tied-to-uploader_addr behaviour until the PS pulses it (safe,
    -- opt-in). Ignored when enable_write_plane_g=true (the HW uploader owns the
    -- write pointer). csr_wr_ptr_load_i is a ONE-CYCLE clk_sys pulse per update
    -- (e.g. a regbank write-strobe) driving the multi-bit MCP CDC enable; hold
    -- csr_wr_ptr_addr_i stable across the crossing (a held host register does).
    csr_wr_ptr_addr_i  : in  std_logic_vector(ddr_addr_width_g-1 downto 0) := (others => '0');
    csr_wr_ptr_load_i  : in  std_logic := '0';
    write_idle_o       : out std_logic;
    read_idle_o        : out std_logic;
    ddr_csr_o        : out fifo_csr_t := C_INIT_FIFO_CSR;
    -- DDR-P3.14: always-on IP identity stamp (= C_DDR_STREAM_VERSION,
    -- major.minor.patch packed). Independent of read/write-plane generics, so
    -- a write-only-plane integrator (enable_read_plane_g=false) can wire it
    -- into its own regbank to confirm at runtime which IP version was built.
    -- Leave `open` if unused. Consumers who prefer no port can pull the
    -- C_DDR_STREAM_VERSION constant directly at elaboration.
    version_o        : out std_logic_vector(31 downto 0);
    -- DS-T2.20 (deepskopion field-feedback): the live DMA write (commit)
    -- address — where the uploader has actually written to DDR. A PS consumer
    -- must walk the ring only up to THIS, not its own enqueue counter, to avoid
    -- reading un-committed regions. Always-on; leave `open` if unused.
    wr_commit_addr_o : out std_logic_vector(ddr_addr_width_g-1 downto 0) := (others => '0');
    -- KV2-T2.2 observability: live reset / chip-select state of the DMA planes,
    -- for diagnosing a DDR-T2.15-class downloader wedge (CS held low because the
    -- endpoint-FIFO reset-busy never clears). Raw same-domain taps — correct as-is
    -- on single-clock targets (clk_sys_i = clk_ddr_i); on a 2-clock target sample
    -- in the reader's domain. Leave `open` if unused.
    --   bit0 downloader_cs  bit1 uploader_cs  bit2 pipeBusyDdr  bit3 pipeBusy
    dbg_reset_status_o : out std_logic_vector(3 downto 0) := (others => '0');
    m_axi4_h2c_bus_o : out axi4_h2c_bus_t;
    m_axi4_c2h_bus_i : in  axi4_c2h_bus_t
    );
end entity ddr_stream_axi;

architecture RTL of ddr_stream_axi is

  -----------------------------------------------------------------------------
  -- Constants
  -----------------------------------------------------------------------------
  constant C_FWFT               : integer := 1;
  constant C_CDC_NUM_STAGES     : integer := 2;
  constant C_MSGS_PER_WORD      : integer := ddr_data_width_g / user_data_width_g;
  constant C_MSGS_PER_WORD_LOG2 : integer := integer(ceil(log2(real(C_MSGS_PER_WORD))));
  constant C_MSGS_PER_WORD_UNS  : unsigned(C_MSGS_PER_WORD_LOG2+1-1 downto 0) :=
                                    to_unsigned(C_MSGS_PER_WORD, C_MSGS_PER_WORD_LOG2+1);
  constant C_RESET_INIT_TIMEOUT : integer := 64;
  constant C_EXTEND_EMPTY_PIPE  : integer := 6;

  -- Width-padded multiply helper (lifted from virtual_fifo.vhd).
  function multPower2(a : unsigned; b : unsigned) return unsigned is
    variable tmp    : unsigned(a'length + b'length - 1 downto 0) := (others => '0');
    variable result : unsigned(a'length- 1 downto 0)             := (others => '0');
  begin
    tmp    := a * b;
    result := tmp(a'length-1 downto 0);
    return result;
  end function;

  -----------------------------------------------------------------------------
  -- Signals
  -----------------------------------------------------------------------------
  signal uploader_addr : std_logic_vector(ddr_addr_width_g - 1 downto 0);
  signal uploader_cs   : std_logic;
  signal uploader_cs_r : std_logic;

  signal downloader_cs   : std_logic;
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
  -- DDR-P3.25: clk_ddr registers + clk_sys-synced levels of the sticky
  -- write/read bus-error flags (busWrError/busRdError already latch-and-hold
  -- in axi4w/axi4r until reset), promoted into ddr_csr_o.fifoStatus.
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
  -- DDR-P3.10 / VFIFO-T9 (2026-05-18): only producer-side skid stays
  -- (back-pressure decoupling); consumer side is combinational so
  -- the FIFO empty signal cleanly accounts for all buffered state.
  signal skidInValid      : std_logic;
  signal skidInReady      : std_logic;
  -- DDR-P3.9: runtime base-address registers. clk_sys holds the CSR-
  -- writable copy; clk_ddr holds a 2-stage-flopped copy presented to
  -- uploader / downloader. Load is gated by NOT *_busy on clk_sys.
  constant C_DDR_BASE_RST  : std_logic_vector(ddr_addr_width_g-1 downto 0)
                              := std_logic_vector(resize(unsigned(ddr_base_addr_g), ddr_addr_width_g));
  signal wr_base_addr_sys  : std_logic_vector(ddr_addr_width_g-1 downto 0) := C_DDR_BASE_RST;
  signal rd_base_addr_sys  : std_logic_vector(ddr_addr_width_g-1 downto 0) := C_DDR_BASE_RST;
  signal wr_base_addr_meta : std_logic_vector(ddr_addr_width_g-1 downto 0) := C_DDR_BASE_RST;
  signal wr_base_addr_ddr  : std_logic_vector(ddr_addr_width_g-1 downto 0) := C_DDR_BASE_RST;
  signal rd_base_addr_meta : std_logic_vector(ddr_addr_width_g-1 downto 0) := C_DDR_BASE_RST;
  signal rd_base_addr_ddr  : std_logic_vector(ddr_addr_width_g-1 downto 0) := C_DDR_BASE_RST;
  -- DDR-T2.63 / DS-T2.63: KEEP stops synthesis propagating the constant
  -- ddr_base_addr_g generic through the 2-FF synchronizer and removing
  -- rd_base_addr_ddr (Synth 8-6014) when a consumer ties csr_rd_base_load_i off
  -- (static-base, e.g. deepskopion). The synchronizer is REAL when a consumer
  -- drives csr_rd_base_load_i at runtime (the DDR validators), so it is kept in
  -- every config rather than guarded out per-consumer.
  --
  -- DDR-T1.3 (2026-09-04): rd_base_addr_ddr alone is NOT enough. The scoped
  -- CDC constraint (constraints/ddr_stream_cdc_scoped.sdc section C) targets the
  -- ACTUAL clk_sys->clk_ddr crossing endpoints — *_base_addr_sys (launch) and
  -- *_base_addr_meta (first clk_ddr capture) — NOT *_base_addr_ddr (a
  -- same-domain second stage). With only rd_base_addr_ddr kept, a static-base
  -- consumer (deepskopion: csr_*_base_load_i tied off, base = ddr_base_addr_g
  -- constant) lets synthesis fold wr/rd_base_addr_{sys,meta} to that constant
  -- and remove them, so section C's get_registers {*_base_addr_sys/*meta}
  -- selectors match ZERO cells -> TMC-20026 empty collection, the exact
  -- silent-0-match the owner's constraints audit forbids. KEEP the crossing
  -- endpoints too so the 2-FF synchroniser structure survives on the static-base
  -- build and the section-C selector always binds >=1. Same fix pattern, same
  -- reasoning as rd_base_addr_ddr. Guarded by
  -- tests/test_reset_request_crossing_bounded.py::test_section_c_crossing_regs_are_keep_protected.
  attribute keep : string;
  attribute keep of rd_base_addr_ddr  : signal is "true";
  attribute keep of wr_base_addr_sys  : signal is "true";
  attribute keep of rd_base_addr_sys  : signal is "true";
  attribute keep of wr_base_addr_meta : signal is "true";
  attribute keep of rd_base_addr_meta : signal is "true";
  -- DDR-T1.3 (2026-09-04): the `keep : string is "true"` form above is the
  -- Vivado/Xilinx spelling (Synth 8-6014). Quartus documents `keep` as a
  -- combinational-net attribute, NOT register retention — its register-retention
  -- attribute is `preserve` (prevents removal/merging of a register). The
  -- deepskopion consumer builds through Quartus and reads the .sdc twin, so the
  -- Vivado `keep` alone is plausibly INERT on the exact tool whose TMC-20026
  -- (section-C empty collection) it means to resolve. Add the Quartus-correct
  -- `preserve` on the SAME section-C crossing endpoints (and rd_base_addr_ddr)
  -- so the 2-FF synchroniser survives constant-folding on BOTH toolchains. This
  -- is desk-attempted; the netlist match-count on the real Quartus build is the
  -- close-out (audit §5.2). Guarded by
  -- tests/test_reset_request_crossing_bounded.py.
  attribute preserve : boolean;
  attribute preserve of rd_base_addr_ddr  : signal is true;
  attribute preserve of wr_base_addr_sys  : signal is true;
  attribute preserve of rd_base_addr_sys  : signal is true;
  attribute preserve of wr_base_addr_meta : signal is true;
  attribute preserve of rd_base_addr_meta : signal is true;
  -- DDR-P3.11: pointer-reset pulse, clk_sys -> clk_ddr. Toggle-and-edge-detect
  -- MCP CDC (same shape as the rd_ptr/wr_ptr crossings below) — a toggle
  -- carries no data payload to hold stable, so this is simpler than the
  -- base_addr 2-FF crossing and still yields exactly one clk_ddr pulse per
  -- host pulse regardless of the clk_sys:clk_ddr frequency ratio.
  signal wr_ptr_reset_tgl_sys   : std_logic := '0';
  signal rd_ptr_reset_tgl_sys   : std_logic := '0';
  signal wr_ptr_reset_tgl_ddr   : std_logic;
  signal rd_ptr_reset_tgl_ddr   : std_logic;
  signal wr_ptr_reset_tgl_ddr_d : std_logic := '0';
  signal rd_ptr_reset_tgl_ddr_d : std_logic := '0';
  signal wr_ptr_reset_ddr       : std_logic := '0';  -- single-cycle clk_ddr pulse
  signal rd_ptr_reset_ddr       : std_logic := '0';
  -- DDR-P2.4 / DS-P2.59: software ring read pointer, clk_sys -> clk_ddr.
  -- NOTE the read pointer changes DURING traffic (unlike the base address,
  -- which is static once configured), so a free-running multi-bit 2-FF would
  -- latch an INCOHERENT old/new mix for 1-2 cycles per update and could
  -- transiently report extra free space -> overwrite un-drained data. We use a
  -- proper multi-bit MCP CDC built on ddr_sync_cdc (toggle the load, synchronize the
  -- single-bit toggle, edge-detect it, capture the bus only on that edge) so the
  -- crossing is coherent. ddr_sync_cdc carries ASYNC_REG="TRUE" as a STRING.
  -- Reset 0 (ring offset 0) so an unwired / never-pulsed consumer is
  -- byte-identical to the legacy tied-low read pointer.
  constant C_RD_PTR_RST    : std_logic_vector(ddr_addr_width_g-1 downto 0) := (others => '0');
  signal rd_ptr_addr_sys   : std_logic_vector(ddr_addr_width_g-1 downto 0) := C_RD_PTR_RST;  -- clk_sys latch of csr_rd_ptr_addr_i (IP-internal MCP source)
  signal rd_ptr_tgl_sys    : std_logic := '0';                                        -- toggles in clk_sys, one edge per load pulse
  signal rd_ptr_tgl_ddr    : std_logic;                                               -- toggle synchronized into clk_ddr (via ddr_sync_cdc)
  signal rd_ptr_tgl_ddr_d  : std_logic := '0';                                        -- delayed copy for edge detect
  signal rd_ptr_addr_ddr   : std_logic_vector(ddr_addr_width_g-1 downto 0) := C_RD_PTR_RST;  -- reset-able read pointer fed to the write side
  -- Read pointer actually consumed by the write side: the HW downloader address
  -- when the read plane is present, else the software-managed CSR pointer. Fed
  -- to BOTH the uploader (whose burstw_setter has its own ptr_mgr that gates the
  -- write FSM on full) AND the top-level status ptr_mgr, so the two agree.
  signal rd_ptr_sel        : std_logic_vector(ddr_addr_width_g-1 downto 0);
  -- DDR-P3.16: software ring WRITE pointer, clk_sys -> clk_ddr. Same coherent
  -- multi-bit MCP CDC as the read pointer (the write pointer also changes DURING
  -- traffic, so a free-running 2-FF could latch an incoherent old/new mix and
  -- transiently report extra available data -> the downloader reads un-produced
  -- regions). Reset 0 so an unwired / never-pulsed producer is byte-identical to
  -- the legacy tied-to-uploader_addr write pointer (uploader_addr is also 0 when
  -- the write plane is elided).
  constant C_WR_PTR_RST    : std_logic_vector(ddr_addr_width_g-1 downto 0) := (others => '0');
  signal wr_ptr_addr_sys   : std_logic_vector(ddr_addr_width_g-1 downto 0) := C_WR_PTR_RST;  -- clk_sys latch of csr_wr_ptr_addr_i (IP-internal MCP source)
  signal wr_ptr_tgl_sys    : std_logic := '0';                                        -- toggles in clk_sys, one edge per load pulse
  signal wr_ptr_tgl_ddr    : std_logic;                                               -- toggle synchronized into clk_ddr (via ddr_sync_cdc)
  signal wr_ptr_tgl_ddr_d  : std_logic := '0';                                        -- delayed copy for edge detect
  signal wr_ptr_addr_ddr   : std_logic_vector(ddr_addr_width_g-1 downto 0) := C_WR_PTR_RST;  -- reset-able write pointer fed to the read side
  -- DDR-T2.18: KEEP the SW write-pointer MCP chain so synthesis cannot prune it
  -- when enable_write_plane_g=false. Found 2026-06-24 on kv260 (KV2-P3.4): with
  -- the write plane elided the ring starts empty (wr_ptr=rd_ptr=0 => DdrEmpty=1),
  -- so Vivado constant-propagates DdrEmpty='1' and walks the circular
  -- DdrEmpty=1 -> burst-idle -> no FIFO write -> VALID=0 -> no observable change
  -- path back to "these pointer FFs have no fanout" and removes wr_ptr_addr_sys /
  -- wr_ptr_addr_ddr / wr_ptr_tgl_ddr_d (Synth 8-6014 in top_kv260_h264.vds naming
  -- exactly these three registers). That reasoning is WRONG: csr_wr_ptr_load_i is
  -- a live input port and a PS producer advances the pointer at runtime, so the
  -- chain is real. KEEP (Vivado, the tool that emitted 8-6014) pins the flops;
  -- `preserve` is added alongside as the Quartus-correct register-retention
  -- spelling, mirroring the DDR-T1.3 base_addr fix a few lines up (Vivado `keep`
  -- is documented for combinational nets, so keep-alone is the known-inert
  -- anti-pattern this file already warns against). Guarded by
  -- tests/test_reset_request_crossing_bounded.py::test_wr_ptr_cdc_regs_are_keep_protected.
  attribute keep     of wr_ptr_addr_sys  : signal is "true";
  attribute keep     of wr_ptr_addr_ddr  : signal is "true";
  attribute keep     of wr_ptr_tgl_ddr_d : signal is "true";
  attribute preserve of wr_ptr_addr_sys  : signal is true;
  attribute preserve of wr_ptr_addr_ddr  : signal is true;
  attribute preserve of wr_ptr_tgl_ddr_d : signal is true;
  -- Write pointer actually consumed by the read side: the HW uploader address
  -- when the write plane is present, else the software-managed CSR pointer. Fed
  -- to BOTH the downloader (whose ptr_mgr gates the read FSM on empty) AND the
  -- top-level status ptr_mgr, so the two agree.
  signal wr_ptr_sel        : std_logic_vector(ddr_addr_width_g-1 downto 0);
  signal fifoDDRPipeEmpty : std_logic;
  signal fifoDdrPipeCnt   : std_logic_vector(fifo_depth_g + C_FWFT + C_MSGS_PER_WORD_LOG2 - 1 downto 0);
  signal pipeBusy         : std_logic;
  signal pipeBusyDdr      : std_logic;   -- DDR-T2.15: pipeBusy synced into clk_ddr
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
  -- and remove fifoStatus.underflow (Synth 8-6014, RTL-P3.933 gate). underflow is
  -- unreachable by construction (DDR-P3.10 handshake gating), so synth proves it
  -- constant '0' — but it is a named CSR status bit exposed via ddr_csr_o and must
  -- stay observable and consistent with overflow (which survives because its CDC
  -- source is not provably constant). Same fix pattern as the rd_base_addr_ddr
  -- keep above. (attribute keep : string; is already declared there.)
  attribute keep of ddrCSR_tp : signal is "true";

  -----------------------------------------------------------------------------
  -- AXI4 sub-record wiring: uploader and downloader expose split write/read
  -- sub-records so they can never collide on the same field of the full
  -- axi4_h2c_bus_t / axi4_c2h_bus_t. The TOP composes the two sub-records
  -- into the full bus by direct field assignment below.
  -----------------------------------------------------------------------------
  signal m_axi_write_h2c : axi4_write_h2c_t;
  signal m_axi_write_c2h : axi4_write_c2h_t;
  signal m_axi_read_h2c  : axi4_read_h2c_t;
  signal m_axi_read_c2h  : axi4_read_c2h_t;

begin
  -- DDR-P3.14: always-on IP identity stamp — driven unconditionally (outside
  -- every plane generate) so it is valid regardless of enable_*_plane_g.
  version_o <= C_DDR_STREAM_VERSION;
  -- DS-T2.20: live DMA write (commit) address.
  -- DDR-P2.19 (2026-09-19): drive the commit address from wr_ptr_sel, the same
  -- ring write-pointer mux the read side consumes (DDR-P3.16), NOT directly from
  -- uploader_addr. When the write plane is PRESENT, g_wr_ptr_src_hw ties
  -- wr_ptr_sel <= uploader_addr, so this is byte-identical to the legacy
  -- assignment. When the write plane is ELIDED, uploader_addr is tied to 0 in the
  -- else-generate, so the old assignment froze wr_commit_addr_o at 0 forever and
  -- the software-managed commit pointer never surfaced to the observer; wr_ptr_sel
  -- instead carries wr_ptr_addr_ddr (the CDC'd SW write pointer), so the commit
  -- address now advances as the PS producer pulses csr_wr_ptr_load. This keeps the
  -- observed commit address consistent with the pointer the read plane actually
  -- gates on in every plane configuration.
  wr_commit_addr_o <= wr_ptr_sel;

  -- DDR-P1.1 (2026-05-18): both-planes-off has no I/O surface. Flag at
  -- elaboration rather than waste sim time chasing zero-output ghosts.
  assert enable_write_plane_g or enable_read_plane_g
    report "ddr_stream_axi: both enable_write_plane_g and enable_read_plane_g are false - at least one must be true"
    severity failure;

  -- Reset-init guard: hold uploader/downloader CS low until BOTH (a) the fixed
  -- settle window has elapsed AND (b) the endpoint FIFO is no longer in its
  -- post-reset busy window (pipeBusyDdr = ddr_fifo_intf busy_o = olo_base_cc_reset
  -- / xpm rst_busy, synced to clk_ddr). (Driver stays shared even when one plane
  -- is elided — the unused cs_r is a harmless dangling driver in that case.)
  --
  -- DDR-T2.15 (2026-06-23): the old guard released CS on the FIXED 64-cycle
  -- timeout ALONE, never checking the FIFO's actual rst_busy. The IP exposes
  -- wr/rd_rst_busy but NOTHING consumed it. In sim the olo reset-busy clears in
  -- < 64 cycles so the timeout sufficed (green) — but on silicon the busy window
  -- (real olo_base_cc_reset / a longer reset deassertion) can outlast 64 cycles,
  -- so CS went high while the FIFO was still resetting: the burstr_setter fired a
  -- phantom read burst, downloader_addr advanced past the (still 0) write ptr ->
  -- ptr_mgr read rd != wr = NOT empty (chip_empty=0 at baseline) -> the ring
  -- wedged, no further AR completed. Gating CS on pipeBusyDdr prevents it.
  --
  -- DDR-T2.17 (2026-10-09): DDR-T2.15 gated the initial CS release on pipeBusyDdr=0,
  -- but CS and rstTimeout_r were only cleared on rst_ddr_i. If the endpoint FIFO
  -- re-entered reset-busy via rst_sys_i alone (runtime or asymmetric reset), CS
  -- remained asserted throughout the FIFO reset-busy window. Re-lower CS and
  -- re-arm the init guard on ANY reset-busy (pipeBusyDdr='1').
  process(clk_ddr_i)
  begin
    if rising_edge(clk_ddr_i) then
      if rstTimeout_r < C_RESET_INIT_TIMEOUT then
        rstTimeout_r <= rstTimeout_r + 1;
      elsif pipeBusyDdr = '0' then       -- DDR-T2.15: AND FIFO reset-busy cleared
        uploader_cs_r   <= '1';
        downloader_cs_r <= '1';
      end if;
      if pipeBusyDdr = '1' then          -- DDR-T2.17: re-lower CS and re-arm init guard on ANY reset-busy
        uploader_cs_r   <= '0';
        downloader_cs_r <= '0';
        rstTimeout_r    <= 0;
      end if;
      if rst_ddr_i = '1' then
        uploader_cs_r   <= '0';
        downloader_cs_r <= '0';
        rstTimeout_r    <= 0;
      end if;
    end if;
  end process;

  -- DDR-T2.15: sync pipeBusy (busy_o, registered in clk_sys) into clk_ddr for the
  -- reset-init guard. set_i = rst_ddr_i forces busy='1' during reset so CS is held
  -- low from the first cycle (mirrors i_sync_cdc_empty's set_i = rst_sys_i). It is
  -- a slow post-reset level, so a 2-FF sync is sufficient.
  i_sync_cdc_pipe_busy : entity rr.ddr_sync_cdc
    generic map (
      num_stages_g => C_CDC_NUM_STAGES,
      or_reduce_g  => false,
      rst_val_g    => '1'
      )
    port map (
      clk_i => clk_ddr_i,
      rst_i => '0',
      set_i => rst_ddr_i,
      d_i   => pipeBusy,
      q_o   => pipeBusyDdr
      );

  -- DDR-T2.17 (2026-10-09): Combinational gating of Chip Select with not pipeBusyDdr.
  -- In a clocked process, uploader_cs_r / downloader_cs_r take one clk_ddr cycle to drop
  -- after pipeBusyDdr asserts. Gating CS combinationally ensures that burstw_setter and
  -- burstr_setter never observe CS asserted while pipeBusyDdr='1', closing the single-cycle
  -- phantom burst window and strictly satisfying the invariant below on every cycle.
  uploader_cs   <= uploader_cs_r and not pipeBusyDdr;
  downloader_cs <= downloader_cs_r and not pipeBusyDdr;

  -- DDR-T2.15 invariant (sim-checked, synthesis-ignored): a burst-setter Chip
  -- Select must NEVER be asserted while the endpoint FIFO is still reset-busy.
  -- This fires on the OLD behaviour (CS released by timeout alone) the moment a
  -- run has a busy window outlasting C_RESET_INIT_TIMEOUT — the regression guard.
  postponed assert not (pipeBusyDdr = '1' and (uploader_cs = '1' or downloader_cs = '1'))
    report "ddr_stream_axi: Chip Select asserted during FIFO reset-busy (DDR-T2.15)"
    severity error;

  -- KV2-T2.2 observability tap: expose the DDR-T2.15 reset/CS state so an
  -- integrator's CSR can confirm on silicon whether the downloader is wedged
  -- with CS held low because pipeBusyDdr (FIFO reset-busy) never cleared.
  dbg_reset_status_o <= pipeBusy & pipeBusyDdr & uploader_cs & downloader_cs;

  -----------------------------------------------------------------------------
  -- WRITE PLANE  (DDR-P1.1, 2026-05-18 — guarded by enable_write_plane_g)
  --
  --   - s_axis_* ingress skid buffer
  --   - uploader (drives AW/W + reads B)
  --   - AXI write sub-record packing into m_axi4_h2c_bus_o / from
  --     m_axi4_c2h_bus_i
  --   - upBusBusy CDC into clk_sys
  --
  -- Else-generate ties off every output the plane would otherwise drive,
  -- including the intermediate signals consumed by shared infrastructure
  -- (fifoUserWrEn, uploader_addr, upBusBusyCdc, ...). Synthesis prunes
  -- the entire write-plane silicon when enable_write_plane_g=false.
  -----------------------------------------------------------------------------
  g_write_plane : if enable_write_plane_g generate
    -- DDR-P3.10: AXI-Stream skid on the producer side. Absorbs back-
    -- pressure from the FWFT FIFO. fifoUserWrEn fires only when (skid
    -- presents valid) AND (FIFO not full) — the historical overflow
    -- guard is unreachable by construction; assertion kept for
    -- defence-in-depth.
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

    i_uploader : entity rr.uploader
      generic map(
        ddr_data_width_g   => ddr_data_width_g,
        ddr_addr_width_g   => ddr_addr_width_g,
        fifo_depth_g       => fifo_depth_g,
        fwft_g             => C_FWFT,
        buffer_width_g     => buffer_width_g,
        axi_id_writeport_g => axi_id_g,
        max_burst_len_g    => max_burst_len_g
        )
      port map (
        clk_ddr_i      => clk_ddr_i,
        rst_ddr_i      => rst_ddr_i,
        cs_i           => uploader_cs,
        fifo_cnt_i     => fifoUpDataCnt,
        fifo_empty_i   => fifoUpEmpty,
        fifo_rd_en_o   => fifoUpRdEn,
        fifo_data_i    => fifoUpDin,
        ptr_write_i    => uploader_addr,
        -- DDR-P2.4 / DS-P2.59: read pointer the write side throttles against.
        -- HW downloader when present, software CSR pointer when the read plane
        -- is elided — this is the input to burstw_setter's gating ptr_mgr.
        ptr_read_i     => rd_ptr_sel,
        ptr_end_o      => uploader_addr,
        ddr_base_addr_i => wr_base_addr_ddr,
        ptr_reset_i    => wr_ptr_reset_ddr,
        bus_busy_o     => upBusBusy,
        bus_error_o    => busWrError,
        h2c_o          => m_axi_write_h2c,
        c2h_i          => m_axi_write_c2h
        );

    -- Compose AXI write-master sub-records into AW/W/B fields.
    m_axi4_h2c_bus_o.AWID    <= m_axi_write_h2c.AWID;
    m_axi4_h2c_bus_o.AWADDR  <= m_axi_write_h2c.AWADDR;
    m_axi4_h2c_bus_o.AWLEN   <= m_axi_write_h2c.AWLEN;
    m_axi4_h2c_bus_o.AWSIZE  <= m_axi_write_h2c.AWSIZE;
    m_axi4_h2c_bus_o.AWBURST <= m_axi_write_h2c.AWBURST;
    m_axi4_h2c_bus_o.AWLOCK  <= m_axi_write_h2c.AWLOCK;
    m_axi4_h2c_bus_o.AWCACHE <= m_axi_write_h2c.AWCACHE;
    m_axi4_h2c_bus_o.AWPROT  <= m_axi_write_h2c.AWPROT;
    m_axi4_h2c_bus_o.AWQOS   <= m_axi_write_h2c.AWQOS;
    m_axi4_h2c_bus_o.AWVALID <= m_axi_write_h2c.AWVALID;
    m_axi4_h2c_bus_o.WDATA   <= m_axi_write_h2c.WDATA;
    m_axi4_h2c_bus_o.WSTRB   <= m_axi_write_h2c.WSTRB;
    m_axi4_h2c_bus_o.WLAST   <= m_axi_write_h2c.WLAST;
    m_axi4_h2c_bus_o.WVALID  <= m_axi_write_h2c.WVALID;
    m_axi4_h2c_bus_o.BREADY  <= m_axi_write_h2c.BREADY;
    m_axi_write_c2h.AWREADY <= m_axi4_c2h_bus_i.AWREADY;
    m_axi_write_c2h.WREADY  <= m_axi4_c2h_bus_i.WREADY;
    m_axi_write_c2h.BID     <= m_axi4_c2h_bus_i.BID;
    m_axi_write_c2h.BRESP   <= m_axi4_c2h_bus_i.BRESP;
    m_axi_write_c2h.BVALID  <= m_axi4_c2h_bus_i.BVALID;

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

    -- DDR-P3.25: promote the sticky write bus-error (busWrError is already
    -- held in axi4w until reset) into clk_sys for the CSR.
    busWrError_r <= busWrError when rising_edge(clk_ddr_i);

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

  else generate
    -- DDR-P1.1: write plane elided. Refuse s_axis input, tie all AW/W/B
    -- record fields to zero, hold upBusBusy* low so write_idle_o
    -- reports '1' and the shared overflow assertion can never fire.
    s_axis_ready_o <= '0';
    fifoUserDin    <= (others => '0');
    skidInValid    <= '0';
    skidInReady    <= '0';
    fifoUserWrEn   <= '0';
    fifoUpRdEn     <= '0';
    uploader_addr  <= (others => '0');
    upBusBusy      <= '0';
    upBusBusy_r    <= '0';
    upBusBusyCdc   <= '0';
    busWrError     <= '0';
    busWrError_r   <= '0';
    wrErrCdc       <= '0';   -- DDR-P3.25: no write plane ⇒ no write error
    m_axi4_h2c_bus_o.AWID    <= (others => '0');
    m_axi4_h2c_bus_o.AWADDR  <= (others => '0');
    m_axi4_h2c_bus_o.AWLEN   <= (others => '0');
    m_axi4_h2c_bus_o.AWSIZE  <= (others => '0');
    m_axi4_h2c_bus_o.AWBURST <= (others => '0');
    m_axi4_h2c_bus_o.AWLOCK  <= (others => '0');
    m_axi4_h2c_bus_o.AWCACHE <= (others => '0');
    m_axi4_h2c_bus_o.AWPROT  <= (others => '0');
    m_axi4_h2c_bus_o.AWQOS   <= (others => '0');
    m_axi4_h2c_bus_o.AWVALID <= '0';
    m_axi4_h2c_bus_o.WDATA   <= (others => '0');
    m_axi4_h2c_bus_o.WSTRB   <= (others => '0');
    m_axi4_h2c_bus_o.WLAST   <= '0';
    m_axi4_h2c_bus_o.WVALID  <= '0';
    m_axi4_h2c_bus_o.BREADY  <= '0';
  end generate g_write_plane;

  -----------------------------------------------------------------------------
  -- READ PLANE  (DDR-P1.1, 2026-05-18 — guarded by enable_read_plane_g)
  --
  --   - m_axis_* egress combinational pass-through
  --   - downloader (drives AR + reads R)
  --   - AXI read sub-record packing into m_axi4_h2c_bus_o / from
  --     m_axi4_c2h_bus_i
  --   - downBusBusy CDC into clk_sys
  --
  -- Else-generate ties off every output the plane would otherwise drive.
  -----------------------------------------------------------------------------
  g_read_plane : if enable_read_plane_g generate
    -- Consumer side (m_axis_*): combinational pass-through of the FWFT
    -- FIFO head. NO output skid_buffer — VFIFO-T9 (2026-05-18) found
    -- that adding consumer-side skid misaligns ddrPipeIsEmpty: the skid
    -- holds 1-2 words past the empty-assertion and downstream consumers
    -- (rr-vfifo's mux_ep_ddr FSM) drop those orphaned words across
    -- DDR_MODE→HOLD_ON transitions. FWFT presents the head with no read
    -- latency; downstream is free to register or skid as needed.
    m_axis_data_o  <= fifoUserDout;
    -- DDR-T2.11: gate VALID low while in sys reset — a bare `not fifoUserEmpty`
    -- leaves TVALID high through a mid-stream reset (AMBA AXIS §2.7.1 violation).
    m_axis_valid_o <= (not fifoUserEmpty) and not rst_sys_i;
    fifoUserRdEn   <= m_axis_valid_o and m_axis_ready_i;

    i_downloader : entity rr.downloader
      generic map(
        ddr_data_width_g  => ddr_data_width_g,
        ddr_addr_width_g  => ddr_addr_width_g,
        buffer_width_g    => buffer_width_g,
        fifo_depth_g      => fifo_depth_g,
        fwft_g            => C_FWFT,
        axi_id_readport_g => axi_id_g,
        max_burst_len_g   => max_burst_len_g
        )
      port map (
        clk_ddr_i      => clk_ddr_i,
        rst_ddr_i      => rst_ddr_i,
        cs_i           => downloader_cs,
        fifo_room_i    => fifoDownDataRoom,
        fifo_full_i    => fifoDownFull,
        fifo_wr_en_o   => fifoDownWrEn,
        fifo_data_o    => fifoDownDin,
        ptr_read_i     => downloader_addr,
        ptr_write_i    => wr_ptr_sel,
        ptr_end_o      => downloader_addr,
        ddr_base_addr_i => rd_base_addr_ddr,
        ptr_reset_i    => rd_ptr_reset_ddr,
        bus_busy_o     => downBusBusy,
        bus_error_o    => busRdError,
        h2c_o          => m_axi_read_h2c,
        c2h_i          => m_axi_read_c2h
        );

    -- Compose AXI read-master sub-records into AR/R fields.
    m_axi4_h2c_bus_o.ARID    <= m_axi_read_h2c.ARID;
    m_axi4_h2c_bus_o.ARADDR  <= m_axi_read_h2c.ARADDR;
    m_axi4_h2c_bus_o.ARLEN   <= m_axi_read_h2c.ARLEN;
    m_axi4_h2c_bus_o.ARSIZE  <= m_axi_read_h2c.ARSIZE;
    m_axi4_h2c_bus_o.ARBURST <= m_axi_read_h2c.ARBURST;
    m_axi4_h2c_bus_o.ARLOCK  <= m_axi_read_h2c.ARLOCK;
    m_axi4_h2c_bus_o.ARCACHE <= m_axi_read_h2c.ARCACHE;
    m_axi4_h2c_bus_o.ARPROT  <= m_axi_read_h2c.ARPROT;
    m_axi4_h2c_bus_o.ARQOS   <= m_axi_read_h2c.ARQOS;
    m_axi4_h2c_bus_o.ARVALID <= m_axi_read_h2c.ARVALID;
    m_axi4_h2c_bus_o.RREADY  <= m_axi_read_h2c.RREADY;
    m_axi_read_c2h.ARREADY <= m_axi4_c2h_bus_i.ARREADY;
    m_axi_read_c2h.RID     <= m_axi4_c2h_bus_i.RID;
    m_axi_read_c2h.RDATA   <= m_axi4_c2h_bus_i.RDATA;
    m_axi_read_c2h.RRESP   <= m_axi4_c2h_bus_i.RRESP;
    m_axi_read_c2h.RLAST   <= m_axi4_c2h_bus_i.RLAST;
    m_axi_read_c2h.RVALID  <= m_axi4_c2h_bus_i.RVALID;

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

    -- DDR-P3.25: promote the sticky read bus-error (busRdError is already held
    -- in axi4r until reset) into clk_sys for the CSR.
    busRdError_r <= busRdError when rising_edge(clk_ddr_i);

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

  else generate
    -- DDR-P1.1: read plane elided. Tie m_axis_* low, tie all AR/R record
    -- fields to zero, hold downBusBusy* low so read_idle_o reports '1'.
    m_axis_data_o   <= (others => '0');
    m_axis_valid_o  <= '0';
    fifoUserRdEn    <= '0';
    fifoDownDin     <= (others => '0');
    fifoDownWrEn    <= '0';
    downloader_addr <= (others => '0');
    downBusBusy     <= '0';
    downBusBusy_r   <= '0';
    downBusBusyCdc  <= '0';
    busRdError      <= '0';
    busRdError_r    <= '0';
    rdErrCdc        <= '0';   -- DDR-P3.25: no read plane ⇒ no read error
    m_axi4_h2c_bus_o.ARID    <= (others => '0');
    m_axi4_h2c_bus_o.ARADDR  <= (others => '0');
    m_axi4_h2c_bus_o.ARLEN   <= (others => '0');
    m_axi4_h2c_bus_o.ARSIZE  <= (others => '0');
    m_axi4_h2c_bus_o.ARBURST <= (others => '0');
    m_axi4_h2c_bus_o.ARLOCK  <= (others => '0');
    m_axi4_h2c_bus_o.ARCACHE <= (others => '0');
    m_axi4_h2c_bus_o.ARPROT  <= (others => '0');
    m_axi4_h2c_bus_o.ARQOS   <= (others => '0');
    m_axi4_h2c_bus_o.ARVALID <= '0';
    m_axi4_h2c_bus_o.RREADY  <= '0';
  end generate g_read_plane;

  -----------------------------------------------------------------------------
  -- Shared infrastructure: pointer-ring math, FIFO bridge, status CDC.
  -- Runs regardless of plane configuration; the elided plane's consumers
  -- read the safe defaults driven in the else-generate branches above.
  -----------------------------------------------------------------------------

  -- DDR-P2.4 / DS-P2.59: select the ring read pointer presented to the write
  -- side. Read plane present -> the HW downloader's address (legacy behaviour).
  -- Read plane elided   -> the software-managed CSR pointer (rd_ptr_addr_ddr),
  -- which defaults to 0 and so is byte-identical to the old tied-low
  -- downloader_addr until a PS consumer starts advancing it.
  g_rd_ptr_src_hw : if enable_read_plane_g generate
    rd_ptr_sel <= downloader_addr;
  end generate g_rd_ptr_src_hw;

  g_rd_ptr_src_sw : if not enable_read_plane_g generate
    -- DDR-P3.36: the SW read-pointer CDC lives INSIDE this generate so it is
    -- never synthesised when enable_read_plane_g=true — where its output
    -- rd_ptr_addr_ddr is shadowed by g_rd_ptr_src_hw and synthesis removed the
    -- now-dangling rd_ptr_addr_sys / rd_ptr_addr_ddr / rd_ptr_tgl_ddr_d (Synth
    -- 8-6014). Exact mirror of DDR-T2.63's write-side move (g_wr_ptr_src_sw
    -- below); analysis: docs/internal/DDR_P2_11_CDC_VERDICT.md.
    --
    -- DDR-P2.4 / DS-P2.59: coherent multi-bit MCP CDC of the software read
    -- pointer, clk_sys -> clk_ddr, built on ddr_sync_cdc. WHY ddr_sync_cdc and not a
    -- free-running 2-FF or a mux-based MCP CDC: the read pointer moves DURING traffic, so a
    -- per-bit 2-FF would latch an incoherent old/new mix for 1-2 cycles and could
    -- transiently report extra free space -> overwrite un-drained data. The MCP
    -- formulation avoids that: toggle on each one-cycle load pulse, synchronize
    -- the single-bit toggle across domains, edge-detect it, and capture the
    -- (host-stable) address bus ONLY on that edge — never sampled mid-transition.
    -- ddr_sync_cdc carries ASYNC_REG="TRUE" as a STRING (so the design-wide
    -- get_cells -filter {ASYNC_REG} CDC max_delay constraint sees its flops) and
    -- already resolves in the rr library here.
    --
    -- TIMING: this crossing is constrained by ddr_stream_cdc_scoped.xdc (shipped
    -- with the IP). The toggle's sync flops match its "*i_sync_cdc_*stages_r_reg*"
    -- selector (hence the i_sync_cdc_rd_ptr instance name); the MCP data path is
    -- the INTERNAL register rd_ptr_addr_sys -> rd_ptr_addr_ddr, added to the XDC's
    -- set_max_delay -datapath_only -to list. Latching csr_rd_ptr_addr_i into the
    -- IP-internal rd_ptr_addr_sys (rather than capturing the input port directly)
    -- is what lets the scoped -from [internal sequential] reach the launch FF —
    -- mirrors the rd_base_addr_sys idiom.
    p_rd_ptr_sys : process(clk_sys_i) is
    begin
      if rising_edge(clk_sys_i) then
        if rst_sys_i = '1' then
          rd_ptr_addr_sys <= C_RD_PTR_RST;
          rd_ptr_tgl_sys  <= '0';
        elsif csr_rd_ptr_load_i = '1' then
          rd_ptr_addr_sys <= csr_rd_ptr_addr_i;   -- IP-internal MCP data source
          rd_ptr_tgl_sys  <= not rd_ptr_tgl_sys;  -- one toggle edge per update
        end if;
      end if;
    end process;

    i_sync_cdc_rd_ptr : entity rr.ddr_sync_cdc
      generic map (
        num_stages_g => C_CDC_NUM_STAGES,
        or_reduce_g  => false
        )
      port map (
        clk_i => clk_ddr_i,
        rst_i => '0',
        set_i => '0',
        d_i   => rd_ptr_tgl_sys,
        q_o   => rd_ptr_tgl_ddr
        );

    -- Edge-detect the synced toggle and capture the address. rst_ddr clears the
    -- pointer to 0 (post-reset the ring is empty, write_ptr=read_ptr=0). When the
    -- toggle edge lands, rd_ptr_addr_sys has been stable >= num_stages clk_ddr
    -- cycles, so the capture is coherent (MCP data path).
    p_rd_ptr_capture : process(clk_ddr_i) is
    begin
      if rising_edge(clk_ddr_i) then
        if rst_ddr_i = '1' then
          rd_ptr_addr_ddr  <= C_RD_PTR_RST;
          rd_ptr_tgl_ddr_d <= rd_ptr_tgl_ddr;
        else
          rd_ptr_tgl_ddr_d <= rd_ptr_tgl_ddr;
          if (rd_ptr_tgl_ddr xor rd_ptr_tgl_ddr_d) = '1' then
            rd_ptr_addr_ddr <= rd_ptr_addr_sys;
          end if;
        end if;
      end if;
    end process;

    rd_ptr_sel <= rd_ptr_addr_ddr;
  end generate g_rd_ptr_src_sw;

  -- DDR-P3.16: select the ring write pointer presented to the read side. Write
  -- plane present -> the HW uploader's address (legacy behaviour). Write plane
  -- elided -> the software-managed CSR pointer (wr_ptr_addr_ddr), which defaults
  -- to 0 and so is byte-identical to the old tied-to-uploader_addr (also 0 when
  -- the write plane is elided) until a PS producer starts advancing it.
  g_wr_ptr_src_hw : if enable_write_plane_g generate
    wr_ptr_sel <= uploader_addr;
  end generate g_wr_ptr_src_hw;

  g_wr_ptr_src_sw : if not enable_write_plane_g generate
    -- DDR-T2.63 / DS-T2.63: the SW write-pointer CDC lives INSIDE this generate so
    -- it is never synthesised when enable_write_plane_g=true — where its output
    -- wr_ptr_addr_ddr is shadowed by g_wr_ptr_src_hw and synthesis removed the
    -- now-dangling wr_ptr_addr_sys / wr_ptr_addr_ddr / wr_ptr_tgl_ddr_d (Synth
    -- 8-6014). DDR-P3.16: coherent multi-bit MCP CDC of the software WRITE pointer,
    -- clk_sys -> clk_ddr, built on ddr_sync_cdc — the exact mirror of the read-pointer
    -- crossing above (toggle on each one-cycle load pulse, synchronize the
    -- single-bit toggle, edge-detect, capture the host-stable bus only on the
    -- edge). ddr_sync_cdc carries ASYNC_REG="TRUE" as a STRING. TIMING: constrained by
    -- ddr_stream_cdc_scoped.xdc — the toggle's sync flops match the
    -- "*i_sync_cdc_*stages_r_reg*" selector (instance i_sync_cdc_wr_ptr) and the
    -- MCP data path is the INTERNAL register wr_ptr_addr_sys -> wr_ptr_addr_ddr,
    -- added to the XDC's set_max_delay -datapath_only -to list.
    p_wr_ptr_sys : process(clk_sys_i) is
    begin
      if rising_edge(clk_sys_i) then
        if rst_sys_i = '1' then
          wr_ptr_addr_sys <= C_WR_PTR_RST;
          wr_ptr_tgl_sys  <= '0';
        elsif csr_wr_ptr_load_i = '1' then
          wr_ptr_addr_sys <= csr_wr_ptr_addr_i;   -- IP-internal MCP data source
          wr_ptr_tgl_sys  <= not wr_ptr_tgl_sys;  -- one toggle edge per update
        end if;
      end if;
    end process;

    i_sync_cdc_wr_ptr : entity rr.ddr_sync_cdc
      generic map (
        num_stages_g => C_CDC_NUM_STAGES,
        or_reduce_g  => false
        )
      port map (
        clk_i => clk_ddr_i,
        rst_i => '0',
        set_i => '0',
        d_i   => wr_ptr_tgl_sys,
        q_o   => wr_ptr_tgl_ddr
        );

    -- Edge-detect the synced toggle and capture the address. rst_ddr clears the
    -- pointer to 0 (post-reset the ring is empty, write_ptr=read_ptr=0). When the
    -- toggle edge lands, wr_ptr_addr_sys has been stable >= num_stages clk_ddr
    -- cycles, so the capture is coherent (MCP data path).
    p_wr_ptr_capture : process(clk_ddr_i) is
    begin
      if rising_edge(clk_ddr_i) then
        if rst_ddr_i = '1' then
          wr_ptr_addr_ddr  <= C_WR_PTR_RST;
          wr_ptr_tgl_ddr_d <= wr_ptr_tgl_ddr;
        else
          wr_ptr_tgl_ddr_d <= wr_ptr_tgl_ddr;
          if (wr_ptr_tgl_ddr xor wr_ptr_tgl_ddr_d) = '1' then
            wr_ptr_addr_ddr <= wr_ptr_addr_sys;
          end if;
        end if;
      end if;
    end process;

    wr_ptr_sel <= wr_ptr_addr_ddr;
  end generate g_wr_ptr_src_sw;

  i_ptr_mgr : entity rr.ptr_mgr
    generic map (
      ptr_width_g => buffer_width_g,
      ptr_size_g  => ddr_data_width_g/8,
      prog_full_g => 2
      )
    port map (
      clk_i          => clk_ddr_i,
      rst_i          => rst_ddr_i,
      in_ptr_wr_i    => wr_ptr_sel(buffer_width_g-1 downto 0),
      in_ptr_rd_i    => rd_ptr_sel(buffer_width_g-1 downto 0),
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
      fifo_pipe_cnt_o   => fifoDDRPipeCnt,
      fifo_up_cnt_o     => fifoUpDataCnt,
      fifo_up_empty_o   => fifoUpEmpty,
      fifo_up_rd_en_i   => fifoUpRdEn,
      fifo_up_data_o    => fifoUpDin,
      fifo_down_room_o  => fifoDownDataRoom,
      fifo_down_full_o  => fifoDownFull,
      fifo_down_wr_en_i => fifoDownWrEn,
      fifo_down_data_i  => fifoDownDin
      );

  -- Pipe status without the cache-anticipation aggregation that virtual_fifo
  -- used to do (the cache layer lives in rr-vfifo now).
  ddrChipIsEmpty <= ddrEmptyCdc;
  ddrPipeIsEmpty <= ddrEmptyCdc and fifoDDRPipeEmpty and (not upBusBusyCdc) and (not downBusBusyCdc);
  ddrPipeCnt     <= assign_with_padding(ddrPipeCnt, unsigned(fifoDDRPipeCnt));

  -----------------------------------------------------------------------------
  -- Outputs
  -----------------------------------------------------------------------------
  ddrCSR_tp.fifoStatus.full       <= fifoUserFull;
  ddrCSR_tp.fifoStatus.empty      <= ddrPipeIsEmpty;
  ddrCSR_tp.fifoStatus.chip_empty <= ddrChipIsEmpty;
  ddrCSR_tp.fifoStatus.chip_full  <= ddrAlmostFullCdc;
  ddrCSR_tp.fifoStatus.write_error <= wrErrCdc;   -- DDR-P3.25
  ddrCSR_tp.fifoStatus.read_error  <= rdErrCdc;   -- DDR-P3.25
  ddrCSR_tp.fifoCnt               <= std_logic_vector(ddrPipeCnt);
  --
  -- DDR-T1: registered outputs MUST clear on rst_sys_i='1'. The
  -- previous `<= ... when rising_edge(clk_sys_i)` form had no reset
  -- clause, so the first registered value on cycle 1 was ddrCSR_tp's
  -- combinational pre-reset state — where the ddr_sync_cdc chain for
  -- ddrEmptyCdc still read its stages_r init='0', producing a stale
  -- ddr_csr_o.fifoStatus.empty='0' for one clk_sys cycle. Downstream
  -- consumers (e.g. rr-vfifo's mux_ep_ddr opportunistic-unload guard,
  -- INV-MEPD-1) read that as "DDR has data when really empty" and
  -- transiently fired rd_en_ddr. C_INIT_FIFO_CSR (empty='1',
  -- chip_empty='1') is the correct quiescent CSR for a freshly-reset
  -- empty pipe.
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
  -- DDR-P3.9: runtime base-address registers + idle status outputs.
  --
  -- write_idle_o / read_idle_o are the CSR-host's signal that it's safe
  -- to issue a load. They take effect with the upBusBusyCdc /
  -- downBusBusyCdc signals (already 2-stage-synced into clk_sys), so the
  -- contract is host-visible WITHOUT additional CDC at the boundary.
  --
  -- Load is double-gated: (a) only when the corresponding bus is idle,
  -- and (b) the latch lives in clk_sys, so a glitch on the host's CSR
  -- bus can't propagate into clk_ddr's address generation.
  --
  -- The clk_ddr-side wr_base_addr_ddr / rd_base_addr_ddr are 2-stage
  -- flops of the clk_sys values. The idle-contract guarantees stability
  -- on the source side when a burst is in flight, so per-bit
  -- metastability is the only concern — handled by the 2-stage flop.
  --
  -- DDR-P2.29: this is a QUASI-STATIC multi-bit crossing, not a synchroniser.
  -- Each bit of *_base_addr_meta can resolve on a different clk_ddr edge, so
  -- *_base_addr_ddr can hold a torn word for a cycle and is coherent from the
  -- 3rd clk_ddr edge after the source changes. The protocol (DDR-REQ-211):
  --   (1) RTL: *_base_addr_sys changes only on a load accepted while the
  --       plane's synchronised idle flag is high (write_idle_o / read_idle_o);
  --       a load while busy is dropped (ping_pong_concurrent_test, DDR-P4.4).
  --   (2) Host: load the base only while the plane holds no data (after reset,
  --       or after the ring drained), and do not feed it until the load has
  --       settled. Idle alone does not cover this: busy reaches clk_sys 2-3
  --       cycles late, and a partial word in the FIFO can still start a burst,
  --       whose address adds the base combinationally (axi4w/axi4r, avmm_w/r).
  -- A static-base consumer (load tied off) holds the source constant.
  -- The path is bounded by constraints/ddr_stream_cdc_scoped.{xdc,sdc} and
  -- ddr_stream_cdc_scoped_microchip.sdc (selector *_base_addr_meta), and the
  -- four captures carry an inline RR-CDC-005 waiver naming this protocol.
  ---------------------------------------------------------------------------
  write_idle_o <= not upBusBusyCdc;
  read_idle_o  <= not downBusBusyCdc;

  ---------------------------------------------------------------------------
  -- DDR-T2.21: split per-plane CSR CDC registers and ddr_sync_cdc instances
  -- into if enable_*_plane_g generate blocks (tie outputs to reset values
  -- when disabled).
  --
  -- With a plane disabled, its CSR CDC registers and ptr_reset synchroniser
  -- have no sink in clk_ddr (uploader/downloader is elided), so Vivado prunes
  -- them as dead logic (Synth 8-6014 on wr_base_addr_ddr_reg,
  -- wr_ptr_reset_tgl_ddr_d_reg, wr_ptr_reset_ddr_reg,
  -- rd_ptr_reset_tgl_ddr_d_reg, rd_ptr_reset_ddr_reg) and rr's 8-6014 gate
  -- refuses the build. Gating elaboration per plane eliminates the dead
  -- crossings.
  ---------------------------------------------------------------------------
  g_wr_csr_cdc : if enable_write_plane_g generate
    csr_wr_base_addr_sys_proc : process(clk_sys_i) is
    begin
      if rising_edge(clk_sys_i) then
        if rst_sys_i = '1' then
          wr_base_addr_sys     <= C_DDR_BASE_RST;
          wr_ptr_reset_tgl_sys <= '0';
        else
          if csr_wr_base_load_i = '1' and upBusBusyCdc = '0' then
            wr_base_addr_sys <= csr_wr_base_addr_i;
          end if;
          if csr_wr_ptr_reset_i = '1' and upBusBusyCdc = '0' then
            wr_ptr_reset_tgl_sys <= not wr_ptr_reset_tgl_sys;
          end if;
        end if;
      end if;
    end process;

    csr_wr_base_addr_ddr_proc : process(clk_ddr_i) is
    begin
      if rising_edge(clk_ddr_i) then
        if rst_ddr_i = '1' then
          wr_base_addr_meta <= C_DDR_BASE_RST;
          wr_base_addr_ddr  <= C_DDR_BASE_RST;
        else
          wr_base_addr_meta <= wr_base_addr_sys;  -- rr-lint-disable-line RR-CDC-005 quasi-static: wr_base_addr_sys loads only while write_idle_o=1 and the plane holds no data until the capture settles (DDR-REQ-211)
          wr_base_addr_ddr  <= wr_base_addr_meta;
        end if;
      end if;
    end process;

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

    csr_wr_ptr_reset_ddr_proc : process(clk_ddr_i) is
    begin
      if rising_edge(clk_ddr_i) then
        if rst_ddr_i = '1' then
          wr_ptr_reset_tgl_ddr_d <= wr_ptr_reset_tgl_ddr;
          wr_ptr_reset_ddr       <= '0';
        else
          wr_ptr_reset_tgl_ddr_d <= wr_ptr_reset_tgl_ddr;
          wr_ptr_reset_ddr       <= wr_ptr_reset_tgl_ddr xor wr_ptr_reset_tgl_ddr_d;
        end if;
      end if;
    end process;
  else generate
    wr_base_addr_sys       <= C_DDR_BASE_RST;
    wr_ptr_reset_tgl_sys   <= '0';
    wr_base_addr_meta      <= C_DDR_BASE_RST;
    wr_base_addr_ddr       <= C_DDR_BASE_RST;
    wr_ptr_reset_tgl_ddr   <= '0';
    wr_ptr_reset_tgl_ddr_d <= '0';
    wr_ptr_reset_ddr       <= '0';
  end generate g_wr_csr_cdc;

  g_rd_csr_cdc : if enable_read_plane_g generate
    csr_rd_base_addr_sys_proc : process(clk_sys_i) is
    begin
      if rising_edge(clk_sys_i) then
        if rst_sys_i = '1' then
          rd_base_addr_sys     <= C_DDR_BASE_RST;
          rd_ptr_reset_tgl_sys <= '0';
        else
          if csr_rd_base_load_i = '1' and downBusBusyCdc = '0' then
            rd_base_addr_sys <= csr_rd_base_addr_i;
          end if;
          if csr_rd_ptr_reset_i = '1' and downBusBusyCdc = '0' then
            rd_ptr_reset_tgl_sys <= not rd_ptr_reset_tgl_sys;
          end if;
        end if;
      end if;
    end process;

    csr_rd_base_addr_ddr_proc : process(clk_ddr_i) is
    begin
      if rising_edge(clk_ddr_i) then
        if rst_ddr_i = '1' then
          rd_base_addr_meta <= C_DDR_BASE_RST;
          rd_base_addr_ddr  <= C_DDR_BASE_RST;
        else
          rd_base_addr_meta <= rd_base_addr_sys;  -- rr-lint-disable-line RR-CDC-005 quasi-static: rd_base_addr_sys loads only while read_idle_o=1 and the plane holds no data until the capture settles (DDR-REQ-211)
          rd_base_addr_ddr  <= rd_base_addr_meta;
        end if;
      end if;
    end process;

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

    csr_rd_ptr_reset_ddr_proc : process(clk_ddr_i) is
    begin
      if rising_edge(clk_ddr_i) then
        if rst_ddr_i = '1' then
          rd_ptr_reset_tgl_ddr_d <= rd_ptr_reset_tgl_ddr;
          rd_ptr_reset_ddr       <= '0';
        else
          rd_ptr_reset_tgl_ddr_d <= rd_ptr_reset_tgl_ddr;
          rd_ptr_reset_ddr       <= rd_ptr_reset_tgl_ddr xor rd_ptr_reset_tgl_ddr_d;
        end if;
      end if;
    end process;
  else generate
    rd_base_addr_sys       <= C_DDR_BASE_RST;
    rd_ptr_reset_tgl_sys   <= '0';
    rd_base_addr_meta      <= C_DDR_BASE_RST;
    rd_base_addr_ddr       <= C_DDR_BASE_RST;
    rd_ptr_reset_tgl_ddr   <= '0';
    rd_ptr_reset_tgl_ddr_d <= '0';
    rd_ptr_reset_ddr       <= '0';
  end generate g_rd_csr_cdc;

  ----- Overflow / underflow assertions
  process(clk_sys_i)
  begin
    if rising_edge(clk_sys_i) then
      ddrCSR_tp.fifoStatus.overflow  <= '0';
      ddrCSR_tp.fifoStatus.underflow <= '0';

      -- DDR-P3.10: handshake-gated, so these are unreachable by
      -- construction (defensive — flag if ever fired). fifoUserWrEn
      -- requires (skidInValid AND NOT fifoUserFull); fifoUserRdEn
      -- requires (m_axis_valid_o AND m_axis_ready_i), and
      -- m_axis_valid_o only asserts when NOT fifoUserEmpty.
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
