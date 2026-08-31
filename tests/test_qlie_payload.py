import hashlib
import struct

from nagi.gameio.qlie.archive import _filename_seed30, _filename_seed31
from nagi.gameio.qlie.payload import (
    _QlieMersenneTwister,
    _build_u32_table,
    _filename_keys,
    _make_decode_table,
    _paddb,
    _paddd,
    _paddw,
    _pslld,
    _table_qword,
    _u32,
    _unbpe,
    read_filepack_entry,
)
from nagi.gameio.qlie.pe import load_icon_key_from_pe, load_reskey_from_pe
from test_qlie_toc import (
    HASH_HEADER,
    PACK_ENTRY,
    TRAILER,
    encode_filename,
    encode_filename30,
    write_filepack10,
)


def make_literal_bpe(data):
    assert len(data) <= 0xFFFF
    header = struct.pack("<4sB3sI", b"1PC\xff", 1, b"\x00\x00\x00", len(data))
    literal_table = b"\xff\x80\xfe"
    return header + literal_table + struct.pack("<H", len(data)) + data


def encrypt_normal_file(filename, plaintext, seed, resource_key):
    key_b, key_s = _filename_keys(filename, 0x86F7E2, 0x4437F1)
    length = len(plaintext)
    key = _u32(
        (0x56E213 ^ length ^ key_b)
        + key_b
        + length
        + (length & 0x00FFFFFF) * 13
    )
    key = ((key ^ seed) + key_s) & 0x00FFFFFF
    table = _build_u32_table(_u32(key * 13), 0x8A77F473)
    box = _make_decode_table(resource_key)
    key_index = (table[8] & 0x0D) * 8
    encrypted = bytearray(plaintext)
    key7 = _table_qword(table, 0x18)
    for offset in range(0, len(plaintext) - len(plaintext) % 8, 8):
        key6 = _table_qword(table, (key_index & 0xF) * 8)
        key6 ^= int.from_bytes(
            box[(key_index & 0x7F) * 8 : (key_index & 0x7F) * 8 + 8],
            "little",
        )
        key7 ^= key6
        key7 = _paddd(key7, key6)
        plain_qword = int.from_bytes(plaintext[offset : offset + 8], "little")
        encrypted[offset : offset + 8] = (plain_qword ^ key7).to_bytes(8, "little")
        key7 = _paddb(key7, plain_qword)
        key7 ^= plain_qword
        key7 = _pslld(key7, 1)
        key7 = _paddw(key7, plain_qword)
        key_index = (key_index + 1) & 0x7F
    return bytes(encrypted)


def encrypt_key_file(filename, plaintext, seed):
    key_b, key_s = _filename_keys(filename, 0x85F532, 0x33F641)
    length = len(plaintext)
    key = _u32(
        (0x8F32DC ^ length ^ key_b)
        + key_b
        + length
        + (length & 0x00FFFFFF) * 7
    )
    key = ((key ^ seed) + key_s) & 0x00FFFFFF
    table = _build_u32_table(_u32(key * 9), 0x8DF21431)
    key_index = (table[13] & 0x0F) * 8
    encrypted = bytearray(plaintext)
    key7 = _table_qword(table, 0x18)
    for offset in range(0, len(plaintext) - len(plaintext) % 8, 8):
        key6 = _table_qword(table, key_index)
        key7 ^= key6
        key7 = _paddd(key7, key6)
        plain_qword = int.from_bytes(plaintext[offset : offset + 8], "little")
        encrypted[offset : offset + 8] = (plain_qword ^ key7).to_bytes(8, "little")
        key7 = _paddb(key7, plain_qword)
        key7 ^= plain_qword
        key7 = _pslld(key7, 1)
        key7 = _paddw(key7, plain_qword)
        key_index = (key_index + 8) & 0x7F
    return bytes(encrypted)


def write_payload_pack(path, name, stored, original_size, compressed, obfuscated=2):
    hash_bytes = bytes(range(256))
    seed = _filename_seed31(hash_bytes)
    record = encode_filename(name, seed)
    record += PACK_ENTRY.pack(
        0,
        0,
        len(stored),
        original_size,
        compressed,
        obfuscated,
        0x12345678,
    )
    hash_header = HASH_HEADER.pack(b"HashVer1.4", 1, 2, 3, 0)
    toc = record + hash_header + bytes(0x48) + hash_bytes
    content = stored + toc + TRAILER.pack(b"FilePackVer3.1", 1, len(stored), 0)
    path.write_bytes(content)
    return content, seed


def write_synthetic_pe(path, resource_key, *, icon_key=None):
    image = bytearray(0x400)
    image[:2] = b"MZ"
    struct.pack_into("<I", image, 0x3C, 0x80)
    image[0x80:0x84] = b"PE\x00\x00"
    coff = 0x84
    struct.pack_into("<HHIIIHH", image, coff, 0x14C, 1, 0, 0, 0, 0xE0, 0x010F)
    optional = coff + 20
    struct.pack_into("<H", image, optional, 0x10B)
    struct.pack_into("<I", image, optional + 92, 16)
    struct.pack_into("<II", image, optional + 112, 0x1000, 0x200)
    section = optional + 0xE0
    image[section : section + 8] = b".rsrc\x00\x00\x00"
    struct.pack_into("<IIII", image, section + 8, 0x200, 0x1000, 0x200, 0x200)

    base = 0x200
    struct.pack_into("<IIHHHH", image, base, 0, 0, 0, 0, 0, 1)
    struct.pack_into("<II", image, base + 0x10, 10, 0x80000018)
    struct.pack_into("<IIHHHH", image, base + 0x18, 0, 0, 0, 0, 1, 0)
    struct.pack_into("<II", image, base + 0x28, 0x80000030, 0x80000040)
    struct.pack_into("<H12s", image, base + 0x30, 6, "RESKEY".encode("utf-16-le"))
    struct.pack_into("<IIHHHH", image, base + 0x40, 0, 0, 0, 0, 0, 1)
    struct.pack_into("<II", image, base + 0x50, 0x409, 0x58)
    struct.pack_into("<IIII", image, base + 0x58, 0x1080, len(resource_key), 0, 0)
    image[base + 0x80 : base + 0x80 + len(resource_key)] = resource_key
    if icon_key is not None:
        image.extend(b"\x05TIcon" + icon_key)
    path.write_bytes(image)


def encrypt_file30(filename, plaintext, seed, key_file, game_key):
    raw_filename = filename.encode("cp932")
    mutator = 0x85F532
    mt_seed = 0x33F641
    for index, value in enumerate(raw_filename):
        mutator = _u32(mutator + (index & 0xFF) * value)
        mt_seed = _u32(mt_seed ^ mutator)
    length = len(plaintext)
    mt_seed = _u32(
        mt_seed
        + (
            seed
            ^ (
                7 * (length & 0xFFFFFF)
                + length
                + mutator
                + (mutator ^ length ^ 0x8F32DC)
            )
        )
    )
    mt_seed = _u32(9 * (mt_seed & 0xFFFFFF)) ^ 0x453A
    generator = _QlieMersenneTwister(mt_seed)
    generator.xor_state(key_file)
    generator.xor_state(game_key)
    table = [generator.rand64() for _ in range(16)]
    for _ in range(9):
        generator.rand()
    rolling = generator.rand64()
    table_index = generator.rand() & 0x0F
    encrypted = bytearray(plaintext)
    for offset in range(0, len(plaintext) - len(plaintext) % 8, 8):
        table_value = table[table_index]
        rolling = _paddd(rolling ^ table_value, table_value)
        plain = int.from_bytes(plaintext[offset : offset + 8], "little")
        encrypted[offset : offset + 8] = (plain ^ rolling).to_bytes(8, "little")
        rolling = _paddb(rolling, plain) ^ plain
        rolling = _paddw(_pslld(rolling, 1), plain)
        table_index = (table_index + 1) & 0x0F
    return bytes(encrypted)


def write_payload_pack30(path, external_key, internal_key, game_key, plaintext):
    hash_bytes = bytes(range(256))
    seed = _filename_seed30(hash_bytes)
    key_name = "pack_keyfile_fixture.key"
    script_name = "scenario\\main.s"
    encoded_key = encrypt_file30(key_name, internal_key, seed, external_key, game_key)
    compressed = make_literal_bpe(plaintext)
    encoded_script = encrypt_file30(script_name, compressed, seed, internal_key, game_key)
    payload = encoded_key + encoded_script
    records = bytearray()
    records += encode_filename30(key_name, seed)
    records += PACK_ENTRY.pack(0, 0, len(encoded_key), len(internal_key), 0, 4, 1)
    records += encode_filename30(script_name, seed)
    records += PACK_ENTRY.pack(
        len(encoded_key),
        0,
        len(encoded_script),
        len(plaintext),
        1,
        4,
        2,
    )
    header = HASH_HEADER.pack(b"HashVer1.3", 1, 2, 3, 0)
    toc = bytes(records) + header + bytes(0x24) + hash_bytes
    path.write_bytes(
        payload + toc + TRAILER.pack(b"FilePackVer3.0", 2, len(payload), 0)
    )


def test_unbpe_decodes_literal_block():
    source = b"QLIE script payload\r\n"
    assert _unbpe(make_literal_bpe(source), len(source)) == source


def test_pe_parser_reads_named_reskey_without_loading_image(tmp_path):
    exe_path = tmp_path / "game.exe"
    resource_key = bytes(range(256))
    write_synthetic_pe(exe_path, resource_key)

    assert load_reskey_from_pe(exe_path) == resource_key


def test_pe_parser_reads_last_icon_key_without_loading_image(tmp_path):
    exe_path = tmp_path / "game.exe"
    resource_key = bytes(range(256))
    icon_key = bytes(reversed(range(256)))
    write_synthetic_pe(exe_path, resource_key, icon_key=icon_key)

    assert load_icon_key_from_pe(exe_path) == icon_key


def test_normal_entry_without_exe_returns_key_required(tmp_path):
    path = tmp_path / "data.pack"
    content, _ = write_payload_pack(path, "scenario\\main.s", b"encrypted", 9, 0)
    modified_before = path.stat().st_mtime_ns

    result = read_filepack_entry(path, internal_path="scenario/main.s")

    assert result.report.status == "key_required"
    assert result.report.decode_stage == "stored"
    assert result.report.stored_sha256 == hashlib.sha256(b"encrypted").hexdigest()
    assert result.data is None
    assert path.read_bytes() == content
    assert path.stat().st_mtime_ns == modified_before


def test_normal_compressed_entry_decodes_in_memory_with_reskey(tmp_path):
    path = tmp_path / "data.pack"
    exe_path = tmp_path / "game.exe"
    name = "scenario\\main.s"
    resource_key = bytes(range(256))
    plaintext = b"message('synthetic fixture only')\r\n"
    compressed = make_literal_bpe(plaintext)
    seed = _filename_seed31(bytes(range(256)))
    encrypted = encrypt_normal_file(name, compressed, seed, resource_key)
    content, _ = write_payload_pack(path, name, encrypted, len(plaintext), 1)
    write_synthetic_pe(exe_path, resource_key)
    pack_modified_before = path.stat().st_mtime_ns
    exe_modified_before = exe_path.stat().st_mtime_ns

    result = read_filepack_entry(path, entry_index=0, exe_path=exe_path)

    assert result.report.status == "supported"
    assert result.report.decode_stage == "complete"
    assert result.report.compression_applied is True
    assert result.report.decoded_size == len(plaintext)
    assert result.report.decoded_sha256 == hashlib.sha256(plaintext).hexdigest()
    assert result.report.decoded_prefix_hex == plaintext[:32].hex()
    assert result.data == plaintext
    assert path.read_bytes() == content
    assert path.stat().st_mtime_ns == pack_modified_before
    assert exe_path.stat().st_mtime_ns == exe_modified_before
    assert "data" not in result.report.to_dict()


def test_keyfile_entry_decodes_without_exe(tmp_path):
    path = tmp_path / "data.pack"
    name = "pack_keyfile_fixture.key"
    plaintext = b"KeyFile ver1.0\x00\x00"
    seed = _filename_seed31(bytes(range(256)))
    encrypted = encrypt_key_file(name, plaintext, seed)
    write_payload_pack(path, name, encrypted, len(plaintext), 0, obfuscated=1)

    result = read_filepack_entry(path, entry_index=0)

    assert result.report.status == "supported"
    assert result.report.key_source == "TOC seed (keyfile branch)"
    assert result.data == plaintext


def test_wrong_reskey_fails_closed_at_bpe_signature(tmp_path):
    path = tmp_path / "data.pack"
    name = "scenario\\main.s"
    correct_key = bytes(range(256))
    wrong_key = bytes(reversed(range(256)))
    plaintext = b"fixture"
    compressed = make_literal_bpe(plaintext)
    seed = _filename_seed31(bytes(range(256)))
    encrypted = encrypt_normal_file(name, compressed, seed, correct_key)
    write_payload_pack(path, name, encrypted, len(plaintext), 1)

    result = read_filepack_entry(path, entry_index=0, resource_key=wrong_key)

    assert result.report.status == "invalid"
    assert "BPE signature" in result.report.reason
    assert result.data is None


def test_probe_rejects_missing_entry_and_size_over_limit(tmp_path):
    path = tmp_path / "data.pack"
    write_payload_pack(path, "main.s", b"1234", 4, 0)

    missing = read_filepack_entry(path, internal_path="missing.s")
    oversized = read_filepack_entry(path, entry_index=0, max_stored_size=3)

    assert missing.report.status == "not_found"
    assert oversized.report.status == "limit_exceeded"
    assert missing.data is None
    assert oversized.data is None


def test_filepack30_uses_external_and_internal_keys_with_icon_key(tmp_path):
    game_dir = tmp_path / "game"
    game_data = game_dir / "GameData"
    dll_dir = game_dir / "DLL"
    game_data.mkdir(parents=True)
    dll_dir.mkdir()
    archive = game_data / "data0.pack"
    exe_path = game_dir / "game.exe"
    external_key = bytes((index * 3) & 0xFF for index in range(4096))
    internal_key = bytes((index * 5 + 1) & 0xFF for index in range(4096))
    game_key = bytes((index * 7 + 2) & 0xFF for index in range(256))
    plaintext = b"message('synthetic FilePackVer3.0 fixture')\r\n"
    (dll_dir / "key.fkey").write_bytes(external_key)
    write_synthetic_pe(exe_path, bytes(range(256)), icon_key=game_key)
    write_payload_pack30(archive, external_key, internal_key, game_key, plaintext)

    result = read_filepack_entry(
        archive,
        internal_path="scenario/main.s",
        exe_path=exe_path,
    )

    assert result.report.status == "supported"
    assert result.report.compression_applied is True
    assert "internal pack_keyfile" in result.report.key_source
    assert result.data == plaintext


def test_filepack10_unencrypted_payload_is_read_without_keys(tmp_path):
    path = tmp_path / "patch.pack"
    payload = b"synthetic patch script"
    write_filepack10(path, payload=payload)

    result = read_filepack_entry(path, entry_index=0)

    assert result.report.status == "supported"
    assert result.report.key_source == "not required"
    assert result.data == payload
