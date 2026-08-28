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


def main() -> None:
    parser = argparse.ArgumentParser(description="Materialize the pinned non-weight MolFormer cache.")
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    with args.lock.open("rb") as source:
        lock = tomllib.load(source)

    files = lock["files"]
    snapshot = Path(
        snapshot_download(
            repo_id=lock["snapshot"]["repository"],
            revision=lock["snapshot"]["revision"],
            allow_patterns=list(files),
            cache_dir=args.output / "hub",
        )
    )

    receipt = []
    for name, expected in files.items():
        path = snapshot / name
        if sha256(path) != expected:
            raise ValueError(f"Downloaded {name} does not match molformer.lock.toml.")
        receipt.append(f"{expected}  {path.relative_to(args.output)}")
    (args.output / ".complete").write_text("\n".join(receipt) + "\n")


if __name__ == "__main__":
    main()
