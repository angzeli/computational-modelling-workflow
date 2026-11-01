"""Optional native observations collected during the existing OUTCAR pass."""
from __future__ import annotations

import re

from .results import NUMBER, _scalar, number, reference

_EXTRA = re.compile(r"\b(NBANDS|NKPTS|AEXX|HFSCREEN|LFOCKACE|LVHAR|LVTOT|LVACPOTAV|EFERMI)\s*=\s*([^\s;]+)")


def collect_analysis_line(segment: dict, evaluation: dict | None, line,
                          *, in_echo: bool, in_final_energy: bool) -> None:
    """Retain only requested-analysis evidence; never change result acceptance."""
    text = line.text
    if evaluation is None:
        target = segment.setdefault("analysis_echo" if in_echo else "analysis_settings", {})
        for match in _EXTRA.finditer(text):
            key, value = match[1], _scalar(match[2])
            if key in target and target[key]["value"] != value:
                segment.setdefault("analysis_findings", []).append({"code": "conflicting_setting", "tag": key})
            target[key] = {"value": value, "reference": reference(line)}
        method = re.search(r"EFERMI\s*=.*?METHOD\s*=\s*(\S+)", text)
        if method:
            segment["fermi_convention"] = method[1].upper()
    if evaluation is None:
        return
    if re.match(r"\s*LOOP\+?\s*:", text, re.I):
        from .analysis_evidence import parse_loop_timing

        outer = bool(re.match(r"\s*LOOP\+", text, re.I))
        record = evaluation if outer else evaluation["iterations"][-1]
        try:
            if segment["termination"]["footer_reference"]:
                raise ValueError("LOOP after timing footer")
            if outer and not evaluation["complete"]:
                raise ValueError("LOOP+ lacks completed native force/energy evaluation")
            if not outer and (evaluation["force_block_complete"] or in_final_energy):
                raise ValueError("LOOP outside electronic interval")
            timer = parse_loop_timing(text)
            if "loop_timing" in record:
                raise ValueError("Multiple timing records in one native interval")
            record["loop_timing"] = {**timer, "reference": reference(line)}
        except ValueError as error:
            record["loop_timing"] = None
            record["timing_issue"] = str(error)
    fermi = re.search(r"(?:E-fermi\s*:|Fermi energy:)\s*(" + NUMBER + ")", text, re.I)
    if fermi:
        evaluation.setdefault("fermi_records", []).append({**number(fermi[1]), "reference": reference(line)})
    for label, name in (("val. band max:", "vbm"), ("cond. band min:", "cbm"), ("fundamental gap:", "gap")):
        if label in text:
            match = re.search(re.escape(label) + r"\s*(" + NUMBER + ")", text)
            if match:
                evaluation.setdefault("reported_edges", {})[name] = {**number(match[1]), "reference": reference(line)}
    vacuum = re.search(r"vacuum level on the upper side and lower side of the slab\s*[:=]?\s*(" + NUMBER + r")\s+(" + NUMBER + ")", text, re.I)
    if vacuum:
        record = {key: {**number(token), "reference": reference(line)}
                  for key, token in zip(("upper", "lower"), vacuum.groups())}
        evaluation.setdefault("vacuum_records", []).append(record)
    # VASP prints these diagnostics across boxed lines, without necessarily
    # placing WARNING, vacuum and the failure phrase on the same line.
    block = evaluation.pop("_vacuum_warning_block", None)
    continuation = False
    if block and block["remaining"] > 0 and text.strip().startswith("|"):
        content = text.strip().strip("| ")
        if content and not set(content) <= set("-+ "):
            record = evaluation["vacuum_warnings"][block["index"]]
            record["message"] += " " + content
            record["reference"].update(line_end=line.line_number, byte_end=line.byte_end)
            block["remaining"] -= 1
            evaluation["_vacuum_warning_block"] = block
            continuation = True
    starts_block = re.search(r"did not find any points to average over|minimum charge density times volume", text, re.I)
    generic_warning = (re.search(r"vacuum|field.free|VACPOT", text, re.I)
                       and re.search(r"WARNING|no points|no field.free|too high|not found", text, re.I))
    if not continuation and (starts_block or generic_warning):
        warnings = evaluation.setdefault("vacuum_warnings", [])
        warnings.append({"message": text.strip().strip("| "), "reference": reference(line)})
        if starts_block:
            evaluation["_vacuum_warning_block"] = {"index": len(warnings) - 1, "remaining": 6}
    spin = re.match(r"\s*spin component\s+(\d+)\s*$", text)
    if spin:
        evaluation["band_spin"] = int(spin[1]) - 1
    point = re.match(r"\s*k-point\s+(\d+)\s*:\s*(" + NUMBER + r")\s+(" + NUMBER + r")\s+(" + NUMBER + ")", text)
    if point:
        entry = {"native_kpoint_label": int(point[1]), "spin_index": evaluation.get("band_spin", 0),
                 "fractional": [number(v) for v in point.groups()[1:]], "bands": [], "reference": reference(line)}
        evaluation.setdefault("band_points", []).append(entry)
        evaluation["band_table_open"] = True
    band = re.fullmatch(r"\s*(\d+)\s+(" + NUMBER + r")\s+(" + NUMBER + r")\s*", text)
    if evaluation.get("band_table_open") and band:
        evaluation["band_points"][-1]["bands"].append({"native_band_label": int(band[1]),
            "energy": number(band[2]), "occupation": number(band[3]), "reference": reference(line)})
    elif evaluation.get("band_table_open") and text.strip() and not point and "band No." not in text:
        evaluation["band_table_open"] = False
