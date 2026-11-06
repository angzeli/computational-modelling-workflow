"""Field-level differences between recorded and re-observed JSON evidence."""

_MISSING = object()


def field_differences(field, stored, observed):
    """Compare JSON by type and value, distinguishing absent fields from null."""
    if isinstance(stored, dict) and isinstance(observed, dict):
        differences = []
        for key in sorted(stored.keys() | observed.keys()):
            differences.extend(field_differences(
                f"{field}.{key}", stored.get(key, _MISSING), observed.get(key, _MISSING)))
        return differences
    if isinstance(stored, list) and isinstance(observed, list):
        differences = []
        for index in range(max(len(stored), len(observed))):
            differences.extend(field_differences(
                f"{field}[{index}]", stored[index] if index < len(stored) else _MISSING,
                observed[index] if index < len(observed) else _MISSING))
        return differences
    if type(stored) is type(observed) and stored == observed:
        return []
    return [{"field": field, "stored": {"missing": True} if stored is _MISSING else stored,
             "observed": {"missing": True} if observed is _MISSING else observed}]
