"""Presentation of read-only VASP evidence and explicit Scratch finalization."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path


def _json(value: dict) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, allow_nan=False))


def _failure(error: Exception, *, as_json: bool) -> int:
    from cmw.core.preparation_publication import PublicationError

    result = {"status": "error", "code": getattr(error, "code", "RESULT_REQUEST_REJECTED"),
              "message": str(error), "exit_code": 2}
    if hasattr(error, "findings"):
        result["findings"] = error.findings
    if isinstance(error, PublicationError):
        result.update(record_path=error.record_path, incomplete=error.incomplete)
    if as_json:
        _json(result)
    else:
        print(f"{result['code']}: {error}")
        if result.get("record_path"):
            print(f"Scratch record: {result['record_path']}; incomplete: {result['incomplete']}")
    return 2


def _summary(result: dict) -> None:
    print(f"Result evidence: {result['status']}")
    current = result.get("selected_segment")
    if current:
        print(f"Mode: {result.get('mode', {}).get('calculation', 'unavailable')}; VASP: {current.get('version') or 'unobserved'}")
        print(f"Selected segment: {current['index']}; identity: {current['segment_id']}")
        evaluations = current.get("evaluations", [])
        native = Counter(item.get("native_convergence") for item in evaluations)
        numeric = Counter(item.get("numeric_convergence", "UNKNOWN") for item in evaluations)
        print(f"Evaluations: {len(evaluations)}; complete: {sum(item.get('complete') is True for item in evaluations)}")
        print(f"Native electronic convergence: yes {native[True]}, no {native[False]}, unknown {native[None]}")
        print("Numeric electronic convergence: " + ", ".join(f"{key} {numeric[key]}" for key in ("PASS", "FAIL", "UNKNOWN", "NOT_APPLICABLE")))
        termination = current.get("termination", {})
        print("Normal terminal output: " + ("observed" if termination.get("normal_footer") else "unestablished"))
        execution = (result.get("binding") or {}).get("execution") or result.get("execution", {})
        print("Process exit: " + (str(execution["exit_code"]) if execution.get("exit_code") is not None else "unobserved"))
        endpoint = result.get("endpoint", {})
        print("Evaluated endpoint: " + ("available" if endpoint.get("geometry") else "unavailable"))
        for name, comparison in endpoint.get("comparisons", {}).items():
            print(f"  {name}: {comparison['status']}")
        summary = endpoint.get("force_summary")
        if summary:
            for group in ("all", "free", "fixed"):
                value = summary[group]
                if value is None:
                    print(f"Force {group}: unavailable constraint evidence")
                    continue
                maximum = value["maximum_norm"]
                display = "unavailable" if maximum is None else f"{maximum:.8g} eV/Angstrom"
                print(f"Force {group}: {value['count']} atoms; maximum norm {display}")
        for reason in result.get("mode", {}).get("reasons", []):
            print(f"Unsupported scope: {reason}")
    elif result.get("segments"):
        print("Select a zero-based --segment:")
        for item in result["segments"]:
            print(f"  {item['index']}: VASP {item['version']}; evaluations {item['evaluation_count']}; terminal footer {item['normal_footer']}")
    print(f"Source snapshot: {result['snapshot_id']}")
    if result.get("error"):
        print(f"Source error: {result['error']}")
    if result.get("conflicts"):
        print("Evidence conflicts: " + ", ".join(item["code"] for item in result["conflicts"]))
    assessment = result.get("policy_assessment")
    if assessment:
        print(f"Policy {assessment['policy_identity']['name']}: {assessment['status']}")
        for check in assessment.get("checks", []):
            if check["status"] not in {"PASS", "NOT_APPLICABLE"}:
                print(f"  {check['code']}: {check['status']}")


def _inspect(args: argparse.Namespace) -> int:
    from .result_finalization import inspect_with_policy

    try:
        result = inspect_with_policy(args.directory, policy_path=args.policy, spec_path=args.spec,
                                     segment=args.segment, stdout=args.stdout, max_bytes=args.max_bytes)
        _json(result) if args.json else _summary(result)
        return result["exit_code"]
    except (OSError, ValueError, TypeError) as error:
        return _failure(error, as_json=args.json)


def _finalize(args: argparse.Namespace) -> int:
    from .result_finalization import finalize_result

    try:
        result = finalize_result(args.directory, policy_path=args.policy, spec_path=args.spec,
                                 scratch_root=args.scratch_root, record_directory=args.record_directory,
                                 scratch_mount=args.scratch_mount, segment=args.segment, stdout=args.stdout,
                                 dry_run=args.dry_run)
        if args.json:
            _json(result)
        else:
            print(f"Finalization: {result['publication']['state']}")
            print(f"Scratch record: {result['publication']['record_path']}")
            print(f"Record identity: {result['record_id']}")
            print(f"Artifacts: {result['artifact_bundle']['artifact_count']}")
            print(f"Policy {result['policy_identity']['name']}: {result['validation_decision']['status']}")
        return 0
    except (OSError, ValueError, TypeError) as error:
        return _failure(error, as_json=args.json)


def _verify(args: argparse.Namespace) -> int:
    from .result_finalization import verify_finalization

    try:
        result = verify_finalization(args.record, policy_path=args.policy)
        result = {**result, "artifacts": [item.to_dict() for item in result.get("artifacts", [])]}
        result["exit_code"] = 0 if result["valid"] else 2
        if args.json:
            _json(result)
        else:
            print("Finalization record: " + ("valid for current recorded sources" if result["valid"] else "invalid"))
            for finding in result.get("findings", []):
                print(f"  {finding}")
        return result["exit_code"]
    except (OSError, ValueError, TypeError) as error:
        return _failure(error, as_json=args.json)


def register(commands: argparse._SubParsersAction) -> None:
    inspecting = commands.add_parser("inspect-result", help="read-only evidence from an existing VASP run")
    inspecting.add_argument("directory", type=Path, help="existing run directory")
    inspecting.add_argument("--policy", type=Path, help="explicit JSON acceptance policy")
    inspecting.add_argument("--spec", type=Path, help="optional execution/input binding specification; requires --policy")
    inspecting.add_argument("--max-bytes", type=int, help="per-large-file read limit; limited evidence is not complete")
    inspecting.set_defaults(handler=_inspect)
    finalizing = commands.add_parser("finalize-result", help="finalize accepted artifact identities in a new Scratch record")
    finalizing.add_argument("directory", type=Path, help="existing run directory")
    finalizing.add_argument("--policy", type=Path, required=True, help="explicit JSON acceptance policy")
    finalizing.add_argument("--spec", type=Path, required=True, help="execution/input binding and artifact specification")
    finalizing.add_argument("--scratch-root", type=Path, required=True, help="existing explicit Scratch root")
    finalizing.add_argument("--record-directory", type=Path, required=True, help="new record directory; relative to Scratch root")
    finalizing.add_argument("--scratch-mount", type=Path, help="optional required mounted filesystem containing Scratch")
    finalizing.add_argument("--dry-run", action="store_true", help="validate and preview without writing")
    finalizing.set_defaults(handler=_finalize)
    for parser in (inspecting, finalizing):
        parser.add_argument("--segment", type=int, help="explicit zero-based OUTCAR segment index")
        parser.add_argument("--stdout", type=Path, help="explicit electronic table source instead of OSZICAR")
        parser.add_argument("--json", action="store_true", help="print structured evidence")
    verifying = commands.add_parser("verify-result-record", help="read-only recheck of a finalization record and its required sources")
    verifying.add_argument("record", type=Path, help="Scratch finalization.json")
    verifying.add_argument("--policy", type=Path, help="optionally require this same policy content identity")
    verifying.add_argument("--json", action="store_true")
    verifying.set_defaults(handler=_verify)
