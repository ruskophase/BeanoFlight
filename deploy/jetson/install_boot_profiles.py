#!/usr/bin/env python3
"""Install idempotent Beano extlinux profiles while retaining a fallback entry."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

DEFAULT_CONFIG = Path("/boot/extlinux/extlinux.conf")
MANAGED_LABELS = {
    "JetsonIO",
    "IMX296_SAFE",
    "BEANO_DESKTOP",
    "BEANO_DESKTOP_PERFORMANCE",
    "BEANO_HEADLESS",
    "BEANO_HEADLESS_PERFORMANCE",
    "BEANO_SAFE_DESKTOP",
    "BEANO_SAFE_HEADLESS",
}


def _blocks(text: str) -> tuple[str, list[tuple[str, str]]]:
    matches = list(re.finditer(r"(?m)^LABEL\s+(\S+)\s*$", text))
    if not matches:
        raise ValueError("extlinux configuration contains no LABEL entries")
    prefix = text[: matches[0].start()]
    blocks = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        blocks.append((match.group(1), text[match.start() : end].rstrip()))
    return prefix, blocks


def _field(block: str, name: str) -> str:
    match = re.search(rf"(?mi)^\s*{re.escape(name)}\s+(.+?)\s*$", block)
    if match is None:
        raise ValueError(f"camera boot entry has no {name} field")
    return match.group(1)


def _entry(label: str, menu: str, fields: dict[str, str], extra: str = "") -> str:
    append = fields["APPEND"]
    if extra:
        append = f"{append} {extra}"
    lines = [
        f"LABEL {label}",
        f"      MENU LABEL {menu}",
        f"      LINUX {fields['LINUX']}",
        f"      FDT {fields['FDT']}",
        f"      INITRD {fields['INITRD']}",
        f"      APPEND {append}",
        f"      OVERLAYS {fields['OVERLAYS']}",
    ]
    return "\n".join(lines)


def render_profiles(text: str) -> str:
    prefix, blocks = _blocks(text)
    source = next(
        (block for label, block in blocks if label in {"BEANO_DESKTOP", "JetsonIO"}),
        None,
    )
    safe = next(
        (
            block
            for label, block in blocks
            if label in {"BEANO_SAFE_DESKTOP", "IMX296_SAFE"}
        ),
        None,
    )
    if source is None:
        raise ValueError("cannot find the dual-IMX296 JetsonIO boot entry")
    fields = {
        name: _field(source, name)
        for name in ("LINUX", "FDT", "INITRD", "APPEND", "OVERLAYS")
    }
    safe_extra = (
        "module_blacklist=imx296 modprobe.blacklist=imx296 "
        "systemd.mask=imx296-reload.service"
    )
    if safe is not None:
        safe_append = _field(safe, "APPEND")
        base_append = fields["APPEND"]
        if safe_append.startswith(base_append):
            safe_extra = safe_append[len(base_append) :].strip() or safe_extra

    prefix = re.sub(r"(?m)^DEFAULT\s+\S+\s*$", "DEFAULT BEANO_DESKTOP", prefix)
    retained = [block for label, block in blocks if label not in MANAGED_LABELS]
    profiles = [
        _entry("BEANO_DESKTOP", "Beano Desktop: IMX296 cameras", fields),
        _entry(
            "BEANO_DESKTOP_PERFORMANCE",
            "Beano Desktop Performance: IMX296 + locked clocks",
            fields,
            "beanoflight.performance=1",
        ),
        _entry(
            "BEANO_HEADLESS",
            "Beano Headless: IMX296 cameras",
            fields,
            "systemd.unit=multi-user.target",
        ),
        _entry(
            "BEANO_HEADLESS_PERFORMANCE",
            "Beano Headless Performance: IMX296 + locked clocks",
            fields,
            "systemd.unit=multi-user.target beanoflight.performance=1",
        ),
        _entry(
            "BEANO_SAFE_DESKTOP",
            "Beano Safe Driver Desktop: IMX296 disabled",
            fields,
            safe_extra,
        ),
        _entry(
            "BEANO_SAFE_HEADLESS",
            "Beano Safe Driver Headless: IMX296 disabled",
            fields,
            f"{safe_extra} systemd.unit=multi-user.target",
        ),
    ]
    return prefix.rstrip() + "\n\n" + "\n\n".join((*retained, *profiles)) + "\n"


def validate(text: str) -> None:
    _prefix, blocks = _blocks(text)
    labels = [label for label, _block in blocks]
    expected = MANAGED_LABELS - {"JetsonIO", "IMX296_SAFE"}
    missing = expected - set(labels)
    if missing:
        raise ValueError(f"generated boot configuration is missing {sorted(missing)}")
    if len(labels) != len(set(labels)):
        raise ValueError("generated boot configuration contains duplicate labels")
    for label, block in blocks:
        if label.startswith("BEANO_"):
            for field in ("LINUX", "FDT", "INITRD", "APPEND", "OVERLAYS"):
                _field(block, field)


def apply(config: Path) -> tuple[Path, bool]:
    original = config.read_text(encoding="utf-8")
    rendered = render_profiles(original)
    validate(rendered)
    if rendered == original:
        return config, False
    backup = config.with_name(f"{config.name}.beano-backup-{int(time.time())}")
    shutil.copy2(config, backup)
    descriptor, temporary_name = tempfile.mkstemp(prefix="extlinux.", dir=config.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
            temporary.write(rendered)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_name, config.stat().st_mode)
        os.replace(temporary_name, config)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
    return backup, True


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    result.add_argument("--apply", action="store_true")
    return result


def main() -> int:
    arguments = parser().parse_args()
    text = arguments.config.read_text(encoding="utf-8")
    rendered = render_profiles(text)
    validate(rendered)
    if not arguments.apply:
        print(rendered, end="")
        return 0
    backup, changed = apply(arguments.config)
    print(
        f"installed Beano boot profiles; backup: {backup}"
        if changed
        else "Beano boot profiles already installed"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
