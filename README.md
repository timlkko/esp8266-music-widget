# ESP8266 Music Widget

A small desk gadget that shows what's playing on your PC: title, artist, elapsed/total time and a progress bar on a 0.96" OLED. Long titles scroll, Cyrillic is supported.

<p align="center">
 
https://github.com/user-attachments/assets/185038a7-9b95-46d4-8a0d-9b5215fd21fc

</p>
A Python script on Windows reads the current track from the system media session and sends it over USB serial to an ESP8266, which draws it on the display. No Wi-Fi or accounts needed.

## You need

- ESP8266 board
- 0.96" I2C OLED, 128x64, SSD1306
- 4 jumper wires and a USB data cable
- [VS Code + PlatformIO](https://platformio.org/install/ide?install=vscode), Python 3 on Windows 10/11

## Wiring

| OLED | ESP8266 |
|------|---------|
| VCC  | 3V3 |
| GND  | GND |
| SDA  | GPIO4 (D2) |
| SCL  | GPIO5 (D1) |

## Setup

**1. Flash the board**

```bash
git clone https://github.com/timlkko/esp8266-music-widget.git
cd esp8266-music-widget
pio run -t upload
```

Using another board (NodeMCU, Wemos D1 mini)? Change `board` in `platformio.ini`, e.g. `nodemcuv2` or `d1_mini`.

**2. Run the PC script**

```bash
python -m pip install winsdk pyserial
```

Set your COM port (Device Manager → Ports) at the top of `script/main.py`:

```python
PORT = "COM6"
```

Start some music, then:

```bash
python script/main.py
```

## Choosing the player

The script only listens to apps in `ALLOWED_APPS` (`script/main.py`). Find your app's ID:

```bash
python script/main.py --list
```

Then put a part of it into the list (case-insensitive), or use `[]` to listen to everything:

```python
ALLOWED_APPS = ["spotify"]
```

## Troubleshooting

- **Black screen:** check SDA/SCL and 3.3 V. If you have an SH1106 display, use `U8G2_SH1106_128X64_NONAME_F_HW_I2C` in `src/main.cpp`.
- **Stuck on "Waiting for the music...":** the script isn't running or `PORT` is wrong.
- **"Nothing playing":** your player isn't in `ALLOWED_APPS`, run `--list`.
- **"Port unavailable":** close any serial monitor that holds the port.
