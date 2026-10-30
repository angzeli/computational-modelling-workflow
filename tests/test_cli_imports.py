"""Keep short ORCA workflow commands independent of unused analysis imports."""
import subprocess
import sys
import unittest


class CliImportTests(unittest.TestCase):
    def run_isolated(self, script):
        result = subprocess.run([sys.executable, '-B', '-c', script],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_stage_cli_does_not_load_unrequested_analysis(self):
        for module in ('cmw.molecular.orca.cli', 'cmw.molecular.workflows.cli'):
            with self.subTest(module=module):
                self.run_isolated(
                    f'import importlib, sys; importlib.import_module({module!r}); '
                    "unrelated = {'cmw.molecular.orca.led', 'cmw.molecular.orca.properties', "
                    "'cmw.molecular.orca.excited_states', 'cmw.molecular.multiwfn.excited_states', "
                    "'cmw.molecular.workflows.intermolecular_led'}; "
                    "assert not unrelated.intersection(sys.modules), unrelated.intersection(sys.modules)")

    def test_public_analysis_exports_keep_identity_and_star_imports(self):
        self.run_isolated("""
import importlib
checks = {
    'cmw.molecular.orca': {
        'properties': ('AtomicCharge', 'parse_ground_state_properties'),
        'excited_states': ('ORCA_EXCITED_STATE_GRAMMAR', 'parse_orca_tda_excited_states'),
        'led': ('OrcaLEDError', 'assemble_interfragment_led'),
    },
    'cmw.molecular.workflows': {
        'intermolecular_led': ('INTERMOLECULAR_LED_CONTRACT', 'build_intermolecular_led_plan'),
    },
}
for package_name, modules in checks.items():
    package = importlib.import_module(package_name)
    assert set(package.__all__) | set(modules) <= set(dir(package))
    for module_name, names in modules.items():
        module = getattr(package, module_name)
        assert module is importlib.import_module(package_name + '.' + module_name)
        for name in names:
            assert getattr(package, name) is getattr(module, name), name
            assert getattr(package, name) is getattr(package, name), name
    namespace = {}
    exec('from ' + package_name + ' import *', namespace)
    assert set(namespace) - {'__builtins__'} == set(package.__all__)
    for name in package.__all__:
        assert namespace[name] is getattr(package, name), name
    try:
        getattr(package, 'no_such_analysis_export')
    except AttributeError:
        pass
    else:
        raise AssertionError('unknown public name was accepted')
""")
