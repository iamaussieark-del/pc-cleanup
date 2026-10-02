"""A tiny NTFS volume image for testing deleted-file recovery without administrator.

It has the parts the scanner reads: a boot sector, a file table with update
sequence fixups, the $Bitmap free-space map, a folder, two deleted files with
contents, one deleted file whose disk space was reused, and one live file.
"""

from __future__ import annotations

import struct

SECTOR = 512
CLUSTER = 4096
RECORD = 1024
MFT_LCN = 2
MFT_CLUSTERS = 8
BITMAP_LCN = 10
REPORT_LCN = 20
REUSED_LCN = 21
TOTAL_CLUSTERS = 32
FILETIME = 133_000_000_000_000_000  # 2022-06-15
REPORT = bytes((index * 7) % 251 for index in range(3000))
NOTE = b"hello from a deleted file"


def _resident(kind: int, value: bytes, attr_id: int) -> bytes:
    header = struct.pack("<IIBBHHHIHBB", kind, 0, 0, 0, 0x18, 0, attr_id, len(value), 0x18, 0, 0)
    body = header + value
    body += b"\0" * (-len(body) % 8)
    return body[:4] + struct.pack("<I", len(body)) + body[8:]


def _nonresident(lcn: int, clusters: int, size: int, attr_id: int) -> bytes:
    runs = bytes([0x11, clusters, lcn, 0]) if lcn < 128 and clusters < 128 else b""
    header = struct.pack("<IIBBHHHQQHHIQQQ", 0x80, 0, 1, 0, 0x40, 0, attr_id, 0, clusters - 1, 0x40, 0, 0, clusters * CLUSTER, size, size)
    body = header + runs
    body += b"\0" * (-len(body) % 8)
    return body[:4] + struct.pack("<I", len(body)) + body[8:]


def _standard_info() -> bytes:
    return struct.pack("<QQQQI", FILETIME, FILETIME, FILETIME, FILETIME, 0x20) + b"\0" * 12


def _file_name(parent: int, name: str) -> bytes:
    encoded = name.encode("utf-16le")
    value = struct.pack("<Q", parent | (1 << 48)) + struct.pack("<QQQQ", FILETIME, FILETIME, FILETIME, FILETIME)
    value += struct.pack("<QQII", 0, 0, 0, 0) + bytes([len(name), 1]) + encoded
    return value


def _record(flags: int, attributes: list[bytes]) -> bytes:
    first = 0x38
    body = b"".join(attributes) + struct.pack("<I", 0xFFFFFFFF) + b"\0" * 4
    header = struct.pack("<4sHHQHHHHIIQH", b"FILE", 0x30, 3, 0, 1, 1, first, flags, first + len(body), RECORD, 0, 4)
    record = bytearray(header.ljust(0x30, b"\0"))
    record += b"\0" * 8  # update sequence array, filled in below
    record = record[:first] + body
    record += b"\0" * (RECORD - len(record))
    usn = 0x0001
    struct.pack_into("<H", record, 0x30, usn)
    for index in (1, 2):
        end = index * SECTOR - 2
        record[0x30 + index * 2 : 0x32 + index * 2] = record[end : end + 2]
        struct.pack_into("<H", record, end, usn)
    return bytes(record)


def build(path: str) -> None:
    image = bytearray(TOTAL_CLUSTERS * CLUSTER)
    boot = bytearray(SECTOR)
    boot[3:11] = b"NTFS    "
    struct.pack_into("<H", boot, 0x0B, SECTOR)
    boot[0x0D] = CLUSTER // SECTOR
    struct.pack_into("<Q", boot, 0x30, MFT_LCN)
    struct.pack_into("<b", boot, 0x40, -10)
    image[0:SECTOR] = boot

    records = {
        0: _record(1, [_resident(0x10, _standard_info(), 0), _resident(0x30, _file_name(5, "$MFT"), 1), _nonresident(MFT_LCN, MFT_CLUSTERS, MFT_CLUSTERS * CLUSTER, 2)]),
        5: _record(3, [_resident(0x10, _standard_info(), 0), _resident(0x30, _file_name(5, "."), 1)]),
        6: _record(1, [_resident(0x10, _standard_info(), 0), _resident(0x30, _file_name(5, "$Bitmap"), 1), _nonresident(BITMAP_LCN, 1, TOTAL_CLUSTERS // 8, 2)]),
        24: _record(3, [_resident(0x10, _standard_info(), 0), _resident(0x30, _file_name(5, "Docs"), 1)]),
        25: _record(0, [_resident(0x10, _standard_info(), 0), _resident(0x30, _file_name(24, "report.bin"), 1), _nonresident(REPORT_LCN, 1, len(REPORT), 2)]),
        26: _record(0, [_resident(0x10, _standard_info(), 0), _resident(0x30, _file_name(24, "note.txt"), 1), _resident(0x80, NOTE, 2)]),
        27: _record(1, [_resident(0x10, _standard_info(), 0), _resident(0x30, _file_name(24, "still-here.txt"), 1), _resident(0x80, b"live", 2)]),
        28: _record(0, [_resident(0x10, _standard_info(), 0), _resident(0x30, _file_name(5, "reused.bin"), 1), _nonresident(REUSED_LCN, 1, CLUSTER, 2)]),
    }
    for index, record in records.items():
        offset = MFT_LCN * CLUSTER + index * RECORD
        image[offset : offset + RECORD] = record

    # Clusters 0-11 hold the boot sector, file table, and bitmap. The deleted
    # report's cluster is free. The other deleted file's cluster was reused.
    bitmap = bytearray(TOTAL_CLUSTERS // 8)
    for cluster in list(range(0, 12)) + [REUSED_LCN]:
        bitmap[cluster // 8] |= 1 << (cluster % 8)
    image[BITMAP_LCN * CLUSTER : BITMAP_LCN * CLUSTER + len(bitmap)] = bitmap
    image[REPORT_LCN * CLUSTER : REPORT_LCN * CLUSTER + len(REPORT)] = REPORT
    image[REUSED_LCN * CLUSTER : REUSED_LCN * CLUSTER + CLUSTER] = b"\xee" * CLUSTER
    with open(path, "wb") as handle:
        handle.write(image)
