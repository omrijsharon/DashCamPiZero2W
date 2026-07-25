# Pi GPS and USB-audio capability evidence

## Scope

- Date: 2026-07-24
- Target: Raspberry Pi Zero 2 W revision `902120`, serial
  `00000000db28ffe4`
- Image: 32-bit Raspbian 13.4 (`trixie`), kernel
  `6.18.34+rpt-rpi-v7`
- Authorization: owner-connected GPS and microphone; read-only GPS receive and
  bounded audio captures
- Excluded: application deployment, destructive storage work, power
  interruption, and endurance/acceptance claims
- Reference supply: unspecified model, rated 5 V / 2.5 A; this is source
  capacity, not measured Pi consumption

Exact location data is intentionally excluded from repository evidence.

## GPS

The connected receiver is a FlyFishRC M10 Mini GPS Module. Its supply input is
rated for 3.3–5.5 V by the manufacturer; the module is powered from the Pi's
5 V rail. The receive-only capability wiring is:

- GPS VCC to Pi 5 V
- GPS GND to Pi GND
- GPS TX to Pi GPIO15/RXD0, physical pin 10
- GPS RX disconnected

The Pi side is `/dev/serial0 -> /dev/ttyAMA0` (PL011). A bounded reader opened
the port at 115200 baud and transmitted no bytes. It captured 65,536 bytes over
about 10.8 seconds:

- raw-capture SHA-256:
  `ADC07A45D11423DBAFFB35322F4BF1890A8077C6FFE9FC4DFE1539499FA2F13A`
- 1,309 complete/capped NMEA records
- 1,308 checksum-valid records
- zero checksum failures
- one expected partial boundary record
- 109 valid-fix GGA records
- 109 active-fix RMC records, one of which began at the capture boundary
- observed cadence: approximately 10 Hz
- observed talkers/sentences: `GNGGA`, `GNRMC`, `GNGSA`, `GNGLL`, `GNVTG`,
  `GAGSV`, `GBGSV`, and `GPGSV`
- reported satellite count: 5
- reported HDOP: approximately 5.1, which is a valid but mediocre outdoor fix
- maximum observed record length: 74 bytes
- UBX sync markers: zero

The repository parser was also run against the capture. It accepted 109 GGA and
108 complete RMC records, produced 217 valid navigation updates and 108 time
anchor candidates, and safely rejected/ignored the unsupported sentence set.
The one capture-boundary record generated bounded envelope/non-ASCII errors.
Unsupported GSA records reached the configured field-count bound before
sentence dispatch; this does not affect the required GGA/RMC path.

This proves the module, PL011 mapping, 115200-baud choice, required NMEA
sentences, checksums, and local parser on a short live run. It is not the later
GPS loss/reconnect, malformed-input, time-anchor, or endurance acceptance test.

A later privacy-safe live read extracted only the time fields from one
checksum-valid active `GNRMC` record:

- GPS UTC: `2026-07-24T10:13:22.200Z`
- simultaneous Pi UTC: `2026-07-24T10:13:22.224Z`
- GPS minus Pi: -0.024 seconds

This confirms that UTC date/time is available from the receiver. It did not set
the system clock: the Pi clock was only read for comparison. Ownership,
plausibility, uncertainty, discontinuity, and clock-step policy remain
Milestone 8 integration work.

## USB microphone

The connected device is:

- USB ID: `08bb:2902`
- USB descriptor: Texas Instruments PCM2902 Audio Codec
- ALSA long name: C-Media Electronics Inc. USB PnP Sound Device
- capture node during this run: `/dev/snd/pcmC0D0c`
- topology: directly attached to the Pi USB root port, full-speed 12 Mbit/s
- USB serial number: none
- stable selector inputs: vendor/product IDs, product identity, and physical
  `ID_PATH=platform-3f980000.usb-usb-0:1:1.0`

The production selector must not use ALSA card index `0`, because that index is
volatile and the device has no unique serial number.

Native hardware capture capabilities are:

- signed 16-bit little-endian PCM
- mono
- 44.1 kHz or 48 kHz
- interleaved read/write or memory-mapped access

A five-second ALSA capture at 48 kHz mono produced a valid PCM WAV:

- artifact:
  `artifacts/pi/2026-07-24/mic-mono-48k-20260724-01.wav`
- SHA-256:
  `C4721ECABD1D272DD5FEF48C28CD888591D6DF85E31DC8B075DED4B75895B5FE`
- duration: 5.000 seconds
- mean / peak level: -27.5 dB / -15.7 dB

The minimal image lacked the GStreamer ALSA source. Installing only
`gstreamer1.0-alsa` version `1.26.2-1+rpt3+deb13u1` added 256 kB and did not
upgrade other packages. The tested branch is:

```text
alsasrc (48 kHz, mono, S16LE, pipeline timestamps)
  -> bounded queue
  -> audioconvert
  -> audioresample
  -> voaacenc bitrate=128000
  -> aacparse
  -> mp4mux
```

Its bounded capture produced:

- artifact:
  `artifacts/pi/2026-07-24/mic-aac-128k-20260724-01.m4a`
- SHA-256:
  `C42A7EC4C0FCB705F980DED51D719D7CF340A4BD05B37D55D39B709AC242CA80`
- AAC-LC, 48 kHz, mono, exactly 128 kbit/s stream bitrate
- duration: 4.992 seconds
- mean / peak level: -37.8 dB / -18.3 dB
- file size / overall bitrate: 81,566 bytes / about 130.7 kbit/s

After capture the Pi reported 35.4 C and `get_throttled=0x0`. The connected
format and encoding path pass. Physical disconnect/reconnect and full
video-plus-audio timing/fault isolation remain separate required tests.

### Physical disconnect

The owner unplugged the microphone while the Pi remained powered:

- USB `08bb:2902` disappeared.
- `/dev/snd/pcmC0D0c` disappeared and ALSA reported no capture hardware.
- The kernel recorded one normal `usb 1-1: USB disconnect` event.
- Opening the configured `plughw:CARD=Device,DEV=0` source failed explicitly
  with `No such device` in 131 ms, well inside the five-second bound.
- A subsequent five-second 1920x1080, 30 fps, 8 Mbit/s High Profile Level 4.1
  video-only H.264 smoke capture completed successfully.
- The Pi reported 36.5 C and `get_throttled=0x0`.

This proves bounded absent-device detection and that the independent camera
path still works after unplug. The later integrated recorder must separately
prove that hot unplug during active A/V recording does not restart or interrupt
video.

### Physical reconnect

The owner reconnected the same microphone without rebooting the Pi:

- the kernel enumerated full-speed USB device `08bb:2902`, with no serial
  number, on the same root port
- ALSA restored `/dev/snd/pcmC0D0c`
- `/dev/snd/by-path/platform-3f980000.usb-usb-0:1:1.0` restored the same
  physical-path identity
- `/dev/snd/by-id/usb-C-Media_Electronics_Inc._USB_PnP_Sound_Device-00`
  restored the same product identity
- a new bounded AAC capture completed without a reboot

Reconnect artifact:

- path:
  `artifacts/pi/2026-07-24/mic-aac-reconnected-128k-20260724-01.m4a`
- SHA-256:
  `02AFEDCA106BA750A5D71FA39E23E283B279A87B15BC4EDAD4E74F1974A31D44`
- AAC-LC, 48 kHz, mono, 127,998 bit/s
- duration: 2.987 seconds
- mean / peak level: -34.7 dB / -20.5 dB
- Pi health: 35.4 C and `get_throttled=0x0`

Stable identity, formats/rates, direct-root topology, unplug/absence behavior,
and reconnect capture therefore pass the Milestone 4 capability gate. Active
A/V hot-unplug isolation, boundary restoration, repeated cycling, different-
device rejection, and A/V skew remain Milestone 7 tasks.
