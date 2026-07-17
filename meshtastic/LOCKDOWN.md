# USB lockdown client support

Lockdown passphrases are sent in cleartext by the firmware protocol and are therefore
supported only on `SerialInterface` USB connections. The Python API refuses TCP and BLE.

The public `meshtastic.lockdown` helpers validate 1–32-byte passphrases, require
operator-only permissions for passphrase files, send an unencrypted local `ADMIN_APP`
request, and wait for the structured `meshtastic.lockdown_status` event. The interface
also retains the most recent defensive protobuf copy in `lockdownStatus`.

CLI actions are `--lockdown-provision`, `--lockdown-unlock`,
`--lockdown-lock-now`, and `--lockdown-disable`. Destructive actions require
`--lockdown-yes` or typed confirmation. Prefer `--lockdown-passphrase-file` with mode
0600; command-line passphrases require explicit acknowledgement of insecure usage.
