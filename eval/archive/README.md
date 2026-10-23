# Raw outputs of the run-once eval sets

`eval/results/` is gitignored, so the raw JSON behind the held-out and
pre-registered headlines existed only on one laptop. These are byte-for-byte
copies (archived 2026-10-23), so every headline can be re-scored with
`eval/run_eval.py --rescore eval/archive/<file>.json` and checked against its
`RESULTS_*.md`. Never edit them; a later run gets its own file.

**Code version:** these runs predate `eval/code_version.py`, so the JSON does
not name the code it measured. The column "recorded in" is the commit that
first committed the summary; the code measured is the tree just before it,
which may have held uncommitted changes. Runs from 2026-10-23 on carry
`code_version` in the JSON and the summary header.

| file | set | role | headline | summary | recorded in | sha256 (first 12) |
|---|---|---|---|---|---|---|
| `holdout_20261014_164734.json` | holdout (TheLook, DuckDB) | first run, **held-out result** | 29/30 | `RESULTS_holdout.md` | `47d6b90` | `2a5d4a980ea6` |
| `bigquery_20261016_050942.json` | bigquery (pre-registered) | first run, **held-out result** | 24/30 | `RESULTS_bigquery.md` | `543cf83` | `2c3653b7cd5c` |
| `bigquery_20261016_053722.json` | bigquery | RERUN after fixes, a tuning number | 28/30 | `RESULTS_bigquery_rerun.md` | `9dc6344` | `9f882cd65c63` |
| `bq-test_20261017_221328.json` | bq-test (pre-registered `ddf0030`) | first run, **held-out result** | 35/40 | `RESULTS_bq_test.md` | `c61ee61` | `6c1cf4e847e9` |
| `bq-pool-test_20261018_043314.json` | bq-pool-test | first run, **held-out result** | 28/31 | `RESULTS_bq_pool_test.md` | `a7816d8` | `c18049d248ab` |
| `bq-owner-test_20261018_065606.json` | bq-owner-test | first run, **held-out result** | 12/12 | `RESULTS_bq_owner_test.md` | `d918493` | `145e72b4fe68` |
| `bq-owner-test_20261018_065915.json` | bq-owner-test, `--llm claude` | RERUN to read LLM suggestions, not scored as held out | 12/12 | `RESULTS_bq_owner_test_llm_haiku.md` | `3d3661a` | `1ff36c3d376d` |

Full hashes: `shasum -a 256 eval/archive/*.json`. Headlines are exact matches
with the labels as they were at the run (`exact_accuracy_original_labels`);
label changes made later are scored both ways in each summary.
