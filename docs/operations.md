# Operation and troubleshooting

[Back to the README](../README.md) · [Installation and configuration](installation.md)

## Contents

- [Open the terminal control panel](#open-the-terminal-control-panel)
- [Check, update and roll back](#check-update-and-roll-back)
- [Outages and troubleshooting](#outages-and-troubleshooting)
- [Remove the monitor](#remove-the-monitor)

## Open the terminal control panel

After a one-line installation, run `sudo server-monitor` whenever you want to manage it. For a copied checkout, keep the project directory and run:

```sh
sudo ./monitor.sh
```

After setup, it opens a dashboard that refreshes every two seconds. It shows **Running**, **Stopped**, **Failed** or a transitional state, the configured server/endpoint, collection settings, startup-at-boot state, process ID, agent memory and service start time. Agent RAM is the service's memory accounting value, not host RAM utilisation. Query parameters are omitted from the displayed endpoint.

Use arrows/Tab and Enter, or the keyboard shortcuts:

| Key | Action |
| --- | --- |
| **S** | Start the monitor |
| **X** | Stop the monitor |
| **R** | Restart the monitor |
| **C** | Change setup settings or rotate the token |
| **K** | Check configuration and metric collection |
| **B** | Restore the previous configuration and token |
| **L** | View recent agent logs; current/previous tokens are redacted |
| **A** | Update from `main`, retain settings/token and restart |
| **U** | Uninstall the monitor after confirmation |
| **Q** | Close the interface; a running monitor continues in the background |

The interface changes only `server-monitor.service`. Start/Stop do not change its startup-at-boot setting; by default, a stopped monitor starts again after a server reboot. Stopping/restarting loses buffered unsent samples. Configure pre-fills the installed settings, so custom intervals/interface filters and the Docker toggle are preserved. Select **Docker monitoring** and press Space to enable or disable container collection; it defaults to disabled. Leave the token blank during reconfiguration to retain it. **Save and start** and **Rollback** activate the selected configuration and start the service.

An installation/check can take time; the screen shows progress and waits for the operation to finish before allowing another action or exit. A running service and a successful local collection check do not confirm CRM delivery. Confirm fresh samples in your CRM separately.

The UI uses Python's standard library and needs an interactive terminal of at least **76 columns by 24 rows**. With SSH, use a terminal session (or `ssh -t` when launching remotely). Resize is supported. To preview it safely on your development machine, without sudo or any service/file changes:

```sh
./monitor.sh --demo
```

## Check, update and roll back

After a one-line installation:

```sh
sudo systemctl status server-monitor.service --no-pager
sudo journalctl -u server-monitor.service -n 50 --no-pager
sudo server-monitor --check
```

For a copied checkout, run `sudo ./install.sh --check` from the project directory instead.

`--check` validates input collection, not HTTPS delivery. Confirm that your CRM receives fresh samples after the first reporting interval. Keep time synchronised (`timedatectl status`). Receiver/network failures leave the agent running and appear in the journal; a running service alone is not proof of delivery.

To fetch and apply the latest `main` release with saved settings and credentials:

```sh
sudo server-monitor --update
```

For a copied checkout containing the updater, use `sudo ./install.sh --update`. It also installs the managed source and `server-monitor` launcher. The terminal panel offers **Update** (A); reopen the panel afterwards to load its new code. Older versions must first obtain the updater by rerunning the one-line installer.

Update stops monitoring during download and validation, installs the new sender/configuration, and starts monitoring again, preserving startup at boot. A failed update restores the previous source/configuration and attempts to restart a previously active service; an already stopped service remains stopped on failure. As with any stop, unsent in-memory samples are lost. Settings and tokens stay local and do not enter download requests or command arguments. The installed Telegraf package is not upgraded. Confirm fresh CRM samples after success.

Re-run installation to change endpoint, token, server ID or timing. Configuration/token pairs are staged together, validated, then activated through a release link. Startup is checked for five seconds; failed activation restores the old release and service state. The last successful release (including its token and service unit) is retained:

```sh
sudo server-monitor --rollback
```

For a copied checkout, use `sudo ./install.sh --rollback` instead.

Rollback swaps the current and previous configurations. It does not downgrade the Telegraf package, restore receiver behaviour or recover buffered samples. Current and previous releases live under `/etc/server-monitor/releases`; credentials have mode 600 and are read by systemd before it launches the collector as `telegraf`. Docker access is granted only when enabled, through the release's service unit; rollback restores that setting and access together. There are at most two successful retained releases after installation.

Each release includes its own HTTPS sender, so rollback preserves the configuration and sender together. Legacy configurations using Telegraf's redirect-following HTTP output are refused by Check/Rollback. Open **Configure** and **Save and start** after updating source to upgrade an existing installation. Once two secure configurations have been saved, normal rollback is available again.

The current sender uses CRM schema v1 instead of the legacy raw `metrics` array. Update the receiver first, then apply the source update through **Configure** and **Save and start**. Rolling back to a pre-v1 release also restores its legacy payload format. See the [rollout and partial-sample contract](crm-integration.md).

Existing unrelated installation directories/units are rejected. Installation is protected against concurrent runs. No automatic Telegraf upgrades are configured by this project; normal host APT policy still applies. Test package upgrades before applying them across your servers. Configuration rollback is for ordinary startup failures; abrupt power loss can require manual recovery using the retained release/unit files.

## Outages and troubleshooting

Unsent metrics are retried at subsequent flushes. The default memory buffer overwrites its oldest metrics when full and disappears when Telegraf restarts. This is best-effort telemetry, not a durable audit trail. Request timeouts can cause duplicates even when a receiver saved the batch, so the CRM must deduplicate.

- **401/403:** check the per-server token and endpoint authentication. Reinstall to rotate the token.
- **3xx:** redirects are rejected and the batch remains buffered for retry. Use the final HTTPS POST ingestion URL directly; even HTTPS redirects are refused.
- **404/405:** use the exact POST ingestion path; HTML login pages are unsuitable.
- **TLS errors:** use a certificate valid for the endpoint hostname. For a private CA, install it in Ubuntu's system trust store; verification is never disabled.
- **Missing network data:** inspect `ip -brief link` and reinstall with matching interface names/globs.
- **No CPU percentages in an initial sample:** counters need a subsequent collection; normal continuous collection supplies these.
- **Missing filesystem:** confirm it is mounted and accessible to the Telegraf user; check its type against the exclusions.
- **Missing Docker metrics:** check the saved Docker setting, Docker daemon state and `/var/run/docker.sock`. Start Docker before enabling; the standard root-owned socket must allow a dedicated non-root group read/write access. A changed socket group requires **Configure** and **Save and start**. A Docker-enabled monitor requires Docker available at startup; disable Docker collection if you need host-only operation without Docker. Stopped containers may have lifecycle status without resource data. Confirm the receiver accepts schema v1 container rows with strings and booleans. Missing health can mean no configured `HEALTHCHECK`; it must not be replaced with an assumed healthy value. Regenerate older configurations from updated source to apply Docker timestamp precision equal to the collection interval. Slow collection can still split rows. If validation reports an unused `time_source` field, update the monitor source and save again: the corrected installer uses `precision` instead. Storage requires Docker Engine 23.0+ and receiver support for the storage extension; its sizes exclude volumes and bind mounts.
- **Installer errors:** command output is withheld because expanded config errors can contain secrets. The HTTPS sender reports only status codes or generic connection errors and never logs receiver response bodies. `--check` and `journalctl` help diagnose issues. Do not enable debug output or share credential files.

Installation uses the currently documented InfluxData signing-key fingerprint. If the upstream key rotates, installation fails closed; verify the new fingerprint against the official documentation before updating the installer. A failed first installation can leave the dependency package/repository installed even though no monitor service was activated.

## Remove the monitor

In the terminal interface, press **U** or select **Uninstall**, then confirm removal. The default selection is **Cancel**; Escape/N cancels and Y confirms. The screen returns to **Not installed** after removal.

For a single command on a server where this project's installer was used:

```sh
sudo server-monitor --uninstall
```

For a copied checkout, use `sudo ./install.sh --uninstall` instead.

Both routes stop and disable `server-monitor.service`, remove its unit and the managed `/etc/server-monitor` configuration/credentials/backups, and reload systemd. They refuse unrelated or symlinked installation paths and preserve files if the service fails to stop. Repeating the command after successful removal is safe. Only this unit's failed state is reset; other services' failed states are preserved.

Also delete any source token file and revoke the token in the CRM. Removal leaves the project directory, Telegraf and its APT repository available for other uses. Only if you know no other service uses the package, optionally run `sudo apt-get remove telegraf`. Remove the `server-monitor-influxdata.list` and `server-monitor-influxdata.gpg` APT files only if this installer created them and they are no longer needed. Never remove an existing shared InfluxData repository.

The downloaded source directory `/opt/server-monitor` and `server-monitor` launcher remain after uninstall, so you can reopen the UI and set up again. They contain program files only; CRM credentials and configuration backups are removed from `/etc/server-monitor` by uninstall.
