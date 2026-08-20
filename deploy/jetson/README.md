# Jetson boot profiles

`install_boot_profiles.py` derives six Beano entries from the active dual-IMX296
extlinux entry and retains every unrelated entry, including the original kernel
fallback. It makes a timestamped backup before an atomic replacement.

Install and enable the conditional clock-locking service:

```bash
sudo install -m 0644 deploy/jetson/beano-performance-clocks.service \
  /etc/systemd/system/beano-performance-clocks.service
sudo systemctl daemon-reload
sudo systemctl enable beano-performance-clocks.service
```

Preview, then install the boot menu:

```bash
python3 deploy/jetson/install_boot_profiles.py
sudo python3 deploy/jetson/install_boot_profiles.py --apply
```

Headless entries add `systemd.unit=multi-user.target` for that boot only.
Performance entries add `beanoflight.performance=1`, which activates the
conditional service and runs `jetson_clocks`. The selected `nvpmodel` power mode
remains independently persistent; this Jetson should remain in `MAXN_SUPER`.

Keep Desktop as the default until every entry has been boot-tested. The
performance service does nothing on normal, headless-only, or safe-driver boots.
All headless profiles start the normal multi-user target, so the existing SSH
service remains available without starting the graphical desktop.
