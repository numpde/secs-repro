import argparse
import hashlib
import tomllib
from pathlib import Path

from huggingface_hub import snapshot_download


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def verify_locked_files(snapshot: Path, files: dict[str, str]) -> None:
    for name, expected in files.items():
        path = snapshot / name
        if sha256(path) != expected:
            raise ValueError(f"Cached {name} does not match molformer.lock.toml.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Download or verify the pinned MolFormer runtime files.")
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()

    with args.lock.open("rb") as source:
        lock = tomllib.load(source)

    files = lock["files"]
    repository = lock["snapshot"]["repository"]
    revision = lock["snapshot"]["revision"]
    snapshot = Path(
        snapshot_download(
            repo_id=repository,
            revision=revision,
            allow_patterns=list(files),
            cache_dir=args.output / "hub",
            local_files_only=args.verify_only,
        )
    )

    verify_locked_files(snapshot, files)


if __name__ == "__main__":
    main()
