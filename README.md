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

> Work in progress: validation is in place, and the publishing workflow is to do.
