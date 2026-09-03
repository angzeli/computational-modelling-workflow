from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from cmw.core.artifacts import (
    ArtifactValidation,
    HoleElectronArtifact,
    NTOArtifact,
    ValidationStatus,
)
from cmw.molecular.excited_state_tables import (
    ExcitedStateTableBundle,
    ExcitedStateTableError,
    HEA_COLUMNS,
    NTO_COLUMNS,
    TABLE_NAMES,
    TDA_MANIFOLD_COLUMNS,
    TDA_MATRIX_COLUMNS,
    join_excited_state_tables,
    normalize_hea_state,
    normalize_nto_state,
    normalize_tda_manifold,
    resolve_finalized_analysis_artifact,
    serialize_csv_rows,
    write_excited_state_csv_bundle,
)
from cmw.molecular.excited_states import (
    ExcitedStateIdentity,
    ExcitedStateRecord,
    OrbitalTransitionRecord,
)
from cmw.molecular.multiwfn.excited_states import (
    MULTIWFN2026_HEA_GRAMMAR,
    MULTIWFN2026_NTO_GRAMMAR,
)
from cmw.molecular.orca.excited_states import (
    OrcaTDAProtocolMetadata,
    ParsedOrcaExcitedStates,
)


def _state(
    index: int,
    *,
    length: float,
    global_index: int | None = None,
    transitions: tuple[OrbitalTransitionRecord, ...] = (),
) -> ExcitedStateRecord:
    energy = 2.0 + index / 10
    return ExcitedStateRecord(
        state_index=index,
        local_state_index=index,
        orca_global_state_index=global_index or index,
        excitation_energy_ev=energy,
        oscillator_strength=length,
        spin_manifold="singlet",
        multiplicity=1,
        wavelength_nm=1239.841984 / energy,
        length_gauge_oscillator_strength=length,
        velocity_gauge_oscillator_strength=length / 2,
        primary_oscillator_strength_gauge="length",
        orbital_transitions=transitions,
    )


def _parsed(count: int = 30) -> ParsedOrcaExcitedStates:
    transitions = (
        OrbitalTransitionRecord("10a", "11a", 0.2, 0.4),
        OrbitalTransitionRecord("9a", "11a", 0.4, -0.6),
        OrbitalTransitionRecord("10a", "12a", 0.3, 0.5),
        OrbitalTransitionRecord("8a", "13a", 0.1, 0.3),
        OrbitalTransitionRecord("7a", "14a", 0.05, 0.2),
        OrbitalTransitionRecord("6a", "15a", 0.01, 0.1),
    )
    states = tuple(
        _state(
            index,
            length=0.001 if index == 1 else 0.02 if index == 2 else 0.005,
            transitions=transitions,
        )
        for index in range(1, count + 1)
    )
    protocol = OrcaTDAProtocolMetadata(
        "6.1.1",
        "TDA",
        True,
        "wB97X-D4",
        "def2-TZVP",
        count,
        0,
        False,
        None,
        None,
        1.0e-8,
        "! wB97X-D4 def2-TZVP",
        "D4",
    )
    return ParsedOrcaExcitedStates(states, protocol, {"fixture": True})


def _validation(*, passed: bool = True) -> ArtifactValidation:
    return ArtifactValidation(
        ValidationStatus.PASSED if passed else ValidationStatus.FAILED,
        {
            "D_rounding_consistency": passed,
            "parser_complete": passed,
            "process_exit_code": passed,
            "required_outputs": passed,
            "source_identity": passed,
            "state_identity": passed,
        },
        "VALID" if passed else "FAILED",
        "fixture",
    )


def _nto(
    index: int,
    *,
    weights: tuple[float, ...] = (0.7, 0.2, 0.05),
    passed: bool = True,
    global_index: int | None = None,
) -> NTOArtifact:
    cumulative = 0.0
    pairs = []
    for pair_index, weight in enumerate(weights, 1):
        cumulative += weight
        pairs.append(
            {"pair_index": pair_index, "weight": weight, "cumulative_weight": cumulative}
        )
    return NTOArtifact(
        producing_calculation=f"nto-s{index}",
        parent_artifacts=("parent",),
        validation=_validation(passed=passed),
        provenance={
            "execution_attempt": {
                "target_id": "a" * 64,
                "attempt_id": "attempt_002",
                "process_exit_code": 0 if passed else 1,
            }
        },
        metadata={
            "selected_state_identity": ExcitedStateIdentity("singlet", index).to_dict(),
            "state_source_indices": {
                "spin_manifold": "singlet",
                "local_state_index": index,
                "orca_global_state_index": global_index or index,
            },
            "parser_grammar": MULTIWFN2026_NTO_GRAMMAR,
            "parser_complete": True,
            "printed_pair_count": len(weights),
            "orbital_pairs": pairs,
        },
    )


def _hea(
    index: int,
    *,
    grid: tuple[int, int, int] | None = (2, 3, 4),
    passed: bool = True,
) -> HoleElectronArtifact:
    metadata: dict[str, object] = {
        "selected_state_identity": ExcitedStateIdentity("singlet", index).to_dict(),
        "parser_grammar": MULTIWFN2026_HEA_GRAMMAR,
        "parser_complete": True,
        "fragment_resolved": False,
        "reported_D_angstrom": 0.123,
        "Sr": 0.87,
        "t_angstrom": -1.25,
        "hole_extent_angstrom": 3.5,
        "electron_extent_angstrom": 3.8,
        "coefficient_cross_term_threshold": 0.01,
    }
    if grid is not None:
        metadata["grid_dimensions"] = list(grid)
        metadata["total_grid_points"] = grid[0] * grid[1] * grid[2]
    return HoleElectronArtifact(
        producing_calculation=f"hea-s{index}",
        parent_artifacts=("parent",),
        validation=_validation(passed=passed),
        provenance={
            "execution_attempt": {
                "target_id": "b" * 64,
                "attempt_id": "attempt_002",
                "process_exit_code": 0 if passed else 1,
            }
        },
        metadata=metadata,
    )


def _manifold(count: int = 30, *, frontier: int | None = None):
    return normalize_tda_manifold(
        "AA",
        _parsed(count),
        finalized_selected_identities=(
            ExcitedStateIdentity("singlet", 1),
            ExcitedStateIdentity("singlet", 2),
        ),
        bright_threshold_length=0.01,
        expected_state_count=count,
        expected_spin_manifold="singlet",
        frontier_homo_index_one_based=frontier,
    )


def _bundle() -> ExcitedStateTableBundle:
    manifold = _manifold(2)
    keys = manifold.selected_keys
    return join_excited_state_tables(
        (manifold,),
        nto_artifacts={key: _nto(key[2]) for key in keys},
        hea_artifacts={key: _hea(key[2]) for key in keys},
    )


class ExcitedStateTableNormalizationTests(unittest.TestCase):
    def test_full_manifold_fields_order_and_selection_contract(self) -> None:
        normalized = _manifold()
        self.assertEqual(len(normalized.rows), 30)
        self.assertEqual([row["state_index"] for row in normalized.rows], list(range(1, 31)))
        first = normalized.rows[0]
        self.assertEqual(first["spin_manifold"], "singlet")
        self.assertEqual(first["orca_global_state_index"], 1)
        self.assertEqual(first["excitation_energy_ev"], 2.1)
        self.assertGreater(first["wavelength_nm"], 0)
        self.assertEqual(first["oscillator_strength_length"], 0.001)
        self.assertEqual(first["oscillator_strength_velocity"], 0.0005)
        self.assertEqual(first["oscillator_strength_gauge_abs_delta"], 0.0005)
        self.assertEqual(first["selection_roles"], "S1")
        self.assertEqual(normalized.rows[1]["selection_roles"], "lowest_bright")
        self.assertEqual(normalized.selected_keys, (("AA", "singlet", 1), ("AA", "singlet", 2)))

    def test_top_five_rank_by_weight_with_frontier_and_fallback_labels(self) -> None:
        frontier = _manifold(2, frontier=10).rows[0]
        self.assertEqual(frontier["transition_count_parsed"], 6)
        self.assertEqual(frontier["leading_transition_weight_sum"], 1.05)
        self.assertEqual(
            frontier["leading_transitions"],
            "HOMO-1->LUMO:0.4;HOMO->LUMO+1:0.3;HOMO->LUMO:0.2;"
            "HOMO-2->LUMO+2:0.1;HOMO-3->LUMO+3:0.05",
        )
        fallback = _manifold(2).rows[0]["leading_transitions"]
        self.assertTrue(str(fallback).startswith("MO9a->MO11a:0.4"))

    def test_configurable_threshold_and_deduplication(self) -> None:
        parsed = ParsedOrcaExcitedStates(
            (_state(1, length=0.02), _state(2, length=0.03)),
            _parsed(2).protocol,
            {},
        )
        normalized = normalize_tda_manifold(
            "X",
            parsed,
            finalized_selected_identities=(ExcitedStateIdentity("singlet", 1),),
            bright_threshold_length=0.015,
            expected_state_count=2,
        )
        self.assertEqual(len(normalized.selected_rows), 1)
        self.assertEqual(normalized.selected_rows[0]["selection_roles"], "S1;lowest_bright")
        with self.assertRaises(ExcitedStateTableError):
            normalize_tda_manifold(
                "X",
                parsed,
                finalized_selected_identities=(ExcitedStateIdentity("singlet", 1),),
                bright_threshold_length=0.025,
                expected_state_count=2,
            )

    def test_selection_mismatch_duplicate_and_incomplete_manifold_fail_closed(self) -> None:
        with self.assertRaisesRegex(ExcitedStateTableError, "disagrees"):
            normalize_tda_manifold(
                "AA",
                _parsed(2),
                finalized_selected_identities=(ExcitedStateIdentity("singlet", 1),),
                bright_threshold_length=0.01,
                expected_state_count=2,
            )
        with self.assertRaises(ValueError):
            ParsedOrcaExcitedStates(
                (_state(1, length=0.001), _state(1, length=0.02, global_index=2)),
                _parsed(2).protocol,
                {},
            )
        with self.assertRaisesRegex(ExcitedStateTableError, "expected 30"):
            normalize_tda_manifold(
                "AA",
                _parsed(29),
                finalized_selected_identities=(
                    ExcitedStateIdentity("singlet", 1),
                    ExcitedStateIdentity("singlet", 2),
                ),
                bright_threshold_length=0.01,
                expected_state_count=30,
            )

    def test_nto_weights_and_available_top_sums(self) -> None:
        row = normalize_nto_state("AA", _nto(2), selection_roles=("lowest_bright",))
        self.assertEqual(row["nto_pairs_parsed"], 3)
        self.assertEqual(row["nto_pair_weight_1"], 0.7)
        self.assertEqual(row["nto_pair_weight_4"], None)
        self.assertAlmostEqual(row["nto_top2_weight_sum"], 0.9)
        self.assertAlmostEqual(row["nto_top5_weight_sum"], 0.95)
        self.assertAlmostEqual(row["nto_top10_weight_sum"], 0.95)
        self.assertAlmostEqual(row["nto_all_parsed_weight_sum"], 0.95)

    def test_nto_invalid_weights_and_failed_artifact_are_rejected(self) -> None:
        with self.assertRaises(ExcitedStateTableError):
            normalize_nto_state("AA", _nto(1, weights=(1.1,)), selection_roles=("S1",))
        with self.assertRaises(ExcitedStateTableError):
            normalize_nto_state("AA", _nto(1, passed=False), selection_roles=("S1",))

    def test_hea_primary_metrics_negative_t_and_optional_grid(self) -> None:
        row = normalize_hea_state("AA", _hea(1), selection_roles=("S1",))
        self.assertEqual(row["D_angstrom"], 0.123)
        self.assertEqual(row["Sr"], 0.87)
        self.assertEqual(row["t_angstrom"], -1.25)
        self.assertEqual(row["hole_extent_angstrom"], 3.5)
        self.assertEqual(row["electron_extent_angstrom"], 3.8)
        self.assertEqual((row["grid_nx"], row["grid_ny"], row["grid_nz"]), (2, 3, 4))
        self.assertEqual(row["grid_points"], 24)
        self.assertEqual(row["cfgcrossthres"], 0.01)
        optional = normalize_hea_state("AA", _hea(1, grid=None), selection_roles=("S1",))
        self.assertIsNone(optional["grid_nx"])
        self.assertIsNone(optional["grid_points"])

    def test_hea_invalid_primary_or_grid_values_are_rejected(self) -> None:
        artifact = _hea(1)
        with self.assertRaises(ExcitedStateTableError):
            normalize_hea_state(
                "AA",
                replace(artifact, metadata={**artifact.metadata, "Sr": 1.1}),
                selection_roles=("S1",),
            )
        with self.assertRaises(ExcitedStateTableError):
            normalize_hea_state(
                "AA",
                replace(artifact, metadata={**artifact.metadata, "grid_dimensions": [2, 3, 5]}),
                selection_roles=("S1",),
            )

    def test_join_uses_canonical_identity_not_raw_global_index(self) -> None:
        manifold = _manifold(2)
        keys = manifold.selected_keys
        bundle = join_excited_state_tables(
            (manifold,),
            nto_artifacts={key: _nto(key[2], global_index=99 + key[2]) for key in keys},
            hea_artifacts={key: _hea(key[2]) for key in keys},
        )
        matrix_keys = {_key(row) for row in bundle.tda_matrix}
        self.assertEqual(matrix_keys, {_key(row) for row in bundle.nto_state})
        self.assertEqual(matrix_keys, {_key(row) for row in bundle.hea_state})

    def test_missing_analysis_or_duplicate_system_fails_closed(self) -> None:
        manifold = _manifold(2)
        keys = manifold.selected_keys
        with self.assertRaisesRegex(ExcitedStateTableError, "NTO"):
            join_excited_state_tables(
                (manifold,),
                nto_artifacts={keys[0]: _nto(1)},
                hea_artifacts={key: _hea(key[2]) for key in keys},
            )
        with self.assertRaisesRegex(ExcitedStateTableError, "HEA"):
            join_excited_state_tables(
                (manifold,),
                nto_artifacts={key: _nto(key[2]) for key in keys},
                hea_artifacts={keys[0]: _hea(1)},
            )
        with self.assertRaisesRegex(ExcitedStateTableError, "systems"):
            join_excited_state_tables(
                (manifold, manifold), nto_artifacts={}, hea_artifacts={}
            )


def _key(row):
    return row["system"], row["spin_manifold"], row["state_index"]


class ExcitedStateCsvTests(unittest.TestCase):
    def test_exact_schemas_ordering_quoting_and_empty_fields(self) -> None:
        bundle = _bundle()
        self.assertEqual(tuple(bundle.tda_state_manifold_qc[0]), TDA_MANIFOLD_COLUMNS)
        self.assertEqual(tuple(bundle.tda_matrix[0]), TDA_MATRIX_COLUMNS)
        self.assertEqual(tuple(bundle.nto_state[0]), NTO_COLUMNS)
        self.assertEqual(tuple(bundle.hea_state[0]), HEA_COLUMNS)
        self.assertEqual([row["state_index"] for row in bundle.tda_matrix], [1, 2])
        row = {column: "" for column in TDA_MATRIX_COLUMNS}
        row.update({"system": "A,A", "leading_transitions": 'MO1a->MO2a:0.5;"phase"'})
        rendered = serialize_csv_rows((row,), TDA_MATRIX_COLUMNS)
        self.assertIn('"A,A"', rendered)
        self.assertIn('"MO1a->MO2a:0.5;""phase"""', rendered)
        self.assertNotIn("None", rendered)
        self.assertNotIn("null", rendered)

    def test_atomic_four_table_write(self) -> None:
        bundle = _bundle()
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            root = Path(directory)
            paths = {name: root / f"{name}.csv" for name in TABLE_NAMES}
            write_excited_state_csv_bundle(bundle, paths)
            self.assertTrue(all(path.is_file() for path in paths.values()))
            with paths["tda_matrix"].open() as handle:
                self.assertEqual(sum(1 for _ in handle), 3)

    def test_validation_failure_writes_none(self) -> None:
        bundle = _bundle()
        invalid = replace(bundle, hea_state=())
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            root = Path(directory)
            paths = {name: root / f"{name}.csv" for name in TABLE_NAMES}
            with self.assertRaises(ExcitedStateTableError):
                write_excited_state_csv_bundle(invalid, paths)
            self.assertFalse(any(path.exists() for path in paths.values()))

    def test_install_error_restores_all_previous_tables(self) -> None:
        bundle = _bundle()
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            root = Path(directory)
            paths = {name: root / f"{name}.csv" for name in TABLE_NAMES}
            for path in paths.values():
                path.write_text("previous\n", encoding="utf-8")
            real_replace = os.replace
            calls = 0

            def flaky(source, destination):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("fixture failure")
                return real_replace(source, destination)

            with mock.patch("cmw.molecular.excited_state_tables.os.replace", side_effect=flaky):
                with self.assertRaises(ExcitedStateTableError):
                    write_excited_state_csv_bundle(bundle, paths)
            self.assertTrue(
                all(
                    path.read_text(encoding="utf-8") == "previous\n"
                    for path in paths.values()
                )
            )


class FinalizedArtifactResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.root = Path(self.temporary.name)
        self.campaign = self.root / "campaign"
        (self.campaign / "calculation").mkdir(parents=True)
        self.target = self.campaign / "calculation" / "analysis" / "target"
        self.attempt = self.target / "attempts" / "attempt_002"
        self.attempt.mkdir(parents=True)
        self.session = self.attempt / "session.log"
        self.session.write_text("fixture\n", encoding="utf-8")
        self.record = self.root / "artifact.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_record(
        self,
        *,
        target: Path | None = None,
        session: Path | None = None,
        passed: bool = True,
        superseded: bool = False,
    ) -> NTOArtifact:
        selected_target = target or self.target
        selected_session = session or self.session
        artifact = _nto(1, passed=passed)
        provenance = {
            **artifact.provenance,
            "renderer": {
                "execution_layout": {
                    "target_directory": str(selected_target),
                    "system_identifier": "fixture",
                }
            },
        }
        if superseded:
            provenance["superseded_by_attempt_id"] = "attempt_003"
        artifact = replace(
            artifact,
            files={"session_log": str(selected_session)},
            provenance=provenance,
            metadata={
                **artifact.metadata,
                "output_mwfn_file": str(selected_session),
            },
        )
        self.record.write_text(json.dumps(artifact.to_dict()), encoding="utf-8")
        return artifact

    def test_historical_v1_attempt_resolution(self) -> None:
        self._write_record()
        resolved = resolve_finalized_analysis_artifact(
            self.record,
            campaign_root=self.campaign,
            expected_type=NTOArtifact,
            canonical_attempt_id="attempt_002",
        )
        self.assertEqual(resolved.attempt_directory, self.attempt.resolve())
        self.assertEqual(resolved.resolved_files["session_log"], self.session.resolve())
        self.assertEqual(resolved.system_identifier, "fixture")
        self.assertEqual(resolved.identity_resolution, "exact")

    def test_migrated_v2_overlay_resolution(self) -> None:
        old_target = self.campaign / "calculation" / "old-target"
        old_session = old_target / "attempts" / "attempt_002" / "session.log"
        registry = self.campaign / ".cmw" / "layout-migrations" / "registry.json"
        registry.parent.mkdir(parents=True)
        registry.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "migrations": [],
                    "paths": {str(old_target): {"new_path": str(self.target)}},
                }
            ),
            encoding="utf-8",
        )
        self._write_record(target=old_target, session=old_session)
        migrated = self.record.read_text(encoding="utf-8").replace(
            str(old_target), str(self.target)
        )
        self.record.write_text(migrated, encoding="utf-8")
        resolved = resolve_finalized_analysis_artifact(
            self.record,
            campaign_root=self.campaign,
            expected_type=NTOArtifact,
        )
        self.assertEqual(resolved.target_directory, self.target.resolve())
        self.assertEqual(resolved.resolved_files["session_log"], self.session.resolve())
        self.assertEqual(resolved.identity_resolution, "registered_path_migration")

    def test_failed_superseded_and_noncanonical_attempts_are_rejected(self) -> None:
        self._write_record(passed=False)
        with self.assertRaises(ExcitedStateTableError):
            resolve_finalized_analysis_artifact(
                self.record, campaign_root=self.campaign, expected_type=NTOArtifact
            )
        self._write_record(superseded=True)
        with self.assertRaisesRegex(ExcitedStateTableError, "superseded"):
            resolve_finalized_analysis_artifact(
                self.record, campaign_root=self.campaign, expected_type=NTOArtifact
            )
        self._write_record()
        with self.assertRaisesRegex(ExcitedStateTableError, "not canonical"):
            resolve_finalized_analysis_artifact(
                self.record,
                campaign_root=self.campaign,
                expected_type=NTOArtifact,
                canonical_attempt_id="attempt_003",
            )

    def test_missing_expected_output_is_rejected(self) -> None:
        self._write_record(session=self.attempt / "missing.log")
        with self.assertRaisesRegex(ExcitedStateTableError, "missing"):
            resolve_finalized_analysis_artifact(
                self.record, campaign_root=self.campaign, expected_type=NTOArtifact
            )

    def test_unregistered_artifact_identity_drift_is_rejected(self) -> None:
        self._write_record()
        raw = json.loads(self.record.read_text(encoding="utf-8"))
        raw["producing_calculation"] = "tampered"
        self.record.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaisesRegex(ExcitedStateTableError, "invalid artifact"):
            resolve_finalized_analysis_artifact(
                self.record, campaign_root=self.campaign, expected_type=NTOArtifact
            )


if __name__ == "__main__":
    unittest.main()
