"""Minimal, read-only PE key readers for QLIE archives."""

from __future__ import annotations

import struct
from pathlib import Path


MAX_PE_SIZE = 256 * 1024 * 1024
MAX_RESOURCE_KEY_SIZE = 4 * 1024 * 1024
RT_RCDATA = 10
ICON_KEY_MARKER = b"\x05TIcon"
ICON_KEY_SIZE = 0x100


class PeResourceError(ValueError):
    def __init__(self, status, reason):
        super().__init__(reason)
        self.status = status
        self.reason = reason


def _require(data, offset, size, label):
    if offset < 0 or size < 0 or offset + size > len(data):
        raise PeResourceError("invalid", f"{label} extends beyond the PE file")


def _u16(data, offset, label):
    _require(data, offset, 2, label)
    return struct.unpack_from("<H", data, offset)[0]


def _u32(data, offset, label):
    _require(data, offset, 4, label)
    return struct.unpack_from("<I", data, offset)[0]


class _PeResources:
    def __init__(self, data):
        self.data = data
        self.sections = []
        self.resource_rva = 0
        self.resource_size = 0
        self.resource_offset = 0
        self._parse_headers()

    def _parse_headers(self):
        _require(self.data, 0, 0x40, "DOS header")
        if self.data[:2] != b"MZ":
            raise PeResourceError("invalid", "file does not have an MZ header")
        pe_offset = _u32(self.data, 0x3C, "PE header offset")
        _require(self.data, pe_offset, 24, "PE header")
        if self.data[pe_offset : pe_offset + 4] != b"PE\x00\x00":
            raise PeResourceError("invalid", "file does not have a PE signature")

        coff = pe_offset + 4
        section_count = _u16(self.data, coff + 2, "section count")
        optional_size = _u16(self.data, coff + 16, "optional header size")
        if section_count > 96:
            raise PeResourceError("invalid", "PE section count exceeds safety limit")
        optional = coff + 20
        _require(self.data, optional, optional_size, "optional header")
        magic = _u16(self.data, optional, "optional header magic")
        if magic == 0x10B:
            directory_count_offset = optional + 92
            directories_offset = optional + 96
        elif magic == 0x20B:
            directory_count_offset = optional + 108
            directories_offset = optional + 112
        else:
            raise PeResourceError("unsupported", f"unsupported PE optional header: 0x{magic:04x}")
        if directories_offset + 24 > optional + optional_size:
            raise PeResourceError("invalid", "optional header truncates the resource data directory")
        directory_count = _u32(self.data, directory_count_offset, "data directory count")
        if directory_count <= 2:
            raise PeResourceError("key_unavailable", "PE file has no resource data directory")
        _require(self.data, directories_offset + 16, 8, "resource data directory")
        self.resource_rva, self.resource_size = struct.unpack_from(
            "<II",
            self.data,
            directories_offset + 16,
        )
        if not self.resource_rva or not self.resource_size:
            raise PeResourceError("key_unavailable", "PE resource data directory is empty")

        section_table = optional + optional_size
        _require(self.data, section_table, section_count * 40, "section table")
        for index in range(section_count):
            offset = section_table + index * 40
            virtual_size, virtual_address, raw_size, raw_offset = struct.unpack_from(
                "<IIII",
                self.data,
                offset + 8,
            )
            self.sections.append((virtual_address, virtual_size, raw_offset, raw_size))
        self.resource_offset = self.rva_to_offset(self.resource_rva, 1)

    def rva_to_offset(self, rva, size):
        for virtual_address, virtual_size, raw_offset, raw_size in self.sections:
            span = max(virtual_size, raw_size)
            if virtual_address <= rva and rva - virtual_address + size <= span:
                delta = rva - virtual_address
                if delta + size > raw_size:
                    break
                file_offset = raw_offset + delta
                _require(self.data, file_offset, size, "RVA-mapped data")
                return file_offset
        raise PeResourceError("invalid", f"PE RVA 0x{rva:08x} is not backed by file data")

    def resource_offset_at(self, relative, size, label):
        if relative < 0 or relative + size > self.resource_size:
            raise PeResourceError("invalid", f"{label} extends beyond the resource directory")
        offset = self.resource_offset + relative
        _require(self.data, offset, size, label)
        return offset

    def directory_entries(self, relative):
        offset = self.resource_offset_at(relative, 16, "resource directory")
        named_count, id_count = struct.unpack_from("<HH", self.data, offset + 12)
        count = named_count + id_count
        if count > 4096:
            raise PeResourceError("invalid", "resource directory entry count exceeds safety limit")
        entries_offset = self.resource_offset_at(relative + 16, count * 8, "resource entries")
        return [
            struct.unpack_from("<II", self.data, entries_offset + index * 8)
            for index in range(count)
        ]

    def resource_name(self, name_field):
        if not name_field & 0x80000000:
            return None
        relative = name_field & 0x7FFFFFFF
        offset = self.resource_offset_at(relative, 2, "resource name length")
        char_count = _u16(self.data, offset, "resource name length")
        name_offset = self.resource_offset_at(relative + 2, char_count * 2, "resource name")
        try:
            return self.data[name_offset : name_offset + char_count * 2].decode("utf-16-le")
        except UnicodeDecodeError as exc:
            raise PeResourceError("invalid", f"resource name is not UTF-16LE: {exc}") from exc

    @staticmethod
    def directory_relative(target_field, label):
        if not target_field & 0x80000000:
            raise PeResourceError("invalid", f"{label} does not point to a resource directory")
        return target_field & 0x7FFFFFFF

    def find_reskey(self):
        type_entry = next(
            (
                entry
                for entry in self.directory_entries(0)
                if not entry[0] & 0x80000000 and (entry[0] & 0xFFFF) == RT_RCDATA
            ),
            None,
        )
        if type_entry is None:
            raise PeResourceError("key_unavailable", "PE file has no RCDATA resource type")
        type_directory = self.directory_relative(type_entry[1], "RCDATA entry")

        name_entry = next(
            (
                entry
                for entry in self.directory_entries(type_directory)
                if (self.resource_name(entry[0]) or "").casefold() == "reskey"
            ),
            None,
        )
        if name_entry is None:
            raise PeResourceError("key_unavailable", "PE file has no RCDATA/RESKEY resource")
        language_directory = self.directory_relative(name_entry[1], "RESKEY entry")
        language_entries = self.directory_entries(language_directory)
        if not language_entries:
            raise PeResourceError("key_unavailable", "RCDATA/RESKEY has no language entry")
        data_target = language_entries[0][1]
        if data_target & 0x80000000:
            raise PeResourceError("invalid", "RESKEY language entry unexpectedly points to a directory")
        data_entry_offset = self.resource_offset_at(
            data_target & 0x7FFFFFFF,
            16,
            "RESKEY data entry",
        )
        data_rva, data_size = struct.unpack_from("<II", self.data, data_entry_offset)
        if data_size < 0x80:
            raise PeResourceError("key_unavailable", "RCDATA/RESKEY is shorter than 128 bytes")
        if data_size > MAX_RESOURCE_KEY_SIZE:
            raise PeResourceError("invalid", "RCDATA/RESKEY exceeds safety limit")
        data_offset = self.rva_to_offset(data_rva, data_size)
        return self.data[data_offset : data_offset + data_size]


def _read_stable_pe(path):
    source_path = Path(path).expanduser().resolve()
    try:
        stat_before = source_path.stat()
        if not source_path.is_file():
            raise PeResourceError("unreadable", "EXE path is not a regular file")
        if stat_before.st_size > MAX_PE_SIZE:
            raise PeResourceError("invalid", "EXE size exceeds safety limit")
        data = source_path.read_bytes()
        stat_after = source_path.stat()
    except PeResourceError:
        raise
    except OSError as exc:
        raise PeResourceError("unreadable", f"unable to read EXE: {exc}") from exc
    if (stat_before.st_size, stat_before.st_mtime_ns) != (
        stat_after.st_size,
        stat_after.st_mtime_ns,
    ):
        raise PeResourceError("changed", "EXE changed while QLIE key data was being read")
    return data


def _validate_pe_signature(data):
    _require(data, 0, 0x40, "DOS header")
    if data[:2] != b"MZ":
        raise PeResourceError("invalid", "file does not have an MZ header")
    pe_offset = _u32(data, 0x3C, "PE header offset")
    _require(data, pe_offset, 4, "PE signature")
    if data[pe_offset : pe_offset + 4] != b"PE\x00\x00":
        raise PeResourceError("invalid", "file does not have a PE signature")


def load_reskey_from_pe(path):
    """Read RCDATA/RESKEY without loading or executing the PE image."""

    return _PeResources(_read_stable_pe(path)).find_reskey()


def load_icon_key_from_pe(path):
    """Read QLIE 3.0 IconKeyImage bytes without loading the PE image."""

    data = _read_stable_pe(path)
    _validate_pe_signature(data)
    marker_offset = data.rfind(ICON_KEY_MARKER)
    if marker_offset < 0:
        raise PeResourceError("key_unavailable", "PE file has no QLIE IconKeyImage marker")
    key_offset = marker_offset + len(ICON_KEY_MARKER)
    _require(data, key_offset, ICON_KEY_SIZE, "IconKeyImage key data")
    return data[key_offset : key_offset + ICON_KEY_SIZE]
