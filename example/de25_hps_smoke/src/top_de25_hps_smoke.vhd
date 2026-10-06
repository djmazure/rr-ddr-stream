-- top_de25_hps_smoke — DE25-Standard (Agilex 5) minimal HPS + heartbeat-LED.
--
-- The BENCH_RUNBOOK CHILD-2 step-1/2 interim contract: bring the Agilex 5 HPS
-- subsystem up with the SMALLEST possible fabric, gated by the Reset Release IP,
-- and prove the design configured + the fabric clock is alive with a
-- free-running heartbeat LED. This is the HPS analog of de25_led_smoke
-- (pure-fabric blinky) plus the declared, rr-generated HPS hard-IP subsystem.
--
-- The HPS subsystem is a VENDOR IP — DECLARED in ip.yml and rr OWNS its qsys
-- generation (RTL-P3.682, sdk/engine/hps_preset.py → qsys-generate). This top
-- instantiates the rr-generated `hps_subsys` wrapper. Its generated top-level
-- port contract (validated 2026-06-04, Quartus Pro 25.3) is exactly three ports:
--
--     fabric_clk_clk        : in  std_logic   -- fabric clock-bridge input
--     fabric_reset_reset    : in  std_logic   -- fabric reset-bridge input (active-high)
--     ninit_done_ninit_done : out std_logic   -- Reset Release IP output
--
-- *** Heartbeat clock provenance (READ THIS). *** The generated flat
-- `hps_subsys` does NOT export any HPS-sourced free-running clock (no
-- f2h_free_clk / emif_usr_clk at the system top): the HPS hard-IP bridge clock
-- interfaces only materialise when the HPS is composed into a re-exporting
-- sub-system (the heavier GHRD flow this minimal milestone deliberately does not
-- adopt — see the note in sdk/engine/hps_preset.render_hps_qsys_script). So the
-- heartbeat here is driven from the board oscillator CLOCK2_50 (the proven
-- de25_led_smoke clock, pin BM71, LVCMOS12), NOT from an HPS clock. Consequence:
-- a toggling heartbeat LED on this build proves CONFIGURATION + FABRIC-CLOCK + the
-- Reset Release gate are alive — it does NOT prove the HPS clock manager / CPU
-- came out of reset. Proving HPS-alive needs an exported HPS clock, which needs
-- the composed-subsystem HPS flow (follow-up ticket).
--
-- *** Heartbeat LED pin (READ THIS). *** The board's HPS_LED (pin Y127, LVCMOS18)
-- is in the **HPS-dedicated I/O bank** — it is owned by the HPS hard IP, so
-- FABRIC logic CANNOT drive it (the Fitter rejects it: "Error 175019: Illegal
-- constraint of I/O pad to location PIN_Y127 … no routing connectivity between
-- source I/O output buffer and the I/O pad"). Lighting HPS_LED would require an
-- HPS GPIO/peripheral driving it under software, which is out of scope for this
-- fabric-only platform-bring-up smoke. So the heartbeat is routed to a
-- FABRIC-accessible user LED — LEDR[0] (pin CC71, LVCMOS12) — exactly the kind of
-- pin the de25_led_smoke canary proved drivable. (RTL-P3.682f.)
--
-- What the Reset Release IP buys us (RTL-P3.682c): Agilex 5 requires the Reset
-- Release Intel FPGA IP for a successful functional configuration (else Critical
-- Warning 20759 / dead fabric). It is instantiated INSIDE the rr-generated
-- hps_subsys and its `ninit_done` is exported here. `ninit_done` is HIGH while
-- the SDM is still initialising the device and deasserts LOW once configuration
-- completes; we (a) drive the HPS fabric reset bridge from it and (b) hold the
-- heartbeat counter in reset until it clears. So the LED only begins toggling
-- AFTER the device reports config-complete — a stronger, honest status contract
-- than a free-running blink.

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity top_de25_hps_smoke is
  port (
    -- Board oscillator (CLOCK2_50, LVCMOS12, pin BM71) — see de25_led_smoke for
    -- why CLOCK0_50/LVCMOS33 is rejected on Agilex 5 (RTL-P3.675).
    CLOCK2_50 : in  std_logic;
    -- Heartbeat LED on a FABRIC-driveable user LED (LEDR[0], pin CC71, LVCMOS12).
    -- NOT HPS_LED (Y127) — that pin is in the HPS-dedicated I/O bank and cannot
    -- be driven by fabric logic. Visibly toggling => configured + fabric clock
    -- alive + the Reset Release IP has released (ninit_done deasserted).
    LEDR      : out std_logic_vector(0 downto 0)
  );
end entity top_de25_hps_smoke;

architecture rtl of top_de25_hps_smoke is

  -- rr-generated Agilex 5 HPS subsystem (qsys-generate, RTL-P3.682). Port list
  -- transcribed from hps_subsys/hps_subsys_inst.vhd — DO NOT hand-edit beyond
  -- mirroring what rr emits.
  component hps_subsys is
    port (
      fabric_clk_clk        : in  std_logic := 'X'; -- clk
      fabric_reset_reset    : in  std_logic := 'X'; -- reset (active-high)
      ninit_done_ninit_done : out std_logic         -- Reset Release IP output
    );
  end component hps_subsys;

  -- 50 MHz / 2^25 ≈ 1.5 Hz heartbeat on cnt(25).
  signal cnt        : unsigned(25 downto 0) := (others => '0');
  signal ninit_done : std_logic;
  -- Active-high "still configuring" → drives the HPS fabric reset bridge and
  -- holds the heartbeat in reset until configuration completes.
  signal config_busy : std_logic;

begin

  -- The rr-generated HPS subsystem. fabric_clk is the board oscillator; the
  -- fabric reset bridge is held asserted (active-high) until ninit_done clears.
  u_hps : component hps_subsys
    port map (
      fabric_clk_clk        => CLOCK2_50,
      fabric_reset_reset    => config_busy,
      ninit_done_ninit_done => ninit_done
    );

  config_busy <= ninit_done;  -- ninit_done HIGH == device still initialising

  -- Heartbeat: free-running once configuration completes (ninit_done low). Held
  -- at 0 while config_busy so the LED is dark until the Reset Release fires,
  -- then visibly toggles — proving config + fabric clock + reset-release.
  process (CLOCK2_50)
  begin
    if rising_edge(CLOCK2_50) then
      if config_busy = '1' then
        cnt <= (others => '0');
      else
        cnt <= cnt + 1;
      end if;
    end if;
  end process;

  LEDR(0) <= cnt(25);

end architecture rtl;
