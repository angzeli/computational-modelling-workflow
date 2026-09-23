"""VASP adapter to the existing typed-artifact finalizer and Scratch publisher.

This module neither runs calculations nor opens mutable Jobs state. A legacy
run's intent is explicitly retrospective; its snapshot is not a pre-run plan.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from cmw.core import artifact_finalization
from cmw.core.artifacts import ArtifactValidation, EnergyArtifact, StructureArtifact, ValidationStatus, artifact_from_dict
from cmw.core.preparation_publication import record_publication_plan, publish_record
from cmw.core.provenance import stable_hash
from .result_binding import inspect_binding
from .result_policy import load_policy, assess_result
from .result_sources import read_json_source, revalidate_sources
from .results import inspect_result, compare_geometry


class ResultFinalizationError(ValueError):
    def __init__(self, message, *, code="FINALIZATION_REFUSED", findings=None):
        super().__init__(message)
        self.code = code
        self.findings = findings or []


def inspect_with_policy(directory, *, policy_path=None, spec_path=None, segment=None, stdout=None, max_bytes=None):
    evidence = inspect_result(directory, segment=segment, stdout=stdout, max_bytes=max_bytes)
    if spec_path is not None and policy_path is None:
        raise ResultFinalizationError("An explicit policy is required with a finalization specification")
    if policy_path is None:
        return evidence
    policy, source = load_policy(policy_path)
    identity = stable_hash(policy)
    binding = None if spec_path is None else inspect_binding(spec_path, evidence, identity)
    assessment = assess_result(evidence, policy, binding=binding,
                               execution=None if binding is None else binding.get("execution"))
    # These contextual records deliberately do not replace the native snapshot ID.
    evidence["policy_assessment"] = assessment
    evidence["policy"] = policy
    evidence["policy_source"] = source
    evidence["binding"] = binding
    evidence["exit_code"] = assessment["exit_code"]
    return evidence


def _record_identity(record):
    return stable_hash({k: v for k, v in record.items() if k not in {"record_id", "publication", "publication_id"}})


def verify_finalization(path, *, policy_path=None, _ancestors=()):
    """Recheck a completed record and every consumed source before reuse.

    This verifies the recorded snapshot against current files, not a permanent
    certificate for a path. It cannot authenticate caller-owned local records.
    """
    from .result_policy import validate_policy

    result = {"valid": False, "findings": [], "record": None, "source": None,
              "artifacts": [], "source_revalidation": {"valid": False}}

    def require(condition, message):
        if not condition:
            raise ValueError(message)

    def identity(source):
        require(isinstance(source, dict), "Malformed required source observation")
        require(source.get("stable") is True and source.get("coverage") == "complete"
                and source.get("eof_reached") is True and source.get("error") is None,
                "Required source observation is not complete and stable")
        require(isinstance(source.get("resolved_path"), str)
                and isinstance(source.get("sha256"), str)
                and isinstance(source.get("stat_before"), dict), "Required source identity is missing")
        return {key: source.get(key) for key in (
            "role", "absolute_path", "resolved_path", "sha256", "bytes_read", "stat_before",
        )}

    try:
        record, source = read_json_source(path, "parent_finalization", max_bytes=64 * 1024 * 1024)
        result.update(record=record, source=source)
        require(isinstance(record, dict) and type(record.get("schema_version")) is int
                and record["schema_version"] == 1 and record.get("kind") == "vasp-artifact-finalization"
                and isinstance(record.get("publication"), dict)
                and record["publication"].get("state") == "complete",
                "Not a complete schema-1 VASP finalization record")
        require(record.get("record_id") == _record_identity(record), "Finalization record content identity mismatch")
        locator = source["resolved_path"]
        require(locator not in _ancestors, "Cyclic parent finalization relationship")
        require(len(_ancestors) < 64, "Parent finalization ancestry exceeds the supported depth of 64")
        evidence = record.get("evidence")
        require(isinstance(evidence, dict), "Stored native evidence is missing")
        policy = validate_policy(evidence.get("policy"))
        binding = evidence.get("binding")
        require(isinstance(binding, dict) and binding.get("valid") is True
                and binding.get("input_binding") is True and binding.get("input_comparison_valid") is True,
                "Stored producing-run/input binding is not valid")
        policy_source = evidence["policy_source"]
        observed_policy, _observed_policy_source = load_policy(policy_source["resolved_path"])
        require(observed_policy == policy, "Stored policy differs from its exact source content")
        spec_sources = [s for s in binding["sources"] if s.get("role") == "finalization_spec"]
        require(len(spec_sources) == 1, "Binding must retain exactly one finalization-specification source")
        observed_binding = inspect_binding(spec_sources[0]["resolved_path"], evidence, stable_hash(policy))
        require(observed_binding.get("valid") is True
                and all(observed_binding.get(key) == binding.get(key) for key in (
                    "spec", "resolved_spec", "checks", "input_binding", "input_comparison_valid", "input_comparisons")),
                "Stored producing-run declaration or binding differs from its source evidence")
        require({s["resolved_path"]: identity(s) for s in observed_binding["sources"]}
                == {s["resolved_path"]: identity(s) for s in binding["sources"]},
                "Stored binding source closure differs from its exact source evidence")
        execution = binding.get("execution")
        require(isinstance(execution, dict) and execution.get("status") == "eligible",
                "Stored terminal execution evidence is not eligible")
        require(all(execution.get(key) == observed_binding["execution"].get(key) for key in (
                    "status", "operational_success", "exit_code", "signal", "job", "checks", "binding")),
                "Stored execution facts differ from native saved Jobs evidence")
        require(isinstance(binding.get("checks"), dict) and bool(binding["checks"])
                and all(value is True for value in binding["checks"].values())
                and isinstance(execution.get("checks"), dict) and bool(execution["checks"])
                and all(value is True for value in execution["checks"].values())
                and execution.get("operational_success") is True
                and type(execution.get("exit_code")) is int and execution["exit_code"] == 0
                and execution.get("signal") is None,
                "Stored binding/completion verdict contradicts its operational checks")
        assessment = assess_result(evidence, policy, binding=binding, execution=execution)
        require(assessment["status"] == "PASS", "Stored scientific evidence no longer passes its recorded policy")
        require(record.get("policy_identity") == assessment["policy_identity"]
                and record.get("validation_decision") == assessment
                and evidence.get("policy_assessment") == assessment,
                "Stored policy identity or assessment contradicts its evidence")
        current = evidence["selected_segment"]
        endpoint = evidence["endpoint"]
        evaluation = current["evaluations"][endpoint["evaluation_index"]]
        spec = binding.get("spec")
        require(isinstance(spec, dict) and spec.get("intent") == "retrospective"
                and record.get("declared_intent") == "retrospective"
                and record.get("producing_calculation") == spec.get("producing_calculation")
                and isinstance(spec.get("producing_calculation"), str) and bool(spec["producing_calculation"].strip()),
                "Retrospective producing-calculation declaration is inconsistent")
        require(spec.get("snapshot_id") == evidence.get("snapshot_id")
                and spec.get("segment_id") == current.get("segment_id")
                and spec.get("policy_id") == stable_hash(policy), "Declaration differs from snapshot, segment or policy identity")
        declared_execution = spec["execution"]
        job = execution["job"]
        require(job["attempt_id"] == declared_execution["attempt_id"]
                and type(job["id"]) is int and job["id"] == declared_execution["job_id"]
                and job.get("status") == "Done" and type(job.get("exit_code")) is int
                and job["exit_code"] == 0 and job.get("signal") is None
                and job.get("cancel_requested") is False
                and declared_execution["source_identities"] == {s["role"]: s["sha256"] for s in execution["sources"]},
                "Selected Jobs invocation or execution source identity is inconsistent")
        native = evidence.get("sources")
        require(isinstance(native, list) and bool(native), "Native source observations are missing")
        require(len({s["role"] for s in native}) == len(native)
                and spec.get("source_identities") == {s["role"]: s["sha256"] for s in native},
                "Declared native source identities differ from recorded observations")
        require(evidence.get("snapshot_id") == stable_hash({"schema_version": 1, "sources": [
            {"role": s["role"], "sha256": s["sha256"], "bytes_read": s["bytes_read"]} for s in native]}),
            "Native source observations do not produce the recorded snapshot identity")
        roles = spec.get("artifact_roles")
        require(isinstance(roles, list) and all(isinstance(role, str) for role in roles)
                and len(set(roles)) == len(roles) and set(roles) == set(policy["artifact_roles"]),
                "Declared artifact roles differ from the recorded policy")
        expected_sources = [*native, evidence["policy_source"], *binding["sources"]]
        parents = []
        resolved = binding.get("resolved_spec")
        require(isinstance(resolved, dict), "Resolved source/parent declaration is absent")
        for parent in resolved.get("parents", []):
            require(isinstance(parent, dict) and parent.get("relationship") == "input-structure",
                    "Unsupported parent relationship")
            checked = verify_finalization(parent["record"], _ancestors=(*_ancestors, locator))
            require(checked["valid"] and checked["source"]["sha256"] == parent["sha256"],
                    "Declared parent finalization is invalid or changed")
            matches = [a for a in checked["artifacts"] if a.artifact_id == parent["artifact_id"]]
            require(len(matches) == 1 and isinstance(matches[0], StructureArtifact),
                    "Required parent does not identify one finalized periodic structure")
            parent_artifact = matches[0]
            parent_geometry = parent_artifact.metadata.get("periodic_geometry")
            poscar = evidence["filesystem_inputs"]["POSCAR"]
            actual = {"cell": poscar["cell"], "species": poscar["atom_species"],
                      "cartesian": poscar["cartesian_coordinates"]}
            require(parent_geometry and compare_geometry(parent_geometry, actual)["status"] == "match"
                    and parent_geometry.get("constraints") == poscar["selective_dynamics"],
                    "Parent periodic structure does not match the ordered bound input")
            parents.append(parent_artifact)
            expected_sources.extend([checked["source"], *checked["record"]["required_sources"]])
        require(len({parent.artifact_id for parent in parents}) == len(parents), "Duplicate parent artifact")
        expected_sources = _unique_sources(expected_sources)
        required = record.get("required_sources")
        require(isinstance(required, list) and bool(required), "Required source identities are absent")
        expected_by_path = {s["resolved_path"]: identity(s) for s in expected_sources}
        required_by_path = {s["resolved_path"]: identity(s) for s in required}
        require(len(required_by_path) == len(required) and required_by_path == expected_by_path,
                "Required source closure is incomplete, duplicated or inconsistent")

        protocol = {"contract": "cmw-vasp-result-v1", "intent": "retrospective",
                    "operational_attempt_id": execution["job"]["attempt_id"],
                    "source_snapshot_id": evidence["snapshot_id"], "segment_id": current["segment_id"],
                    "evaluation_index": evaluation["index"], "policy_id": assessment["policy_identity"]["policy_id"],
                    "effective_settings": {k: v["value"] for k, v in current["settings"].items()}}
        files = {s["role"]: s["resolved_path"] for s in native
                 if s["role"] in {"OUTCAR", "OSZICAR", "stdout", "POSCAR", "CONTCAR", "INCAR"}}
        bundle = record.get("artifact_bundle")
        require(isinstance(bundle, dict) and bundle.get("status") == "FINALIZED"
                and isinstance(bundle.get("artifacts"), list) and bool(bundle["artifacts"])
                and type(bundle.get("artifact_count")) is int, "Missing finalized artifact bundle")
        artifacts = []
        for value in bundle["artifacts"]:
            artifact = artifact_from_dict(value)
            require(artifact.artifact_id == value.get("artifact_id") and artifact.validation.passed,
                    "Artifact identity/validation mismatch")
            require(artifact.producing_calculation == spec["producing_calculation"]
                    and artifact.protocol == protocol and artifact.files == files
                    and artifact.parent_artifacts == tuple(parent.artifact_id for parent in parents),
                    "Artifact producing-run, policy, source, file or parent linkage is inconsistent")
            require(artifact.validation.code == "VASP_POLICY_ACCEPTED"
                    and artifact.validation.checks == {
                        c["code"]: True if c["status"] == "PASS" else False if c["status"] == "FAIL" else None
                        for c in assessment["checks"]},
                    "Artifact validation differs from the accepted policy checks")
            require(artifact.provenance.get("policy_assessment") == assessment
                    and artifact.provenance.get("binding_kind") == "retrospective_declared_snapshot"
                    and artifact.provenance.get("source_identities") == [
                        {k: s[k] for k in ("role", "resolved_path", "sha256", "bytes_read")} for s in required],
                    "Artifact provenance does not retain its complete source/policy binding")
            role = artifact.metadata.get("role")
            require(role in roles, "Artifact role is not declared by this policy")
            if role.startswith("energy:"):
                kind = role.split(":", 1)[1]
                require(type(artifact) is EnergyArtifact and artifact.metadata.get("energy_kind") == kind
                        and artifact.metadata.get("energy") == evaluation["energies"].get(kind)
                        and artifact.metadata.get("periodic_geometry_id") == endpoint["geometry"]["geometry_id"],
                        "Energy artifact differs from its declared evaluated energy/geometry")
            else:
                geometry = endpoint["geometry"]
                require(type(artifact) is StructureArtifact and policy["calculation"] == "fixed-cell-relaxation"
                        and artifact.metadata.get("periodic_geometry") == geometry
                        and artifact.source == "evaluated-periodic-snapshot:" + evidence["snapshot_id"]
                        and artifact.format == "vasp-periodic" and artifact.charge is None and artifact.multiplicity is None
                        and artifact.atom_count == len(geometry["species"])
                        and artifact.elemental_composition == dict(Counter(geometry["species"]))
                        and artifact.geometry_hash == stable_hash(geometry),
                        "Periodic structure artifact differs from its evaluated endpoint/constraints")
            artifacts.append(artifact)
        require(len({a.artifact_id for a in artifacts}) == len(artifacts) == bundle["artifact_count"]
                and [a.metadata["role"] for a in artifacts] == roles,
                "Artifact count, roles or identities are inconsistent")
        result["artifacts"] = artifacts
        try:
            validation = revalidate_sources(required)
        except Exception as error:
            raise ValueError(f"Source revalidation failed: {error}") from error
        result["source_revalidation"] = validation
        require(isinstance(validation, dict) and validation.get("valid") is True,
                "Required source contents or identities have changed")
        if policy_path is not None:
            selected_policy, _policy_source = load_policy(policy_path)
            require(stable_hash(selected_policy) == assessment["policy_identity"]["policy_id"],
                    "Selected policy differs from finalized policy")
        result["valid"] = True
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError, RecursionError) as error:
        result["findings"].append(str(error) or "Malformed VASP finalization record")
        if result["source"] is None and getattr(error, "record", None) is not None:
            result["source"] = error.record
    return result


def _parents(binding, evidence):
    artifacts, sources = [], []
    for parent in binding.get("resolved_spec", binding["spec"]).get("parents", []):
        checked = verify_finalization(parent["record"])
        if not checked["valid"] or checked["source"]["sha256"] != parent["sha256"]:
            raise ResultFinalizationError("Required parent evidence is invalid or changed", findings=checked["findings"])
        matches = [a for a in checked["artifacts"] if a.artifact_id == parent["artifact_id"]]
        if len(matches) != 1 or not isinstance(matches[0], StructureArtifact):
            raise ResultFinalizationError("Input-structure parent must name one finalized periodic structure")
        artifact = matches[0]
        geometry = artifact.metadata.get("periodic_geometry")
        poscar = evidence.get("filesystem_inputs", {}).get("POSCAR", {})
        if poscar.get("status") != "valid":
            raise ResultFinalizationError("Input geometry is unavailable for parent matching")
        actual = {"cell": poscar["cell"], "species": poscar["atom_species"],
                  "cartesian": poscar["cartesian_coordinates"]}
        if (not geometry or compare_geometry(geometry, actual)["status"] != "match"
                or geometry.get("constraints") != poscar["selective_dynamics"]):
            raise ResultFinalizationError("Required structure parent does not match the ordered input geometry/constraints")
        artifacts.append(artifact)
        sources.extend([checked["source"], *checked["record"]["required_sources"]])
    if len({a.artifact_id for a in artifacts}) != len(artifacts):
        raise ResultFinalizationError("Duplicate parent artifact")
    return artifacts, sources


def _unique_sources(sources):
    selected = {}
    for source in sources:
        key = source["resolved_path"]
        if key in selected and any(source[k] != selected[key][k] for k in ("sha256", "stat_before")):
            raise ResultFinalizationError("A source has conflicting observations within this finalization")
        selected[key] = source
    return list(selected.values())


def finalize_result(directory, *, policy_path, spec_path, scratch_root, record_directory,
                    scratch_mount=None, segment=None, stdout=None, dry_run=False):
    """Finalize a complete supported bundle, then explicitly publish only its record."""
    evidence = inspect_with_policy(directory, policy_path=policy_path, spec_path=spec_path,
                                   segment=segment, stdout=stdout)
    binding = evidence["binding"]
    assessment = evidence["policy_assessment"]
    if not binding or not binding.get("valid"):
        raise ResultFinalizationError("Producing-run/source/input binding is not established",
                                      findings=[] if binding is None else binding.get("findings", []))
    if binding.get("execution", {}).get("status") != "eligible":
        raise ResultFinalizationError("Bound successful terminal execution evidence is required")
    if not binding.get("input_binding") or not binding.get("input_comparison_valid"):
        raise ResultFinalizationError("Executed input identity/comparison is not established")
    if assessment["status"] != "PASS":
        raise ResultFinalizationError("Evidence is not accepted under the selected policy", findings=assessment["checks"])
    spec = binding["spec"]
    if set(spec["artifact_roles"]) != set(evidence["policy"]["artifact_roles"]):
        raise ResultFinalizationError("Specification and policy request different artifact roles")
    if not evidence["endpoint"]["is_final_segment"]:
        raise ResultFinalizationError("Earlier concatenated segments cannot bind the current endpoint for finalization")
    parents, parent_sources = _parents(binding, evidence)
    sources = _unique_sources([*evidence["sources"], evidence["policy_source"], *binding["sources"], *parent_sources])
    protected = [Path(directory).absolute(), *(s["resolved_path"] for s in sources)]
    preparation = binding.get("preparation")
    if preparation is not None:
        prepared_input_path = preparation.get("input_path")
        if not isinstance(prepared_input_path, str) or not Path(prepared_input_path).is_absolute():
            raise ResultFinalizationError("Preparation record lacks its original absolute input destination")
        protected.append(prepared_input_path)
    plan = record_publication_plan(scratch_root, record_directory,
                                   sources=protected,
                                   scratch_mount=scratch_mount)
    current = evidence["selected_segment"]
    endpoint = evidence["endpoint"]
    evaluation = current["evaluations"][endpoint["evaluation_index"]]
    settings = {k: v["value"] for k, v in current["settings"].items()}
    protocol = {"contract": "cmw-vasp-result-v1", "intent": "retrospective",
                "operational_attempt_id": binding["execution"]["job"]["attempt_id"],
                "source_snapshot_id": evidence["snapshot_id"], "segment_id": current["segment_id"],
                "evaluation_index": evaluation["index"], "policy_id": assessment["policy_identity"]["policy_id"],
                "effective_settings": settings}
    validation = ArtifactValidation(ValidationStatus.PASSED,
        {c["code"]: True if c["status"] == "PASS" else False if c["status"] == "FAIL" else None for c in assessment["checks"]},
        "VASP_POLICY_ACCEPTED", "Accepted under the explicitly recorded policy and bounded dialect")
    files = {s["role"]: s["resolved_path"] for s in evidence["sources"] if s["role"] in {"OUTCAR", "OSZICAR", "stdout", "POSCAR", "CONTCAR", "INCAR"}}
    provenance = {"source_identities": [{k: s[k] for k in ("role", "resolved_path", "sha256", "bytes_read")} for s in sources],
                  "policy_assessment": assessment, "binding_kind": "retrospective_declared_snapshot",
                  "limitations": evidence["limitations"] + ["No wavefunction or charge-density artifact is finalized.",
                    "Method/k-point/potential comparability and model accuracy beyond observed settings are unassessed."]}
    planned = []
    for role in spec["artifact_roles"]:
        common = {"producing_calculation": spec["producing_calculation"], "protocol": protocol,
                  "parent_artifacts": tuple(p.artifact_id for p in parents)}
        if role.startswith("energy:"):
            energy_kind = role.split(":", 1)[1]
            energy = evaluation["energies"].get(energy_kind)
            if energy is None or energy.get("value") is None:
                raise ResultFinalizationError("Requested evaluated energy is unavailable")
            artifact = EnergyArtifact(**common, metadata={"role": role, "energy": energy, "energy_kind": energy_kind,
                                       "periodic_geometry_id": endpoint["geometry"]["geometry_id"]})
        elif role == "periodic-structure" and evidence["mode"]["calculation"] == "fixed-cell-relaxation":
            geometry = endpoint["geometry"]
            artifact = StructureArtifact(**common, source="evaluated-periodic-snapshot:" + evidence["snapshot_id"],
                format="vasp-periodic", atom_count=len(geometry["species"]),
                elemental_composition=dict(Counter(geometry["species"])), geometry_hash=stable_hash(geometry),
                metadata={"role": role, "periodic_geometry": geometry,
                          "relaxation_scope": "whole-atom constraints retained; not unconstrained relaxation"})
        else:
            raise ResultFinalizationError("Requested artifact role is unsupported")
        planned.append(artifact)
    supplied = {a.artifact_id: artifact_finalization.ArtifactFinalizationEvidence(validation, files, provenance) for a in planned}
    try:
        bundle = artifact_finalization.finalize_artifact_bundle(planned, supplied, external_artifacts=parents)
    except artifact_finalization.ArtifactBundleFinalizationError as error:
        # Even core partial_artifacts are diagnostic only: never publish them.
        raise ResultFinalizationError("Core artifact bundle finalization refused", findings=[f.to_dict() for f in error.failures]) from error
    record = {"schema_version": 1, "kind": "vasp-artifact-finalization", "adapter": {"name": "cmw-vasp", "version": "1"},
              "artifact_bundle": bundle.to_dict(), "evidence": evidence, "policy_identity": assessment["policy_identity"],
              "validation_decision": assessment, "required_sources": sources,
              "declared_intent": "retrospective", "producing_calculation": spec["producing_calculation"],
              "limitations": ["Sources were observed sequentially, not as an atomic filesystem snapshot.",
                "A write after source revalidation can race publication; completed records describe their recorded snapshots.",
                "Saved local execution records and retrospective declarations are not cryptographically authenticated."]}
    record["record_id"] = _record_identity(record)
    if dry_run:
        return {**record, "dry_run": True, "publication": {"state": "preview", "record_path": plan["record_path"]}}
    return publish_record(record, plan, validate_sources=lambda: revalidate_sources(sources))
