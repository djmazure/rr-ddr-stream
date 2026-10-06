-------------------------------------------------------------------------------
-- Title      : ddr_sync_cdc
-- Project    : 
-------------------------------------------------------------------------------
-- File       : ddr_sync_cdc.vhd
-- Author     : Daniel  <dasjimaz@gmail.com>
-- Author     : Daniel J. Mazure
-- Created    : 2024-08-19
-- Last update: 2024-08-20
-- Platform   : 
-- Standard   : VHDL'08
-------------------------------------------------------------------------------
-- Description: Simple CDC
-------------------------------------------------------------------------------
-- Copyright (c) 2024 
-------------------------------------------------------------------------------
-- Revisions  :
-- Date        Version  Author  Description
-- 2024-08-19  1.0      dmazure Created
-------------------------------------------------------------------------------
library IEEE;
use IEEE.STD_LOGIC_1164.all;
use IEEE.NUMERIC_STD.all;
use IEEE.std_logic_misc.all;

entity ddr_sync_cdc is
  generic (
    num_stages_g : integer := 3;
    or_reduce_g  : boolean := FALSE
    );
  port (
    clk_i : in  std_logic;
    rst_i   : in  std_logic;
    set_i   : in  std_logic;
    d_i   : in  std_logic;
    q_o   : out std_logic
    );
end ddr_sync_cdc;


architecture RTL of ddr_sync_cdc is

  signal stages_r : std_logic_vector(num_stages_g-1 downto 0) := (others => '0');

  attribute ASYNC_REG             : string;
  attribute ASYNC_REG of stages_r : signal is "TRUE";

begin

  -- DDR-T3 (2026-05-27): when or_reduce_g, q_o reduces only stages 2..N-1 (see
  -- GEN_Q). The first two stages are reserved for metastability resolution, so
  -- the OR-reduce form needs at least three stages.
  assert (not or_reduce_g) or (num_stages_g >= 3)
    report "ddr_sync_cdc: or_reduce_g=TRUE requires num_stages_g >= 3 "
         & "(stages 0..1 are reserved for metastability resolution before the OR-reduce)."
    severity failure;

  SYNC_PR : process(clk_i)
  begin
    if rising_edge(clk_i) then
      stages_r <= stages_r(num_stages_g-2 downto 0) & d_i;
      if set_i = '1' then
        stages_r <= (others => '1');
      end if;
      if rst_i = '1' then
        stages_r <= (others => '0');
      end if;
    end if;
  end process;

  GEN_Q : if or_reduce_g = TRUE generate
    -- DDR-T3 (2026-05-27): reduce ONLY the post-synchronization stages.
    -- stages_r(0) and stages_r(1) are the metastability-resolution flops
    -- (ASYNC_REG) that directly capture the asynchronous d_i. OR-ing them into
    -- q_o (the previous `or_reduce(stages_r)`) exposed a metastable value
    -- through combinational logic and defeated the synchronizer — Vivado
    -- report_cdc flagged it CDC-1 "unknown CDC circuitry" and on silicon the
    -- read-plane busy crossing glitched (DDR-T3: read-back intermittent zeros).
    -- Reducing stages 2..N-1 yields a metastability-safe "synchronize (2 FF)
    -- + stretch" in one primitive. Requires num_stages_g >= 3 (asserted above).
    q_o <= or_reduce(stages_r(num_stages_g-1 downto 2));
  else generate
    q_o <= stages_r(num_stages_g-1);
  end generate GEN_Q;

end architecture;
