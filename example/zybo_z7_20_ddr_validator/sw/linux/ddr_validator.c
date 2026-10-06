// ddr_validator.c — userspace orchestrator for the Zybo Z7-20 ddr_stream_axi
// hardware validator.
//
// The data path is entirely in fabric: the PL BIST writes a counter pattern
// through the IP into PS DDR3 (S_AXI_HP0), reads it back, and compares it
// word-for-word. This app does NOT touch the payload — it only drives the
// AXI-Lite CSR (mapped at 0x4000_0000) to start a scenario and read the
// verdict registers. Because the comparison happens in PL and the payload
// never reaches the CPU, there is no cache-coherency surface on the data
// path; the CSR itself is mapped uncached via /dev/mem O_SYNC.
//
// Build (on target or cross):  ${CROSS}gcc -O2 -Wall -o ddr_validator ddr_validator.c
// Run:                         ./ddr_validator --scenario roundtrip --n 8192
// Exit code: 0 = pass, 1 = fail, 2 = skip/usage/probe error.
//
// CSR map — docs/DESIGN.md §4.

#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>
#include <errno.h>
#include <sys/mman.h>

#define CSR_BASE   0x40000000UL
#define CSR_SPAN   0x1000UL

// Register byte offsets (docs/DESIGN.md §4).
#define REG_CTRL            0x00
#define REG_STATUS          0x04
#define REG_SCENARIO        0x08
#define REG_N_WORDS         0x0C
#define REG_ERROR_COUNT     0x10
#define REG_WORDS_WRITTEN   0x14
#define REG_WORDS_READ      0x18
#define REG_FIRST_ERR_INDEX 0x1C
#define REG_FIRST_ERR_EXP   0x20
#define REG_FIRST_ERR_GOT   0x24
#define REG_WR_BASE         0x28
#define REG_RD_BASE         0x2C
#define REG_BASE_LOAD       0x30
#define REG_PTR_RESET       0x34
#define REG_IDENT           0x3C

// CTRL bits.
#define CTRL_START      (1u << 0)
#define CTRL_SOFT_RESET (1u << 1)
#define CTRL_CONTINUOUS (1u << 8)

// STATUS bits.
#define ST_RUNNING    (1u << 0)
#define ST_DONE       (1u << 1)
#define ST_PASS       (1u << 2)
#define ST_FAIL       (1u << 3)
#define ST_WRITE_IDLE (1u << 4)
#define ST_READ_IDLE  (1u << 5)

#define IDENT_MAGIC 0x0DD50200u

static volatile uint32_t *g_csr;

static inline void     reg_wr(unsigned off, uint32_t v) { g_csr[off / 4] = v; }
static inline uint32_t reg_rd(unsigned off)             { return g_csr[off / 4]; }

struct scenario { const char *name; uint32_t id; };
static const struct scenario SCENARIOS[] = {
    { "roundtrip",      0 },
    { "boundary",       1 },
    { "reset_recovery", 2 },
    { "pingpong",       3 },
};

static int parse_scenario(const char *s, uint32_t *out) {
    for (size_t i = 0; i < sizeof(SCENARIOS) / sizeof(SCENARIOS[0]); i++) {
        if (strcmp(s, SCENARIOS[i].name) == 0) { *out = SCENARIOS[i].id; return 0; }
    }
    char *end = NULL;
    unsigned long v = strtoul(s, &end, 0);
    if (end && *end == '\0' && v <= 0xF) { *out = (uint32_t)v; return 0; }
    return -1;
}

static const char *scenario_name(uint32_t id) {
    for (size_t i = 0; i < sizeof(SCENARIOS) / sizeof(SCENARIOS[0]); i++)
        if (SCENARIOS[i].id == id) return SCENARIOS[i].name;
    return "unknown";
}

static void usage(const char *argv0) {
    fprintf(stderr,
        "usage: %s [--scenario roundtrip|boundary|reset_recovery|pingpong|<id>] [--n N]\n"
        "       --scenario  scenario to run (default: roundtrip)\n"
        "       --n         user-words to push this run (default: 8192)\n",
        argv0);
}

int main(int argc, char **argv) {
    uint32_t scenario = 0;            // roundtrip
    uint32_t n_words  = 8192;

    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--scenario") == 0 && i + 1 < argc) {
            if (parse_scenario(argv[++i], &scenario) != 0) {
                fprintf(stderr, "error: unknown scenario '%s'\n", argv[i]);
                usage(argv[0]);
                return 2;
            }
        } else if (strcmp(argv[i], "--n") == 0 && i + 1 < argc) {
            n_words = (uint32_t)strtoul(argv[++i], NULL, 0);
        } else if (strcmp(argv[i], "--help") == 0 || strcmp(argv[i], "-h") == 0) {
            usage(argv[0]);
            return 2;
        } else {
            fprintf(stderr, "error: unexpected argument '%s'\n", argv[i]);
            usage(argv[0]);
            return 2;
        }
    }

    int fd = open("/dev/mem", O_RDWR | O_SYNC);
    if (fd < 0) {
        fprintf(stderr, "error: open /dev/mem: %s\n", strerror(errno));
        return 2;
    }
    void *map = mmap(NULL, CSR_SPAN, PROT_READ | PROT_WRITE, MAP_SHARED, fd, CSR_BASE);
    if (map == MAP_FAILED) {
        fprintf(stderr, "error: mmap CSR @ 0x%08lx: %s\n", CSR_BASE, strerror(errno));
        close(fd);
        return 2;
    }
    g_csr = (volatile uint32_t *)map;

    // Sanity probe — confirm we're talking to the validator CSR.
    uint32_t ident = reg_rd(REG_IDENT);
    if (ident != IDENT_MAGIC) {
        fprintf(stderr,
            "error: IDENT mismatch — read 0x%08x, expected 0x%08x. "
            "Wrong bitstream, or CSR not at 0x%08lx?\n",
            ident, IDENT_MAGIC, CSR_BASE);
        munmap(map, CSR_SPAN);
        close(fd);
        return 2;
    }

    printf("ddr_stream_axi validator — scenario '%s' (id %u), N=%u words\n",
           scenario_name(scenario), scenario, n_words);

    // Clean slate, then load run parameters and kick off.
    reg_wr(REG_CTRL, CTRL_SOFT_RESET);
    reg_wr(REG_SCENARIO, scenario);
    reg_wr(REG_N_WORDS, n_words);
    reg_wr(REG_CTRL, CTRL_START);

    // Poll STATUS.done with a generous timeout. The drain phase alone is
    // C_DRAIN_CYCLES (1024) plus the burst time for N words at 125 MHz; a
    // few seconds of wall-clock polling is comfortably above the worst case.
    const int    POLL_US     = 1000;        // 1 ms
    const long   TIMEOUT_US  = 5L * 1000000; // 5 s
    long         waited_us   = 0;
    uint32_t     status      = 0;
    while (1) {
        status = reg_rd(REG_STATUS);
        if (status & ST_DONE) break;
        if (waited_us >= TIMEOUT_US) {
            fprintf(stderr,
                "error: timeout after %ld ms — STATUS=0x%08x "
                "(running=%u write_idle=%u read_idle=%u). DUT hang?\n",
                TIMEOUT_US / 1000, status,
                !!(status & ST_RUNNING),
                !!(status & ST_WRITE_IDLE),
                !!(status & ST_READ_IDLE));
            fprintf(stderr, "  WORDS_WRITTEN=%u WORDS_READ=%u ERROR_COUNT=%u\n",
                reg_rd(REG_WORDS_WRITTEN), reg_rd(REG_WORDS_READ),
                reg_rd(REG_ERROR_COUNT));
            munmap(map, CSR_SPAN);
            close(fd);
            return 1;
        }
        usleep(POLL_US);
        waited_us += POLL_US;
    }

    uint32_t err_count = reg_rd(REG_ERROR_COUNT);
    uint32_t wr        = reg_rd(REG_WORDS_WRITTEN);
    uint32_t rd        = reg_rd(REG_WORDS_READ);
    int      hw_pass   = !!(status & ST_PASS);
    int      hw_fail   = !!(status & ST_FAIL);

    printf("  WORDS_WRITTEN=%u  WORDS_READ=%u  ERROR_COUNT=%u  STATUS=0x%08x\n",
           wr, rd, err_count, status);

    // The pingpong scenario reports neither pass nor fail (done only) — it is
    // stubbed until the IP ships the ptr-reset CSR (DDR-P3.11).
    if (!hw_pass && !hw_fail) {
        printf("SKIP: scenario '%s' is stubbed (no pass/fail asserted).\n",
               scenario_name(scenario));
        munmap(map, CSR_SPAN);
        close(fd);
        return 2;
    }

    // Adversarial cross-check (feedback_test_success_is_bugs_found): a 'pass'
    // that moved zero data, or didn't read back everything it wrote, is a
    // FAIL regardless of what the hardware latched. The PL BIST already
    // enforces this floor; we re-assert it host-side so a wiring regression
    // that fakes pass can't slip through.
    if (hw_pass && (wr < n_words || rd < n_words || err_count != 0)) {
        printf("FAIL: hardware reported pass but traffic floor breached "
               "(wr=%u rd=%u err=%u, expected N=%u, err=0).\n",
               wr, rd, err_count, n_words);
        munmap(map, CSR_SPAN);
        close(fd);
        return 1;
    }

    if (hw_pass) {
        printf("PASS: %u words round-tripped through PS DDR, zero mismatches.\n", rd);
        munmap(map, CSR_SPAN);
        close(fd);
        return 0;
    }

    // hw_fail — report the first mismatch the BIST captured.
    printf("FAIL: %u mismatch(es). First error at index %u: expected 0x%08x, got 0x%08x.\n",
           err_count,
           reg_rd(REG_FIRST_ERR_INDEX),
           reg_rd(REG_FIRST_ERR_EXP),
           reg_rd(REG_FIRST_ERR_GOT));
    munmap(map, CSR_SPAN);
    close(fd);
    return 1;
}
