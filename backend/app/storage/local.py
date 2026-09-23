from pathlib import Path
from shutil import copyfileobj
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
        temporary = path.with_suffix(path.suffix + ".partial")
        try:
            with source_path.open("rb") as source, temporary.open("wb") as output:
                copyfileobj(source, output, 1024 * 1024)
            temporary.replace(path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return key

    def path(self, key: str) -> Path:
        return self._safe(key)

    def exists(self, key: str) -> bool:
        return self._safe(key).exists()

    def delete(self, key: str) -> None:
        self._safe(key).unlink(missing_ok=True)
