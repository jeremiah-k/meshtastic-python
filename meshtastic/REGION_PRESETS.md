# Firmware-declared LoRa region/preset capabilities

Firmware 2.8 can send `FromRadio.region_presets` during configuration download. The
Python interface stores a defensive protobuf copy in `regionPresetMap`, exposes a
flattened immutable lookup in `regionPresets`, and publishes
`meshtastic.region_presets`.

Missing messages, absent regions, malformed group references, empty preset groups, and
default presets outside their group all mean “no client-side constraint.” The firmware
remains the source of truth. Applications should use the declared default when a region
change makes the current preset illegal, surface `licensed_only`, and re-read LoRa
configuration after writes because EU sibling-region auto-selection can change region.
