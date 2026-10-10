"""Fictional numbers in the observed ORCA 6.1 DIIS/SOSCF/geometry grammar.

No coordinates, private output, engine invocation or scientific record is used.
"""


def header(stage='OPT', version='6.1.1', mode='Total+1el-Energy'):
    return f'''Program Version {version}
INPUT FILE
| 1> ! HF STO-3G {stage}
| 2> ****END OF INPUT****
Max. no of cycles MaxIter .... 40
SCF Procedure:
 Maximum # iterations MaxIter .... 125
Convergence Tolerance:
 Convergence Check Mode ConvCheckMode .... {mode}
 Energy Change TolE .... 1.000e-08 Eh
 RMS Density Change TolRMSP .... 5.000e-09
 Max Density Change TolMaxP .... 1.000e-07
 DIIS Error TolErr .... 5.000e-07
 Orbital Gradient TolG .... 1.000e-05
'''


def scf(*, soscf=True, converged=True):
    text = '''--------------------------------D-I-I-S--------------------------------
Iteration    Energy (Eh)           Delta-E    RMSDP     MaxDP     DIISErr   Damp  Time(sec)
 1 -10.0000000000 0.00e+00 2.00e-03 4.00e-03 1.00e-02 0.700 0.1
 2 -10.0100000000 -1.00e-02 1.00e-03 2.00e-03 4.00e-03 0.000 0.2
'''
    if soscf:
        text += '''*** Initializing SOSCF ***
--------------------------------S-O-S-C-F--------------------------------
Iteration    Energy (Eh)           Delta-E    RMSDP     MaxDP     MaxGrad    Time(sec)
 3 -10.0100000010 -1.00e-09 1.00e-06 2.00e-06 1.00e-06 0.1
'''
    if converged:
        text += '''* SCF CONVERGED AFTER 3 CYCLES *
SCF CONVERGENCE
Last Energy change ... -1.0000e-09 Tolerance : 1.0000e-08
Last MAX-Density change ... 2.0000e-06 Tolerance : 1.0000e-07
Last RMS-Density change ... 1.0000e-06 Tolerance : 5.0000e-09
Last DIIS Error ... 4.0000e-03 Tolerance : 5.0000e-07
Last Orbital Gradient ... 1.0000e-06 Tolerance : 1.0000e-05
FINAL SINGLE POINT ENERGY -10.0100000010
'''
    return text


def geometry(*, max_gradient='0.0002000000', max_target='0.0003000000', max_native='YES', complete=True):
    text = f'''----------------------|Geometry convergence|-------------------------
Item value Tolerance Converged
---------------------------------------------------------------------
Energy change -0.0000010000 0.0000050000 YES
RMS gradient 0.0002000000 0.0001000000 NO
MAX gradient {max_gradient} {max_target} {max_native}
RMS step 0.0010000000 0.0020000000 YES
'''
    if complete:
        text += '''MAX step 0.0030000000 0.0040000000 YES
---------------------------------------------------------------------
'''
    return text


def cycle(number=1, *, soscf=True, table=True, converged=True):
    return (f'* GEOMETRY OPTIMIZATION CYCLE {number} *\n' +
            scf(soscf=soscf, converged=converged) + (geometry() if table else ''))


def output(cycles=2, *, footer=True):
    return header() + ''.join(cycle(n) for n in range(1, cycles + 1)) + (
        'THE OPTIMIZATION HAS CONVERGED\n****ORCA TERMINATED NORMALLY****\n'
        'TOTAL RUN TIME: 0 days 0 hours 0 minutes 9 seconds 125 msec\n' if footer else '')
