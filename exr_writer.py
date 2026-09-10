# exr_writer.py
# This file is part of I, Voyager (https://ivoyager.dev)
# *****************************************************************************
# Copyright 2019-2026 Charlie Whitfield
# Licensed under the Apache License, Version 2.0 (the "License").
# *****************************************************************************
"""Write an uncompressed half-float RGBA OpenEXR file. numpy only, no OpenEXR dep.

Godot imports .exr to Image.FORMAT_RGBAH and generates its mipmaps by averaging
in LINEAR light, which is what a map holding radiance needs and what an 8-bit
sRGB-encoded PNG cannot give (Godot would average the encoded codes). That is
the whole reason this exists; the format is otherwise nobody's favourite.

Only the subset Godot's loader reads is emitted: uncompressed scanline, four HALF
channels, one scanline per chunk. That is ~60 lines against a dependency the rest
of `tools` does not have -- and `verify_roundtrip()` reads the file back with this
module's own parser, so a malformed write fails here rather than in the engine.

CHANNELS ARE WRITTEN IN ALPHABETICAL ORDER (A, B, G, R), per the spec, in both the
channel list and each scanline's payload. Getting that backwards produces a file
that opens without error and has its red and blue swapped.
"""

from __future__ import annotations

import struct

import numpy as np

MAGIC = 20000630
VERSION = 2
PIXEL_TYPE_HALF = 1
CHANNELS = ("A", "B", "G", "R")  # alphabetical, as the format requires


def _attribute(name: str, kind: str, payload: bytes) -> bytes:
    return name.encode() + b"\0" + kind.encode() + b"\0" + struct.pack("<i", len(payload)) + payload


def _header(width: int, height: int) -> bytes:
    channel_list = b""
    for name in CHANNELS:
        channel_list += (name.encode() + b"\0" + struct.pack("<i", PIXEL_TYPE_HALF)
                         + struct.pack("<B", 0) + b"\0\0\0" + struct.pack("<ii", 1, 1))
    channel_list += b"\0"
    window = struct.pack("<iiii", 0, 0, width - 1, height - 1)
    return (struct.pack("<ii", MAGIC, VERSION)
            + _attribute("channels", "chlist", channel_list)
            + _attribute("compression", "compression", struct.pack("<B", 0))
            + _attribute("dataWindow", "box2i", window)
            + _attribute("displayWindow", "box2i", window)
            + _attribute("lineOrder", "lineOrder", struct.pack("<B", 0))
            + _attribute("pixelAspectRatio", "float", struct.pack("<f", 1.0))
            + _attribute("screenWindowCenter", "v2f", struct.pack("<ff", 0.0, 0.0))
            + _attribute("screenWindowWidth", "float", struct.pack("<f", 1.0))
            + b"\0")


def write_rgba_half(path, rgba) -> None:
    """Write `rgba`, a (height, width, 4) array in R,G,B,A order, to `path`."""
    rgba = np.ascontiguousarray(rgba, dtype=np.float16)
    if rgba.ndim != 3 or rgba.shape[2] != 4:
        raise ValueError(f"expected (height, width, 4), got {rgba.shape}")
    height, width = rgba.shape[:2]
    header = _header(width, height)
    row_bytes = width * 2
    chunk_size = 8 + row_bytes * len(CHANNELS)  # y + payload size + four channel rows
    table_end = len(header) + 8 * height
    offsets = struct.pack(f"<{height}q", *(table_end + chunk_size * y for y in range(height)))
    source = {"R": rgba[..., 0], "G": rgba[..., 1], "B": rgba[..., 2], "A": rgba[..., 3]}
    chunks = []
    for y in range(height):
        payload = b"".join(source[name][y].tobytes() for name in CHANNELS)
        chunks.append(struct.pack("<ii", y, len(payload)) + payload)
    with open(path, "wb") as file:
        file.write(header + offsets + b"".join(chunks))


def read_rgba_half(path):
    """Read back a file written by `write_rgba_half`. Not a general EXR reader:
    it accepts only what this module emits, and raises on anything else."""
    data = open(path, "rb").read()
    magic, version = struct.unpack_from("<ii", data, 0)
    if magic != MAGIC or version != VERSION:
        raise ValueError("not an uncompressed EXR written by this module")
    position = 8
    attributes = {}
    while data[position] != 0:
        name_end = data.index(b"\0", position)
        name = data[position:name_end].decode()
        kind_end = data.index(b"\0", name_end + 1)
        size = struct.unpack_from("<i", data, kind_end + 1)[0]
        payload_at = kind_end + 5
        attributes[name] = data[payload_at:payload_at + size]
        position = payload_at + size
    position += 1
    x_min, y_min, x_max, y_max = struct.unpack("<iiii", attributes["dataWindow"])
    width, height = x_max - x_min + 1, y_max - y_min + 1
    offsets = struct.unpack_from(f"<{height}q", data, position)
    rgba = np.zeros((height, width, 4), dtype=np.float16)
    index = {name: i for i, name in enumerate(("R", "G", "B", "A"))}
    for y, offset in enumerate(offsets):
        row_at = offset + 8
        for name in CHANNELS:
            row = np.frombuffer(data, dtype=np.float16, count=width, offset=row_at)
            rgba[y, :, index[name]] = row
            row_at += width * 2
    return rgba


def verify_roundtrip(path, rgba) -> None:
    """Raise unless `path` reads back bit-identical to `rgba` as float16."""
    written = read_rgba_half(path)
    expected = np.ascontiguousarray(rgba, dtype=np.float16)
    if written.shape != expected.shape:
        raise ValueError(f"round trip changed shape: {expected.shape} -> {written.shape}")
    differing = int((written.view(np.uint16) != expected.view(np.uint16)).sum())
    if differing:
        raise ValueError(f"round trip changed {differing} of {expected.size} half values")
