-------------------------------------------------------------------------------
-- rr_fifo_sync_xpm_body — Intel/Altera STUB (RR--P3.3).
--
-- Provides an entity declaration for `rr_fifo_sync_xpm_body` so Quartus
-- can complete its elaboration-time entity-binding check on the
-- GEN_XPM branch of rr_fifo_sync (which Quartus statically analyses
-- even when the generate condition is false). Compiling this stub
-- INSTEAD OF the real Xilinx body lets Quartus build the wrapper
-- cleanly as long as fifo_impl_g="olo" (never elaborates the
-- xpm_fifo_sync inside).
--
-- If a consumer instantiates with fifo_impl_g="xpm" on an Intel
-- target, the assert below fires at elab. That's by design — XPM is
-- Xilinx-private.
-------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use ieee.math_real.all;

entity rr_fifo_sync_xpm_body is
  generic (
    fifo_depth_g        : positive;
    data_width_g        : positive;
    fwft_g              : boolean := true;
    prog_full_thresh_g  : natural := 0;
    prog_empty_thresh_g : natural := 0
  );
  port (
    clk_i           : in  std_logic;
    rst_i           : in  std_logic;
    din_i           : in  std_logic_vector(data_width_g-1 downto 0);
    wr_en_i         : in  std_logic;
    full_o          : out std_logic;
    almost_full_o   : out std_logic;
    prog_full_o     : out std_logic;
    overflow_o      : out std_logic;
    rd_en_i         : in  std_logic;
    dout_o          : out std_logic_vector(data_width_g-1 downto 0);
    empty_o         : out std_logic;
    almost_empty_o  : out std_logic;
    prog_empty_o    : out std_logic;
    underflow_o     : out std_logic;
    data_valid_o    : out std_logic;
    data_cnt_o      : out std_logic_vector(integer(ceil(log2(real(fifo_depth_g+1))))-1 downto 0);
    rst_busy_o      : out std_logic
  );
end entity rr_fifo_sync_xpm_body;

architecture intel_stub of rr_fifo_sync_xpm_body is
begin
  full_o         <= '1';
  almost_full_o  <= '1';
  prog_full_o    <= '1';
  overflow_o     <= '0';
  rst_busy_o     <= '1';
  dout_o         <= (others => '0');
  empty_o        <= '1';
  almost_empty_o <= '1';
  prog_empty_o   <= '1';
  data_cnt_o     <= (others => '0');
  underflow_o    <= '0';
  data_valid_o   <= '0';
  assert false
    report "rr_fifo_sync_xpm_body: XPM path is Xilinx-only; set fifo_impl_g=olo for Intel."
    severity failure;
end architecture intel_stub;
