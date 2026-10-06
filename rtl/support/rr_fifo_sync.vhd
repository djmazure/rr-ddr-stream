-------------------------------------------------------------------------------
-- Title      : rr_fifo_sync — vendor-agnostic synchronous FIFO wrapper
-- Project    : rr-ddr-stream
-------------------------------------------------------------------------------
-- File       : rr_fifo_sync.vhd
-- Author     : Daniel J. Mazure <dasjimaz@gmail.com>
-- Created    : 2026-05-15
-- Standard   : VHDL'08
-------------------------------------------------------------------------------
-- Description:
--   Synchronous FIFO with an XPM-shape slim port set so existing
--   xpm_fifo_sync consumers (cache_fifo, ddr_fifo_intf pad-event FIFO)
--   can drop in without churn. The fifo_impl_g generic picks the
--   underlying implementation:
--
--     "olo"  (default) : open-logic/olo_base_fifo_sync — Quartus +
--                        Vivado portable, no vendor primitives.
--     "xpm"            : Xilinx Parameterized Macro xpm_fifo_sync —
--                        the historical golden reference, Vivado-only.
--
--   VHDL elaboration prunes the unused branch; declaring `library xpm`
--   at file scope is harmless on Quartus when fifo_impl_g="olo" because
--   no XPM entity is instantiated.
--
--   Output flag mapping when fifo_impl_g="olo":
--     PROG_FULL    = ALMOST_FULL  = olo In_AlmFull   (threshold =
--                                   prog_full_thresh_g)
--     PROG_EMPTY   = ALMOST_EMPTY = olo Out_AlmEmpty (threshold =
--                                   prog_empty_thresh_g)
--     OVERFLOW     = synthesised: WR_EN AND FULL,  registered.
--     UNDERFLOW    = synthesised: RD_EN AND EMPTY, registered.
--     DATA_VALID   = NOT EMPTY (FWFT semantic).
--     DATA_CNT     = olo Out_Level, resized.
--
--   DDR-P3.6 (2026-05-15) — vendor-portability for the AVMM target.
-------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use ieee.math_real.all;

-- RR--T2.1: open-logic entities are referenced via their canonical
-- library `olo`, NOT `work`. Library-qualified references survive
-- cross-library consumption (this wrapper compiles into library
-- `rr`, open-logic compiles into library `olo`). The downstream
-- resolver must route open-logic into `olo`; the registry default
-- update lands in the companion routertl-ip-index change.
library olo;

-- RR--P3.3: file-scope `library xpm; use xpm.vcomponents.all;` removed.
-- The XPM body is delegated to rr_fifo_sync_xpm_body (vendor-gated
-- under rtl/support/xilinx/) so this wrapper compiles cleanly on
-- Quartus / GHDL / NVC without an `xpm` library. The wrapper still
-- offers the same fifo_impl_g = "olo" | "xpm" surface to consumers.

entity rr_fifo_sync is
  generic (
    fifo_impl_g         : string  := "olo";  -- "olo" | "xpm"
    fifo_depth_g        : positive;          -- number of words
    data_width_g        : positive;
    fwft_g              : boolean := true;   -- first-word-fall-through
    prog_full_thresh_g  : natural := 0;      -- 0 → fifo_depth_g - 1
    prog_empty_thresh_g : natural := 0       -- 0 → 1
  );
  port (
    clk_i           : in  std_logic;
    rst_i           : in  std_logic;
    -- Write side
    din_i           : in  std_logic_vector(data_width_g-1 downto 0);
    wr_en_i         : in  std_logic;
    full_o          : out std_logic;
    almost_full_o   : out std_logic;
    prog_full_o     : out std_logic;
    overflow_o      : out std_logic;
    -- Read side
    rd_en_i         : in  std_logic;
    dout_o          : out std_logic_vector(data_width_g-1 downto 0);
    empty_o         : out std_logic;
    almost_empty_o  : out std_logic;
    prog_empty_o    : out std_logic;
    underflow_o     : out std_logic;
    data_valid_o    : out std_logic;
    -- Status
    data_cnt_o      : out std_logic_vector(integer(ceil(log2(real(fifo_depth_g+1))))-1 downto 0);
    rst_busy_o      : out std_logic
  );
end entity rr_fifo_sync;

architecture rtl of rr_fifo_sync is

  -- Strip surrounding double-quotes if the simulator's `-g` flag passed
  -- the string with literal quote chars (NVC behaviour as of 1.16 —
  -- routertl-side workaround pending). Lets callers pass either "olo"
  -- or olo without ambiguity.
  function strip_quotes(s : string) return string is
  begin
    if s'length >= 2 and s(s'left) = '"' and s(s'right) = '"' then
      return s(s'left+1 to s'right-1);
    end if;
    return s;
  end function;

  constant C_IMPL : string := strip_quotes(fifo_impl_g);

  -- Threshold defaults. XPM legality: PROG_*_THRESH must lie in
  -- [5, fifo_depth_g - 5]; we clamp the 0-default to that range so a
  -- caller-omitted threshold works on both backends without surprise.
  function default_full_th return natural is
  begin
    if prog_full_thresh_g = 0 then
      return fifo_depth_g - 5;
    else
      return prog_full_thresh_g;
    end if;
  end function;

  function default_empty_th return natural is
  begin
    if prog_empty_thresh_g = 0 then
      return 5;
    else
      return prog_empty_thresh_g;
    end if;
  end function;

  constant C_PROG_FULL_TH  : natural := default_full_th;
  constant C_PROG_EMPTY_TH : natural := default_empty_th;
  constant C_COUNT_WIDTH   : positive := integer(ceil(log2(real(fifo_depth_g+1))));

  function fwft_str return string is
  begin
    if fwft_g then return "fwft"; else return "std"; end if;
  end function;

begin

  -----------------------------------------------------------------------------
  -- XPM backend (Xilinx, Vivado golden reference).
  -- RR--P3.3: body delegated to rr_fifo_sync_xpm_body (vendor-gated).
  -----------------------------------------------------------------------------
  GEN_XPM : if C_IMPL = "xpm" generate
    i_xpm_body : entity work.rr_fifo_sync_xpm_body
      generic map (
        fifo_depth_g        => fifo_depth_g,
        data_width_g        => data_width_g,
        fwft_g              => fwft_g,
        prog_full_thresh_g  => prog_full_thresh_g,
        prog_empty_thresh_g => prog_empty_thresh_g
      )
      port map (
        clk_i          => clk_i,
        rst_i          => rst_i,
        din_i          => din_i,
        wr_en_i        => wr_en_i,
        full_o         => full_o,
        almost_full_o  => almost_full_o,
        prog_full_o    => prog_full_o,
        overflow_o     => overflow_o,
        rd_en_i        => rd_en_i,
        dout_o         => dout_o,
        empty_o        => empty_o,
        almost_empty_o => almost_empty_o,
        prog_empty_o   => prog_empty_o,
        underflow_o    => underflow_o,
        data_valid_o   => data_valid_o,
        data_cnt_o     => data_cnt_o,
        rst_busy_o     => rst_busy_o
      );
  end generate GEN_XPM;

  -----------------------------------------------------------------------------
  -- olo backend (open-logic, Quartus + Vivado portable). Default.
  -----------------------------------------------------------------------------
  GEN_OLO : if C_IMPL = "olo" generate
    signal full_s         : std_logic;
    signal empty_s        : std_logic;
    signal almost_full_s  : std_logic;
    signal almost_empty_s : std_logic;
    signal out_valid_s    : std_logic;
    signal out_level_s    : std_logic_vector(C_COUNT_WIDTH-1 downto 0);
    -- Synthesised XPM-style flags (registered 1 cycle to match XPM's
    -- "rejected-request-from-previous-cycle" semantic).
    signal overflow_r     : std_logic := '0';
    signal underflow_r    : std_logic := '0';
  begin
    i_olo : entity olo.olo_base_fifo_sync
      generic map (
        Width_g         => data_width_g,
        Depth_g         => fifo_depth_g,
        AlmFullOn_g     => true,
        AlmFullLevel_g  => C_PROG_FULL_TH,
        AlmEmptyOn_g    => true,
        AlmEmptyLevel_g => C_PROG_EMPTY_TH,
        RamStyle_g      => "auto",
        RamBehavior_g   => "RBW",
        ReadyRstState_g => '0'                -- mirror XPM: full while in reset
      )
      port map (
        Clk        => clk_i,
        Rst        => rst_i,
        In_Data    => din_i,
        In_Valid   => wr_en_i,
        In_Ready   => open,
        In_Level   => open,
        Out_Data   => dout_o,
        Out_Valid  => out_valid_s,
        Out_Ready  => rd_en_i,
        Out_Level  => out_level_s,
        Full       => full_s,
        Empty      => empty_s,
        AlmFull    => almost_full_s,
        AlmEmpty   => almost_empty_s
      );

    process(clk_i)
    begin
      if rising_edge(clk_i) then
        if rst_i = '1' then
          overflow_r  <= '0';
          underflow_r <= '0';
        else
          overflow_r  <= wr_en_i and full_s;
          underflow_r <= rd_en_i and empty_s;
        end if;
      end if;
    end process;

    full_o          <= full_s;
    empty_o         <= empty_s;
    almost_full_o   <= almost_full_s;
    almost_empty_o  <= almost_empty_s;
    prog_full_o     <= almost_full_s;     -- alias: olo offers one threshold/side
    prog_empty_o    <= almost_empty_s;
    overflow_o      <= overflow_r;
    underflow_o     <= underflow_r;
    data_valid_o    <= out_valid_s;
    data_cnt_o      <= out_level_s;
    -- olo has no reset-busy line; tied low (XPM's wr_rst_busy 20-40 cycle
    -- settle is unique to its DRAM-init flow).
    rst_busy_o      <= '0';
  end generate GEN_OLO;

  -----------------------------------------------------------------------------
  -- Compile-time guard: catch an invalid fifo_impl_g at elaboration so a
  -- typo doesn't silently degrade to a no-op.
  -----------------------------------------------------------------------------
  assert (C_IMPL = "olo") or (C_IMPL = "xpm")
    report "rr_fifo_sync: fifo_impl_g must be ""olo"" or ""xpm"" (got """
           & fifo_impl_g & """)"
    severity failure;

end architecture rtl;
