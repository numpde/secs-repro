from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

import polars as pl

from qualify_candidate_index import (
    SamplingProfile,
    QualificationPlan,
    linear_projection,
    load_plan,
    gpu_summary,
    progress_summary,
    qualification_projection,
    sample_source,
    selected_row_count,
    sha256,
    verified_artifact,
    write_receipt_command,
)


class SamplingPlanTest(unittest.TestCase):
    def test_midpoint_profiles_are_nested_and_keep_source_order(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source.parquet"
            pl.DataFrame(
                {
                    "source_ordinal": range(641),
                    "smiles": [f"C{i}" for i in range(641)],
                    "molecular_formula": [f"C{i}H{i * 2}" for i in range(641)],
                }
            ).write_parquet(source)
            qualification_spec = root / "candidate-qualification.toml"
            qualification_spec.write_text(
                "\n".join(
                    (
                        "[source]",
                        f'sha256 = "{sha256(source)}"',
                        "",
                        "[profiles.functional]",
                        "source_modulus = 320",
                        "source_remainder = 170",
                        "",
                        "[profiles.scale]",
                        "source_modulus = 20",
                        "source_remainder = 10",
                        "",
                        "[projection]",
                        "maximum_memory_fraction = 0.8",
                        "maximum_disk_fraction = 0.8",
                        "",
                    )
                ),
                encoding="utf-8",
            )
            candidate_spec = root / "candidates.toml"
            candidate_spec.write_text(
                '[archive]\nmember = "candidate.parquet"\n\n[table]\nbatch_rows = 64\n',
                encoding="utf-8",
            )
            output = root / "samples"

            sample_source(
                SimpleNamespace(
                    source=source,
                    qualification_spec=qualification_spec,
                    candidate_spec=candidate_spec,
                    output_directory=output,
                )
            )

            scale_rows = pl.read_parquet(output / "scale.parquet")["source_ordinal"].to_list()
            functional_rows = pl.read_parquet(output / "functional.parquet")["source_ordinal"].to_list()
            self.assertEqual(scale_rows, list(range(10, 641, 20)))
            self.assertEqual(functional_rows, [170, 490])
            self.assertTrue(set(functional_rows).issubset(scale_rows))

    def test_plan_rejects_a_functional_profile_outside_scale(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            specification = Path(temporary_directory) / "candidate-qualification.toml"
            specification.write_text(
                """
[source]
sha256 = "0000000000000000000000000000000000000000000000000000000000000000"

[profiles.functional]
source_modulus = 100
source_remainder = 40

[profiles.scale]
source_modulus = 20
source_remainder = 10

[projection]
maximum_memory_fraction = 0.8
maximum_disk_fraction = 0.8
""".lstrip(),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "functional source row"):
                load_plan(specification)

    def test_selected_row_count_includes_the_last_matching_ordinal(self):
        profile = SamplingProfile("example", modulus=20, remainder=10)

        self.assertEqual(selected_row_count(31, profile), 2)


class ProjectionTest(unittest.TestCase):
    def test_projects_the_line_through_both_observations(self):
        self.assertEqual(linear_projection(10, 100, 20, 300, 30), 500)

    def test_rejects_non_increasing_measurements(self):
        with self.assertRaisesRegex(ValueError, "increase strictly"):
            linear_projection(10, 100, 20, 100, 30)

    def test_enforces_the_predeclared_memory_margin(self):
        plan = QualificationPlan(
            source_sha256="0" * 64,
            functional=SamplingProfile("functional", 3, 0),
            scale=SamplingProfile("scale", 2, 0),
            maximum_memory_fraction=0.8,
            maximum_disk_fraction=0.8,
        )
        functional = {
            "builder_run": {"memory_limit_bytes": 1000, "memory_peak_bytes": 100, "elapsed_nanoseconds": 100},
            "artifacts": {"index": {"bytes": 100}, "table": {"bytes": 100}},
            "sample": {"rows": 10},
        }
        scale = {
            "builder_run": {"memory_limit_bytes": 1000, "memory_peak_bytes": 300, "elapsed_nanoseconds": 300},
            "artifacts": {"index": {"bytes": 300}, "table": {"bytes": 300}},
            "sample": {"rows": 20},
        }

        projection = qualification_projection(plan, functional, scale, 30, 10_000)

        self.assertEqual(projection["memory_peak_bytes"], 500)
        functional["builder_run"]["memory_limit_bytes"] = 600
        scale["builder_run"]["memory_limit_bytes"] = 600
        with self.assertRaisesRegex(ValueError, "memory gate"):
            qualification_projection(plan, functional, scale, 30, 10_000)

        functional["builder_run"]["memory_limit_bytes"] = 1000
        scale["builder_run"]["memory_limit_bytes"] = 1000
        with self.assertRaisesRegex(ValueError, "disk gate"):
            qualification_projection(plan, functional, scale, 30, 1000)


class ReceiptCompositionTest(unittest.TestCase):
    def test_rejects_a_retained_log_changed_after_profile_verification(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            evidence = Path(temporary_directory)
            log = evidence / "functional-builder.log"
            log.write_text("verified\n", encoding="utf-8")
            receipt = {"file": log.name, "bytes": log.stat().st_size, "sha256": sha256(log)}
            log.write_text("changed after verification\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "byte count changed"):
                verified_artifact(evidence, receipt, "publish a changed functional builder log")

    def test_rejects_sampling_evidence_after_the_specification_changes(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            specification = root / "candidate-qualification.toml"
            specification.write_text(
                """
[source]
sha256 = "0000000000000000000000000000000000000000000000000000000000000000"

[profiles.functional]
source_modulus = 100
source_remainder = 50

[profiles.scale]
source_modulus = 20
source_remainder = 10

[projection]
maximum_memory_fraction = 0.8
maximum_disk_fraction = 0.8
""".lstrip(),
                encoding="utf-8",
            )
            samples = root / "samples.json"
            samples.write_text(
                '{"kind":"secs.candidate-build-qualification.v1",'
                '"qualification_spec":{"sha256":"ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"},'
                '"source":{"sha256":"0000000000000000000000000000000000000000000000000000000000000000","rows":1000}}',
                encoding="utf-8",
            )
            functional = root / "functional.json"
            scale = root / "scale.json"
            functional.write_text("{}", encoding="utf-8")
            scale.write_text("{}", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "stale sampling receipt"):
                write_receipt_command(
                    SimpleNamespace(
                        qualification_spec=specification,
                        samples_receipt=samples,
                        functional_report=functional,
                        scale_report=scale,
                    )
                )


class ProgressTest(unittest.TestCase):
    def test_summarizes_strictly_increasing_progress_inside_the_run(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            log = Path(temporary_directory) / "builder.log"
            log.write_text(
                "2026-08-30T10:00:01.000000Z|Indexed 10 of 20 candidate rows.\n"
                "2026-08-30T10:00:03.000000Z|Indexed 20 of 20 candidate rows.\n",
                encoding="utf-8",
            )

            summary = progress_summary(
                log,
                20,
                datetime(2026, 8, 30, 10, 0, 0, tzinfo=timezone.utc),
                datetime(2026, 8, 30, 10, 0, 4, tzinfo=timezone.utc),
            )

            self.assertEqual(summary["observations"], 2)
            self.assertEqual(summary["batch_seconds_median"], 2.0)

    def test_rejects_regressing_row_counts(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            log = Path(temporary_directory) / "builder.log"
            log.write_text(
                "2026-08-30T10:00:01.000000Z|Indexed 20 of 20 candidate rows.\n"
                "2026-08-30T10:00:02.000000Z|Indexed 10 of 20 candidate rows.\n"
                "2026-08-30T10:00:03.000000Z|Indexed 20 of 20 candidate rows.\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "increase strictly"):
                progress_summary(
                    log,
                    20,
                    datetime(2026, 8, 30, 10, 0, 0, tzinfo=timezone.utc),
                    datetime(2026, 8, 30, 10, 0, 4, tzinfo=timezone.utc),
                )


class GpuSummaryTest(unittest.TestCase):
    def test_excludes_observations_outside_the_bound_run(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            log = Path(temporary_directory) / "gpu.csv"
            log.write_text(
                "2026-08-30T09:59:59.000000Z|GPU-1, Test GPU, 1000, 900, 99\n"
                "2026-08-30T10:00:01.000000Z|GPU-1, Test GPU, 1000, 100, 50\n"
                "2026-08-30T10:00:02.000000Z|GPU-1, Test GPU, 1000, 200, 60\n",
                encoding="utf-8",
            )

            summary = gpu_summary(
                log,
                datetime(2026, 8, 30, 10, 0, 0, tzinfo=timezone.utc),
                datetime(2026, 8, 30, 10, 0, 3, tzinfo=timezone.utc),
            )

            self.assertEqual(summary["observations"], 2)
            self.assertEqual(summary["peak_used_mib"], 200)
            self.assertEqual(summary["peak_utilization_percent"], 60)


if __name__ == "__main__":
    unittest.main()
