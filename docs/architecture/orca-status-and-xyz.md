# ORCA status and XYZ contracts

`cmw.structure.xyz` provides the initial molecular structure boundary without a
chemistry-framework dependency. An `XYZGeometry` is an ordered, zero-indexed
sequence of element symbols and finite Cartesian coordinates in ångström. XYZ
atom counts and rows are strict. Comments, charge, and multiplicity are not part
of geometry identity; electronic state belongs to the calculation target.

`cmw.molecular.orca.status` separates three questions:

1. What factual evidence is present in the ORCA output?
2. Did the ORCA process and program terminate successfully?
3. Does that evidence satisfy the selected OPT, FREQ, or SP policy?

Normal termination is therefore not optimisation convergence, and process
success is not scientific validity. Frequency parsing reports every negative
mode. A minimum requirement and its imaginary-frequency tolerance are explicit
configuration; no molecule-specific threshold is embedded as a universal
default. The SP validator checks completion, SCF convergence, and final energy,
but makes no claim that one electronic-structure method is higher-level than
another.
