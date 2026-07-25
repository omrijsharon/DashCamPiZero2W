# NetworkManager AP profile

`dashcam-ap.nmconnection.template` is inert source material, not an installable
profile. Provisioning must probe the Wi-Fi interface, generate a connection UUID,
derive the SSID suffix from a non-secret stable device ID, and inject a unique
random or owner-provided passphrase without writing it to logs or reports.

The generated profile must be installed as root with mode `0600`, pass
`nmcli connection load`/inspection on the target image, and remain outside the
repository. Any unresolved `REPLACE_` token is a hard refusal. There is no
universal password.

The default IPv4 shared-mode address is `192.168.50.1/24`, with the required
`192.168.50.20`–`192.168.50.100` DHCP range declared through
`ipv4.shared-dhcp-range`. Phase 0B must verify that the installed NetworkManager
and internal DHCP plugin support and enforce this property; an unsupported
property blocks AP acceptance rather than silently changing the range.

Autoconnect is intentional for an AP-only dashcam. Phase 0B must test coexistence
with any client profile and prove AP retry/failure cannot restart or stop
`dashcamd`. The web application—not the unit file—must bind only the configured
AP/local addresses and prove that restriction in integration tests. Target
probing also decides the supported WPA2/WPA3 policy.
