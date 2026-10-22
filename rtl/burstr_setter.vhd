-------------------------------------------------------------------------------
-- Title      : burstr_setter
-- Project    :
-------------------------------------------------------------------------------
-- File       : burstr_setter.vhd
-- Author     :   <daniel@RigTich.com>
-- Company    :
-- Created    : 2024-05-29
-- Last update: 2024-10-21
-- Platform   :
-- Standard   : VHDL'08
-------------------------------------------------------------------------------
-- Description: calculates the right size of the burst, depending on the
-- distance of the ddr pointer to different boundaries, and the space available
-- in the downstream FIFO.
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

entity burstr_setter is
  generic(
    ddr_data_width_g : integer := 512;
    ddr_addr_width_g : integer := 32;   -- 4G
    buffer_width_g   : integer := 16;
    fifo_depth_g     : integer := 12;   -- 4096 words
    -- VFIFO-T2.7 / VFIFO-T2.6 / IDP-T2.5: max AXI read-burst length in BEATS.
    -- AXI4 = 256 (ARLEN is 8 bits, 0..255); an AXI3 target (e.g. Zynq-7
    -- S_AXI_HP, ARLEN 4-bit, 0..15) MUST set 16 — else the >16-beat read
    -- bursts emitted when the down FIFO has room are illegal for the slave
    -- and the AXI4->AXI3 SmartConnect mishandles them (the read plane wedges
    -- with the readback frozen just past the cache — the silicon RD=61 stall).
    -- This is the exact read-side mirror of burstw_setter's clamp. Default
    -- 256 = AXI4 no-op.
    max_burst_len_g  : integer := 256
    );
  port (
    ---------------------------------------------------------------------------
    -- Burst-sizing ports only: consume the downstream FIFO room (fifo_room_i)
    -- and emit cmd_valid/cmd_len. The DDR read + FIFO write are done by axi4r.
    ---------------------------------------------------------------------------
    clk_ddr_i   : in  std_logic;
    rst_ddr_i   : in  std_logic;
    cs_i        : in  std_logic;          -- Chip Select, enables the reading
    fifo_room_i : in  std_logic_vector(fifo_depth_g-1 downto 0);
    fifo_full_i : in  std_logic;          -- programmable full, avoids burst if
    ptr_write_i : in  std_logic_vector(ddr_addr_width_g-1 downto 0);  -- absolute pointer
    ptr_read_i  : in  std_logic_vector(ddr_addr_width_g-1 downto 0);  -- Req Addr
    bus_ready_i : in  std_logic;          -- AXIW is ready to transfer
    cmd_valid_o : out std_logic;
    cmd_len_o   : out std_logic_vector(7 downto 0)
    );
end burstr_setter;

architecture RTL of burstr_setter is

  constant C_MIN_BURST_NBITS    : integer               := 4;
  constant C_MIN_BURST          : integer               := 2**C_MIN_BURST_NBITS-1;
  constant C_BYTES_PER_BEAT     : integer               := ddr_data_width_g/8;
  -- Transforms "byte" distance into "burst" distance
  constant C_BYTES_BEAT_NBITS   : integer               := integer(ceil(log2(real(C_BYTES_PER_BEAT))));
  constant C_MAX_BURST_LENGTH   : integer               := 256;  -- AXI4 max burst length (AWLEN is 8 bits, 256)
  constant C_MAX_LEN_WIDTH      : integer               := integer(ceil(log2(real(C_MAX_BURST_LENGTH))));
  constant C_AXI4_4KBOUND_NBITS : integer               := 12;
  constant C_AXI4_4K_VAL        : unsigned(12 downto 0) := to_unsigned(4096, 13);  -- x"1000";

  type fsm_burst_t is (IDLE, WAIT_LATCH, SET_TRANSFER, ACK_TRANSFER);
  signal fsm_burst_r : fsm_burst_t := IDLE;
  signal fsm_burst_n : fsm_burst_t;

  -------------------------------------------------------------------------------
  -- DDR Pointers
  -------------------------------------------------------------------------------
  -- +1 to ease the substracting operation
  signal axi4kBoundayDistance : unsigned(C_AXI4_4KBOUND_NBITS-1+1 downto 0)  := (others => '0');
  signal axi4kBoundaryBurst   : unsigned(C_MAX_LEN_WIDTH-1 downto 0)                 := (others => '0');
  signal PtrEmptyDistance     : unsigned(buffer_width_g-1 downto 0)                  := (others => '0');
  signal PtrEmptyBurst        : unsigned(C_MAX_LEN_WIDTH-1 downto 0)                 := (others => '0');
  signal PtrEmptyBurst_r      : unsigned(C_MAX_LEN_WIDTH-1 downto 0)                 := (others => '0');

  signal DdrEmpty        : std_logic;
  -- Data Count is in fact "beat" counting
  signal latch_data_cnt_s3_r : std_logic := '0';
  signal latch_data_cnt_s2_r : std_logic := '0';
  signal latch_data_cnt_s1_r : std_logic := '0';
  signal latch_data_cnt_n    : std_logic;
  --
  signal cnt_wait_burst_r : unsigned(C_MIN_BURST_NBITS-1 downto 0) := (others => '0');
  signal cnt_wait_burst_n : unsigned(C_MIN_BURST_NBITS-1 downto 0);
  --
  signal fifoDataRoom        : unsigned(fifo_depth_g-1 downto 0) := (others => '0');
  signal fifo_data_room_s1_r : unsigned(fifo_depth_g-1 downto 0);
  signal fifo_data_room_s2_r : unsigned(fifo_depth_g-1 downto 0);
  signal fifo_data_room_s3_r : unsigned(fifo_depth_g-1 downto 0);
  --
  signal localReset      : std_logic;
  --
  signal cmd_valid_r : std_logic := '0';
  signal cmd_valid_n : std_logic;
  signal cmd_len_r   : std_logic_vector(7 downto 0) := (others => '0');
  signal cmd_len_n   : std_logic_vector(7 downto 0);

begin


  fifoDataRoom <= unsigned(fifo_room_i);
  localReset   <= rst_ddr_i;
  -----------------------------------------------------------------------------
  -- Calculate the distance between the actual pointer and the requested read address.
  -- Then shift/divide by 32 to get the number of ddr_data_width_g slots.
  -----------------------------------------------------------------------------
  -- Distance in bytes to the next 4K (12) boundary
  axi4kBoundayDistance <= C_AXI4_4K_VAL - unsigned(ptr_read_i(C_AXI4_4KBOUND_NBITS-1 downto 0));
  axi4kBoundaryBurst   <= get_burst_from_distance(axi4kBoundayDistance, C_BYTES_PER_BEAT);

    i_ptr_mgr : entity rr.ptr_mgr
    generic map (
      ptr_width_g    => buffer_width_g,
      ptr_size_g     => C_BYTES_PER_BEAT
      )
    port map (
      clk_i        => clk_ddr_i,
      rst_i        => rst_ddr_i,
      in_ptr_wr_i  => ptr_write_i(buffer_width_g-1 downto 0),
      in_ptr_rd_i  => ptr_read_i(buffer_width_g-1 downto 0),
      empty_o      => DdrEmpty,
      full_o       => open,
      diff_o       => PtrEmptyDistance
      );

  PtrEmptyBurst <= get_burst_from_distance(unsigned(PtrEmptyDistance), C_BYTES_PER_BEAT);
 -- This counts holds many "bursts" are left to become empty

  --------------------------------------------------------------------------------------------------
  -- This process sets the size of the burst depending on the actual number of words to be sent and
  -- how far is the pointer from two boundaries, 4K and DDR max address
  --------------------------------------------------------------------------------------------------
  PROC_SET_BURST : process(clk_ddr_i)
  begin
    if rising_edge(clk_ddr_i) then
      --------------------------------------------------------------------------
      -- DDR-T2.22: register the data available in the ring before the s2
      -- clamp, as burstw_setter registers its free space. The unregistered
      -- chain (ptr_mgr wr-rd diff, the burst shift, the s1 compare) is the
      -- mechanism of DDR-P3.12's DE25 setup miss. The extra cycle does not
      -- change the burst: inside the latch window the read pointer cannot move
      -- (axi4r loads it only in IDLE on a new command, and the next command is
      -- this setter's), and the write pointer only advances, so the registered
      -- amount is the same or smaller than the live one.
      --------------------------------------------------------------------------
      PtrEmptyBurst_r <= PtrEmptyBurst;
      --
      latch_data_cnt_s2_r <= latch_data_cnt_s1_r;
      latch_data_cnt_s3_r <= latch_data_cnt_s2_r;
      --
      fifo_data_room_s2_r <= fifo_data_room_s1_r;
      fifo_data_room_s3_r <= fifo_data_room_s2_r;

      -----------------------------------------------------------------------
      -- Check if the amount of data to read is bigger than the maximum burst
      -----------------------------------------------------------------------
      if fifo_data_room_s1_r > PtrEmptyBurst_r then
        fifo_data_room_s2_r <= assign_with_padding(fifo_data_room_s2_r, PtrEmptyBurst_r);
      end if;
      -----------------------------------------------------------------------
      -- If after all, data count is over MAX BURST, saturate
      -----------------------------------------------------------------------
      if fifo_data_room_s2_r > C_MAX_BURST_LENGTH then
        fifo_data_room_s3_r <= to_unsigned(C_MAX_BURST_LENGTH-1, fifo_data_room_s3_r'length);
      end if;
      -----------------------------------------------------------------------
      -- VFIFO-T2.7 / VFIFO-T2.6 / IDP-T2.5: target read-burst-length cap.
      -- cmd_len_o (= ARLEN = beats-1) must not exceed max_burst_len_g-1, else
      -- an AXI3 slave (Zynq-7 S_AXI_HP) cannot accept the burst. This is the
      -- LAST s3 assignment in the process, so it wins over the AXI4 saturation
      -- above. With the AXI4 default (256) it is a no-op (s2 is already <= 255
      -- from the saturate). Exact read-side mirror of burstw_setter's clamp.
      -----------------------------------------------------------------------
      if fifo_data_room_s2_r > to_unsigned(max_burst_len_g - 1, fifo_data_room_s2_r'length) then
        fifo_data_room_s3_r <= to_unsigned(max_burst_len_g - 1, fifo_data_room_s3_r'length);
      end if;

      --------------------------------------------------------------------------------------------
      -- Capture the data count
      --------------------------------------------------------------------------------------------
      if latch_data_cnt_s1_r = '1' then
          -- Latch number of FIFO words stored once
          fifo_data_room_s1_r <= fifoDataRoom-1; -- One word means one burst, and one burst is LEN=0, hence -1.
          -- It is guaranteed that the FIFO is not full, so the -1 is safe.
          ---------------------------------------------------------------------
          -- 1) Adjust the burst to the limits of the 4k boundary.
          -- 2) Single/zero-word edge (DDR-P3.34): fifoDataRoom-1 is safe. The
          --    latch is gated by fifo_full_i='0' + DdrEmpty='0' (see PROC_NSL/
          --    IDLE) and the downstream FIFO cannot fill before cmd_valid_o
          --    issues, so fifoDataRoom>=1 here; one word => LEN=0 (a legal 1-beat
          --    burst). A large room is bounded by the 4k clamp below, never an
          --    underflow. Locked by sim/cocotb/tests/test_burstr_setter.py.
          ---------------------------------------------------------------------
          if fifoDataRoom > axi4kBoundaryBurst then
            fifo_data_room_s1_r <= assign_with_padding(fifo_data_room_s1_r, axi4kBoundaryBurst);
          end if;
      end if;  -- latch
    end if;  -- clk
  end process;

  --------------------------------------------------------------------------------------------------
  -- FSM to trigger the Write transfers
  --------------------------------------------------------------------------------------------------
  PROC_NSL : process(all)
  begin
    -- default
    fsm_burst_n      <= fsm_burst_r;
    latch_data_cnt_n <= '0';
    cnt_wait_burst_n <= (others => '0');
    cmd_valid_n      <= '0';
    cmd_len_n        <= (others => '0');
    --
    case fsm_burst_r is
      when IDLE =>
        if cs_i = '1' and fifo_full_i = '0' and DdrEmpty = '0' and bus_ready_i = '1' then
          if (fifoDataRoom > C_MIN_BURST or cnt_wait_burst_r = C_MIN_BURST) then
            latch_data_cnt_n <= '1';
            fsm_burst_n      <= WAIT_LATCH;
          else
            cnt_wait_burst_n <= cnt_wait_burst_r + 1;
          end if;
        end if;
      when WAIT_LATCH =>
        if latch_data_cnt_s3_r = '1' then
          fsm_burst_n <= SET_TRANSFER;
        end if;
      when SET_TRANSFER =>
        fsm_burst_n <= ACK_TRANSFER;
        cmd_valid_n <= '1';
        cmd_len_n   <= std_logic_vector(fifo_data_room_s3_r(cmd_len_o'length-1 downto 0));
      when ACK_TRANSFER =>
        if bus_ready_i = '0' then
          -- wait for the AXIr module to receive the valid and trigger the transfer.
          -- otherwise, this FSM falsely triggers again.
          fsm_burst_n <= IDLE;
        end if;
      when others =>
        fsm_burst_n <= IDLE;
    end case;
  end process;

  -- Register the FSM signals

  PROC_REG_NSL : process(clk_ddr_i)
  begin
    if rising_edge(clk_ddr_i) then
      if localReset = '1' then
        fsm_burst_r         <= IDLE;
        latch_data_cnt_s1_r <= '0';
        cmd_valid_r         <= '0';
      else
        fsm_burst_r         <= fsm_burst_n;
        latch_data_cnt_s1_r <= latch_data_cnt_n;
        cnt_wait_burst_r    <= cnt_wait_burst_n;
        cmd_valid_r         <= cmd_valid_n;
        cmd_len_r           <= cmd_len_n;
      end if;
    end if;
  end process;

  -----------------------------------------------------------------------------
  -- Outputs
  -----------------------------------------------------------------------------
  -- Set the write request
  cmd_valid_o <= cmd_valid_r;
  cmd_len_o   <= cmd_len_r;

end RTL;
