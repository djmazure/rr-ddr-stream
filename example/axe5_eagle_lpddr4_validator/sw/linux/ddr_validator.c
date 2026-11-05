/*
 * ddr_validator.c - Linux runner for the fabric BIST of
 * example/axe5_eagle_lpddr4_validator.
 *
 * Maps the ddr_validator_csr block through /dev/mem, starts one BIST run and
 * prints the result. Exit code 0 = pass, 1 = fail or error.
 *
 * CSR_BASE = 0x20000000: start of the lightweight HPS-to-FPGA bridge window
 * in the Agilex 5 HPS address map (a property of the SoC, not of the board).
 *
 * Needs: the fabric configured before the HPS boots, and `bridge enable` run
 * by the boot script. Otherwise the bridge is held in reset and the first
 * access faults (SIGBUS).
 *
 * Usage:
 *   ddr_validator [--scenario N] [--n-words N]
 *   ddr_validator --help
 *
 * Scenarios (ddr_validator_bist.vhd):
 *   0  roundtrip       write n_words, read back, compare
 *   1  boundary        as 0; use a large n_words to span many bursts
 *   2  reset_recovery  resets the IP once in the middle of the write pass,
 *                      then restarts and completes a roundtrip
 *   3  pingpong        not implemented; reports done without pass or fail
 *
 * Registers:
 *   0x00  IDENT       RO  0x0DD50200
 *   0x04  CONTROL     RW  bit0 start, bit1 soft reset
 *   0x08  SCENARIO    RW  [3:0] scenario number
 *   0x0C  N_WORDS     RW  number of 256-bit words
 *   0x10  STATUS      RO  bit0 running, bit1 done, bit2 pass, bit3 fail
 *   0x14  ERROR_COUNT RO  mismatching words
 *   0x18  WORDS_WR    RO  words written
 *   0x1C  WORDS_RD    RO  words read and compared
 *   0x20  ERR_INDEX   RO  index of the first mismatch
 *   0x24  ERR_EXP_LO  RO  expected data[31:0] at the first mismatch
 *   0x28  ERR_GOT_LO  RO  read data[31:0] at the first mismatch
 */

#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

#define CSR_BASE    0x20000000UL
#define CSR_SIZE    0x1000

#define REG_IDENT       0x00
#define REG_CONTROL     0x04
#define REG_SCENARIO    0x08
#define REG_N_WORDS     0x0C
#define REG_STATUS      0x10
#define REG_ERR_COUNT   0x14
#define REG_WORDS_WR    0x18
#define REG_WORDS_RD    0x1C
#define REG_ERR_INDEX   0x20
#define REG_ERR_EXP_LO  0x24
#define REG_ERR_GOT_LO  0x28

#define IDENT_EXPECTED  0x0DD50200U

#define CTRL_START      (1u << 0)
#define CTRL_SOFT_RESET (1u << 1)

#define STATUS_RUNNING  (1u << 0)
#define STATUS_DONE     (1u << 1)
#define STATUS_PASS     (1u << 2)
#define STATUS_FAIL     (1u << 3)

static volatile uint32_t *csr_map;
static int devmem_fd = -1;

static uint32_t csr_rd(unsigned off) { return csr_map[off / 4]; }
static void     csr_wr(unsigned off, uint32_t v) { csr_map[off / 4] = v; }

static int csr_open(void)
{
    devmem_fd = open("/dev/mem", O_RDWR | O_SYNC);
    if (devmem_fd < 0) { perror("open /dev/mem"); return -1; }
    csr_map = mmap(NULL, CSR_SIZE, PROT_READ | PROT_WRITE, MAP_SHARED,
                   devmem_fd, CSR_BASE);
    if (csr_map == MAP_FAILED) { perror("mmap"); return -1; }
    return 0;
}

static void csr_close(void)
{
    if (csr_map && csr_map != MAP_FAILED) munmap((void *)csr_map, CSR_SIZE);
    if (devmem_fd >= 0) close(devmem_fd);
}

static void usage(const char *prog)
{
    printf("Usage: %s [--scenario N] [--n-words N]\n", prog);
    printf("  --scenario N   0 roundtrip, 1 boundary, 2 reset recovery, 3 pingpong (not implemented) [default 0]\n");
    printf("  --n-words N    256-bit words to BIST (default 65536 = 2 MB)\n");
}

int main(int argc, char **argv)
{
    uint32_t scenario = 0;
    uint32_t n_words  = 65536;   /* 2 MB in 256-bit words */

    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--scenario") && i + 1 < argc)
            scenario = (uint32_t)atoi(argv[++i]);
        else if (!strcmp(argv[i], "--n-words") && i + 1 < argc)
            n_words  = (uint32_t)atoi(argv[++i]);
        else if (!strcmp(argv[i], "--help") || !strcmp(argv[i], "-h")) {
            usage(argv[0]); return 0;
        } else {
            fprintf(stderr, "Unknown argument: %s\n", argv[i]);
            usage(argv[0]); return 1;
        }
    }

    if (csr_open() < 0) return 1;

    uint32_t ident = csr_rd(REG_IDENT);
    if (ident != IDENT_EXPECTED) {
        fprintf(stderr,
                "IDENT mismatch: got 0x%08X want 0x%08X\n"
                "Check: SOF loaded? Bridge enabled?\n",
                ident, IDENT_EXPECTED);
        csr_close(); return 1;
    }
    printf("IDENT  OK  0x%08X\n", ident);

    /* Soft-reset then arm */
    csr_wr(REG_CONTROL, CTRL_SOFT_RESET);
    usleep(10);
    csr_wr(REG_CONTROL, 0);
    csr_wr(REG_SCENARIO, scenario);
    csr_wr(REG_N_WORDS,  n_words);
    printf("Scenario %u, %u words (%u bytes)\n",
           scenario, n_words, n_words * 32);
    csr_wr(REG_CONTROL, CTRL_START);

    /* Poll until done */
    uint32_t status;
    while (1) {
        status = csr_rd(REG_STATUS);
        if (status & (STATUS_DONE | STATUS_FAIL)) break;
        usleep(1000);
    }

    uint32_t err_count = csr_rd(REG_ERR_COUNT);
    uint32_t words_wr  = csr_rd(REG_WORDS_WR);
    uint32_t words_rd  = csr_rd(REG_WORDS_RD);

    printf("Status      0x%08X  %s%s%s%s\n", status,
           (status & STATUS_RUNNING) ? "RUNNING " : "",
           (status & STATUS_DONE)    ? "DONE "    : "",
           (status & STATUS_PASS)    ? "PASS "    : "",
           (status & STATUS_FAIL)    ? "FAIL "    : "");
    printf("Words WR    %u\n", words_wr);
    printf("Words RD    %u\n", words_rd);
    printf("Errors      %u\n", err_count);

    if (status & STATUS_FAIL) {
        printf("First error:\n");
        printf("  Index     %u\n",       csr_rd(REG_ERR_INDEX));
        printf("  Expected  0x%08X\n",   csr_rd(REG_ERR_EXP_LO));
        printf("  Got       0x%08X\n",   csr_rd(REG_ERR_GOT_LO));
    }

    csr_close();
    return (status & STATUS_PASS) ? 0 : 1;
}
