# CI timing baseline — 9 November 2026

Read-only GitHub Actions sample collected before workflow edits: the most recent
20 **CI** runs (Pages excluded), 24 attempts, 18 completed runs and two active at
the sampling boundary. All occurred on 9 November; this describes that workload,
not a long-term average. Active jobs are excluded from completed-job statistics.
Successful job execution is measured from the first actual step to completion;
queue time is job creation to its first step. No-runner cancellations have zero
execution and are reported separately. Carried-over jobs across attempts are
deduplicated by name/start/completion, so a retry does not charge old successes twice.

| Job | Successful samples | Median execution, min | Range, min |
|---|---:|---:|---:|
| Lint | 20 | 0.13 | 0.12–0.20 |
| Ubuntu full suite | 12 | 9.57 | 6.55–12.07 |
| macOS full suite | 2 | 13.58 | 13.33–13.83 |
| Ubuntu full matrix | 10 | 30.37 | 21.58–38.97 |
| macOS full matrix | 2 | 22.90 | 21.83–23.97 |
| Ubuntu core wheel | 12 | 2.44 | 1.97–2.90 |
| macOS core wheel | 2 | 3.20 | 3.12–3.28 |
| Ubuntu all install modes | 2 | 8.53 | 7.22–9.85 |
| macOS all install modes | 1 | 11.85 | 11.85 |

PR successful first attempts: six samples, median **26.74 min**, 22.08–39.55.
The 14 completed PR runs including superseded/cancelled runs and retry delays
have a 16.32-minute end-to-end median; that mixed number is not a successful-check
latency estimate. The two main runs took **31.63 and 104.95 minutes** end-to-end;
the latter includes capacity cancellations and later requests. No manual run in
this sample had fully succeeded at collection: two completed interrupted runs
took 2.18 and 10.47 minutes; the third was still active.

Queue times for executed jobs: Ubuntu median **0.05 min**, range 0.03–0.07
(77 jobs); macOS median **5.05 min**, range 0.07–13.95 (11 jobs). Ten macOS and
three Ubuntu jobs were cancelled without a runner; macOS capacity cancellations
must not be interpreted as test failures. Queue time is distinct from dependency
waiting before GitHub creates a job and from human delay before a new request.

Across completed executed jobs (including interrupted partial work), matrices
consumed **57.9%** of execution time; install jobs consumed **12.5%** (6.3% core,
6.2% all modes); suites 29.2%; lint 0.4%. These are runner-time shares, not additive
critical-path shares: jobs run concurrently.

All three manual runs had a PR run at the identical head, and both Ubuntu suite
copies started: `00708c3` completed both; `5fb0937` completed the manual copy
while PR was cancelled; `18639ac` interrupted both. This is 3/3 duplicated
starts, 1/3 duplicated completed suites, not three fully completed duplicate pairs.

The complete run/attempt and per-job tables below preserve cancelled and active
observations. Raw API metadata is retained privately with the task evidence.

| Run | Event | Head | Snapshot outcome | Attempts | End-to-end min |
|---|---|---|---|---:|---:|
| [42651650344](https://github.com/angzeli/computational-modelling-workflow/actions/runs/42651650344) | pull_request | `808c9a4` | in_progress | 1 | active |
| [42648396935](https://github.com/angzeli/computational-modelling-workflow/actions/runs/42648396935) | workflow_dispatch | `00708c3` | in_progress | 1 | active |
| [42648365044](https://github.com/angzeli/computational-modelling-workflow/actions/runs/42648365044) | pull_request | `00708c3` | success | 1 | 22.87 |
| [42647155822](https://github.com/angzeli/computational-modelling-workflow/actions/runs/42647155822) | push | `6ffbb8e` | success | 1 | 31.63 |
| [42646703272](https://github.com/angzeli/computational-modelling-workflow/actions/runs/42646703272) | workflow_dispatch | `5fb0937` | cancelled | 1 | 10.57 | 1 | 10.47 |
| [42646666415](https://github.com/angzeli/computational-modelling-workflow/actions/runs/42646666415) | pull_request | `5fb0937` | cancelled | 1 | 10.57 | 1 | 10.47 |
| [42646364757](https://github.com/angzeli/computational-modelling-workflow/actions/runs/42646364757) | workflow_dispatch | `18639ac` | cancelled | 1 | 2.63 | 1 | 2.18 |
| [42646269301](https://github.com/angzeli/computational-modelling-workflow/actions/runs/42646269301) | pull_request | `18639ac` | cancelled | 1 | 2.63 | 1 | 2.18 |
| [42643960234](https://github.com/angzeli/computational-modelling-workflow/actions/runs/42643960234) | pull_request | `7144be1` | success | 1 | 39.55 |
| [42643691962](https://github.com/angzeli/computational-modelling-workflow/actions/runs/42643691962) | pull_request | `44acfed` | cancelled | 1 | 1.88 |
| [42640503389](https://github.com/angzeli/computational-modelling-workflow/actions/runs/42640503389) | pull_request | `7c2c3c9` | success | 1 | 22.07 |
| [42640387344](https://github.com/angzeli/computational-modelling-workflow/actions/runs/42640387344) | pull_request | `a3b58cc` | cancelled | 1 | 0.97 |
| [42633334504](https://github.com/angzeli/computational-modelling-workflow/actions/runs/42633334504) | push | `8d4cfc3` | success | 4 | 104.95 |
| [42632638438](https://github.com/angzeli/computational-modelling-workflow/actions/runs/42632638438) | pull_request | `2e75c02` | success | 2 | 41.92 |
| [42628449955](https://github.com/angzeli/computational-modelling-workflow/actions/runs/42628449955) | pull_request | `309bb88` | success | 1 | 30.62 |
| [42628001324](https://github.com/angzeli/computational-modelling-workflow/actions/runs/42628001324) | pull_request | `480c116` | success | 1 | 22.17 |
| [42627558695](https://github.com/angzeli/computational-modelling-workflow/actions/runs/42627558695) | pull_request | `51b0e91` | cancelled | 1 | 6.38 |
| [42627493525](https://github.com/angzeli/computational-modelling-workflow/actions/runs/42627493525) | pull_request | `082ae57` | cancelled | 1 | 0.47 |
| [42627430175](https://github.com/angzeli/computational-modelling-workflow/actions/runs/42627430175) | pull_request | `e315e0c` | cancelled | 1 | 0.70 |
| [42620006673](https://github.com/angzeli/computational-modelling-workflow/actions/runs/42620006673) | pull_request | `0ce2e14` | success | 1 | 31.17 |

### Per-job executed wall and queue times

| Run | Job | Outcome | Execution min | Queue min |
|---|---|---|---:|---:|
| 42651650344 | Lint and publication hygiene | success | 0.13 | 0.05 |
| 42651650344 | Tests / ubuntu-24.04 / Python 3.11 | success | 7.90 | 0.03 |
| 42651650344 | Installed artifacts / ubuntu-24.04 / core wheel | success | 2.30 | 0.05 |
| 42648396935 | Lint and publication hygiene | success | 0.13 | 0.03 |
| 42648396935 | Tests / ubuntu-24.04 / Python 3.11 | success | 10.30 | 0.03 |
| 42648396935 | Tests / macos-15 / Python 3.14 | failure | 12.50 | 9.85 |
| 42648396935 | Full ownership matrix / ubuntu-24.04 / Python 3.11 | success | 34.80 | 0.05 |
| 42648396935 | Installed artifacts / macos-15 / all modes | success | 11.85 | 10.07 |
| 42648396935 | Installed artifacts / ubuntu-24.04 / all modes | success | 9.85 | 0.03 |
| 42648365044 | Lint and publication hygiene | success | 0.13 | 0.05 |
| 42648365044 | Installed artifacts / ubuntu-24.04 / core wheel | success | 2.77 | 0.05 |
| 42648365044 | Tests / ubuntu-24.04 / Python 3.11 | success | 6.55 | 0.05 |
| 42648365044 | Full ownership matrix / ubuntu-24.04 / Python 3.11 | success | 22.35 | 0.05 |
| 42647155822 | Lint and publication hygiene | success | 0.13 | 0.05 |
| 42647155822 | Full ownership matrix / ubuntu-24.04 / Python 3.11 | success | 30.67 | 0.05 |
| 42647155822 | Tests / macos-15 / Python 3.14 | success | 13.83 | 0.07 |
| 42647155822 | Full ownership matrix / macos-15 / Python 3.14 | success | 23.97 | 7.47 |
| 42647155822 | Installed artifacts / ubuntu-24.04 / core wheel | success | 2.70 | 0.05 |
| 42647155822 | Tests / ubuntu-24.04 / Python 3.11 | success | 10.08 | 0.07 |
| 42647155822 | Installed artifacts / macos-15 / core wheel | success | 3.28 | 13.95 |
| 42646703272 | Lint and publication hygiene | success | 0.13 | 0.03 |
| 42646703272 | Full ownership matrix / ubuntu-24.04 / Python 3.11 | cancelled | 10.05 | 0.05 |
| 42646703272 | Installed artifacts / macos-15 / all modes | cancelled | 10.17 | 0.10 |
| 42646703272 | Full ownership matrix / macos-15 / Python 3.14 | cancelled | 10.12 | 0.07 |
| 42646703272 | Tests / ubuntu-24.04 / Python 3.11 | success | 9.57 | 0.05 |
| 42646703272 | Tests / macos-15 / Python 3.14 | cancelled | 10.15 | 0.10 |
| 42646703272 | Installed artifacts / ubuntu-24.04 / all modes | success | 7.22 | 0.05 |
| 42646666415 | Lint and publication hygiene | success | 0.13 | 0.03 |
| 42646666415 | Tests / ubuntu-24.04 / Python 3.11 | cancelled | 10.03 | 0.05 |
| 42646666415 | Full ownership matrix / ubuntu-24.04 / Python 3.11 | cancelled | 10.03 | 0.05 |
| 42646666415 | Installed artifacts / ubuntu-24.04 / core wheel | success | 2.77 | 0.05 |
| 42646364757 | Lint and publication hygiene | success | 0.13 | 0.03 |
| 42646364757 | Full ownership matrix / ubuntu-24.04 / Python 3.11 | cancelled | 1.90 | 0.05 |
| 42646364757 | Tests / ubuntu-24.04 / Python 3.11 | cancelled | 1.90 | 0.03 |
| 42646364757 | Installed artifacts / ubuntu-24.04 / all modes | cancelled | 1.90 | 0.05 |
| 42646269301 | Lint and publication hygiene | success | 0.12 | 0.05 |
| 42646269301 | Installed artifacts / ubuntu-24.04 / core wheel | cancelled | 2.33 | 0.05 |
| 42646269301 | Full ownership matrix / ubuntu-24.04 / Python 3.11 | cancelled | 2.38 | 0.05 |
| 42646269301 | Tests / ubuntu-24.04 / Python 3.11 | cancelled | 2.38 | 0.03 |
| 42643960234 | Lint and publication hygiene | success | 0.20 | 0.03 |
| 42643960234 | Full ownership matrix / ubuntu-24.04 / Python 3.11 | success | 38.97 | 0.07 |
| 42643960234 | Tests / ubuntu-24.04 / Python 3.11 | success | 10.42 | 0.07 |
| 42643960234 | Installed artifacts / ubuntu-24.04 / core wheel | success | 2.30 | 0.07 |
| 42643691962 | Lint and publication hygiene | success | 0.12 | 0.05 |
| 42643691962 | Tests / ubuntu-24.04 / Python 3.11 | cancelled | 1.63 | 0.03 |
| 42643691962 | Installed artifacts / ubuntu-24.04 / core wheel | cancelled | 1.65 | 0.03 |
| 42643691962 | Full ownership matrix / ubuntu-24.04 / Python 3.11 | cancelled | 1.65 | 0.05 |
| 42640503389 | Lint and publication hygiene | success | 0.12 | 0.05 |
| 42640503389 | Full ownership matrix / ubuntu-24.04 / Python 3.11 | success | 21.58 | 0.05 |
| 42640503389 | Tests / ubuntu-24.04 / Python 3.11 | success | 12.07 | 0.07 |
| 42640503389 | Installed artifacts / ubuntu-24.04 / core wheel | success | 2.38 | 0.05 |
| 42640387344 | Lint and publication hygiene | success | 0.12 | 0.05 |
| 42640387344 | Full ownership matrix / ubuntu-24.04 / Python 3.11 | cancelled | 0.73 | 0.03 |
| 42640387344 | Tests / ubuntu-24.04 / Python 3.11 | cancelled | 0.73 | 0.05 |
| 42640387344 | Installed artifacts / ubuntu-24.04 / core wheel | cancelled | 0.75 | 0.05 |
| 42633334504 | Lint and publication hygiene | success | 0.12 | 0.05 |
| 42633334504 | Installed artifacts / ubuntu-24.04 / core wheel | success | 2.50 | 0.07 |
| 42633334504 | Tests / ubuntu-24.04 / Python 3.11 | success | 10.55 | 0.07 |
| 42633334504 | Full ownership matrix / ubuntu-24.04 / Python 3.11 | success | 29.87 | 0.05 |
| 42633334504 | Installed artifacts / macos-15 / core wheel | success | 3.12 | 5.05 |
| 42633334504 | Tests / macos-15 / Python 3.14 | success | 13.33 | 5.05 |
| 42633334504 | Full ownership matrix / macos-15 / Python 3.14 | success | 21.83 | 0.08 |
| 42632638438 | Lint and publication hygiene | success | 0.13 | 0.05 |
| 42632638438 | Full ownership matrix / ubuntu-24.04 / Python 3.11 | success | 32.58 | 0.05 |
| 42632638438 | Installed artifacts / ubuntu-24.04 / core wheel | success | 2.90 | 0.05 |
| 42632638438 | Tests / ubuntu-24.04 / Python 3.11 | failure | 8.90 | 0.07 |
| 42632638438 | Tests / ubuntu-24.04 / Python 3.11 | success | 7.83 | 0.07 |
| 42628449955 | Lint and publication hygiene | success | 0.12 | 0.05 |
| 42628449955 | Installed artifacts / ubuntu-24.04 / core wheel | success | 1.97 | 0.05 |
| 42628449955 | Full ownership matrix / ubuntu-24.04 / Python 3.11 | success | 30.07 | 0.07 |
| 42628449955 | Tests / ubuntu-24.04 / Python 3.11 | success | 9.57 | 0.05 |
| 42628001324 | Lint and publication hygiene | success | 0.12 | 0.05 |
| 42628001324 | Installed artifacts / ubuntu-24.04 / core wheel | success | 2.37 | 0.05 |
| 42628001324 | Tests / ubuntu-24.04 / Python 3.11 | success | 8.20 | 0.05 |
| 42628001324 | Full ownership matrix / ubuntu-24.04 / Python 3.11 | success | 21.92 | 0.05 |
| 42627558695 | Lint and publication hygiene | success | 0.13 | 0.05 |
| 42627558695 | Full ownership matrix / ubuntu-24.04 / Python 3.11 | cancelled | 6.07 | 0.05 |
| 42627558695 | Installed artifacts / ubuntu-24.04 / core wheel | success | 2.07 | 0.03 |
| 42627558695 | Tests / ubuntu-24.04 / Python 3.11 | cancelled | 6.10 | 0.05 |
| 42627493525 | Lint and publication hygiene | success | 0.12 | 0.05 |
| 42627430175 | Lint and publication hygiene | success | 0.13 | 0.03 |
| 42627430175 | Tests / ubuntu-24.04 / Python 3.11 | cancelled | 0.37 | 0.03 |
| 42627430175 | Installed artifacts / ubuntu-24.04 / core wheel | cancelled | 0.47 | 0.05 |
| 42627430175 | Full ownership matrix / ubuntu-24.04 / Python 3.11 | cancelled | 0.43 | 0.05 |
| 42620006673 | Lint and publication hygiene | success | 0.13 | 0.03 |
| 42620006673 | Tests / ubuntu-24.04 / Python 3.11 | success | 8.02 | 0.05 |
| 42620006673 | Installed artifacts / ubuntu-24.04 / core wheel | success | 2.87 | 0.05 |
| 42620006673 | Full ownership matrix / ubuntu-24.04 / Python 3.11 | success | 30.93 | 0.05 |
