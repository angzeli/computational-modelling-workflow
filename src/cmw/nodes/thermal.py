"""Endpoint-owned optional settings, separate from enrollment and readiness."""
from pathlib import Path

from cmw.jobs.thermal import ThermalProvider
from cmw.jobs.thermal_config import read_settings


def configured_provider(config_root, *, provider_factory=None):
    """Use only this endpoint's private file, never frontend/inherited settings."""
    try:
        settings = read_settings(Path(config_root) / 'thermal.env')
    except (OSError, ValueError) as exc:
        return ThermalProvider.unavailable('Endpoint thermal configuration invalid: ' + str(exc))
    factory = provider_factory or ThermalProvider
    return factory(settings.get('CMW_THERMAL_MACMON'),
                   interval=float(settings.get('CMW_THERMAL_INTERVAL_SECONDS', '20')))
