// ESP8266 + SSD1306 128x64 (I2C): title, artist, track time and progress.
// Data comes over Serial from the PC Python script.
//
// Packet: AA 55 | type (1 byte) | length (2 bytes, little-endian) | payload
//   type 1 - title  (UTF-8)
//   type 2 - artist (UTF-8)
//   type 4 - time: position (uint16, sec) | duration (uint16, sec) | flags
//            flags: bit0 = playing, bit1 = idle (nothing to show)
//
// Stale data never stays on the screen:
//   - no packets (PC off, script stopped) -> "No signal", then the OLED turns off
//   - idle flag (music window closed / paused for long) -> "Nothing playing", then the OLED turns off

#include <Arduino.h>
#include <U8g2lib.h>
#include <Wire.h>

// ESP8266 default I2C: SDA = D2 (GPIO4), SCL = D1 (GPIO5)
// If you have an SH1106 instead of an SSD1306, use U8G2_SH1106_128X64_NONAME_F_HW_I2C
U8G2_SSD1306_128X64_NONAME_F_HW_I2C u8g2(U8G2_R0, U8X8_PIN_NONE);

constexpr uint32_t BAUD = 460800;
constexpr uint16_t MAX_PAYLOAD = 100;
constexpr int SCREEN_W = 128;
constexpr int GAP = 24;  // gap between the two copies of a scrolling line

constexpr uint32_t LINK_TIMEOUT_MS = 5000;   // no packets for this long -> "No signal"
constexpr uint32_t SLEEP_AFTER_MS = 60000;   // no packets for this long -> OLED off
constexpr uint32_t IDLE_SLEEP_MS = 30000;    // "idle" state for this long -> OLED off

enum PacketType : uint8_t { T_TITLE = 1, T_ARTIST = 2, T_TIME = 4 };

char title[96] = "Waiting...";
char artist[64] = "";
int16_t titleOff = 0, artistOff = 0;

uint16_t posSec = 0, durSec = 0;
bool playing = false;
uint32_t timeRxMs = 0;

uint32_t lastRxMs = 0;
bool linkLost = false;
bool asleep = false;
bool idle = false;
uint32_t idleSinceMs = 0;

void handlePacket(uint8_t type, const uint8_t* d, uint16_t len) {
  // any valid packet means the PC side is alive
  lastRxMs = millis();
  linkLost = false;

  switch (type) {
    case T_TITLE: {
      if (len > sizeof(title) - 1) len = sizeof(title) - 1;
      char tmp[sizeof(title)];
      memcpy(tmp, d, len);
      tmp[len] = 0;
      if (strcmp(tmp, title) != 0) {  // restart the scroll only when the text really changed
        strcpy(title, tmp);
        titleOff = 0;
      }
      break;
    }
    case T_ARTIST: {
      if (len > sizeof(artist) - 1) len = sizeof(artist) - 1;
      char tmp[sizeof(artist)];
      memcpy(tmp, d, len);
      tmp[len] = 0;
      if (strcmp(tmp, artist) != 0) {
        strcpy(artist, tmp);
        artistOff = 0;
      }
      break;
    }
    case T_TIME:
      if (len == 5) {
        posSec = d[0] | (d[1] << 8);
        durSec = d[2] | (d[3] << 8);
        playing = d[4] & 1;
        timeRxMs = millis();

        bool nowIdle = d[4] & 2;
        if (nowIdle && !idle) idleSinceMs = millis();
        idle = nowIdle;
        if (!idle && asleep) {  // real music data wakes the screen up
          u8g2.setPowerSave(0);
          asleep = false;
        }
      }
      break;
  }
}

void readSerial() {
  static uint8_t state = 0, type = 0;
  static uint16_t len = 0, pos = 0;
  static uint8_t buf[MAX_PAYLOAD];
  static uint32_t lastByte = 0;

  // if a packet got cut off, reset the parser
  if (state != 0 && millis() - lastByte > 300) state = 0;

  while (Serial.available()) {
    uint8_t c = Serial.read();
    lastByte = millis();
    switch (state) {
      case 0:
        if (c == 0xAA) state = 1;
        break;
      case 1:
        state = (c == 0x55) ? 2 : (c == 0xAA ? 1 : 0);
        break;
      case 2:
        type = c;
        state = 3;
        break;
      case 3:
        len = c;
        state = 4;
        break;
      case 4:
        len |= (uint16_t)c << 8;
        pos = 0;
        if (len > MAX_PAYLOAD) {
          state = 0;
        } else if (len == 0) {
          handlePacket(type, buf, 0);
          state = 0;
        } else {
          state = 5;
        }
        break;
      case 5:
        buf[pos++] = c;
        if (pos >= len) {
          handlePacket(type, buf, len);
          state = 0;
        }
        break;
    }
  }
}

// Watchdog for the PC link: stale data must not stay on the screen forever
void checkLink() {
  uint32_t silent = millis() - lastRxMs;

  if (silent > LINK_TIMEOUT_MS && !linkLost) {
    linkLost = true;
    strcpy(title, "No signal");
    artist[0] = 0;
    titleOff = artistOff = 0;
    posSec = durSec = 0;
    playing = false;
  }

  bool idleTooLong = idle && (millis() - idleSinceMs > IDLE_SLEEP_MS);
  if ((silent > SLEEP_AFTER_MS || idleTooLong) && !asleep) {
    u8g2.setPowerSave(1);  // protects the OLED from burn-in
    asleep = true;
  }
}

// Current position: between packets the time runs locally so the seconds tick smoothly
uint32_t currentPos() {
  uint32_t p = posSec;
  if (playing) p += (millis() - timeRxMs) / 1000;
  if (durSec > 0 && p > durSec) p = durSec;
  return p;
}

void formatTime(char* out, size_t n, uint32_t sec) {
  if (sec >= 3600) snprintf(out, n, "%u:%02u:%02u", (unsigned)(sec / 3600), (unsigned)((sec / 60) % 60), (unsigned)(sec % 60));
  else snprintf(out, n, "%u:%02u", (unsigned)(sec / 60), (unsigned)(sec % 60));
}

// Draws a line; if it doesn't fit the screen, scrolls it in a loop
void drawMarquee(const char* s, int y, int16_t& off) {
  int w = u8g2.getUTF8Width(s);
  if (w <= SCREEN_W) {
    u8g2.drawUTF8(0, y, s);
    return;
  }
  u8g2.drawUTF8(-off, y, s);
  u8g2.drawUTF8(-off + w + GAP, y, s);
  off += 2;
  if (off >= w + GAP) off = 0;
}

void draw() {
  u8g2.clearBuffer();

  // title big, artist smaller
  u8g2.setFont(u8g2_font_9x15_t_cyrillic);
  drawMarquee(title, 14, titleOff);
  u8g2.setFont(u8g2_font_6x12_t_cyrillic);
  drawMarquee(artist, 31, artistOff);

  // progress bar (only if the duration is known)
  uint32_t pos = currentPos();
  if (durSec > 0) {
    u8g2.drawFrame(0, 40, SCREEN_W, 7);
    int fill = (uint64_t)pos * (SCREEN_W - 4) / durSec;
    u8g2.drawBox(2, 42, fill, 3);
  }

  // time: elapsed on the left, total on the right
  char buf[12];
  formatTime(buf, sizeof(buf), pos);
  u8g2.drawStr(0, 62, buf);
  if (durSec > 0) {
    formatTime(buf, sizeof(buf), durSec);
    u8g2.drawStr(SCREEN_W - u8g2.getStrWidth(buf), 62, buf);
  }

  // play / pause icon in the middle (shows the button you would press)
  if (playing) {
    u8g2.drawBox(61, 53, 2, 8);
    u8g2.drawBox(65, 53, 2, 8);
  } else {
    u8g2.drawTriangle(61, 53, 61, 61, 67, 57);
  }

  u8g2.sendBuffer();
}

void setup() {
  Serial.setRxBufferSize(1024);  // must be called before begin()
  Serial.begin(BAUD);

  u8g2.begin();
  u8g2.setBusClock(400000);
  u8g2.enableUTF8Print();
  lastRxMs = millis();
  draw();
}

void loop() {
  readSerial();
  checkLink();

  static uint32_t lastDraw = 0;
  if (!asleep && millis() - lastDraw >= 60) {
    lastDraw = millis();
    draw();
  }
}