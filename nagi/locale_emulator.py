"""Nagi's machine-local Locale Emulator preference (never committed)."""

from pathlib import Path
from .paths import state_root

from .gameio.deployment_runtime.locale_support import (
    load_settings,
    public_settings,
    save_settings,
)


class LocaleEmulatorSettings:
    def __init__(self, path=None):
        self.path = (
            Path(path)
            if path
            else state_root() / "web/locale-emulator.json"
        )

    def public(self):
        return public_settings(self.path)

    def save(self, directory):
        save_settings(self.path, directory)
        return self.public()

    def snapshot(self):
        return load_settings(self.path)
