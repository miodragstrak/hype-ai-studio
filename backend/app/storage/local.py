from pathlib import Path
from typing import BinaryIO


class LocalStorage:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _safe(self, key: str) -> Path:
        candidate = (self.root / key).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ValueError("storage key escapes storage root")
        return candidate

    def save(self, key: str, source: bytes | BinaryIO) -> str:
        path = self._safe(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as output:
            if isinstance(source, bytes):
                output.write(source)
            else:
                while chunk := source.read(1024 * 1024):
                    output.write(chunk)
        return key

    def save_file(self, key: str, source_path: Path) -> str:
        path = self._safe(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(source_path.read_bytes())
        return key

    def path(self, key: str) -> Path:
        return self._safe(key)

    def exists(self, key: str) -> bool:
        return self._safe(key).exists()
