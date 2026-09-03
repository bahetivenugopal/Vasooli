"""One CLI for all three generators.

    python -m data.generators.cli payments --seed 42 --out data/samples/payments
    python -m data.generators.cli mandates --seed 42 --out data/samples/mandates
    python -m data.generators.cli invoices --seed 42 --out data/samples/invoices
    python -m data.generators.cli all      --seed 42 --out data/samples

Run from the repo root. Each command writes `<dataset>.jsonl` plus
`<dataset>.manifest.json`, and prints the batch id and the output hash — the two
things a reader needs to check that a committed sample is the batch it claims to
be:

    python -m data.generators.cli all --seed 42 --out /tmp/check
    diff /tmp/check/payments/payments.jsonl data/samples/payments/payments.jsonl
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from data.generators import REPO_ROOT
from data.generators import invoices as invoices_generator
from data.generators import mandates as mandates_generator
from data.generators import payments as payments_generator
from data.generators.common import GeneratedBatch, load_config

GENERATORS = {
    payments_generator.DATASET: (payments_generator, "data/generators/configs/payments.default.json"),
    mandates_generator.DATASET: (mandates_generator, "data/generators/configs/mandates.default.json"),
    invoices_generator.DATASET: (invoices_generator, "data/generators/configs/invoices.default.json"),
}

DEFAULT_SEED = 42


def run_one(dataset: str, seed: int, config_path: str, out_dir: Path) -> GeneratedBatch:
    """Generate one dataset and write it, with its manifest, to `out_dir`."""
    module, _ = GENERATORS[dataset]
    config = load_config(config_path)
    batch = module.generate(
        config,
        seed,
        config_path=config_path,
        # The only wall-clock read in the whole foundry, and it lands solely in
        # the manifest's `nondeterministic` block. Everything else is a pure
        # function of (seed, config, generator version).
        generated_at=datetime.now(UTC),
    )
    data_path, manifest_path = batch.write(out_dir)
    print(f"  {dataset:9s} batch_id={batch.batch_id}")
    print(f"    rows     : {batch.manifest['output']['rows']}")
    print(f"    sha256   : {batch.manifest['output']['sha256']}")
    print(f"    data     : {data_path}")
    print(f"    manifest : {manifest_path}")
    return batch


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m data.generators.cli",
        description="Generate a seeded, reproducible Vasooli synthetic batch.",
    )
    parser.add_argument("dataset", choices=[*sorted(GENERATORS), "all"])
    parser.add_argument(
        "--seed", type=int, default=DEFAULT_SEED, help="Master seed (default: 42)."
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Config file. Defaults to the dataset's config in data/generators/configs/. "
        "Not allowed with 'all', which uses each dataset's default.",
    )
    parser.add_argument(
        "--out",
        default="data/samples",
        help="Output directory (default: data/samples). 'all' writes one subdirectory per dataset.",
    )
    args = parser.parse_args(argv)

    if args.dataset == "all" and args.config:
        parser.error("--config applies to a single dataset; run each one separately")

    out_root = Path(args.out)
    if not out_root.is_absolute():
        out_root = REPO_ROOT / out_root

    datasets = sorted(GENERATORS) if args.dataset == "all" else [args.dataset]
    print(f"seed={args.seed}")
    for dataset in datasets:
        config_path = args.config or GENERATORS[dataset][1]
        target = out_root / dataset if args.dataset == "all" else out_root
        run_one(dataset, args.seed, config_path, target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
