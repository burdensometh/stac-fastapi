# Federated collections

`stac_fastapi.api.federation` lets one STAC API serve several independent
backends ("sources") at once. This is useful when two catalogs expose a
collection with the **same id but different extent or license**, and the API
must present them without silently preferring whichever backend happens to
answer first.

```python
from stac_fastapi.api.app import StacApi
from stac_fastapi.api.federation import (
    ConflictResolution,
    FederatedClient,
    SourceCapabilities,
    SourceConfig,
    add_federation_exception_handlers,
)

federated = FederatedClient(
    sources=[
        SourceConfig(
            source_id="alpha",
            client=alpha_backend,            # any BaseCoreClient / AsyncBaseCoreClient
            title="Alpha Catalog",
            capabilities=SourceCapabilities(),          # full bbox/intersects/datetime
            collections=["shared", "only_a"],           # optional static hosting list
            priority=1,
        ),
        SourceConfig(
            source_id="bravo",
            client=bravo_backend,
            title="Bravo Catalog",
            # A backend that cannot filter spatially: conditions are enforced
            # locally by the federation instead of being silently ignored.
            capabilities=SourceCapabilities(bbox=False, intersects=False),
            priority=2,
        ),
    ],
    conflict_resolution=ConflictResolution.PRIORITY,
)

api = StacApi(settings=settings, client=federated)
add_federation_exception_handlers(api.app)
```

## Guarantees

### 1. Each source executes only what it supports

`SourceCapabilities` declares whether a source executes `bbox`, `intersects`,
`datetime` and `ids` server-side. A condition flagged `False` is **never sent**
to that source; the federation applies it locally on the returned records and
records the local enforcement in the response's coverage block.

### 2. Conflicts follow an explicit, deterministic rule

`ConflictResolution.PRIORITY` (default) keeps one variant per collection id.
The winner is chosen by static `priority` (ties broken by registration order),
never by response latency. Losing variants remain listed in
`federation:provenance.federation:variants`.

`ConflictResolution.SPLIT` exposes every variant as its own collection with the
stable id `"{source_id}:{collection_id}"`, so no source's data is hidden.

### 3. Provenance is stable on every record

Every Collection and Item carries:

* `federation:source` — the source id it came from,
* `federation:original_collection_id` — the collection id at that source,
* a `federation-source` link, and Items get a `self` link containing an
  `x-stac-source` hint.

The same registry resolves search, collection details, item listings and item
details, so following an Item's `self` link always reaches the **same real
source** the item was retrieved from. The source can also be pinned with the
`x-stac-source` request header or query parameter.

### 4. Missing coverage is explicit, never silent

Every aggregated response contains a `federation:coverage` block with a
per-source status (`ok`, `error`, `not_hosted`, `skipped`) and a `partial`
flag. When **no** source can answer, the request fails with
`FederationCoverageError` (HTTP 503 once
`add_federation_exception_handlers` is registered) carrying the per-source
report — it never returns a successful empty response that hides the outage.
Set `fail_fast=True` to abort on the first source error instead.

## Response fields

| Field | Where | Meaning |
|---|---|---|
| `federation:coverage` | Collections / ItemCollection | per-source status report |
| `federation:provenance.federation:source` | Collection / Item | originating source id |
| `federation:provenance.federation:original_collection_id` | Collection / Item | upstream collection id |
| `federation:provenance.federation:variants` | Collection | every source hosting a same-named collection |
| `properties.federation:source` | Item | originating source id |
| link `rel=federation-source` | Collection / Item | link to the source attribution |

The extension URI
`https://stac-utils.github.io/stac-fastapi/federation/v1` is declared in
`stac_extensions` on stamped documents.

## Sync and async backends

Sync `BaseCoreClient` implementations are run in a worker thread, so a single
federation can mix sync and async backends transparently.
