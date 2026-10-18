"""Lossless historical JSON storage; current documents and API JSON stay plain."""
import json
import zlib

MAGIC = b'RSV1\x00'


def pack_history(text: str):
    raw = text.encode('utf-8')
    packed = MAGIC + zlib.compress(raw, 6)
    return packed if len(raw) >= 2048 and len(packed) < len(raw) else text


def history_text(value):
    if isinstance(value, str):
        return value
    raw = bytes(value)
    return (zlib.decompress(raw[len(MAGIC):]) if raw.startswith(MAGIC) else raw).decode('utf-8')


def history_document(value):
    return json.loads(history_text(value))
