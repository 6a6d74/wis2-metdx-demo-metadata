# WIS2 metadata publisher: `io-wis2dev-metdx-demo`

A no-code WIS2 Node for the SG-FIT **metdx-demo** activity. It publishes WCMP2 discovery
metadata into the WIS2 test environment (`gb.wis2dev.io`).

To publish a record, add or change a JSON file in [`metadata/`](metadata/) through a pull
request. Once a collaborator approves the PR and validation passes, merging it publishes the
record:

1. The record is placed on GitHub Pages under a timestamped filename.
2. A WIS2 Notification Message is published to the local broker.
3. The workflow confirms that the local broker and the Global Broker delivered the message.
4. The workflow collects the Global Discovery Catalogue's ETS and KPI reports.

Deleting a file in a merged PR publishes a deletion notice.

The browsable catalogue is at <https://6a6d74.github.io/wis2-metdx-demo-metadata/>.

The design is described in [PLAN.md](PLAN.md), and the configuration is in [config/node.yml](config/node.yml).

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
