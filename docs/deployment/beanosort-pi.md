# Running the BeanoSort WebGUI on the Raspberry Pi

The Pi hosts the WebGUI, inventory database, reference photographs and job synchronizer. The Jetson hosts the machine API, ESP controller, capture pipeline and live-run crops. FastCamUSB runs as a separate service on the Pi.

| Service | Address on this LAN |
| --- | --- |
| BeanoSort WebGUI | http://10.203.77.197:8090 |
| Jetson machine API | http://10.203.77.198:8091 |
| Jetson manual controller | http://10.203.77.198:8000 |

## Moving an existing SQLite installation

1. Use the same committed revision on both hosts. On the Pi check it out at `~/Beano/BeanoFlight`. In `beanosort/` create a Python virtual environment, install `-r requirements.lock -e .` and run `pip check`.
2. While the original application is still available, release any manual controller lease and save the Jetson's LAN address in **Sorter Controls**. The saved address takes precedence over the environment default. Wait for machine jobs to finish and synchronize and for the machine command queue to empty.
3. Gracefully stop the original WebGUI. Use SQLite's backup API to snapshot the authoritative database, including committed WAL changes. Keep that backup on the original host. Transfer it privately to the Pi. Verify its SHA-256 against the source snapshot and run SQLite `PRAGMA quick_check` and `PRAGMA foreign_key_check` before starting the destination. Copy registered reference snapshots and retained import spools at their existing absolute paths.
4. Copy the private environment and machine API token over SSH with owner-only permissions. Keep the material environment, database identity, users and session secret. Set `BEANOSORT_PUBLIC_URL=http://10.203.77.197:8090`, `BEANOSORT_SORTER_HOST=10.203.77.198`, `BEANOSORT_SORTER_PORT=8000`, `BEANOSORT_MACHINE_JOBS_PORT=8091` and the Pi-local token file path. The service reads `beanosort/var/testing.env`.
5. Historical jobs can pin `http://127.0.0.1:8091` into immutable manifests. For that case set `BEANOSORT_LEGACY_JETSON=10.203.77.198:8091` in the same environment. Verify the Jetson machine ID against the historical job requests. On the **Pi only**, install both `beanosort-legacy-jobs.*` units in `~/.config/systemd/user/`. The socket accepts loopback connections and forwards them to that specific Jetson. New jobs connect directly to its LAN address.
6. Install `beanosort.service` in the Pi's user unit directory. Run `systemctl --user daemon-reload`, `systemctl --user enable --now beanosort-legacy-jobs.socket` if needed, then `systemctl --user enable --now beanosort.service`. User lingering must be enabled for unattended boot. Sign in with the existing account. Verify stock, labels, a historical photograph, machine settings and read-only controller status from the new address.

Use `journalctl --user -u beanosort.service` for the WebGUI log. Back up the Pi database with SQLite's backup API along with reference datasets, import spools and private configuration. Continue backing up machine journals and recorded images on the Jetson. New label URLs use the Pi address. Existing printed URLs can be read through BeanoSort's **Scan** field by identity.

For rollback, stop the Pi WebGUI before bringing the Jetson WebGUI back. If users have written to the Pi database, take a fresh checked backup from the Pi and restore that version on the Jetson first. Restore the Jetson public label URL and local private configuration. Keep exactly one authoritative writer.
