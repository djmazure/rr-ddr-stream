-------------------------------------------------------------------------------
-- Title      : burstw_setter
-- Project    :
-------------------------------------------------------------------------------
-- File       : burstw_setter.vhd
-- Author     :   <daniel@RigTich.com>
-- Company    :
-- Created    : 2024-05-29
-- Last update: 2024-10-21
-- Platform   :
-- Standard   : VHDL'08
-------------------------------------------------------------------------------
-- Description: calculates the right size of the burst, depending on the
-- distance of the ddr pointer to different boundaries, and the number of
-- current words in the upstream FIFO.
-------------------------------------------------------------------------------
-- Copyright (c) 2024
-------------------------------------------------------------------------------
-- Revisions  :
-- Date        Version  Author  Description
-- 2024-05-29  1.0      dmazure Created
-------------------------------------------------------------------------------
library IEEE;
use IEEE.STD_LOGIC_1164.all;
use IEEE.NUMERIC_STD.all;
use IEEE.MATH_REAL.all;
use ieee.std_logic_textio.all;
use std.textio.all;
library rr;
use rr.functions_pkg.all;

entity burstw_setter is
  generic (
    ddr_data_width_g : integer := 512;
    ddr_addr_width_g : integer := 32; -- 4G
    buffer_width_g   : integer := 16;
    fifo_depth_g     : integer := 12; -- 4096 words
    -- VFIFO-T2.6 / IDP-T2.5: max AXI write-burst length in BEATS. AXI4 = 256
    -- (AWLEN is 8 bits, 0..255); an AXI3 target (e.g. Zynq-7 S_AXI_HP, AWLEN
    -- 4-bit, 0..15) MUST set 16 — else the >16-beat bursts emitted when the
    -- egress FIFO accumulates are illegal for the slave, and the AXI4->AXI3
    -- SmartConnect's burst-split-drop loses data. Default 256 = AXI4 no-op.
    max_burst_len_g  : integer := 256
  );
  port (
    ---------------------------------------------------------------------------
    -- Burst-sizing ports only: consume the upstream FIFO word count (fifo_cnt_i)
    -- and emit cmd_valid/cmd_len. The FIFO read + DDR write are done by axi4w.
    ---------------------------------------------------------------------------
    clk_ddr_i    : in std_logic;
    rst_ddr_i    : in std_logic;
    cs_i         : in std_logic;
    fifo_cnt_i   : in std_logic_vector(fifo_depth_g - 1 downto 0);
    fifo_empty_i : in std_logic;
    ptr_write_i  : in std_logic_vector(ddr_addr_width_g - 1 downto 0); -- Req Addr
    ptr_read_i   : in std_logic_vector(ddr_addr_width_g - 1 downto 0); -- Read ptr
    bus_ready_i  : in std_logic; -- AXIW is ready to transfer
    cmd_valid_o  : out std_logic;
    cmd_len_o    : out std_logic_vector(7 downto 0)
  );
end burstw_setter;

architecture RTL of burstw_setter is

  constant C_MIN_BURST_NBITS : integer := 4;
  constant C_MIN_BURST       : integer := 2 ** C_MIN_BURST_NBITS - 1;
  constant C_BYTES_PER_BEAT  : integer := ddr_data_width_g/8;
  -- Transforms "byte" distance into "burst" distance
  constant C_BYTES_BEAT_NBITS : integer := integer(ceil(log2(real(C_BYTES_PER_BEAT))));

  constant C_MAX_BURST_LENGTH   : integer               := 256; -- AXI4 max burst length (AWLEN is 8 bits, 256)
  constant C_MAX_LEN_WIDTH      : integer               := integer(ceil(log2(real(C_MAX_BURST_LENGTH))));
  constant C_AXI4_4KBOUND_NBITS : integer               := 12;
  constant C_AXI4_4K_VAL        : unsigned(12 downto 0) := to_unsigned(4096, 13); -- x"1000";
  constant C_BUFFER_SIZE        : unsigned(buffer_width_g - 1 downto 0) := (others => '1');


  type fsm_burst_t is (IDLE, WAIT_LATCH, SET_TRANSFER, ACK_TRANSFER);
  signal fsm_burst_r : fsm_burst_t := IDLE;
  signal fsm_burst_n : fsm_burst_t;
  -------------------------------------------------------------------------------
  -- DDR Pointers
  -------------------------------------------------------------------------------
  signal axi4kBoundayDistance : unsigned(C_AXI4_4KBOUND_NBITS - 1 + 1 downto 0) := (others => '0');
  signal axi4kBoundaryBurst   : unsigned(C_MAX_LEN_WIDTH - 1 downto 0)          := (others => '0');
  signal PtrFullDistance      : unsigned(buffer_width_g - 1 downto 0)           := (others => '0');
  signal PtrFullBurst         : unsigned(C_MAX_LEN_WIDTH - 1 downto 0)          := (others => '0');
  signal PtrFullBurst_r       : unsigned(C_MAX_LEN_WIDTH - 1 downto 0)          := (others => '0');
  signal PtrFreeBytes         : unsigned(buffer_width_g - 1 downto 0)           := (others => '0');
  signal PtrNoBeat_r          : std_logic                                       := '0';
  signal ddrCnt               : unsigned(buffer_width_g - 1 downto 0)           := (others => '0');

  signal ddrFull              : std_logic;
  --
  signal latch_data_cnt_s3_r : std_logic := '0';
  signal latch_data_cnt_s2_r : std_logic := '0';
  signal latch_data_cnt_s1_r : std_logic := '0';
  signal latch_data_cnt_n    : std_logic;
  --
  signal cnt_wait_burst_r : unsigned(C_MIN_BURST_NBITS - 1 downto 0) := (others => '0');
  signal cnt_wait_burst_n : unsigned(C_MIN_BURST_NBITS - 1 downto 0);
  --
  signal fifoDataCnt        : unsigned(fifo_depth_g - 1 downto 0) := (others => '0');
  signal fifo_data_cnt_s1_r : unsigned(fifo_depth_g - 1 downto 0) := (others => '0');
  signal fifo_data_cnt_s2_r : unsigned(fifo_depth_g - 1 downto 0) := (others => '0');
  signal fifo_data_cnt_s3_r : unsigned(fifo_depth_g - 1 downto 0) := (others => '0');
  --
  signal cmd_valid_r : std_logic := '0';
  signal cmd_valid_n : std_logic;
  signal cmd_len_r   : std_logic_vector(7 downto 0) := (others => '0');
  signal cmd_len_n   : std_logic_vector(7 downto 0);

begin

  fifoDataCnt <= unsigned(fifo_cnt_i);
  --
  axi4kBoundayDistance <= C_AXI4_4K_VAL - unsigned(ptr_write_i(C_AXI4_4KBOUND_NBITS - 1 downto 0));
  axi4kBoundaryBurst   <= get_burst_from_distance(axi4kBoundayDistance, C_BYTES_PER_BEAT);
  --PtrFullDistance      <= signed(unsigned(ptr_write_i) - unsigned(ptr_read_i)) when DDR_EMPTY = '0' else (others => '1');
  -- The burst is either the remaining distance to the 4K boundary or the maximum burst length if there is any active bit above the 4K boundary

  i_ptr_mgr : entity rr.ptr_mgr
    generic map(
      ptr_width_g => buffer_width_g,
      ptr_size_g  => C_BYTES_PER_BEAT
    )
    port map
    (
      clk_i       => clk_ddr_i,
      rst_i       => rst_ddr_i,
      in_ptr_wr_i => ptr_write_i(buffer_width_g - 1 downto 0),
      in_ptr_rd_i => ptr_read_i(buffer_width_g - 1 downto 0),
      empty_o     => open,
      full_o      => ddrFull,
      diff_o      => ddrCnt
    );

  PtrFullDistance <= C_BUFFER_SIZE - ddrCnt;

  PtrFullBurst <= get_burst_from_distance(PtrFullDistance, C_BYTES_PER_BEAT);
  -- DDR-T2.23: bytes from the write pointer to the read pointer. 0 is the
  -- empty ring or the full ring (ddrFull tells them apart).
  PtrFreeBytes <= unsigned(ptr_read_i(buffer_width_g - 1 downto 0)) - unsigned(ptr_write_i(buffer_width_g - 1 downto 0));
 -- This counts holds many "bursts" are left to become full
  --------------------------------------------------------------------------------------------------
  -- This process sets the size of the burst depending on the actual number of words to be sent and
  -- how far is the pointer from two boundaries, 4K and DDR max address
  --------------------------------------------------------------------------------------------------
  PROC_SET_BURST : process (clk_ddr_i)
  begin
    if rising_edge(clk_ddr_i) then
      --------------------------------------------------------------------------
      -- DDR-T2.22: register the ring's free space before the s2 clamp. The
      -- unregistered chain (ptr_mgr wr-rd diff, C_BUFFER_SIZE - diff, the burst
      -- shift, the s1 compare) was 16 logic levels from axi4w ddr_ptr_r and
      -- missed setup at 125 MHz on xc7z020. The extra cycle does not change the
      -- burst: the clamp at s2 now uses the pointers held before s1 was
      -- captured. Inside that window the write pointer cannot move (axi4w
      -- commits it on BRESP while cmd_ready_o is low, and the next commit needs
      -- this setter's command), and the read pointer only advances, so the
      -- registered free space is the same or smaller than the live one.
      --------------------------------------------------------------------------
      PtrFullBurst_r <= PtrFullBurst;
      --------------------------------------------------------------------------
      -- DDR-T2.23: less than one whole beat free. A software read pointer is a
      -- byte offset, so the consumer can free part of a beat. A write burst
      -- there overwrites bytes the reader has not consumed. Exactly one beat
      -- free (write + one beat = read) is not flagged: that beat is the ring's
      -- last slot, and writing it fills the ring (ddrFull, DDR-REQ-501).
      -- Registered with PtrFullBurst_r and checked when the latch reaches s3.
      --------------------------------------------------------------------------
      if PtrFreeBytes /= 0 and PtrFreeBytes < C_BYTES_PER_BEAT then
        PtrNoBeat_r <= '1';
      else
        PtrNoBeat_r <= '0';
      end if;
      --
      latch_data_cnt_s2_r <= latch_data_cnt_s1_r;
      latch_data_cnt_s3_r <= latch_data_cnt_s2_r;
      --
      fifo_data_cnt_s2_r <= fifo_data_cnt_s1_r;
      fifo_data_cnt_s3_r <= fifo_data_cnt_s2_r;

      --------------------------------------------------------------------------------------------
      -- Check if the amount of data to read is bigger than the maximum burst
      --------------------------------------------------------------------------------------------
      if fifo_data_cnt_s1_r > PtrFullBurst_r then
        fifo_data_cnt_s2_r <= assign_with_padding(fifo_data_cnt_s2_r, PtrFullBurst_r);
      end if;
      -----------------------------------------------------------------------
      -- If after all, data count is over MAX BURST, saturate
      -----------------------------------------------------------------------
      if fifo_data_cnt_s2_r > C_MAX_BURST_LENGTH then
        fifo_data_cnt_s3_r <= to_unsigned(C_MAX_BURST_LENGTH - 1, fifo_data_cnt_s3_r'length);
      end if;
      -----------------------------------------------------------------------
      -- VFIFO-T2.6 / IDP-T2.5: target burst-length cap. cmd_len_o (= AWLEN =
      -- beats-1) must not exceed max_burst_len_g-1, else an AXI3 slave (Zynq-7
      -- S_AXI_HP) cannot accept the burst. This is the LAST s3 assignment in
      -- the process, so it wins over the AXI4 saturation above. With the AXI4
      -- default (256) it is a no-op (s2 is already <= 255 from the saturate).
      -----------------------------------------------------------------------
      if fifo_data_cnt_s2_r > to_unsigned(max_burst_len_g - 1, fifo_data_cnt_s2_r'length) then
        fifo_data_cnt_s3_r <= to_unsigned(max_burst_len_g - 1, fifo_data_cnt_s3_r'length);
      end if;

      --------------------------------------------------------------------------------------------
      -- Capture the data count
      --------------------------------------------------------------------------------------------
      if latch_data_cnt_s1_r = '1' then -- latch the data count one cycle after allowing the latch. This is safe.
        -- Latch number of FIFO words reported by the FIFO (not completely accurate!) -- PG327
        fifo_data_cnt_s1_r <= fifoDataCnt-1; -- One word means one burst, and one burst is LEN=0, hence -1.
        -- It is guaranteed that the FIFO is not empty, so the -1 is safe.
        ---------------------------------------------------------------------
        -- 1) Adjust the burst to the limits of the 4k boundary.
        -- 2) Single/zero-word edge (DDR-P3.34): fifoDataCnt-1 is safe. The latch
        --    is gated by fifo_empty_i='0' (see PROC_NSL/IDLE) and the egress FIFO
        --    cannot drain before cmd_valid_o issues, so fifoDataCnt>=1 here; one
        --    word => LEN=0 (a legal 1-beat burst). A large count is bounded by the
        --    4k clamp below, never an underflow. Locked by
        --    sim/cocotb/tests/test_burstw_setter.py.
        ---------------------------------------------------------------------
        if fifoDataCnt > axi4kBoundaryBurst then
          fifo_data_cnt_s1_r <= assign_with_padding(fifo_data_cnt_s1_r, axi4kBoundaryBurst);
        end if;
      end if; -- latch
    end if; -- clk
  end process;

  --------------------------------------------------------------------------------------------------
  -- FSM to trigger the Write transfers
  --------------------------------------------------------------------------------------------------
  PROC_NSL : process (all)
  begin
    -- default
    fsm_burst_n        <= fsm_burst_r;
    latch_data_cnt_n   <= '0';
    cnt_wait_burst_n   <= (others => '0');
    cmd_valid_n        <= '0';
    cmd_len_n          <= (others => '0');
    --
    case fsm_burst_r is
      when IDLE =>
        if cs_i = '1' and ddrFull = '0' and bus_ready_i = '1' and fifo_empty_i = '0' then
          if (fifoDataCnt > C_MIN_BURST or cnt_wait_burst_r = C_MIN_BURST) then
            latch_data_cnt_n <= '1';
            fsm_burst_n      <= WAIT_LATCH;
          else
            cnt_wait_burst_n <= cnt_wait_burst_r + 1;
          end if;
        end if;
      when WAIT_LATCH =>
        if latch_data_cnt_s3_r = '1' then
          if PtrNoBeat_r = '1' then
            -- DDR-T2.23: no whole beat free, issue nothing.
            fsm_burst_n <= IDLE;
          else
            fsm_burst_n <= SET_TRANSFER;
          end if;
        end if;
      when SET_TRANSFER =>
        fsm_burst_n <= ACK_TRANSFER;
        cmd_valid_n <= '1';
        cmd_len_n   <= std_logic_vector(fifo_data_cnt_s3_r(cmd_len_o'length - 1 downto 0));
      when ACK_TRANSFER =>
        if bus_ready_i = '0' then -- ready going low is a form of ACK
          -- wait for the AXIw module to receive the valid and trigger the transfer.
          -- otherwise, this FSM falsely triggers again.
          fsm_burst_n <= IDLE;
        end if;
      when others =>
        fsm_burst_n <= IDLE;
    end case;
  end process;

  -- Register the FSM signals

  PROC_REG_NSL : process (clk_ddr_i)
  begin
    if rising_edge(clk_ddr_i) then
      if rst_ddr_i = '1' then
        fsm_burst_r        <= IDLE;
        latch_data_cnt_s1_r <= '0';
        cmd_valid_r        <= '0';
      else
        fsm_burst_r        <= fsm_burst_n;
        latch_data_cnt_s1_r <= latch_data_cnt_n;
        cnt_wait_burst_r   <= cnt_wait_burst_n;
        cmd_valid_r        <= cmd_valid_n;
        cmd_len_r          <= cmd_len_n;
      end if;
    end if;
  end process;

  -----------------------------------------------------------------------------
  -- Outputs
  -----------------------------------------------------------------------------
  -- Set the write request
  cmd_valid_o <= cmd_valid_r;
  cmd_len_o   <= cmd_len_r;

  -----------------------------------------------------------------------------
  -- Assertions
  -----------------------------------------------------------------------------
  process (clk_ddr_i)
  begin
    if rising_edge(clk_ddr_i) then
      if cmd_valid_r = '1' then
        -- assert (unsigned(cmd_len_r) <= to_unsigned(C_MAX_BURST_LENGTH-1, cmd_len_r'length)) report "Burst length is over the maximum" severity error;
      end if;
    end if;
  end process;

end RTL;
