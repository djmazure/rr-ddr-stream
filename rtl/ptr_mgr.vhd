
-------------------------------------------------------------------------------
-- Title      : ptr_mgr
-- Project    : 
-------------------------------------------------------------------------------
-- File       : ptr_mgr.vhd
-- Author     : Daniel  <dasjimaz@gmail.com>
-- Author     : Daniel J. Mazure
-- Created    : 2024-08-16
-- Last update: 2024-10-19
-- Platform   : 
-- Standard   : VHDL 2008
-------------------------------------------------------------------------------
-- Description: Receives a signal and counts wheter if it goes high, low or both
-------------------------------------------------------------------------------
-- Copyright (c) 2024 
-------------------------------------------------------------------------------
-- Revisions  :
-- Date        Version  Author  Description
-- 2024-08-12  1.0      dmazure Created
-------------------------------------------------------------------------------

library IEEE;
use IEEE.std_logic_1164.all;
use IEEE.numeric_std.all;

entity ptr_mgr is
  generic (
    ptr_width_g : integer := 5;
    ptr_size_g  : integer := 16; -- AXI Burst Size in bytes
    prog_full_g : integer := 2
  );
  port (
    clk_i          : in std_logic;
    rst_i          : in std_logic;
    in_ptr_wr_i    : in std_logic_vector(ptr_width_g - 1 downto 0);
    in_ptr_rd_i    : in std_logic_vector(ptr_width_g - 1 downto 0);
    empty_o        : out std_logic;
    full_o         : out std_logic;
    almost_empty_o : out std_logic := '0';
    almost_full_o  : out std_logic := '0';
    diff_o         : out unsigned(ptr_width_g - 1 downto 0)
  );
end entity ptr_mgr;

architecture RTL of ptr_mgr is

  -- = 2^ptr_width_g expressed as a (ptr_width_g+1)-bit unsigned.
  -- Original idiom `(ptr_width_g => '1', others => '0')` rejected by NVC
  -- --std=2008 (generic-indexed aggregate choices aren't locally static).
  constant C_BUFFER_SIZE      : unsigned(ptr_width_g downto 0) := shift_left(to_unsigned(1, ptr_width_g + 1), ptr_width_g);
  signal A_int                : unsigned(ptr_width_g - 1 downto 0)     := (others => '0');
  signal B_int                : unsigned(ptr_width_g - 1 downto 0)     := (others => '0');
  signal res_int              : signed(ptr_width_g - 1 + 1 downto 0)   := (others => '0');
  signal diff_int             : unsigned(res_int'range);
  signal full_r               : std_logic := '0';
  signal almostFull_r         : std_logic := '0';
  signal full_int             : std_logic;
  signal empty_r              : std_logic := '1';
  signal almostEmpty_r        : std_logic := '1';
  signal empty_int            : std_logic;
  signal prog_full_int        : std_logic; 

begin

  A_int <= unsigned(in_ptr_wr_i);
  B_int <= unsigned(in_ptr_rd_i);

  -- This substraction can yield 0 both for full_o and empty_o
  res_int <= signed(('0' & A_int) - ('0' & B_int));

  process (all)
  begin
    if res_int(res_int'length - 1) = '1' then -- negative value
      diff_int <= unsigned(res_int(diff_o'range)) + C_BUFFER_SIZE;
    else
      diff_int <= unsigned(res_int);
    end if;
  end process;

  process(clk_i)
  begin
    if rising_edge(clk_i) then
      empty_r <= '0'; -- default
      full_r  <= '0';
      if diff_int(diff_o'range) /= 0 then
        almostEmpty_r <= empty_int;
        almostFull_r  <= full_int or prog_full_int;
      end if;       
      if diff_int(diff_o'range) = 0 and almostEmpty_r = '1' then
          empty_r <= '1';
          full_r  <= '0';    
      elsif diff_int(diff_o'range) = 0 and almostFull_r = '1' then
          empty_r <= '0';
          full_r  <= '1';
      elsif diff_int(diff_o'range) = 0 then 
          -- assume empty. This can happen on reset
          empty_r <= '1';
          full_r  <= '0';
      end if;
      if rst_i = '1' then
        empty_r       <= '1';
        almostEmpty_r <= '1';
        full_r        <= '0';
        almostFull_r  <= '0';
      end if;
    end if;
  end process;

  prog_full_int <= '1' when A_int + prog_full_g * ptr_size_g = B_int else
    '0'; 

  full_int <= '1' when A_int + ptr_size_g = B_int else
    '0';
  empty_int <= '1' when B_int + ptr_size_g = A_int else
    '0';

  diff_o <= diff_int(diff_o'range) when full_int = '0' else
    (others => '1');

  empty_o        <= empty_r;
  full_o         <= full_r;
  almost_empty_o <= almostEmpty_r or empty_r;
  almost_full_o  <= almostFull_r or full_r;

end architecture RTL;
