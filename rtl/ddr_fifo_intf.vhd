-- ============================================================================
-- File Name    : ddr_cdc.vhd
-- Author       : dmazure
-- Date         : 2024-10-06
-- Description  : This module implements the Clock Domain Crossing (CDC) FIFOs
--                to transfer data from the user logic to the uploader and downloader
--                modules, that expect FIFO interfaces.
-- ============================================================================
-- Revision History:
-- Date        Author      Description
-- ----------------------------------------------------------------------------
-- 2024-10-06  dmazure     Initial creation.
-- ============================================================================
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use IEEE.MATH_REAL.all;
-- RR--P3.3: vestigial `library xpm; use xpm.vcomponents.all;` removed.
-- This file no longer instantiates any xpm component directly — the
-- xpm bodies are factored down into rr_fifo_async_mixed / rr_fifo_sync
-- via fifo_impl_g passthrough.
library rr;

entity ddr_fifo_intf is
  generic (
    fifo_depth_g : integer := 9;        -- In terms of fifo_width_g words
    fifo_width_g : integer := 128;
    msg_width_g  : integer := 32;
    fifo_fwft_g  : integer := 1;
    -- DDR-P3.6 / RR--P3.1: FIFO backend impl for the upload + download
    -- async FIFOs (mixed-width) and the pad-event sync FIFO. "olo"
    -- (default) = Quartus + Vivado portable via open-logic wconv +
    -- olo_base_fifo_async/sync chains. "xpm" = Xilinx golden reference.
    fifo_impl_g  : string  := "olo"
    );
  port (
    -- Clock and reset
    clk_sys_i         : in  std_logic;
    clk_ddr_i         : in  std_logic;
    rst_sys_i         : in  std_logic;
    rst_ddr_i         : in  std_logic;
    busy_o            : out std_logic;
    -- User logic
    -- 1. Comes from the arbitrer. This modules accept data 
    -- from the arbitrer and sends it to the uploader
    fifo_in_data_i    : in  std_logic_vector(msg_width_g - 1 downto 0);
    fifo_in_wren_i    : in  std_logic;
    fifo_in_full_o    : out std_logic;    -- Likely not used
    -- 2. Goes to the EP FIFO
    -- This modules accepts data from the downloader ands sends it 
    -- to the EP FIFO, when requested via RDEN
    fifo_out_data_o   : out std_logic_vector(msg_width_g - 1 downto 0);
    fifo_out_rden_i   : in  std_logic;
    fifo_out_empty_o  : out std_logic;    -- Valid signal for read
    -- 
    fifo_pipe_empty_o : out std_logic;  -- Not data in the input neither in the output FIFO
    fifo_pipe_cnt_o   : out std_logic_vector(fifo_depth_g + fifo_fwft_g + integer(ceil(log2(real(fifo_width_g/msg_width_g))))- 1 downto 0);  -- FWFT
    -- Uploader interface
    fifo_up_cnt_o     : out std_logic_vector(fifo_depth_g + fifo_fwft_g - 1 downto 0) := (others => '0');  -- FWFT
    fifo_up_empty_o   : out std_logic;
    fifo_up_rd_en_i   : in  std_logic;
    fifo_up_data_o    : out std_logic_vector(fifo_width_g - 1 downto 0);
    -- Downloader interface
    fifo_down_full_o  : out std_logic;
    fifo_down_room_o  : out std_logic_vector(fifo_depth_g + fifo_fwft_g - 1 downto 0);  -- FWFT    
    fifo_down_wr_en_i : in  std_logic;
    fifo_down_data_i  : in  std_logic_vector(fifo_width_g - 1 downto 0)
    );
end entity ddr_fifo_intf;

architecture rtl of ddr_fifo_intf is

  -- Constants
  constant C_FIFO_WORD_DEPTH : integer := 2 ** fifo_depth_g;
  constant C_MSGS_PER_WORD   : integer := fifo_width_g / msg_width_g;
  constant C_FIFO_CNT_WIDTH  : integer := fifo_depth_g + fifo_fwft_g;
  constant C_CDC_NUM_STAGES : integer := 3;
  constant C_RST_PIPE_STAGES : integer := 16;  -- relax timing for this reset. This FIFO is only used after the filling the ep FIFO,
  -- so it is not critical to release the reset at the same time as the rest of the system
  -- Signals
  signal busyUp              : std_logic;
  signal busyDown            : std_logic;
  signal inIsEmpty           : std_logic;
  signal outIsEmpty          : std_logic;
  signal ddrPipeCntUp        : unsigned(fifo_pipe_cnt_o'length-1 downto 0);
  signal ddrPipeCntDown      : unsigned(fifo_pipe_cnt_o'length-1 downto 0);
  --
  signal emptyUpCdc       : std_logic; -- Empy signal in the Write Domain for the downloader block
  --  the signal is necessary to rr.round the fact that the wr_data_count is falsely reporting
  -- one word when empty, hence can't be used to determine the empty state of the FIFO

  -----------------------------------------------------------------------------
  -- DDR-P3.2 pad-event sideband. The UP block injects 1-3 zero-pad messages
  -- on partial-DDR-word flush (DDR-T2); without filtering, those pads emerge
  -- at the user-out side and pollute the consumer's stream. This sideband
  -- FIFO records ONE entry per flush event:
  --   (snapshot of real-message write count at flush, pad_count)
  -- On the read side, an auto-drain FSM compares the running emit-count
  -- against the FIFO head; on match it pops the event and silently
  -- consumes the next pad_count emissions from DOWN FIFO via an internal
  -- rd_en pulse (gating fifo_out_empty_o = '1' for those cycles so the
  -- user sees nothing). Net behaviour at the IP boundary becomes
  -- "N writes = N reads in order" with no visible pads.
  --
  -- Both endpoints are on clk_sys → sync FIFO, no CDC. Width = count + 2;
  -- depth 16 is generous (one entry per producer-idle-flush event).
  constant C_PAD_EVENT_COUNT_WIDTH : integer := 32;
  -- DDR-T2 pad-count field width = ceil(log2(ratio)) so it spans
  -- 0..C_MSGS_PER_WORD-1 pads. Was hardcoded to 2 bits (ratio-4 / 128-bit DDR);
  -- 256-bit DDR has ratio 8 and needs 3 bits, else the pad count overflows and
  -- the partial-word flush strands/corrupts the read tail (VFIFO-T2.4). Requires
  -- C_MSGS_PER_WORD a power of 2 (the wconv already requires it).
  constant C_PAD_COUNT_W           : integer := integer(ceil(log2(real(C_MSGS_PER_WORD))));
  constant C_PAD_EVENT_WIDTH       : integer := C_PAD_EVENT_COUNT_WIDTH + C_PAD_COUNT_W;
  constant C_PAD_EVENT_DEPTH_LOG2  : integer := 4;
  constant C_PAD_EVENT_DEPTH       : integer := 2 ** C_PAD_EVENT_DEPTH_LOG2;

  signal pad_event_din   : std_logic_vector(C_PAD_EVENT_WIDTH-1 downto 0);
  signal pad_event_wr_en : std_logic;
  signal pad_event_dout  : std_logic_vector(C_PAD_EVENT_WIDTH-1 downto 0);
  signal pad_event_rd_en : std_logic;
  signal pad_event_empty : std_logic;
  signal pad_event_full  : std_logic;

  -- Pad-strip FSM signals (auto-drain that hides DDR-T2 pads from the
  -- consumer surface). At architecture scope so the DOWN block can
  -- splice the internal drain pulse into its xpm rd_en line.
  signal emit_count_r       : unsigned(C_PAD_EVENT_COUNT_WIDTH-1 downto 0) := (others => '0');
  signal auto_drain_r       : unsigned(C_PAD_COUNT_W-1 downto 0) := (others => '0');
  signal event_threshold_s  : unsigned(C_PAD_EVENT_COUNT_WIDTH-1 downto 0);
  signal event_pad_count_s  : unsigned(C_PAD_COUNT_W-1 downto 0);
  signal event_ready_to_pop : std_logic;
  signal internal_drain_rd  : std_logic;
  signal real_emit_pulse    : std_logic;
  signal fifo_out_rden_eff  : std_logic;
begin


  GEN_UPLOADER_BLOCK : block
    -- Uploader receives msg_width_g width data from the user logic
    constant C_WR_DATA_COUNT_WIDTH   : integer   := C_FIFO_CNT_WIDTH + integer(ceil(log2(real(C_MSGS_PER_WORD))));
    constant C_RD_DATA_COUNT_WIDTH   : integer   := C_FIFO_CNT_WIDTH;
    -- RR--P3.6: XPM legal range for PROG_FULL_THRESH is [5, depth-35]
    -- (xpm_fifo_async runtime DRC). Use depth-40 to keep a margin
    -- without crowding the upper bound. Old `-16` worked for the
    -- historical 32/128 default but tripped XPM at 32/512 (ratio 16,
    -- depth 8192, threshold of depth-16=8176 > legal max 8157).
    constant C_FIFO_PROG_FULL_THRES  : integer   := 2 ** (C_WR_DATA_COUNT_WIDTH-fifo_fwft_g) - 40;
    constant C_FIFO_PROG_EMPTY_THRES : integer   := 8;
    -- Signals for the FIFO
    signal almost_empty              : std_logic;
    signal almost_full               : std_logic;
    signal data_valid                : std_logic;
    signal dbiterr                   : std_logic;
    signal dout                      : std_logic_vector(fifo_width_g - 1 downto 0);
    signal empty                     : std_logic;
    signal full                      : std_logic;
    -- DDR-T2.14: backend-accurate "a write THIS cycle lands". For olo upsize
    -- this is the wconv accept (a narrow write is absorbed even when the wide
    -- FIFO is full); for xpm it is `not full`. The pad-flush gates pad-injection
    -- advance on this so a pad zero-write dropped by a full FIFO is never
    -- counted as injected (which previously caused egress over-strip of reals).
    signal wr_ready                  : std_logic;
    signal overflow                  : std_logic;
    signal prog_empty                : std_logic;
    signal prog_full                 : std_logic;
    signal rd_data_count             : std_logic_vector(C_RD_DATA_COUNT_WIDTH - 1 downto 0);
    signal rd_rst_busy               : std_logic;
    signal sbiterr                   : std_logic;
    signal underflow                 : std_logic;
    signal wr_ack                    : std_logic;
    signal wr_data_count             : std_logic_vector(C_WR_DATA_COUNT_WIDTH - 1 downto 0);
    signal wr_rst_busy               : std_logic;
    signal din                       : std_logic_vector(msg_width_g - 1 downto 0);
    signal injectdbiterr             : std_logic := '0';
    signal injectsbiterr             : std_logic := '0';
    signal rd_clk                    : std_logic;
    signal rd_en                     : std_logic;
    signal sleep                     : std_logic := '0';
    signal wr_clk                    : std_logic;
    signal wr_en                     : std_logic;
    signal rst                       : std_logic;

    -- DDR-T2 partial-word flush. The UP FIFO is mixed-width
    -- (msg_width_g write × fifo_width_g read = 4:1 packing). Its
    -- read-side `empty` flag fires whenever no COMPLETE
    -- fifo_width_g word is available, so 1-3 leftover messages from
    -- the producer end up invisibly stuck on the write side. Both
    -- the uploader (reads in 128-bit DDR-words) and any external
    -- consumer reading fifo_up_empty_o / fifo_pipe_empty_o see
    -- "drained" while the partial fragment lingers, and the
    -- messages are forever lost.
    --
    -- Mechanism: a small FSM tracks how many messages sit in the
    -- current packing slot (partial_cnt_r, 0..C_MSGS_PER_WORD-1). On sustained
    -- producer idle (C_IDLE_FLUSH_THRES cycles with no fifo_in_wren_i
    -- pulse) AND a non-zero partial, the FSM takes over the write
    -- port and injects (C_MSGS_PER_WORD - partial) zero-messages to complete
    -- the DDR-word and let it drain through. The 0..ratio-1 padding messages
    -- emerge at the read side as extra valid messages at the very
    -- end of the stream — the consumer is expected to know its own
    -- write count and either ignore or strip them.
    --
    -- One-shot per partial. Multiple flushes happen naturally if the
    -- producer resumes writing (re-fills partial) and then idles
    -- again. The FSM never injects more than 3 pads in any single
    -- flush; it never injects when partial=0.
    constant C_IDLE_FLUSH_THRES : integer := 128;
    constant C_IDLE_CNT_WIDTH   : integer := integer(ceil(log2(real(C_IDLE_FLUSH_THRES+1))));

    signal partial_cnt_r : unsigned(C_PAD_COUNT_W-1 downto 0)      := (others => '0');
    signal idle_cnt_r    : unsigned(C_IDLE_CNT_WIDTH-1 downto 0)   := (others => '0');
    signal pad_active_r  : std_logic                               := '0';

    -- DDR-P3.2: track real-message writes (excluding pads) so we can
    -- snapshot the count at the moment a flush event starts and push
    -- it to the sideband pad_event_fifo.
    signal real_wr_count_r : unsigned(C_PAD_EVENT_COUNT_WIDTH-1 downto 0) := (others => '0');
  begin

    wr_clk <= clk_sys_i;
    rd_clk <= clk_ddr_i;
    --

    -- Effective write port: pad takes over when active.
    din   <= (others => '0') when pad_active_r = '1' else fifo_in_data_i;
    wr_en <= '1'             when pad_active_r = '1' else fifo_in_wren_i;

    -- Default the pad_event_fifo write strobe; the FSM may pulse it on
    -- a flush trigger below.
    pad_event_wr_en <= '0' when pad_active_r = '1'
                       else '1' when (idle_cnt_r = to_unsigned(C_IDLE_FLUSH_THRES, C_IDLE_CNT_WIDTH)
                                       and partial_cnt_r /= 0
                                       and pad_event_full = '0')
                       else '0';
    -- pad_event_din is packed: high bits = real-write-count snapshot at
    -- this cycle (before the next write increments it); low C_PAD_COUNT_W bits
    -- = pad_count = C_MSGS_PER_WORD - partial_cnt_r. Computed as -partial_cnt_r
    -- in C_PAD_COUNT_W-bit arithmetic (ratio mod ratio = 0 maps the partial=0
    -- case which we never push anyway). Parameterised by the ratio so 256-bit
    -- DDR (ratio 8) flushes correctly, not just 128-bit (ratio 4).
    pad_event_din <= std_logic_vector(real_wr_count_r) &
                     std_logic_vector(to_unsigned(0, C_PAD_COUNT_W) - partial_cnt_r);

    pad_flush_proc : process(clk_sys_i) is
    begin
      if rising_edge(clk_sys_i) then
        if rst_sys_i = '1' then
          partial_cnt_r   <= (others => '0');
          idle_cnt_r      <= (others => '0');
          pad_active_r    <= '0';
          real_wr_count_r <= (others => '0');
        else
          if pad_active_r = '1' then
            -- Pad mode: one zero-write per cycle until partial wraps to 0.
            -- DDR-T2.14: advance the pad sequence ONLY on cycles the zero-write
            -- actually LANDS (wr_ready='1'). Driving din=0/wr_en=1 into a FIFO
            -- that cannot accept it (xpm asymmetric FIFO when full) drops the
            -- pad, yet the sideband pad-event is already queued — so the egress
            -- pad-strip would later consume that many REAL words. Gating on
            -- wr_ready (the backend-accurate accept, NOT `not full`: olo's wconv
            -- absorbs a write even when the wide FIFO is full) holds here until
            -- the pad is genuinely written, so injected pads == event pad_count
            -- on BOTH backends. olo's wr_ready is asserted whenever its HEAD
            -- behaviour accepted the pad, so this is a no-op for olo.
            if wr_ready = '1' then
              partial_cnt_r <= partial_cnt_r + 1;
              if partial_cnt_r = C_MSGS_PER_WORD - 1 then
                -- This pad completes the ratio-pack; drop pad_active so the
                -- next cycle returns to normal user-driven writes.
                pad_active_r <= '0';
                idle_cnt_r   <= (others => '0');
              end if;
            end if;
          else
            -- Normal mode: track user-driven writes that actually land.
            if fifo_in_wren_i = '1' and full = '0' then
              partial_cnt_r   <= partial_cnt_r + 1;  -- wraps at C_MSGS_PER_WORD
              idle_cnt_r      <= (others => '0');
              real_wr_count_r <= real_wr_count_r + 1;
            else
              if idle_cnt_r /= to_unsigned(C_IDLE_FLUSH_THRES, C_IDLE_CNT_WIDTH) then
                idle_cnt_r <= idle_cnt_r + 1;
              end if;
            end if;
            -- Trigger pad flush on sustained idle with non-zero partial.
            -- pad_event_fifo is written combinationally on the same edge
            -- (see pad_event_wr_en assignment above), so by the time
            -- pad_active_r='1' the event is already queued.
            if idle_cnt_r = to_unsigned(C_IDLE_FLUSH_THRES, C_IDLE_CNT_WIDTH)
               and partial_cnt_r /= 0
            then
              pad_active_r <= '1';
            end if;
          end if;
        end if;
      end if;
    end process;


    -- Uploader interfaces, is reading from this FIFO to promote to the DDR
    rd_en                                           <= fifo_up_rd_en_i;
    fifo_up_cnt_o(C_RD_DATA_COUNT_WIDTH - 1 downto 0) <= rd_data_count;
    fifo_up_empty_o                                   <= empty;
    fifo_up_data_o                                    <= dout;

    -- RR--P3.1 (2026-05-15): mixed-width async FIFO via the
    -- vendor-portable rr.rr_fifo_async_mixed wrapper. olo backend
    -- chains olo_base_wconv_n2xn (sys-side pack) + olo_base_fifo_async
    -- (CDC at wide width); XPM backend uses xpm_fifo_async with
    -- WRITE_DATA_WIDTH ≠ READ_DATA_WIDTH. The fifo_impl_g entity
    -- generic threads to the wrapper.
    i_uploader_fifo : entity rr.rr_fifo_async_mixed
      generic map (
        fifo_impl_g           => fifo_impl_g,
        wr_data_width_g       => msg_width_g,
        rd_data_width_g       => fifo_width_g,
        fifo_wr_depth_g       => C_FIFO_WORD_DEPTH*C_MSGS_PER_WORD,
        fwft_g                => true,
        cdc_sync_stages_g     => 2,
        prog_full_thresh_g    => C_FIFO_PROG_FULL_THRES,
        prog_empty_thresh_g   => C_FIFO_PROG_EMPTY_THRES,
        wr_data_count_width_g => C_WR_DATA_COUNT_WIDTH,
        rd_data_count_width_g => C_RD_DATA_COUNT_WIDTH
      )
      port map (
        rst_i           => rst_sys_i,
        wr_clk_i        => wr_clk,
        din_i           => din,
        wr_en_i         => wr_en,
        full_o          => full,
        wr_ready_o      => wr_ready,
        almost_full_o   => almost_full,
        prog_full_o     => prog_full,
        wr_data_count_o => wr_data_count,
        wr_rst_busy_o   => wr_rst_busy,
        rd_clk_i        => rd_clk,
        rd_en_i         => rd_en,
        dout_o          => dout,
        empty_o         => empty,
        almost_empty_o  => almost_empty,
        prog_empty_o    => prog_empty,
        data_valid_o    => data_valid,
        rd_data_count_o => rd_data_count,
        rd_rst_busy_o   => rd_rst_busy
      );

      i_sync_cdc_empty : entity rr.ddr_sync_cdc
      generic map(
        num_stages_g => C_CDC_NUM_STAGES,
        rst_val_g    => '1'
        )
      port map
      (
        clk_i => WR_CLK,
        set_i   => rst_sys_i,
        rst_i   => '0',
        d_i   => empty,
        q_o   => emptyUpCdc
        );

    inIsEmpty <= emptyUpCdc;

    ddrPipeCntUp <= unsigned(wr_data_count(wr_data_count'length-1 downto 1) & ((not emptyUpCdc) and wr_data_count(0)));
    -- End of xpm_fifo_async_inst instantiation
    busyUp       <= wr_rst_busy;
    -- DDR-T2.14: assert "full" to the producer while a pad-flush owns the
    -- write port. During pad_active the pad FSM drives wr_en/din (din=0); a
    -- coincident real user write (fifo_in_wren_i='1') would otherwise be
    -- silently dropped because wr_en is hijacked by the pad — `full` alone is
    -- 0 here so the upstream skid sees ready, advances, and loses the word.
    -- Back-pressuring the producer for the (short) pad burst holds the real
    -- write in the skid until the pad completes, then it lands normally. No
    -- deadlock: the uploader keeps draining the FIFO so pad_active always
    -- clears. Harmless on olo (brief pad window); essential on xpm.
    fifo_in_full_o <= full or pad_active_r;

  end block GEN_UPLOADER_BLOCK;

  GEN_DOWNLOADER_BLOCK : block
    -- The downloader offers msg_width_g width data to the user logic
    constant C_WR_DATA_COUNT_WIDTH   : integer   := C_FIFO_CNT_WIDTH;  -- this is what the DDR sees
    constant C_RD_DATA_COUNT_WIDTH   : integer   := C_FIFO_CNT_WIDTH + integer(ceil(log2(real(C_MSGS_PER_WORD))));
    -- RR--P3.6: XPM legal range [5, depth-35] — same fix as the uploader.
    constant C_FIFO_PROG_FULL_THRES  : integer   := 2 ** (C_FIFO_CNT_WIDTH-fifo_fwft_g) - 40;
    constant C_FIFO_PROG_EMPTY_THRES : integer   := 8;
    -- Signals for the FIFO
    -- Signals for the FIFO
    signal almost_empty              : std_logic;
    signal almost_full               : std_logic;
    signal data_valid                : std_logic;
    signal dbiterr                   : std_logic;
    signal dout                      : std_logic_vector(msg_width_g - 1 downto 0);
    signal empty                     : std_logic;
    signal full                      : std_logic;
    signal overflow                  : std_logic;
    signal prog_empty                : std_logic;
    signal prog_full                 : std_logic;
    signal rd_data_count             : std_logic_vector(C_RD_DATA_COUNT_WIDTH - 1 downto 0);
    signal rd_rst_busy               : std_logic;
    signal sbiterr                   : std_logic;
    signal underflow                 : std_logic;
    signal wr_ack                    : std_logic;
    signal wr_data_count             : std_logic_vector(C_FIFO_CNT_WIDTH - 1 downto 0);
    signal wr_rst_busy               : std_logic;
    signal din                       : std_logic_vector(fifo_width_g - 1 downto 0);
    signal injectdbiterr             : std_logic := '0';
    signal injectsbiterr             : std_logic := '0';
    signal rd_clk                    : std_logic;
    signal rd_en                     : std_logic;
    signal sleep                     : std_logic := '0';
    signal wr_clk                    : std_logic;
    signal wr_en                     : std_logic;
    signal rst                       : std_logic;

  begin

    wr_clk <= clk_ddr_i;                  -- DDR is writing to this FIFO
    rd_clk <= clk_sys_i;                  -- System is reading from this FIFO

    -- DDR-P3.2: rd_en into the DOWN xpm is either the user's pulse OR
    -- the auto-drain pulse from the pad-strip FSM (driven at arch
    -- scope). The strip FSM ensures the two never collide.
    rd_en <= fifo_out_rden_eff;

    -- The incoming DDR data gets into the FIFO
    wr_en <= fifo_down_wr_en_i;
    din   <= fifo_down_data_i;

    -- RR--P3.1 (2026-05-15): mixed-width async FIFO via the
    -- vendor-portable rr.rr_fifo_async_mixed wrapper. olo backend
    -- chains olo_base_fifo_async (CDC at wide width) + olo_base_wconv_xn2n
    -- (sys-side unpack); XPM backend uses xpm_fifo_async natively.
    i_downloader_fifo : entity rr.rr_fifo_async_mixed
      generic map (
        fifo_impl_g           => fifo_impl_g,
        wr_data_width_g       => fifo_width_g,
        rd_data_width_g       => fifo_width_g/C_MSGS_PER_WORD,
        fifo_wr_depth_g       => C_FIFO_WORD_DEPTH,
        fwft_g                => true,
        cdc_sync_stages_g     => 2,
        prog_full_thresh_g    => C_FIFO_PROG_FULL_THRES,
        prog_empty_thresh_g   => C_FIFO_PROG_EMPTY_THRES,
        wr_data_count_width_g => C_WR_DATA_COUNT_WIDTH,
        rd_data_count_width_g => C_RD_DATA_COUNT_WIDTH
      )
      port map (
        rst_i           => rst_ddr_i,
        wr_clk_i        => wr_clk,
        din_i           => din,
        wr_en_i         => wr_en,
        full_o          => full,
        wr_ready_o      => open,   -- DDR-T2.14: downloader writes come from DDR, not the pad-flush
        almost_full_o   => almost_full,
        prog_full_o     => prog_full,
        wr_data_count_o => wr_data_count,
        wr_rst_busy_o   => wr_rst_busy,
        rd_clk_i        => rd_clk,
        rd_en_i         => rd_en,
        dout_o          => dout,
        empty_o         => empty,
        almost_empty_o  => almost_empty,
        prog_empty_o    => prog_empty,
        data_valid_o    => data_valid,
        rd_data_count_o => rd_data_count,
        rd_rst_busy_o   => rd_rst_busy
      );

    busyDown <= rd_rst_busy;

    -- Downloader interface, it is writing to this FIFO
    fifo_down_full_o <= prog_full;
    -- Use a -1 to be pesimistic about the space available in the FIFO, as the wr_data_count is not reliable when empty
    fifo_down_room_o <= std_logic_vector(to_unsigned(2 ** (C_FIFO_CNT_WIDTH-fifo_fwft_g), fifo_down_room_o'length)-1 - unsigned(wr_data_count));
    fifo_out_data_o  <= dout;

    -- Goes to the EP FIFO
    ddrPipeCntDown <= unsigned(rd_data_count);
    outIsEmpty     <= empty;

  end block GEN_DOWNLOADER_BLOCK;

  busy_o            <= busyUp or busyDown when rising_edge(clk_sys_i);
  fifo_pipe_empty_o <= inIsEmpty and outIsEmpty;
  fifo_pipe_cnt_o   <= std_logic_vector(ddrPipeCntUp + ddrPipeCntDown);

  -----------------------------------------------------------------------------
  -- DDR-P3.2 pad-strip layer.
  --
  -- Sync FIFO for pad events (clk_sys_i on both ports) + auto-drain
  -- FSM that gates the user-facing rd_en / empty so the 0-3 trailing
  -- zero-pads emitted by DDR-T2's flush mechanism never reach the
  -- consumer.
  --
  -- DDR-P3.6 / RR--P3.1 (2026-05-15): wrapped via rr.rr_fifo_sync so
  -- the impl (open-logic/olo_base_fifo_sync default, XPM opt-in) is
  -- selectable. Forwards the entity's fifo_impl_g generic so consumers
  -- pick the same backend across all three FIFOs in this file.
  -----------------------------------------------------------------------------
  i_pad_event_fifo : entity rr.rr_fifo_sync
    generic map (
      fifo_impl_g  => fifo_impl_g,
      fifo_depth_g => C_PAD_EVENT_DEPTH,
      data_width_g => C_PAD_EVENT_WIDTH,
      fwft_g       => true
      )
    port map (
      clk_i           => clk_sys_i,
      rst_i           => rst_sys_i,
      din_i           => pad_event_din,
      wr_en_i         => pad_event_wr_en,
      full_o          => pad_event_full,
      almost_full_o   => open,
      prog_full_o     => open,
      overflow_o      => open,
      rd_en_i         => pad_event_rd_en,
      dout_o          => pad_event_dout,
      empty_o         => pad_event_empty,
      almost_empty_o  => open,
      prog_empty_o    => open,
      underflow_o     => open,
      data_valid_o    => open,
      data_cnt_o      => open,
      rst_busy_o      => open
      );

  -- Unpack FIFO head.
  event_threshold_s <= unsigned(pad_event_dout(C_PAD_EVENT_WIDTH-1 downto C_PAD_COUNT_W));
  event_pad_count_s <= unsigned(pad_event_dout(C_PAD_COUNT_W-1 downto 0));

  -- Pop condition: NORMAL state (auto_drain=0), event available, and
  -- the emit_count has reached the head's threshold. Pop the event
  -- and arm the drain counter at the same edge.
  event_ready_to_pop <= '1' when (auto_drain_r = 0
                                  and pad_event_empty = '0'
                                  and emit_count_r = event_threshold_s)
                        else '0';
  pad_event_rd_en <= event_ready_to_pop;

  -- An internal rd_en pulse during pad drain advances the DOWN FIFO
  -- silently. Only AFTER auto_drain_r is registered (=event_pad_count) —
  -- on the pop cycle itself, event_ready_to_pop merely gates the
  -- user's rd_en without advancing the FIFO yet (the user's would-be
  -- "next real" emission is actually pad_1 of the upcoming drain).
  internal_drain_rd <= '1' when (auto_drain_r /= 0 and outIsEmpty = '0') else '0';

  -- Effective rd_en into the xpm:
  --   * draining     → internal_drain_rd pulses 1/cycle.
  --   * pop cycle    → suppress completely (auto_drain_r is about to
  --                    arm; consuming now would skip a pad).
  --   * normal       → pass through user's rd_en.
  fifo_out_rden_eff <= internal_drain_rd when auto_drain_r /= 0
                       else '0'           when event_ready_to_pop = '1'
                       else fifo_out_rden_i;

  -- A user-visible emission only counts when the DOWN FIFO produced a
  -- real (non-pad) word. The pop cycle itself does NOT count — the
  -- emission that the user "would have made" this cycle is actually
  -- pad_1, which the drain FSM will consume silently.
  real_emit_pulse <= '1' when (auto_drain_r = 0
                                and event_ready_to_pop = '0'
                                and outIsEmpty = '0'
                                and fifo_out_rden_i = '1')
                     else '0';

  drain_fsm_proc : process(clk_sys_i) is
  begin
    if rising_edge(clk_sys_i) then
      if rst_sys_i = '1' then
        emit_count_r <= (others => '0');
        auto_drain_r <= (others => '0');
      else
        if event_ready_to_pop = '1' then
          auto_drain_r <= event_pad_count_s;
        elsif internal_drain_rd = '1' then
          auto_drain_r <= auto_drain_r - 1;
        end if;
        if real_emit_pulse = '1' then
          emit_count_r <= emit_count_r + 1;
        end if;
      end if;
    end if;
  end process;

  -- The user sees "empty" whenever the DOWN FIFO is truly empty OR a
  -- pop is firing this cycle (next emission is a pad) OR we are
  -- mid-drain.
  fifo_out_empty_o <= '1' when (auto_drain_r /= 0 or event_ready_to_pop = '1')
                      else outIsEmpty;

end rtl;
