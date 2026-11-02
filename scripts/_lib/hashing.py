"""Shared hashing helper. Single owner of `sha256_file`, split out of wave_prepare.py so that
sync_verify.py and resolve_wave.py can both import it without wave_prepare.py needing to import
anything FROM sync_verify.py in return (sync_verify.py already imports from wave_prepare.py, so a
reverse top-level import would be circular) -- see wave_prepare.py's KNOWN_SOURCES for the
consumer of this un-circularity.

    from _lib.hashing import sha256_file
"""
import hashlib


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
