# WIS2 metadata publisher: `io-wis2dev-metdx-demo`

A no-code WIS2 Node for the SG-FIT **metdx-demo** activity. It publishes WCMP2 discovery
metadata into the WIS2 test environment (`gb.wis2dev.io`).

> [!TIP]
> **Collaborators: please add the keyword `metdx-demo` to your records.** It isn't mandatory,
> but it makes the demo's records easy to find in the Global Discovery Catalogue. Add it to
> `properties.keywords`:
>
> ```json
> {
>   "id": "urn:wmo:md:io-wis2dev-metdx-demo:my-dataset",
>   "properties": {
>     "title": "My dataset",
>     "keywords": ["metdx-demo", "surface", "observations"]
>   }
> }
> ```
>
> Find them with
> [`…/items?q=metdx-demo`](https://wmo-resource-catalogue.labs.wis2dev.io/collections/wis2-discovery-metadata/items?q=metdx-demo)
> in the Labs GDC.

To publish a record, add or change a JSON file in [`metadata/`](metadata/) through a pull
request. Once a collaborator approves the PR and validation passes, merging it publishes the
record:

1. The record is placed on GitHub Pages under a timestamped filename.
2. A WIS2 Notification Message is published to the local broker.
3. The workflow confirms that the local broker and the Global Broker delivered the message.
4. The workflow collects the Global Discovery Catalogue's validation report (ETS), plus its
   quality report (KPI) when the GDC sends one.
5. The workflow confirms that the record is now in the Labs Global Discovery Catalogue.

Deleting a file in a merged PR publishes a deletion notice.

The browsable catalogue is at <https://6a6d74.github.io/wis2-metdx-demo-metadata/>.

See [How it works](#how-it-works) and [Configuration](#configuration) below. The design history and findings are in [PLAN.md](PLAN.md).

## How it works

This repository is a minimal **WIS2 Node** that runs entirely on GitHub, with no server:

| WIS2 Node function | Provided by |
|---|---|
| Metadata authoring and change control | this repo: pull requests to `metadata/` |
| Validation | the **Validate** workflow, a required check on every PR |
| HTTP endpoint serving the records | **GitHub Pages**, from the `gh-pages` branch |
| Local broker (where notifications are published) | **EMQX** Serverless (address held in the `LOCAL_BROKER_URL` secret) |
| Publishing and verification | the **Publish** workflow, on merge to `main` |

Downstream WIS2 components that we only observe:

| Component | What we check |
|---|---|
| **Global Broker** (`gb.wis2dev.io`) | It delivers our message, and it raises validation events if the message is invalid. |
| **Global Discovery Catalogue** (Labs) | It sends a WCMP2 ETS report, and a KPI report if enabled. It then holds the record, which we confirm through its API. |

```text
1. Change control (GitHub)

   +-----------------+     +------------------+     +--------------------+
   | Pull request    |     | Validate check   |     | Publish workflow   |
   | metadata/*.json |---->| + 1 approval     |---->| (on merge to main) |
   +-----------------+     | -> merge to main |     +---------+----------+
                           +------------------+               |
                                                              | stages records
                                                              v
                                       +-------------------------------------+
                                       | gh-pages branch  ->  GitHub Pages   |
                                       |  <name>.<timestamp>.json + .sha512  |
                                       |  catalogue.json, index.html         |
                                       +-------------------------------------+

2. Publication, for each record

   Publish workflow                 WIS2 test environment
   (helper thread)
                                    +--------------+      +------------------+
   (a) publish WNM ---------------->| Local broker |----->| Global Broker    |
       (publisher login)            | (EMQX)       |      | (gb.wis2dev.io)  |
                                    +------+-------+      +---+----------+---+
                                           |                  |          |
   (b) confirm delivery <------------------+------------------+          |
       (our message id, via both brokers)                                v
                                                          +------------------+
                                                          | Global Discovery |
   (c) collect monitoring events <--- ETS / KPI reports --| Catalogue (Labs) |
       (relayed by the Global Broker)                     +--------+---------+
                                                                   |
   (d) confirm the record is in   <--- GDC API: items/<id> --------+
       the GDC (or gone, if deleted)                               |
                                                     fetches the   |
                                                     record from   v
                                                             GitHub Pages
```

### On a pull request: Validate

The **Validate** workflow runs on every PR. It checks only the records that the PR adds or changes, and fails if any of the following is true:

- A file is not valid JSON, or has no `id`.
- A record fails the WCMP2 schema, or any WCMP2 ETS test returns FAILED (pywcmp, the same version the GDC runs).
- An `id` doesn't start with `urn:wmo:md:<centre_id>:`.
- Two records share an `id`.
- A changed record has a different `id`, or its `properties.updated` was not moved later.
- A file under `metadata/` isn't `metadata/<name>.json`.

The KPI score is reported for information only. The report is posted as a comment on the PR. Branch rules on `main` require this check to pass and one approving review from a collaborator before merging.

### On merge: Publish

The **Publish** workflow runs when a merge changes `metadata/`. It has four jobs:

| Job | GitHub environment | What it does |
|---|---|---|
| **prepare** | `wis2-publish` | **Plan:** list every record added, changed or deleted since the last published commit, recorded as `published_commit` in `catalogue.json`. This means a run that GitHub cancels while queued is caught up by the next run. **Preflight:** connect to both brokers, with the publisher login too, and stop before anything becomes public if either one refuses. **Stage:** copy each record to `gh-pages` as `metadata/<name>/<name>.<UTC timestamp>.json`, with a `.sha512` file. |
| **deploy** | `github-pages` | Deploy the site. |
| **publish** | `wis2-publish` | Subscribe to the local broker and the GB first. Then run one helper thread per record (see below). Finally, write each outcome and the logs to `gh-pages`. |
| **report** | `github-pages` | Redeploy, so the catalogue page shows the outcome of each record. |

Each helper works through these steps, and a failure in one record never stops the others:

1. **Wait until live:** poll the record's new URL until Pages serves exactly the committed bytes, by SHA-512. Because every publication uses a new filename, a stale cached copy is impossible.
2. **Publish** the WIS2 Notification Message to `origin/a/wis2/<centre_id>/metadata`, with QoS 1, and wait for the broker to acknowledge it. The message `rel` is `canonical` for a new record, `update` for a change and `deletion` for a removal.
3. **Confirm delivery:** our message, identified by its message ID, must come back via the **local broker and the Global Broker** within `timeouts.delivery`.
4. **Collect monitoring events** on `monitor/a/wis2/<centre_id>`, within `timeouts.monitor`:
   - GB validation events about our message (ERROR fails the record)
   - the GDC's **ETS** report (required; any FAILED test fails the record)
   - the GDC's **KPI** report (recorded if it arrives; required only if `monitor.require_kpi` is set)
   - the GDC's download-failure and deletion reports
5. **Confirm in the GDC:** poll the Labs GDC's API until it holds this version of the record, matched on `updated`/`created`. For a deletion, poll until the record is gone.

Every event received is written to a log. The run fails if any record fails, and the job summary lists the reason for each failure.

### Where things live

| Location | Contents |
|---|---|
| `metadata/` (on `main`) | The authoritative WCMP2 records, one per file. |
| `gh-pages` branch → <https://6a6d74.github.io/wis2-metdx-demo-metadata/> | Every published version (old URLs keep working) with `.sha512` checksums, `catalogue.json` (machine-readable: current file, checksums, version history, last outcome) and `index.html` (the browsable catalogue). |
| `logs/<run-id>/` on the site | Per-record JSON Lines logs: the message sent, deliveries, every monitoring event, and the result. |
| Actions → run → summary | The plan, plus a table of outcomes for each record. |
| `src/wis2node/` | The code: `validate`, `publish` (plan / preflight / run), `helper` (per-record threads), `mqtt`, `monitor`, `gdc`, `site`, `wnm`, `config`. |

## Configuration

Settings live in [`config/node.yml`](config/node.yml). Change them through a PR, like any other change, so they are reviewed and recorded.

| Setting | Default | Meaning |
|---|---|---|
| `centre_id` | `io-wis2dev-metdx-demo` | WIS2 centre-id. Used in topics, `data_id`, and the required record-`id` prefix. |
| `local_broker.url` | `""` | **Not set here:** the local broker address is kept as the `LOCAL_BROKER_URL` secret, so it isn't advertised. It is used only if the secret is absent. |
| `local_broker.subscribe.username` / `.password` | `everyone` / `everyone` | Used to confirm delivery. These are the standard public WIS2 credentials. |
| `local_broker.publish.username` | `sgfit` | Publisher login. The password is **not** in this file (see below). |
| `global_broker.url` | `mqtts://gb.wis2dev.io:8883` | Global Broker to confirm delivery on and to receive monitoring events from. |
| `global_broker.subscribe.username` / `.password` | `everyone` / `everyone` | Public subscriber credentials. |
| `global_discovery_catalogue.url` | Labs GDC `…/collections/wis2-discovery-metadata` | OGC API - Records collection to confirm records in. Empty turns the check off. |
| `global_discovery_catalogue.poll_interval` | `10` | Seconds between GDC checks. |
| `pages.base_url` | `""` | Public base URL of the records. Empty means use the URL GitHub reports for Pages. Set it if you use a custom domain. |
| `timeouts.broker_connect` | `15` | Seconds to wait for each broker to accept a connection or subscription. |
| `timeouts.pages_live` | `300` | Seconds to wait for a record to be served correctly by Pages. |
| `timeouts.delivery` | `60` | Seconds for both brokers to deliver our message. |
| `timeouts.monitor` | `60` | Seconds to collect GDC reports, counted from delivery. |
| `timeouts.gdc` | `120` | Seconds for the GDC to show the record (or remove it). |
| `timeouts.clock_skew` | `30` | GDC reports dated more than this many seconds before our publish are ignored as stale. |
| `kpi.warn_below_percentage` | `null` | Warn (never fail) when a KPI score is below this value. |
| `validation.relax_centre_id_checks` | `true` | pywcmp fails centre-ids that aren't in the official WIS2 list, and ours is a test centre. `true` reports those tests as warnings. Our own prefix rule still applies. |
| `monitor.ets_failures_as_warnings` | `[]` | GDC ETS tests (short names, e.g. `identifier`) whose FAILED result counts as a warning instead of an error. |
| `monitor.require_kpi` | `false` | Require a GDC KPI report for success. The GDC only sends KPI reports when its `WMO_RESOURCE_CATALOGUE_RUN_KPI` setting is on, and that is off on the Labs GDC. |
| `publish.max_workers` | `4` | Number of records published in parallel. |

**Secrets** are kept in the `wis2-publish` GitHub environment. Only workflows running from `main` can use them, so collaborators never see them and PRs from forks never get them.

| Secret | Contents |
|---|---|
| `LOCAL_BROKER_URL` | Local broker address, e.g. `mqtts://<host>:8883`. Kept secret so it isn't advertised. GitHub masks it in workflow logs, and the code refers to it only as "local broker". |
| `LOCAL_BROKER_PUB_PASSWORD` | Publisher password for `local_broker.publish.username`. |

To set or change one (you are prompted for the value):

```
gh secret set LOCAL_BROKER_URL --env wis2-publish -R 6a6d74/wis2-metdx-demo-metadata
gh secret set LOCAL_BROKER_PUB_PASSWORD --env wis2-publish -R 6a6d74/wis2-metdx-demo-metadata
```

For local runs of the tools, export `LOCAL_BROKER_URL` (and `LOCAL_BROKER_PUB_PASSWORD` where needed) in your shell.

**Environment overrides (local runs):** any setting can be overridden with an environment variable named `WIS2_` followed by its path in upper case, joined with underscores. For example, `WIS2_TIMEOUTS_DELIVERY=90` or `WIS2_GLOBAL_BROKER_URL=mqtts://other:8883`. Lists are comma-separated, and `null` clears a value. The workflows don't set any overrides, so in Actions `config/node.yml` is what applies.

**Repository settings** (outside this file):
- Branch ruleset `protect-main`: 1 approval, the `validate` check, stale approvals dismissed, admin bypass for PRs only. See `ruleset/protect-main.json`.
- Environments: `wis2-publish` and `github-pages`, both limited to `main`.
- Pages source: GitHub Actions.

## Checking a record locally (optional)

```
pip install -e .
pywcmp bundle sync
python -m wis2node.validate metadata/my-record.json
```

## Tools

- `tools/check_publisher.py`: checks the publisher login and, with `--publish`, the end-to-end path
  through the local broker and the GB, logging any monitor events.
- `tools/listen.py`: logs everything on this centre's origin, cache and monitor topics (GB or local broker)
  for N minutes. It's useful for watching a publication independently of the workflow.

## Re-announcing records

Use **Actions → Publish → Run workflow**:

- Leave **files** empty to publish anything merged but not yet published.
- Enter space-separated paths (e.g. `metadata/a.json metadata/b.json`), or `all`, to re-announce
  records as they are now. This is useful after a GDC outage.

Each run's outcome (delivery, GB events, GDC ETS/KPI) is shown in the job summary and on the
catalogue page, with full logs under `logs/<run-id>/` on the site.
