# Full Foundation scalability

## Permanent decision

The Product Owner authorized permanent four-lane execution on 25 November 2026 using development baseline `0ae625c71b9cdbf387abb075f77d22144921b3be`. The reviewed [experiment at 22e0052](https://github.com/ahmedali5696/care-pulse-3/tree/22e0052ba5ab4d05094e3cbc0536f667381e79db) established sufficient functional parity and provisional performance evidence for implementation. Its [full historical review](https://github.com/ahmedali5696/care-pulse-3/blob/22e0052ba5ab4d05094e3cbc0536f667381e79db/docs/full-foundation-scalability.md) remains the detailed record of earlier measurements, fixture analysis and proposed thresholds. The PO no longer requires identical hosted runner-image builds across lanes. Pinned toolchain and container inputs still apply. Approval to implement is not acceptance of this candidate or approval to push, dispatch, merge or deploy.

This branch carries only CI orchestration, evidence validation, tests and documentation. It retains the development migration head `0046_f01_audit_integrity`, the historical 0040 source and the bounded six-successor compatibility exercise. It does not import Wave 3 tests, application code, migrations, profiling tools or comparison modes. See [PostgreSQL CI diagnostics](postgresql-ci.md) for the acceptance procedure.

## Historical measurements (not acceptance of this change)

The original review used five successful complete Full runs. These were different application SHAs, schemas and case inventories, so the elapsed time series is descriptive rather than an A/B comparison.

| Run (attempt 1) | Tested commit | Full elapsed | Common / Ordinary / Historical / Compatibility / Gate (s) | Aggregate runner-min | Ordinary cases |
| --- | --- | ---: | --- | ---: | ---: |
| [45492117349](https://github.com/ahmedali5696/care-pulse-3/actions/runs/45492117349/attempts/1) | 0ae625c | 10:35 | 94 / 520 / 142 / 262 / 10 | 17.13 | 991 |
| [45596665119](https://github.com/ahmedali5696/care-pulse-3/actions/runs/45596665119/attempts/1) | eb43b7f | 17:04 | 97 / 901 / 132 / 330 / 13 | 24.55 | 1,037 |
| [45602487223](https://github.com/ahmedali5696/care-pulse-3/actions/runs/45602487223/attempts/1) | e2badab | 17:53 | 95 / 957 / 163 / 258 / 8 | 24.68 | 1,039 |
| [45613610891](https://github.com/ahmedali5696/care-pulse-3/actions/runs/45613610891/attempts/1) | bc27849 | 19:37 | 118 / 1,041 / 164 / 322 / 7 | 27.53 | 1,049 |
| [45621749596](https://github.com/ahmedali5696/care-pulse-3/actions/runs/45621749596/attempts/1) | 0c325ce | 19:52 | 101 / 1,062 / 162 / 256 / 16 | 26.62 | 1,051 |

The prior [profiling run 45655182854, attempt 1](https://github.com/ahmedali5696/care-pulse-3/actions/runs/45655182854/attempts/1) at `7f54927dbffd9df4f2d6da75fbebb880226cfdb0` succeeded as a profiling-only run. Both variants passed 1,051 mandatory cases with zero skips and cleanup, with observed head 0056 and runner image 20261122.23.1. Serial control / instrumented controller elapsed: 966.979 / 1019.840 seconds; integration job elapsed: 1014 / 1077 seconds. All pytest intervals: 809.360 / 886.763 seconds; seven focus groups: 560.409 / 629.234 seconds; ten proposed DB invocations: 446.832 / 509.780 seconds. The instrumented focus groups reported 64.243s setup, 547.032s call and 1.325s teardown. These are observations, not causal fixture or profiler-overhead attribution.

The subsequent isolated partition run [45682686890](https://github.com/ahmedali5696/care-pulse-3/actions/runs/45682686890) tested `22e0052ba5ab4d05094e3cbc0536f667381e79db` in both attempts. All three execution jobs and the common job succeeded in each attempt. **Both final comparison jobs failed** at the experimental equality check for runner-image versions (ops/ordinary_partition.py:137 at the tested commit), so neither attempt was Full Foundation acceptance. The PO accepts the functional parity and provisional performance results without imposing that equality requirement permanently.

| Attempt | Common | Serial control | DB | Stack | Comparison | Workflow elapsed |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| [1](https://github.com/ahmedali5696/care-pulse-3/actions/runs/45682686890/attempts/1) | 122s | 1161s | 578s | 536s | 7s (failed) | 21:40 |
| [2](https://github.com/ahmedali5696/care-pulse-3/actions/runs/45682686890/attempts/2) | 91s | 1199s | 575s | 555s | 8s (failed) | 21:50 |

Attempt 1 used runner images 20261116.376.1 (control and stack) and 20261116.133.1 (DB). Attempt 2 used 20261116.376.1 (control) and 20261122.23.1 (DB and stack). Both used ubuntu24 x86_64. Job durations come from the attempt-specific start and completion timestamps, and workflow duration includes scheduling, control, comparison and handoffs. Taking the longer of the two shard jobs versus the serial job gives a provisional Ordinary critical-path reduction of 50.2% and 52.0%, respectively. The two shard jobs together cost 47s less and 69s less than serial in these two observations. These are not measured Full-workflow savings, and the later Wave 3 inventory differs from development.

The retained attempt-2 sanitized artifacts were fetched again and their ZIP SHA-256 digests matched GitHub metadata:

| Variant | Artifact ID | SHA-256 | Controller seconds | Cases / reports |
| --- | ---: | --- | ---: | --- |
| Control | 15058363702 | e61ad5a7398062a853b9cf28ff169ed673fc27fce82efb7ecabb578f20199353 | 1147.764521 | 1051 / 39 |
| DB | 15058247411 | f1162bc6bd064e656d7a42198de2e03dd03a27e793c6d5c793067b6462de0ef6 | 526.987640 | 446 / 10 |
| Stack | 15058382161 | e518eb8312574b98eafd74eb024a0e4ca1315a52d397fdfb332fcce214333be9 | 489.507737 | 605 / 29 |

All three record the same tested SHA, run/attempt, head `0056_b14_target_binding`, verified cleanup and zero skips. The two shard maps have no overlapping report keys, and their union equals the control for both report counts and complete per-report case-identity digests. The experiment intentionally includes serial control and does not produce Full receipts. Attempt-1 artifacts are no longer in the run's artifact listing; its job metadata and comparison logs remain available. Do not combine its evidence with attempt 2 or relabel a failed comparison as successful.

## Development inventory and maintenance

The current baseline contains 991 Ordinary cases, not 1,051. Keep F-01 and D-08 together on DB; retain all other ordinary pre-service and service scenarios on Stack, preserving original relative order, including B-09 before D-11. Both start with fresh isolated PostgreSQL clusters and independently validate the actual head. The historical and compatibility executions are unchanged. Distinct job runners, project identifiers, private synthetic secrets and cleanup keep environments independent. DB avoids Playwright and service image builds but retains the mandatory database/toolchain checks, tests and canary scans. Stack retains all service transitions, probes, browser acceptance, secret scans and topology checks.

When adding or reassigning a future suite, review the original selector, report/scenario identity, required setup and ordering dependencies. Change the inventory and associated execution together, prove exact selected-node and report parity, and add the focused positive/negative checks. Preserve intentional repeated tests under separate scenarios (topology seed/verify, health outages and recovery, split sender and B-05 selections). Counts and schema heads belong to the reviewed executable candidate, never to an unrelated experimental SHA. A lane's claimed success or an arbitrary nonempty report cannot replace the reviewed inventory.

The checked-in inventory in `ops/foundation_inventory.py` records all 37 report counts and expected identity digests. For a reviewed inventory change, collect each Python report's exact selector from `ops/check.ps1` with the locked pytest version using `python -m pytest --collect-only -q -p no:cacheprovider`, retaining its deselections and marker/name filters. Historical collection must use the pinned source archive and its own selectors. Convert collected node IDs using pytest's JUnit `mangle_test_address` and `bin_xml_escape` for the test name; hash the sorted (classname, name) pairs with the inventory's canonical JSON digest function. For TAP, use sorted exact test titles; for Playwright, use the locked reporter's relative classname and title. Verify the reviewed case counts, including intentionally repeated scenarios, before updating the manifest. Runtime acceptance compares actual private reports with these expected digests; it never learns or approves an inventory from an arbitrary successful execution.

## Hosted acceptance and measurement procedure

After independent review and separately authorized publication, push the reviewed candidate and verify the remote branch SHA. Dispatch exactly one normal Full run:

```sh
gh workflow run foundation.yml --repo ahmedali5696/care-pulse-3 --ref feature/ci-scalability-permanent -f postgres_diagnostic=full
```

Record the actual run ID, exact checkout SHA, event/ref and attempt. Require the common gate, all four lanes and exact `Full Foundation acceptance` check to succeed in the same attempt. Inspect actual mandatory reports/counts, zero skips, source/candidate schema, historical selectors, per-lane environment and cleanup logs; download the four current-attempt receipt artifacts with digest verification. Check their schema, kind, provenance and coverage against the reviewed inventory. Use an all-jobs rerun if another attempt is needed; do not combine artifacts or reuse Fast Feedback, diagnostics or profiling evidence.

Measure workflow elapsed (creation to final update), common and each lane's job intervals, gate interval, fan-out/handoff/queue gaps and summed unrounded runner minutes. Record runner OS/image, pinned inputs, actual coverage, any cold builds and failure/cancellation. Compare the candidate with development run 45492117349 only as a same-application-inventory historical reference; differing hosted runner images, time, and other conditions limit attribution. Report the actual Full result and cost separately from the Wave 3 experiment estimates. Further comparable complete runs can quantify variance; do not dispatch additional runs without authorization. No exact runner-image equality gate or new numerical performance gate is imposed by the permanent PO decision.
