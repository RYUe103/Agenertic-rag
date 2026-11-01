import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path


def sha(value):
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def write(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f'.{os.getpid()}.tmp')
    temporary.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


@contextmanager
def lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def read_rows(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def verify_pack(pack):
    pack = Path(pack)
    manifest = json.loads((pack/'manifest.json').read_text())
    for name, digest in manifest['files'].items():
        path = (pack/name).resolve()
        if not path.is_relative_to(pack.resolve()) or sha(path.read_bytes()) != digest:
            raise ValueError('pack_identity_mismatch')
    return manifest
