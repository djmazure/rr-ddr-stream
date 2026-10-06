-- de25_led_smoke — DE25-Standard (Agilex 5) bench bring-up smoke.
--
-- Pure-fabric: the 50 MHz board oscillator drives a free-running counter; the
-- top counter bits drive the 10 user LEDs as a visible binary "scan" (LEDR[0]
-- fast, LEDR[9] slow). No HPS, no reset pin — the counter powers up at 0 via
-- the register init (Quartus uses it as the configuration power-up value).
--
-- Purpose: validate the whole bench path on real Agilex 5 — Quartus Pro
-- synth→fit→bitstream + `rr program run` → LEDs visibly alive — before the HPS
-- work (RTL-P3.682/681). Also a program-time IDCODE confirmation of the part.

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity de25_led_smoke is
  port (
    -- Use CLOCK2_50 (LVCMOS12, pin BM71): board CLOCK0_50 is LVCMOS33, which
    -- iostandards.yml maps to "3.3-V LVTTL" — NOT supported on Agilex 5 (it wants
    -- "3.3-V LVCMOS"). That's the RTL-P3.675 gap; CLOCK2_50 is 1.2 V (valid),
    -- matching the LEDR bank, so the whole design is LVCMOS12.
    CLOCK2_50 : in  std_logic;                       -- 50 MHz oscillator (1.2 V)
    LEDR      : out std_logic_vector(9 downto 0)     -- user LEDs (1.2 V)
  );
end entity de25_led_smoke;

architecture rtl of de25_led_smoke is
  -- 50 MHz / 2^27 ≈ 0.37 Hz on cnt(26); cnt(17) toggles ~190 Hz.
  signal cnt : unsigned(26 downto 0) := (others => '0');
begin
  process (CLOCK2_50)
  begin
    if rising_edge(CLOCK2_50) then
      cnt <= cnt + 1;
    end if;
  end process;

  -- Visible binary scan across the bar: LEDR(9) slowest, LEDR(0) fastest.
  LEDR <= std_logic_vector(cnt(26 downto 17));
end architecture rtl;
