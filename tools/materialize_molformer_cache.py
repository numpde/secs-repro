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


def verify_snapshot(snapshot: Path, files: dict[str, str]) -> list[str]:
    actual_files = {path.name for path in snapshot.iterdir()}
    if actual_files != set(files):
        raise ValueError("Cached MolFormer files do not match molformer.lock.toml.")

    receipt = []
    for name, expected in files.items():
        path = snapshot / name
        if sha256(path) != expected:
            raise ValueError(f"Cached {name} does not match molformer.lock.toml.")
        receipt.append(f"{expected}  {path}")
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description="Materialize the pinned non-weight MolFormer cache.")
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()

    with args.lock.open("rb") as source:
        lock = tomllib.load(source)

    files = lock["files"]
    repository = lock["snapshot"]["repository"]
    revision = lock["snapshot"]["revision"]
    if args.verify_only:
        repository_directory = f"models--{repository.replace('/', '--')}"
        snapshot = args.output / "hub" / repository_directory / "snapshots" / revision
    else:
        snapshot = Path(
            snapshot_download(
                repo_id=repository,
                revision=revision,
                allow_patterns=list(files),
                cache_dir=args.output / "hub",
            )
        )

    receipt = verify_snapshot(snapshot, files)
    if not args.verify_only:
        relative_receipt = [line.replace(str(args.output) + "/", "", 1) for line in receipt]
        (args.output / ".complete").write_text("\n".join(relative_receipt) + "\n")


if __name__ == "__main__":
    main()
