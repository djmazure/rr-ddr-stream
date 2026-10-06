// SPDX-License-Identifier: MIT
// de25_lcd_kmsg — scroll the Linux kernel log on the DE25-Standard 128x64 LCD.
//
// The DE25-Standard (Agilex 5 SoC) carries a 128x64 monochrome LCD on a
// Sitronix ST7567 / NT7534-class controller, hung off the HPS SPI master
// (SPIM0 -> /dev/spidev0.0) with three HPS GPIOs on /dev/gpiochip0:
//
//     line 10  HPS_LCM_RST_n   reset (active low)
//     line 11  HPS_LCM_D_C     A0 / data(1) vs command(0)
//     line 18  HPS_LCM_BK      backlight enable (needs board header JP4 closed)
//
// These FACTS (controller command set, SPI speed/mode, gpiochip line numbers,
// init order, 8-page framebuffer layout) are lifted from Terasic's reference
// `hps_lcd` demo in the DE25-Standard ResourcePackage — reference-only; this is
// a self-contained single-translation-unit reimplementation so it drops into
// the pure-`rr` `linux.apps:` builder (one .c, `gcc -static`, no libgpiod).
//
// Behaviour: open /dev/kmsg (which replays the kernel ring from its oldest
// record), render the tail on the LCD as 21 cols x 8 rows of 5x7 text, and
// follow new records live. Started from early init, the panel shows Linux
// booting. Build: rr linux rootfs (declared in project.yml linux.apps).

#include <stdint.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <poll.h>
#include <sys/ioctl.h>
#include <linux/gpio.h>
#include <linux/spi/spidev.h>

// ---- board wiring (DE25-Standard, confirmed on silicon 2026-06-10) ---------
// RST/D-C/BK are HPS GPIO1 (porta is gpio0/gpiochip0; portb is gpio1/gpiochip1
// = hps_io_gpio34/35/42 → GPIO1:IO10/11/18). The Terasic demo's "/dev/gpiochip0"
// is for THEIR kernel's probe order; on linux-socfpga portb enumerates as
// gpiochip1. Driving gpiochip0 left D/C dead and every command mis-framed.
#define SPI_DEV       "/dev/spidev0.0"
#define GPIO_DEV      "/dev/gpiochip1"
#define LINE_RST      10
#define LINE_DC       11
#define LINE_BK       18
#define SPI_HZ        2500000u

#define LCD_W         128
#define LCD_H         64
#define LCD_PAGES     (LCD_H / 8)   // 8
#define COLS          21            // 128 / 6  -> 21 chars of a 5x7+gap cell
#define ROWS          LCD_PAGES     // 8 text rows (one page per row)

// ---- ST7567 / NT7534 command opcodes ---------------------------------------
#define CMD_DISPLAY_ON      0xAF
#define CMD_DISPLAY_OFF     0xAE
#define CMD_START_LINE(l)   (0x40 | ((l) & 0x3F))
#define CMD_PAGE(p)         (0xB0 | ((p) & 0x0F))
#define CMD_COL_LO(c)       (0x00 | ((c) & 0x0F))
#define CMD_COL_HI(c)       (0x10 | (((c) >> 4) & 0x0F))
#define CMD_POWER(m)        (0x28 | ((m) & 0x07))
#define CMD_COM_REVERSE     0xC8   // SetOutputStatusSelect(false) in the vendor lib

static int g_spi = -1;
static int g_rst = -1, g_dc = -1, g_bk = -1;   // gpio line-handle fds

// ---- 5x7 font, ASCII 0x20..0x7F (5 column bytes each, LSB = top pixel) ------
// Classic public-domain 5x7 cell font.
static const uint8_t FONT5X7[96][5] = {
    {0x00,0x00,0x00,0x00,0x00}, // 0x20 space
    {0x00,0x00,0x5F,0x00,0x00}, // !
    {0x00,0x07,0x00,0x07,0x00}, // "
    {0x14,0x7F,0x14,0x7F,0x14}, // #
    {0x24,0x2A,0x7F,0x2A,0x12}, // $
    {0x23,0x13,0x08,0x64,0x62}, // %
    {0x36,0x49,0x55,0x22,0x50}, // &
    {0x00,0x05,0x03,0x00,0x00}, // '
    {0x00,0x1C,0x22,0x41,0x00}, // (
    {0x00,0x41,0x22,0x1C,0x00}, // )
    {0x14,0x08,0x3E,0x08,0x14}, // *
    {0x08,0x08,0x3E,0x08,0x08}, // +
    {0x00,0x50,0x30,0x00,0x00}, // ,
    {0x08,0x08,0x08,0x08,0x08}, // -
    {0x00,0x60,0x60,0x00,0x00}, // .
    {0x20,0x10,0x08,0x04,0x02}, // /
    {0x3E,0x51,0x49,0x45,0x3E}, // 0
    {0x00,0x42,0x7F,0x40,0x00}, // 1
    {0x42,0x61,0x51,0x49,0x46}, // 2
    {0x21,0x41,0x45,0x4B,0x31}, // 3
    {0x18,0x14,0x12,0x7F,0x10}, // 4
    {0x27,0x45,0x45,0x45,0x39}, // 5
    {0x3C,0x4A,0x49,0x49,0x30}, // 6
    {0x01,0x71,0x09,0x05,0x03}, // 7
    {0x36,0x49,0x49,0x49,0x36}, // 8
    {0x06,0x49,0x49,0x29,0x1E}, // 9
    {0x00,0x36,0x36,0x00,0x00}, // :
    {0x00,0x56,0x36,0x00,0x00}, // ;
    {0x08,0x14,0x22,0x41,0x00}, // <
    {0x14,0x14,0x14,0x14,0x14}, // =
    {0x00,0x41,0x22,0x14,0x08}, // >
    {0x02,0x01,0x51,0x09,0x06}, // ?
    {0x32,0x49,0x79,0x41,0x3E}, // @
    {0x7E,0x11,0x11,0x11,0x7E}, // A
    {0x7F,0x49,0x49,0x49,0x36}, // B
    {0x3E,0x41,0x41,0x41,0x22}, // C
    {0x7F,0x41,0x41,0x22,0x1C}, // D
    {0x7F,0x49,0x49,0x49,0x41}, // E
    {0x7F,0x09,0x09,0x09,0x01}, // F
    {0x3E,0x41,0x49,0x49,0x7A}, // G
    {0x7F,0x08,0x08,0x08,0x7F}, // H
    {0x00,0x41,0x7F,0x41,0x00}, // I
    {0x20,0x40,0x41,0x3F,0x01}, // J
    {0x7F,0x08,0x14,0x22,0x41}, // K
    {0x7F,0x40,0x40,0x40,0x40}, // L
    {0x7F,0x02,0x0C,0x02,0x7F}, // M
    {0x7F,0x04,0x08,0x10,0x7F}, // N
    {0x3E,0x41,0x41,0x41,0x3E}, // O
    {0x7F,0x09,0x09,0x09,0x06}, // P
    {0x3E,0x41,0x51,0x21,0x5E}, // Q
    {0x7F,0x09,0x19,0x29,0x46}, // R
    {0x46,0x49,0x49,0x49,0x31}, // S
    {0x01,0x01,0x7F,0x01,0x01}, // T
    {0x3F,0x40,0x40,0x40,0x3F}, // U
    {0x1F,0x20,0x40,0x20,0x1F}, // V
    {0x3F,0x40,0x38,0x40,0x3F}, // W
    {0x63,0x14,0x08,0x14,0x63}, // X
    {0x07,0x08,0x70,0x08,0x07}, // Y
    {0x61,0x51,0x49,0x45,0x43}, // Z
    {0x00,0x7F,0x41,0x41,0x00}, // [
    {0x02,0x04,0x08,0x10,0x20}, // backslash
    {0x00,0x41,0x41,0x7F,0x00}, // ]
    {0x04,0x02,0x01,0x02,0x04}, // ^
    {0x40,0x40,0x40,0x40,0x40}, // _
    {0x00,0x01,0x02,0x04,0x00}, // `
    {0x20,0x54,0x54,0x54,0x78}, // a
    {0x7F,0x48,0x44,0x44,0x38}, // b
    {0x38,0x44,0x44,0x44,0x20}, // c
    {0x38,0x44,0x44,0x48,0x7F}, // d
    {0x38,0x54,0x54,0x54,0x18}, // e
    {0x08,0x7E,0x09,0x01,0x02}, // f
    {0x0C,0x52,0x52,0x52,0x3E}, // g
    {0x7F,0x08,0x04,0x04,0x78}, // h
    {0x00,0x44,0x7D,0x40,0x00}, // i
    {0x20,0x40,0x44,0x3D,0x00}, // j
    {0x7F,0x10,0x28,0x44,0x00}, // k
    {0x00,0x41,0x7F,0x40,0x00}, // l
    {0x7C,0x04,0x18,0x04,0x78}, // m
    {0x7C,0x08,0x04,0x04,0x78}, // n
    {0x38,0x44,0x44,0x44,0x38}, // o
    {0x7C,0x14,0x14,0x14,0x08}, // p
    {0x08,0x14,0x14,0x18,0x7C}, // q
    {0x7C,0x08,0x04,0x04,0x08}, // r
    {0x48,0x54,0x54,0x54,0x20}, // s
    {0x04,0x3F,0x44,0x40,0x20}, // t
    {0x3C,0x40,0x40,0x20,0x7C}, // u
    {0x1C,0x20,0x40,0x20,0x1C}, // v
    {0x3C,0x40,0x30,0x40,0x3C}, // w
    {0x44,0x28,0x10,0x28,0x44}, // x
    {0x0C,0x50,0x50,0x50,0x3C}, // y
    {0x44,0x64,0x54,0x4C,0x44}, // z
    {0x00,0x08,0x36,0x41,0x00}, // {
    {0x00,0x00,0x7F,0x00,0x00}, // |
    {0x00,0x41,0x36,0x08,0x00}, // }
    {0x08,0x04,0x08,0x10,0x08}, // ~
    {0x00,0x00,0x00,0x00,0x00}, // 0x7F
};

// ---- framebuffer + text ring -----------------------------------------------
static uint8_t  g_fb[LCD_PAGES][LCD_W];   // page-major, byte = 8 vertical px
static char     g_lines[ROWS][COLS + 1];  // scrolling text ring (top..bottom)

// ---- low level: gpio + spi -------------------------------------------------
static int gpio_request_output(int chip_fd, unsigned line, int initial)
{
    struct gpiohandle_request req;
    memset(&req, 0, sizeof(req));
    req.lineoffsets[0] = line;
    req.lines = 1;
    req.flags = GPIOHANDLE_REQUEST_OUTPUT;
    req.default_values[0] = (uint8_t)(initial ? 1 : 0);
    strncpy(req.consumer_label, "de25_lcd", sizeof(req.consumer_label) - 1);
    if (ioctl(chip_fd, GPIO_GET_LINEHANDLE_IOCTL, &req) < 0) {
        fprintf(stderr, "de25_lcd: LINEHANDLE line %u: %s\n", line, strerror(errno));
        return -1;
    }
    return req.fd;
}

static void gpio_set(int handle_fd, int value)
{
    struct gpiohandle_data data;
    memset(&data, 0, sizeof(data));
    data.values[0] = (uint8_t)(value ? 1 : 0);
    (void)ioctl(handle_fd, GPIOHANDLE_SET_LINE_VALUES_IOCTL, &data);
}

static int spi_write(const uint8_t *buf, size_t len)
{
    struct spi_ioc_transfer xfer;
    memset(&xfer, 0, sizeof(xfer));
    xfer.tx_buf = (unsigned long)buf;
    xfer.len = (uint32_t)len;
    xfer.speed_hz = SPI_HZ;
    xfer.bits_per_word = 8;
    if (ioctl(g_spi, SPI_IOC_MESSAGE(1), &xfer) < 0) {
        fprintf(stderr, "de25_lcd: SPI xfer(%zu) failed: %s\n", len, strerror(errno));
        return -1;
    }
    return 0;
}

static void lcd_cmd1(uint8_t c)
{
    gpio_set(g_dc, 0);          // command
    spi_write(&c, 1);
}

static void lcd_data(const uint8_t *buf, size_t len)
{
    gpio_set(g_dc, 1);          // data
    spi_write(buf, len);
}

// ---- lcd bring-up + flush --------------------------------------------------
static void lcd_reset(void)
{
    gpio_set(g_rst, 0);
    usleep(60000);
    gpio_set(g_rst, 1);
    usleep(60000);
}

static void lcd_init(void)
{
    // Full ST7567 init, validated live on the DE25 panel (2026-06-10). The
    // vendor's minimal init (relying on POR bias/contrast) left the panel
    // blank here; staged power-up + explicit bias/resistor/contrast is what
    // produced visible pixels.
    lcd_reset();
    lcd_cmd1(0xE2); usleep(5000);  // soft reset
    lcd_cmd1(0xA2);                // bias 1/9
    lcd_cmd1(0xA0);                // ADC normal (segment direction)
    lcd_cmd1(CMD_COM_REVERSE);     // 0xC8 — COM reverse (vendor orientation)
    lcd_cmd1(0x2C); usleep(2000);  // power: booster on
    lcd_cmd1(0x2E); usleep(2000);  //        + voltage regulator
    lcd_cmd1(0x2F); usleep(2000);  //        + voltage follower
    lcd_cmd1(0x26);                // V0 resistor ratio 6
    lcd_cmd1(0x81); lcd_cmd1(0x30);// electronic volume (contrast) 0x30
    lcd_cmd1(0xA6);                // normal (non-inverted) display
    lcd_cmd1(CMD_START_LINE(0));   // 0x40
    lcd_cmd1(CMD_DISPLAY_ON);      // 0xAF
}

static void lcd_flush(void)
{
    for (int page = 0; page < LCD_PAGES; page++) {
        lcd_cmd1(CMD_PAGE(page));
        lcd_cmd1(CMD_COL_LO(0));
        lcd_cmd1(CMD_COL_HI(0));
        lcd_data(g_fb[page], LCD_W);
    }
}

// ---- render the text ring into the framebuffer -----------------------------
static void draw_text(void)
{
    memset(g_fb, 0, sizeof(g_fb));
    for (int row = 0; row < ROWS; row++) {
        const char *s = g_lines[row];
        for (int col = 0; col < COLS && s[col]; col++) {
            unsigned char ch = (unsigned char)s[col];
            if (ch < 0x20 || ch > 0x7F)
                ch = '?';
            const uint8_t *glyph = FONT5X7[ch - 0x20];
            int x = col * 6;
            for (int gx = 0; gx < 5; gx++)
                g_fb[row][x + gx] = glyph[gx];   // 6th column stays blank (gap)
        }
    }
}

static void push_line(const char *text)
{
    for (int r = 0; r < ROWS - 1; r++)
        memcpy(g_lines[r], g_lines[r + 1], COLS + 1);
    strncpy(g_lines[ROWS - 1], text, COLS);
    g_lines[ROWS - 1][COLS] = '\0';
}

// /dev/kmsg record: "<prio>,<seq>,<ts_usec>,<flags>[,...];<message>\n"
// Strip the metadata prefix up to ';' and hand back the message text.
static const char *kmsg_text(const char *rec)
{
    const char *semi = strchr(rec, ';');
    return semi ? semi + 1 : rec;
}

int main(void)
{
    int chip = open(GPIO_DEV, O_RDONLY | O_CLOEXEC);
    if (chip < 0) { perror("open gpiochip"); return 1; }
    g_rst = gpio_request_output(chip, LINE_RST, 1);
    g_dc  = gpio_request_output(chip, LINE_DC, 0);
    g_bk  = gpio_request_output(chip, LINE_BK, 1);   // backlight on (needs JP4)
    if (g_rst < 0 || g_dc < 0 || g_bk < 0) return 1;

    g_spi = open(SPI_DEV, O_RDWR | O_CLOEXEC);
    if (g_spi < 0) { perror("open spidev"); return 1; }
    uint8_t mode = SPI_MODE_0, bits = 8;
    uint32_t hz = SPI_HZ;
    ioctl(g_spi, SPI_IOC_WR_MODE, &mode);
    ioctl(g_spi, SPI_IOC_WR_BITS_PER_WORD, &bits);
    ioctl(g_spi, SPI_IOC_WR_MAX_SPEED_HZ, &hz);

    lcd_init();
    // Startup splash: flash the whole panel bright for ~0.8 s so it's obvious
    // the driver reached the glass, then fall through to the kmsg view.
    memset(g_fb, 0xFF, sizeof(g_fb));
    lcd_flush();
    usleep(800000);

    for (int r = 0; r < ROWS; r++)
        g_lines[r][0] = '\0';
    push_line("DE25 kmsg LCD");
    push_line("---------------------");
    draw_text();
    lcd_flush();

    // /dev/kmsg replays the ring from its oldest record on open, then blocks
    // (EAGAIN in non-blocking mode) once drained; poll for new records.
    int km = open("/dev/kmsg", O_RDONLY | O_NONBLOCK | O_CLOEXEC);
    if (km < 0) { perror("open kmsg"); /* keep the splash up */ }

    char rec[2048];
    struct pollfd pfd = { .fd = km, .events = POLLIN };
    bool dirty = false;
    for (;;) {
        if (km >= 0) {
            ssize_t n = read(km, rec, sizeof(rec) - 1);
            if (n > 0) {
                rec[n] = '\0';
                char msg[COLS * 4];
                const char *t = kmsg_text(rec);
                size_t i = 0;
                for (; t[i] && t[i] != '\n' && i < sizeof(msg) - 1; i++)
                    msg[i] = t[i];
                msg[i] = '\0';
                // wrap long messages across multiple LCD rows
                for (size_t off = 0; off == 0 || msg[off]; off += COLS) {
                    char chunk[COLS + 1];
                    strncpy(chunk, msg + off, COLS);
                    chunk[COLS] = '\0';
                    push_line(chunk);
                    if (strlen(msg + off) <= COLS)
                        break;
                }
                dirty = true;
                continue;               // drain everything available first
            }
            if (n < 0 && errno == EPIPE) // ring wrapped; just keep reading
                continue;
        }
        if (dirty) {
            draw_text();
            lcd_flush();
            dirty = false;
        }
        if (km < 0) { pause(); }        // no kmsg: hold the splash
        else (void)poll(&pfd, 1, 1000); // wait for new records (1s tick)
    }
    return 0;
}
