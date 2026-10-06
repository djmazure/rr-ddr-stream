-------------------------------------------------------------------------------
-- Title      : avmm read master — Avalon-MM peer of axi4r.vhd
-- Project    : rr-ddr-stream
-------------------------------------------------------------------------------
-- File       : avmm_r.vhd
-- Author     : Daniel J. Mazure <dasjimaz@gmail.com>
-- Standard   : VHDL'93/02 (compatible with NVC --std=2008)
-------------------------------------------------------------------------------
-- Description:
--   Avalon-MM read master mirroring axi4r.vhd's IDLE -> ADDR_ACK ->
--   DATA_READ shape. Issues one Avalon burst-read per cmd_valid_i pulse
--   (BURSTCOUNT = cmd_rlen+1, ADDRESS held), then drains BURSTCOUNT
--   readdatavalid pulses into the downstream FIFO via cmd_wren_o /
--   cmd_rdata_o.
--
--   True Avalon-MM burst reads (vs avmm_w's single-beat writes): the
--   v0.1 cocotb AvmmRam BFM (sdk/cocotb_helpers/avmm_ram.py) supports
--   burst reads end-to-end via its in-order _PendingRead queue and an
--   internal address bump per response beat. Real Intel EMIF / Hard
--   Memory Controller slaves prefer bursts for read efficiency, so
--   pipelining the address phase here is a win on both BFM and silicon.
-------------------------------------------------------------------------------

library IEEE;
use IEEE.STD_LOGIC_1164.all;
use IEEE.NUMERIC_STD.all;
use IEEE.MATH_REAL.all;
library rr;
use rr.ddr_stream_pkg.all;

entity avmm_r is
  generic (
    ddr_data_width_g : integer                                 := 128;
    ddr_addr_width_g : integer                                 := 32;  -- 4G
    buffer_width_g   : integer                                 := 16
  );
  port (
    -- Burst Intf
    cmd_valid_i  : in  std_logic;
    cmd_rlen_i   : in  std_logic_vector(7 downto 0);
    cmd_araddr_i : in  std_logic_vector(ddr_addr_width_g - 1 downto 0);
    cmd_rdata_o  : out std_logic_vector(ddr_data_width_g - 1 downto 0);
    cmd_wren_o   : out std_logic;        -- Push read data into downstream FIFO
    cmd_ready_o  : out std_logic;
    -- DDR-P3.9 (2026-05-18): runtime base address — see axi4r.vhd.
    ddr_base_addr_i : in std_logic_vector(ddr_addr_width_g - 1 downto 0);
    -- DDR-P3.11 (2026-08-23): pointer-reset pulse — see axi4w.vhd. Clears the
    -- OFFSET tracker (ddr_addr_r) to 0. Gated to fsm_r=IDLE by the TOP.
    ptr_reset_i : in std_logic := '0';
    ---------------------------------------------------------------------------
    -- Avalon-MM master interface (read-only sub-record).
    ---------------------------------------------------------------------------
    aclk_i  : in std_logic;
    arstn_i : in std_logic;
    h2c_o : out avmm_read_h2c_t;
    c2h_i : in  avmm_read_c2h_t;
    --
    bus_rerror_o  : out std_logic;
    ddr_pointer_o : out std_logic_vector(ddr_addr_width_g - 1 downto 0)
  );
end avmm_r;

architecture RTL of avmm_r is


  -- DDR-P3.9 (was RR--P3.4 constant): runtime base address.
  signal ddr_base_addr_c : unsigned(ddr_addr_width_g - 1 downto 0);
  -----------------------------------------------------------------------------
  -- Constants
  -----------------------------------------------------------------------------
  constant C_BEAT_BYTES : integer := ddr_data_width_g/8;
  constant C_DDR_BUFFER_SIZE : unsigned(ddr_addr_width_g downto 0) :=
    shift_left(to_unsigned(1, ddr_addr_width_g + 1), ddr_addr_width_g);

  -----------------------------------------------------------------------------
  -- FSM mirrors axi4r: IDLE → ADDR_ACK (read+address asserted, wait for
  -- waitrequest=0 to commit) → DATA_READ (drain readdatavalid pulses
  -- into the downstream FIFO).
  -----------------------------------------------------------------------------
  type fsm_t is (IDLE, ADDR_ACK, DATA_READ);
  signal fsm_r, fsm_n : fsm_t := IDLE;

  -----------------------------------------------------------------------------
  -- Address pointer + beat counter.
  -----------------------------------------------------------------------------
  signal ddr_addr_r, ddr_addr_n : unsigned(ddr_addr_width_g - 1 downto 0) :=
    (others => '0');
  signal beats_left_r, beats_left_n : unsigned(8 downto 0) := (others => '0');
  signal burstcount_r, burstcount_n : unsigned(C_AVMM_BURSTCOUNT_WIDTH-1 downto 0) :=
    (others => '0');

  -----------------------------------------------------------------------------
  -- Outputs
  -----------------------------------------------------------------------------
  signal read_req_r,  read_req_n  : std_logic := '0';
  signal read_idle_r, read_idle_n : std_logic := '1';
  signal cmd_wren_n               : std_logic;
  signal rdata_n                  : std_logic_vector(ddr_data_width_g - 1 downto 0);
  signal cmd_wren_r               : std_logic := '0';
  signal rdata_r                  : std_logic_vector(ddr_data_width_g - 1 downto 0) :=
    (others => '0');

begin

  -- DDR-P3.9: present the runtime base address as unsigned.
  ddr_base_addr_c <= unsigned(ddr_base_addr_i);

  -----------------------------------------------------------------------------
  -- Next-state logic.
  -----------------------------------------------------------------------------
  FSM_NSL : process (all)
  begin
    fsm_n         <= fsm_r;
    ddr_addr_n    <= ddr_addr_r;
    beats_left_n  <= beats_left_r;
    burstcount_n  <= burstcount_r;
    read_req_n    <= read_req_r;
    read_idle_n   <= '0';
    cmd_wren_n    <= '0';
    rdata_n       <= (others => '0');

    case fsm_r is
      when IDLE =>
        read_idle_n <= '1';
        read_req_n  <= '0';
        if cmd_valid_i = '1' then
          -- DDR-P3.9: ddr_addr_r holds offset, AVMM ADDRESS adds base
          -- combinationally at the bus output.
          ddr_addr_n   <= unsigned(cmd_araddr_i);
          beats_left_n <= resize(unsigned(cmd_rlen_i), beats_left_n'length) + 1;
          burstcount_n <= resize(unsigned(cmd_rlen_i), burstcount_n'length) + 1;
          read_req_n   <= '1';
          read_idle_n  <= '0';
          fsm_n        <= ADDR_ACK;
        end if;

      when ADDR_ACK =>
        -- Read + address + burstcount asserted. Slave commits on the cycle
        -- waitrequest='0'. After commit, deassert read and wait for the
        -- response beats to roll in.
        if c2h_i.WAITREQUEST = '0' then
          read_req_n <= '0';
          fsm_n      <= DATA_READ;
        end if;

      when DATA_READ =>
        -- Drain readdatavalid pulses into the downstream FIFO. The slave
        -- guarantees in-order completion (AVL-MM-RDVALID-002), so pop one
        -- beat per pulse and bump the address pointer for the NEXT burst.
        if c2h_i.READDATAVALID = '1' then
          -- RR--P3.6: take only the lower ddr_data_width_g bits.
          rdata_n      <= c2h_i.READDATA(ddr_data_width_g - 1 downto 0);
          cmd_wren_n   <= '1';
          beats_left_n <= beats_left_r - 1;
          -- DDR-P3.9: per-beat offset increment; wrap to offset 0.
          ddr_addr_n   <= ddr_addr_r + C_BEAT_BYTES;
          if ddr_addr_r = C_DDR_BUFFER_SIZE - C_BEAT_BYTES then
            ddr_addr_n <= (others => '0');
          end if;
          if beats_left_r = 1 then
            -- Last beat of the burst — return to IDLE to accept next cmd.
            fsm_n <= IDLE;
          end if;
        end if;

      when others =>
        fsm_n <= IDLE;
    end case;

    -- DDR-P3.11: pointer-reset override, gated to IDLE — see axi4w.vhd for
    -- the priority-over-cmd_valid_i rationale.
    if ptr_reset_i = '1' and fsm_r = IDLE then
      ddr_addr_n <= (others => '0');
    end if;
  end process;

  -----------------------------------------------------------------------------
  -- Registered state.
  -----------------------------------------------------------------------------
  FSM_REG : process (aclk_i)
  begin
    if rising_edge(aclk_i) then
      if arstn_i = '0' then
        fsm_r        <= IDLE;
        -- DDR-P3.9: offset-based, reset to 0.
        ddr_addr_r   <= (others => '0');
        beats_left_r <= (others => '0');
        burstcount_r <= (others => '0');
        read_req_r   <= '0';
        read_idle_r  <= '1';
        cmd_wren_r   <= '0';
        rdata_r      <= (others => '0');
      else
        fsm_r        <= fsm_n;
        ddr_addr_r   <= ddr_addr_n;
        beats_left_r <= beats_left_n;
        burstcount_r <= burstcount_n;
        read_req_r   <= read_req_n;
        read_idle_r  <= read_idle_n;
        cmd_wren_r   <= cmd_wren_n;
        rdata_r      <= rdata_n;
      end if;
    end if;
  end process;

  -----------------------------------------------------------------------------
  -- Outputs. ADDRESS and BURSTCOUNT are gated on read_req_r so the TOP's
  -- OR-mux against avmm_w stays collision-free when this sub-master is
  -- not asserting a request.
  -----------------------------------------------------------------------------
  -- DDR-P3.9: AVMM ADDRESS is offset + base.
  h2c_o.ADDRESS    <= std_logic_vector(ddr_addr_r + ddr_base_addr_c) when read_req_r = '1'
                      else (others => '0');
  h2c_o.READ       <= read_req_r;
  h2c_o.BURSTCOUNT <= std_logic_vector(burstcount_r) when read_req_r = '1'
                      else (others => '0');

  -- Downstream FIFO.
  cmd_wren_o    <= cmd_wren_r;
  cmd_rdata_o   <= rdata_r;
  cmd_ready_o   <= read_idle_r;
  -- Avalon-MM has no per-transfer error response; tied low for AXI parity.
  bus_rerror_o  <= '0';
  ddr_pointer_o <= std_logic_vector(ddr_addr_r);

end RTL;
