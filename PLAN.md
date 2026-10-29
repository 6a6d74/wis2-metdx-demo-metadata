# Implementation plan: no-code WCMP2 publisher for `io-wis2dev-metdx-demo`

Status: **agreed 2026-10-29**. Nothing has been built yet. It builds on `pre/HANDOFF.md` and supersedes the parts of that brief that conflict with it.

## Decisions (2026-10-29)

- Repo: `6a6d74/wis2-metdx-demo-metadata` (public). Pages: `https://6a6d74.github.io/wis2-metdx-demo-metadata/`
- **Deletions are in scope:** a file removed in a merged PR publishes `rel=deletion`, with `href` set to the record's last published URL. The deletion helper skips Pages and the ETS/KPI wait. It succeeds when the message is delivered on both brokers and no error event arrives within the `monitor` window.
- **One Pages deployment per merge (D1).** The per-record helpers start from "wait until live".
- **KPI is informational:** logged, with an optional warning threshold. Only an ETS FAILED, a GB validation error, or a timeout fails a record.

## Environment endpoints

| Component | Endpoint |
|---|---|
| Local broker (EMQX) | kept as the `LOCAL_BROKER_URL` secret (not advertised; decided 2026-10-29) |
| Global Broker (test) | `mqtts://gb.wis2dev.io:8883` |
| **Labs Global Discovery Catalogue (where our records should end up)** | <https://wmo-resource-catalogue.labs.wis2dev.io/collections/wis2-discovery-metadata> |
| Published records and catalogue | <https://6a6d74.github.io/wis2-metdx-demo-metadata/> |

- The Labs GDC is an OGC API - Records endpoint:
  - search: `/items?q=<text>&f=json`
  - one record: `/items/<metadata-id>?f=json` (404 if absent)
- It is **not** `wmo-resource-catalogue.wis2dev.io` (where `gdc.wis2dev.io` redirects), which is a separate catalogue.
- Baseline check (2026-10-29): the Labs GDC held 604 records, none from `io-wis2dev-metdx-demo`.
- **Phase 5 check:** after each publication, confirm the record appears in the Labs GDC with the expected content, and that a deletion removes it.
- **GDC confirmation (agreed and implemented 2026-10-29, `wis2node.gdc`):** after the ETS/KPI stage, each helper polls `<url>/items/<metadata-id>?f=json`:
  - For new or updated records, it polls until the GDC serves the record with the same `properties.updated` (else `created`) time as ours, compared as instants. The GDC adds its own properties and links, so the bytes can't be compared.
  - For deletions, it polls until the item returns 404.
  - Config: `global_discovery_catalogue.url` (empty disables the check), `.poll_interval` (10 s) and `timeouts.gdc` (120 s).
  - It is skipped if publishing itself failed.
  - The result appears in the job summary ("In GDC" column) and on the catalogue page (badge linking to the GDC item).
  - Tested against the live Labs GDC: an existing record is found, a wrong version is reported, and an absent ID returns 404.

## Phase 0 findings (2026-10-29)

- `everyone`/`everyone` connects to both brokers (MQTT v5 and 3.1.1). QoS 1 is granted on `origin/a/wis2/io-wis2dev-metdx-demo/#`, `.../metadata` (local and GB) and `monitor/a/wis2/io-wis2dev-metdx-demo/#` (GB).
- TLS: use the `certifi` CA bundle explicitly (`tls_set(ca_certs=certifi.where())`). python.org macOS builds have no system bundle, and certifi behaves the same on runners.
- **Live GB event types differ from the draft spec examples.** Observed types: `int.wmo.wis.wme.wnm.validation.schema` and `int.wmo.wis.wme.wnm.validation.metadata`, with `source: io-wis2dev-global-broker` and `severity: ERROR`. The topic is at `data.channel` (not `data.content.channel`) and the WNM at `data.content.wnm` (`.id` = message ID). So the matcher will use prefixes (`…wnm.validation*`, `…wcmp2.ets`, `…wcmp2.kpi`) plus `data.content.report_type`.
- **Publisher check (`tools/check_publisher.py --publish`, 2026-10-29):**
  - `sgfit` connects and is allowed to publish on `origin/a/wis2/io-wis2dev-metdx-demo/metadata` (PUBACK Success).
  - The message was delivered by the local broker and by `gb.wis2dev.io`, and **the GB keeps the original message `id`**.
  - The deliberately invalid test payload was forwarded and produced **two distinct** `…wnm.validation.schema` ERROR events on `monitor/a/wis2/io-wis2dev-metdx-demo`. They have different event `id`s. One carries `data.content.errors: [...]` (the rel check), the other has no detail.
  - Both embed the full offending message at `data.content.wnm`, so match on `data.content.wnm.id`.
  - Helpers log every distinct event (de-duplicating only on the event `id`, in case QoS 1 redelivers), and surface `data.content.errors` in the job summary.
  - The full sample is in `logs/check_publisher-20261029T173511Z.jsonl` (local only, not committed).
- No GDC ETS/KPI events were seen in 150 s of `monitor/a/wis2/#`. Their real shape will be confirmed during the Phase 5 end-to-end test.
- WNM conventions (WNM 1.3):
  - `rel` is exactly one of `canonical` (new), `update` or `deletion`.
  - `properties.metadata_id` SHOULD be present and is REQUIRED for deletions. We always include it (= the record `id`).
  - `data_id` must be unique and meaningful. We use `<centre-id>/metadata/<record-id>`.
  - `integrity` is `{method: sha512, value: base64}`.
  - `geometry` must be a Point, a Polygon or `null`. A WCMP2 MultiPolygon or other geometry is reduced to its bounding-box Polygon.
- The `validate` check must run on **every** PR, with no `paths:` filter, exiting quickly when no metadata changed. Otherwise, as a required check, it would block PRs that don't touch `metadata/`.
- Config: `config/node.yml` is the single source of non-secret settings, so no repo variables are needed. Environment variables (`WIS2_*`) can still override any value.

## Phase 2 findings (pywcmp 0.14.5)

- **`run_tests()` omits the schema validation result** from `tests` and `summary`, so a schema-invalid record can show `FAILED: 0`. The validator runs `test_requirement_validation()` separately and treats a failure as an error.
- The ETS tests index record fields directly (`record['properties'][...]`), so a malformed record raises `KeyError` instead of reporting FAILED. The validator catches that as an error.
- **Centre-id:** the identifier and links tests FAIL any centre-id that isn't in the WIS2 topic-hierarchy list, unless it ends with `-test` or `relax_centre_id_checks=True` (which reports a WARNING). `io-wis2dev-metdx-demo` is not in the list.
  - PR validation runs relaxed (`validation.relax_centre_id_checks: true`), and our own rule enforces the `urn:wmo:md:io-wis2dev-metdx-demo:` prefix.
  - **Risk for Phase 5:** if the wis2dev GDC runs the ETS strictly, its ETS report will contain FAILED results for every record. That's to be confirmed on the first publish, and we may need to agree how to treat it.
- Local macOS (python.org build) needs `SSL_CERT_FILE=$(python -m certifi)` for `pywcmp bundle sync`. The runners don't.

## Phase 3 findings and design adjustments

- **WNM schema (1.3.0) requires `properties.datetime`** (or `start_datetime`+`end_datetime`), and `null` is allowed. Without it, every message would have triggered a GB `wnm.validation.schema` error. We set it to the record's `properties.updated`, falling back to `created`. The tests validate the built messages against the official schema (`tests/fixtures/wnm-1.3.0-bundled.json`).
- **The Pages deployment needs its own job** (`github-pages` environment) while publishing needs `wis2-publish`, so the run is split into three steps connected by a **manifest**:
  - `python -m wis2node.publish plan` writes `manifest.json`: what to announce and each record's timestamped URL.
  - `... preflight` checks the brokers. It runs before anything is staged.
  - `... run --manifest` handles the subscriptions, helpers, delivery check, monitoring and report.
  - Exit codes: 0 OK, 1 some records failed, 2 config/preflight error.
- **Renames:** a file renamed with the same `id` is announced once as `update`, and its deletion is dropped.
- **Deletions:** `href` is the record's `current_url` from `catalogue.json` (Phase 4), falling back to the source path.
- **Monitoring clock:** the `monitor` timeout starts once delivery is confirmed, or when the delivery wait times out. Events stay queued from the moment of publishing, so none are missed.
- **New config:**
  - `timeouts.clock_skew` (30 s): ETS/KPI events timestamped earlier than publish time minus this are ignored as stale.
  - `monitor.ets_failures_as_warnings`: GDC ETS test ids to downgrade, e.g. `identifier` if the GDC is strict about our test centre-id.
  - `publish.max_workers`.
- Checked against the live brokers: `preflight`, `open_clients` and `subscribe_all` succeed, and a bad password is reported with the client name.

## Phase 4 design notes

- **`published_commit` in `catalogue.json`** (on `gh-pages`) is the last commit whose records were announced. Each run publishes `published_commit..HEAD`, not just the push's own range. GitHub cancels all but one *queued* run in a concurrency group, so a cancelled run's records are caught up by the next run. If there is no `published_commit`, the run falls back to the push's `before` commit, and failing that announces every record.
- **Workflow (`publish.yml`):**
  1. `prepare` (wis2-publish): plan, preflight, stage onto `gh-pages`, upload the Pages artifact and the manifest.
  2. `deploy` (github-pages).
  3. `publish` (wis2-publish): `run`, then record the results and logs on `gh-pages`.
  4. `report` (github-pages): redeploys so the catalogue shows the ETS/KPI outcome.
- **Exit code 2 from `run`** (a broker or config failure before publishing) does **not** advance `published_commit` and skips `report`, so the next run retries. The staged files stay, which is harmless.
- **Manual run:** `workflow_dispatch` with `files` set to paths or `all` re-announces those records as they are at `HEAD`. It uses `update` if the catalogue already holds the record, otherwise `canonical`. Leaving `files` empty means "publish anything not yet published".
- **Site:**
  - Published files: `metadata/<stem>/<stem>.<stamp>.json`, plus `.sha512` sidecars (`sha512sum -c` compatible).
  - `catalogue.json` holds the current file, checksums, version history and last publication outcome.
  - `index.html` is static HTML (works without JS; JS is only used for "copy checksum"), with light and dark themes.
  - `logs/<run-id>/*.jsonl`.

## Phase 5 findings: first live publication (run 40938152129, 2026-10-29)

Record `urn:wmo:md:io-wis2dev-metdx-demo:test-surface-observations` (PR #5):

| Step | Result |
|---|---|
| Pages | staged as `test-surface-observations.20261029T191359Z.json`, live, `.sha512` matches the served bytes |
| Delivery | local broker 0.15 s, GB 0.17 s |
| GB validation events | **none**: the WNM passes the GB's checks |
| GDC ETS | received about 1 s after publishing. `source: ca-eccc-msc-global-discovery-catalogue`, severity WARNING, 10 passed / 0 failed / 2 warnings (identifier, links: unknown centre-id). **The Labs GDC runs relaxed.** |
| GDC KPI | **never sent** (checked for 30 min with `tools/listen.py`) |
| Labs GDC | record present 0.9 s after the ETS stage |

The run was marked failed only because of the missing KPI. What the GDC source (`wmo-im/wmo-resource-catalogue`, `wis2_gdc/registrar.py`) shows:
- **KPI reports are only sent when `WMO_RESOURCE_CATALOGUE_RUN_KPI=true`**, which is off by default. Because of this, `monitor.require_kpi` (default `false`) was added: helpers succeed on ETS (+ GDC check) and record a KPI report when one arrives. **Suggestion:** ask the Labs GDC operators to enable RUN_KPI, then set `require_kpi: true`.
- `REJECT_ON_FAILING_ETS` defaults to true: a record with an ETS FAILED is **not** added to the catalogue. This is consistent with treating ETS FAILED as an error.
- ETS event severity is INFO, WARNING or ERROR depending on the worst test code.
- **The GDC reports download failures and deletions as type `….item.download`**:
  - A download failure is ERROR "WCMP2 access failure". It has no `metadata_id`, only a link to the URL fetched, so the router now matches it by the record URL.
  - A deletion gives INFO "metadata … deleted" or ERROR "… not found", with `metadata_id`. A deletion helper now finishes when this report arrives. A missing report is a warning, an ERROR fails the record, and the GDC 404 check still confirms the deletion.
- **`"length": 7` in the ETS event's link is explained ([#7](https://github.com/6a6d74/wis2-metdx-demo-metadata/issues/7)).** The GDC sets `length = len(self.metadata)`, and `self.metadata` is the parsed record (a dict), so the value is the count of top-level keys (7 for us), not bytes. No change is needed here. A possible upstream fix is `len(self.source)`.
- **The test environment has a cache hop for metadata.** Something copies records to an S3 bucket (`tests3-tests352b11fd6-l4o6wt8gtoam.s3.amazonaws.com/data/<centre>/metadata/<timestamp>/…`) and re-announces them on `cache/a/wis2/<centre>/metadata`. The GDC processes that re-notification, which is why its event links to S3, not to Pages. Which component does this, and whether it is a full Global Cache, is still to be confirmed with the test environment operators.

## Progress

| Phase | Status |
|---|---|
| 0: checks | done 2026-10-29 |
| 1: repo, Pages, environment, ruleset | done 2026-10-29 |
| 2: config, validator, `validate.yml`, tests | done (PR #1) |
| 3: MQTT, router, helpers, monitoring, orchestrator | done (PR #3) |
| 4: Pages staging, catalogue, `publish.yml`, Labs GDC check | done (PR #4) |
| 5: end-to-end test | **done 2026-10-29**: new record (run 40938152129; failed only on the then-required KPI), update (run 40947880316) and deletion (run 40950578470) both fully green. Deletion chain: WNM → cache re-notification (+0.2 s) → GDC `item.download` INFO "deleted" (+0.03 s) → Labs GDC 404 (+0.5 s). GDC behaviour handled in PR #6. |
| 6 | to do |

## Follow-up tasks (recorded 2026-10-29, not scheduled)

To raise with the **Labs GDC operators**, for example via `tomkralidis`:

1. **Enable KPI reports.** The Labs GDC runs with `WMO_RESOURCE_CATALOGUE_RUN_KPI` off (the default), so it never sends WCMP2 KPI reports. If it is enabled, set `monitor.require_kpi: true` in `config/node.yml` to restore the original success rule (ETS **and** KPI received).
2. **MQTT links rewritten to production Global Brokers.** The Labs GDC's copy of our record has its MQTT links pointing at the production GBs (`gb.wis.cma.cn`, `wis2globalbroker.nws.noaa.gov`, `globalbroker.inmet.gov.br`, `globalbroker.meteo.fr`) instead of the test GB `gb.wis2dev.io`. Ask whether this is intended for the Labs environment.

Kept open meanwhile: [#7](https://github.com/6a6d74/wis2-metdx-demo-metadata/issues/7). `"length": 7` is explained (the GDC counts the parsed record's keys); a possible upstream report is pending a decision.

## 1. What we're building

This is a minimal WIS2 Node that runs entirely on GitHub, with no server:

| WIS2 Node function | Implementation |
|---|---|
| Metadata authoring and change control | GitHub repo + pull requests (PRs) |
| Validation | GitHub Actions check on each PR (required for merge) |
| HTTP endpoint for records | GitHub Pages (immutable, timestamped files) |
| Local broker | EMQX Serverless (address in the `LOCAL_BROKER_URL` secret) |
| Publishing notifications | GitHub Actions job on merge to `main` |
| Proof of delivery and quality | Subscriptions to local broker + `gb.wis2dev.io`, capturing GB and GDC monitoring events |
| Browsable catalogue | `index.html` + `catalogue.json` on Pages, with SHA-512 checksums |

Already verified (2026-10-29): both brokers present TLS certificates that validate against the stock CA bundle, so no custom CA is needed.

## 2. Repository layout (new public repo under `6a6d74`)

```
.github/
  workflows/
    validate.yml          # on: pull_request -> required status check
    publish.yml           # on: push to main (metadata/**) + workflow_dispatch
  pull_request_template.md
config/
  node.yml                # non-secret config: centre-id, broker URLs, sub creds, timeouts
metadata/                 # authoritative WCMP2 records, one per file: <name>.json
src/wis2node/
  config.py               # load node.yml, apply env overrides, check required keys
  validate.py             # ETS (pywcmp) + local rules + KPI summary
  changes.py              # git diff -> added/modified records
  pages.py                # stage timestamped files, rebuild index/catalogue, wait-until-live
  wnm.py                  # build WIS2 Notification Messages
  mqtt.py                 # connect/liveness, subscriber with message router
  helper.py               # per-record helper (one thread per record)
  monitor.py              # monitoring-event parsing/matching (WCMP2 ETS/KPI, WNM validation)
  publish.py              # orchestrator / CLI entrypoint
site/                     # templates for index.html
tests/                    # unit tests (message build, routing, event matching, change detection)
ruleset/protect-main.json
README.md                 # collaborator guide ("how to publish a record")
```

The **`gh-pages` branch** is the published-state store. It holds every timestamped file ever published, `catalogue.json`, `index.html` and the run logs. The workflow writes to it, and `main` stays protected.

## 3. Configuration (nothing hard-coded)

`config/node.yml` holds the defaults, which are reviewed by PR like everything else. Each key can be overridden by an environment variable, i.e. a repo variable in Actions.

```yaml
centre_id: io-wis2dev-metdx-demo
local_broker:
  url: ""   # from the LOCAL_BROKER_URL secret
  subscribe: {username: everyone, password: everyone}
  publish:   {username: sgfit}          # password from secret LOCAL_BROKER_PUB_PASSWORD
global_broker:
  url: mqtts://gb.wis2dev.io:8883
  subscribe: {username: everyone, password: everyone}
pages:
  base_url: ""                           # empty = use the Pages URL reported by the deploy step
timeouts:                                # seconds
  broker_connect: 15
  pages_live: 300
  delivery: 60                           # local + GB must both deliver our message id
  monitor: 60                            # ETS + KPI must both arrive
kpi:
  warn_below_percentage: null            # optional: warn (not fail) if KPI % is below this
```

The only secret is `LOCAL_BROKER_PUB_PASSWORD`. It lives in a GitHub **environment** called `wis2-publish`, deployable only from `main`. Collaborators never see it, and PRs from forks never get it.

## 4. Workflow A: PR validation (`validate.yml`, required check)

This runs on every PR that touches `metadata/**` or `config/**`. It validates only the **changed** records, but it also checks for duplicate IDs across the whole folder.

Checks that fail the job:
1. The file is valid JSON and its name ends in `.json`, under `metadata/`.
2. **WCMP2 ETS** via the pywcmp Python API (not the CLI, so failures are detected reliably). Any `FAILED` test fails the job.
3. **Identifier rule:** `id` starts with `urn:wmo:md:io-wis2dev-metdx-demo:`. The GDC rejects records whose ID centre doesn't match the publishing centre.
4. **Unique ID:** no other file in `metadata/` uses the same `id`.
5. **Modified records:** `properties.updated` must be later than the version on `main`, so the GDC can tell the record has changed.
6. No ID change on a modified file. Changing an ID really means a new record plus a deletion, so it is flagged.

Informational output (doesn't fail the job):
- A KPI score, grade and the comments for each record, written to the job summary and posted as a PR comment. Reviewers see quality before they approve.

The ruleset on `main` requires this check (by the job name `validate`), 1 approval, and dismisses stale approvals. The owner keeps a bypass for PRs only.

## 5. Workflow B: publish on merge (`publish.yml`)

`concurrency: wis2-publish` (no cancellation) serialises runs. The publish step runs in environment `wis2-publish`.

### Stage 0: preflight (fails fast before anything becomes public)
1. Load the config. Determine the changed records with `git diff --name-status <before> <after> -- metadata/`, giving **A**dded, **M**odified and **D**eleted files. Renames are treated as delete + add. If there are none, exit successfully.
2. **Check the local broker is live:** connect with the **publisher** credentials and wait for a successful CONNACK within `broker_connect`. Also connect with the subscriber credentials. Either failure stops the job with a clear error.
3. **Check the Global Broker is live:** connect with the subscriber credentials and wait for CONNACK. Failure stops the job.

### Stage 1: shared subscriptions (main thread, before any publishing)
4. Local broker: subscribe to `origin/a/wis2/<centre-id>/metadata` (QoS 1).
5. GB: subscribe to `origin/a/wis2/<centre-id>/metadata` and `monitor/a/wis2/<centre-id>/#`.
6. A **router** in the paho network threads hands each incoming message to the matching helper's queue:
   - WNM, matched on `id` (the message ID the helper registered)
   - WNM validation event, matched on `data.content.wnm.id`, or failing that `data.ref`
   - WCMP2 ETS/KPI events, matched on `data.content.metadata_id`, but **only** events whose `time` is after that helper's publish time, so events from an earlier publication of the same ID are ignored
   - All `monitor/...` traffic also goes to the run log, whether or not it matches

   We wait for SUBACK on every subscription before moving on.

### Stage 2: stage and deploy to Pages (once per run, see design change D1)
7. For each record, copy it to `gh-pages:metadata/<stem>/<stem>.<YYYYMMDDTHHMMSSZ>.json` and compute its SHA-512 and length.
8. Update `catalogue.json` and `index.html`. Commit to `gh-pages` and deploy with `actions/deploy-pages` in **one** deployment.
9. **Wait until live (no fixed sleep):** `deploy-pages` blocks until GitHub reports the deployment complete. The helpers then poll their own URL until it returns 200 and the bytes match the SHA-512. Because the filename is new, a stale cached copy isn't possible. We avoid requesting a URL before the deploy completes, so the CDN can't cache a 404.

### Stage 3: one helper thread per record (`ThreadPoolExecutor`)
Each helper:
1. Polls its timestamped URL until live with a matching hash (`pages_live` timeout).
2. Builds the WNM:
   - `id`: a fresh UUID
   - `conformsTo`
   - `geometry`: copied from the record
   - `properties`: `data_id`, `pubtime`, `integrity` (sha512, base64)
   - `links`: `href` to the timestamped URL, `rel` set to `canonical` for new records or `update` for modified ones, `type: application/geo+json`, and `length`
3. Registers its message ID and metadata ID with the router.
4. Publishes to the local broker on `origin/a/wis2/<centre-id>/metadata` (QoS 1, not retained, publisher connection) and waits for PUBACK.
5. **Confirms delivery:** waits up to `delivery` seconds until the router has seen our message ID on **both** the local and GB subscriptions. Missing either one is an error that names which broker failed.
6. **Collects monitoring events** for up to `monitor` seconds:
   - GB WNM validation events: logged. In relaxed mode the GB only emits these on error, so any one received is an **error** for this record.
   - GDC WCMP2 ETS report: any `FAILED` result (`summary.FAILED > 0` or any test with `code: FAILED`) is an **error**.
   - GDC WCMP2 KPI report: logged, with an optional warning threshold.
   - The helper exits **successfully** as soon as both ETS (with no failures) and KPI have arrived. If they haven't both arrived by the timeout, it is an error.
7. Writes every matched event to `logs/<run-id>/<stem>.jsonl` and returns a structured result.

### Stage 4: report (always runs, even on failure)
- Writes the per-record outcome (publish OK, delivered local/GB, ETS result, KPI grade, errors) to the GitHub job summary.
- Commits the logs and the latest status for each record into `catalogue.json` on `gh-pages`, then redeploys. The redeploy is cheap, and it means the catalogue shows the GDC verdict.
- Uploads the logs as a workflow artifact too.
- The job exits non-zero if any helper failed.

### Error trapping
Every stage has explicit exceptions and messages for: config missing, git diff failure, broker unreachable or auth refused, SUBACK refused, Pages deploy failure, URL not live or hash mismatch, PUBACK timeout, delivery timeout (local or GB), GB validation error, ETS FAILED, ETS/KPI timeout, and an unexpected exception in a helper. The executor catches these per helper, so one failing record never kills the others. MQTT clients are always disconnected in `finally` blocks, which keeps EMQX session minutes low.

### Manual re-run
`workflow_dispatch` takes an optional input listing the files to republish, for example after a GDC outage. If the input is empty, every changed file in the last merge is republished. It never republishes the whole folder unless asked explicitly.

## 6. Catalogue on GitHub Pages

- `index.html` is a static page that loads `catalogue.json`. It shows a table with: title, metadata ID, current file link, SHA-512 (with a copy button), size, published time, last ETS result and KPI grade, plus a link to the run log. Each record expands to show its earlier versions.
- `catalogue.json` is the machine-readable equivalent.
- Each published file also gets a `<file>.sha512` sidecar for scripted verification.

## 7. Delivery phases

| Phase | Work | Who |
|---|---|---|
| 0 | Confirm EMQX ACLs: `sgfit` can publish to `origin/a/wis2/io-wis2dev-metdx-demo/metadata`; `everyone` can subscribe to `origin/a/wis2/io-wis2dev-metdx-demo/#`. Add secret `LOCAL_BROKER_PUB_PASSWORD`. | You |
| 0 | Check that `everyone`/`everyone` subscription works on both brokers (a short read-only connect test). Check the WNM conventions for metadata: `data_id` form, `rel` values, topic. | Claude |
| 1 | Create the repo, `gh-pages` branch, Pages (source: Actions), environment `wis2-publish` (main only), repo variables, ruleset | Claude |
| 2 | Config loader, validator, `validate.yml`, unit tests, PR template | Claude |
| 3 | MQTT layer, router, helper, monitor matching, orchestrator with `--dry-run` and `--no-pages` modes, unit tests | Claude |
| 4 | Pages staging, catalogue/index, `publish.yml` | Claude |
| 5 | End-to-end test with one test record via a real PR, which you approve. Watch the GB/GDC events. Tune the timeouts. | Together |
| 6 | README collaborator guide, add collaborators | Claude + you |

## 8. Design changes and suggestions

**D1. Deploy to Pages once per run, not once per helper (recommended).** Every Pages deployment replaces the whole site, and runs are queued. If N helpers each push to Pages, they race and each one waits through a full deploy cycle. Instead, stage all timestamped files together, deploy once, and then let the helpers run in parallel from the "wait until live" step onwards. This keeps per-record isolation for everything that can fail independently: liveness, publish, delivery, ETS/KPI.

**D2. Fail fast before anything becomes public.** Check broker liveness, including the publisher credentials, *before* deploying to Pages. A broken broker then leaves no orphaned files.

**D3. Subscribe before publishing, in one place.** Shared subscriptions plus a router, instead of each helper having its own subscriber connections, means fewer EMQX sessions (the free tier counts session minutes). It also means no helper can miss its own message because it subscribed too late.

**D4. Match monitoring events strictly.** ETS and KPI reports carry `metadata_id`, not the WNM ID. Filter by the event `time` being after the publish time, otherwise an old report for the same record could be counted as a pass.

**D5. Stronger PR checks.** Besides the ETS, check the centre ID in `id`, unique IDs, and that `updated` has been bumped. Show the KPI on the PR so reviewers can judge quality. These checks catch most of what the GDC would otherwise reject after merge.

**D6. Immutable, timestamped files and a gh-pages history.** Old URLs keep resolving, which matters because earlier notifications point at them. The catalogue also becomes an audit trail.

**D7. Deletions (not in your workflow; suggest adding).** Removing a file in a PR could publish `rel=deletion`, so the GDC drops the record. Without this, the only way to retire a record is manually. Suggest adding it, behind the same review gate.

**D8. Truly no-code authoring.** Collaborators can use GitHub's web UI ("Add file → Upload files" creates a branch and PR). It could be connected to your existing `6a6d74/wcmp2-editor` later, for example an "open PR" button that pre-fills the file.

**D9. Optional daily health check** (cron) that runs Stage 0 only, so a dead broker is found before someone tries to publish.

**D10. Things to check against the specs before coding** (Phase 0):
- the WNM `data_id` convention for metadata
- `rel` values (`canonical`/`update`/`deletion`)
- whether the GB echoes the original message `id` unchanged (assumed yes; this is how the delivery check works)
- the exact monitoring event `type` codes: the draft examples use `int.wmo.wis.wme.event.wcmp2.ets`, `…wcmp2.kpi` and `…wnm.validation`, so the matcher will key on `data.content.report_type` as well as `type`, to tolerate codelist changes
