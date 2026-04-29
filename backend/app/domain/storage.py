from __future__ import annotations

import hashlib
from pathlib import Path


class LocalFileStorage:
    def __init__(self, root: Path) -> None:
        self.root = root

    def save(self, case_id: str, filename: str, content: bytes) -> tuple[str, str, int]:
        sha256 = hashlib.sha256(content).hexdigest()
        safe_name = Path(filename).name or "document.bin"
        target_dir = self.root / "cases" / case_id / sha256[:2] / sha256[2:4]
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / safe_name
        target.write_bytes(content)
        return str(target), sha256, len(content)

    def save_artifact(
        self,
        document_id: str,
        filename: str,
        content: bytes,
    ) -> tuple[str, str, int]:
        sha256 = hashlib.sha256(content).hexdigest()
        safe_name = Path(filename).name or "artifact.bin"
        target_dir = self.root / "artifacts" / document_id / sha256[:2] / sha256[2:4]
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / safe_name
        target.write_bytes(content)
        return str(target), sha256, len(content)

    def save_quarantine(self, case_id: str, filename: str, content: bytes) -> tuple[str, str, int]:
        sha256 = hashlib.sha256(content).hexdigest()
        safe_name = Path(filename).name or "quarantined.bin"
        target_dir = self.root / "quarantine" / "cases" / case_id / sha256[:2] / sha256[2:4]
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / safe_name
        target.write_bytes(content)
        return str(target), sha256, len(content)
