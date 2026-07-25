# Hardware

## Validation status

Partially validated on the reference Pi during the owner-authorized 2026-07-24
capability run. This is not yet a complete bill of materials or compatibility
guarantee; storage, integrated audio fault behavior, measured power consumption,
and vehicle-power behavior remain open.

Observed hardware:

- Raspberry Pi Zero 2 W Rev 1.0.
- Current replacement reference board: revision `902120`, serial
  `00000000db28ffe4`, Wi-Fi MAC `2c:cf:67:98:4c:49`.
- IMX219 CSI camera, enumerated through libcamera after its explicit overlay.
- Nominal 32 GB microSD card; the flashed root filesystem currently occupies
  nearly the whole card.
- FlyFishRC M10 Mini GPS Module, powered from 5 V and read at 115200 baud through
  GPS TX to Pi GPIO15/RXD0. GPS RX is intentionally disconnected.
- USB microphone/capture device `08bb:2902`, exposed as a Texas Instruments
  PCM2902 / C-Media USB PnP Sound Device. It supports mono S16LE at 44.1 or
  48 kHz and has no unique USB serial number.
- Unspecified-model regulated power supply rated 5 V / 2.5 A.
- The owner confirmed there is no power hold-up or safe-shutdown controller.

The absence of a hold-up controller is a material risk: software cannot perform
a controlled shutdown when vehicle power disappears abruptly. The final
deployment must not represent previously finalized clips or exFAT metadata as
power-loss-proof.

The 2.5 A value is available supply capacity, not a claim that the Pi consumes
2.5 A. The load draws only the current it requires. Actual input current,
connector voltage under camera/Wi-Fi/GPS/USB-audio load, cable drop, and transient
margin require later instrumented endurance measurements.

The product still requires a dedicated exFAT recording volume labeled
`DASHCAM`, mounted at `/srv/dashcam`, but its final layout and provisioner are not
validated. See `docs/test-reports/2026-07-24-milestone4-progress.md` for exact
measurements and remaining blockers.

The microphone is directly attached to the Pi USB root port at full speed. Its
stable identity must combine USB vendor/product and product/path information;
never select it by volatile ALSA card index. A short live test passed PCM capture
and AAC-LC encoding at 48 kHz mono / 128 kbit/s. After physical unplug, its
capture node disappeared, absent-device open failed explicitly within 131 ms,
and a video-only camera smoke test still passed. Reconnecting without reboot
restored the same USB/product/physical-path identity and AAC capture.

The GPS capability connection is receive-only. The FlyFishRC module accepts a
3.3–5.5 V supply, but UART signaling into GPIO15 must remain Pi-compatible
3.3 V logic. A short capture produced checksum-valid GGA/RMC fixes at about
10 Hz. Exact coordinates are deliberately omitted from repository evidence.
See `docs/test-reports/2026-07-24-pi-gps-audio.md`.

The replacement-board smoke test also showed that `/dev/media2` and
`/dev/media3` roles can change between otherwise equivalent Pi Zero 2 W boards.
Never identify camera/codec components by a fixed `/dev/mediaN` number; inspect
the media graph. The tested camera and encoder video nodes were `/dev/video0`
and `/dev/video11`, but production startup must probe their identities too.
