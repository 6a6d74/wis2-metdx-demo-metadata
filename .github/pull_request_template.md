<!-- To publish metadata: add or change files in metadata/ (one WCMP2 record per <name>.json). -->

## What this PR does

- [ ] Adds new record(s)
- [ ] Updates existing record(s): `properties.updated` has been advanced
- [ ] Removes record(s): a deletion notice will be published
- [ ] Changes the publisher itself (code, workflows, config)

## Checklist

- [ ] Record `id` has the form `urn:wmo:md:<centre-id>:<identifier>`. For this demo, the centre-id may be
      another WIS centre's (`validation.require_centre_id_prefix` is off in `config/node.yml`).
- [ ] No links copied from a catalogue (e.g. `root`, `self` or `alternate` links pointing at the GDC)
- [ ] The **Validate** check passes (see the report comment below for ETS/KPI)

Once a collaborator has approved this PR and validation has passed, merging it publishes the
records to WIS2 (`gb.wis2dev.io`).
