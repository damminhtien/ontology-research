# Vietnam and South China Sea profile

This directory configures a regional profile over the shared ontology modules.
It does not declare an ontology or add Vietnam-specific classes. Country and
administrative units use `location:Country` and
`location:AdministrativeRegion`; vessel and aircraft records use the existing
`core:Platform` class until a domain requirement justifies a shared extension.
The profile's `wikidata:Q881` value is an external reference, not a canonical
entity identifier. The identity service still assigns and resolves canonical
IDs under its normal evidence policy.

The area of interest matches `VIETNAM_SCS` in `foundry/osint_sources.py`.
Preferred labels are Vietnamese and English. Source connectors and their
freshness settings remain in `config/source_registry.json` and
`config/worldmonitor_sources.json`.

## Membership and affiliation

Use the generic assertion lane for every relationship claim, including
membership, role assignment and event participation. Keep `validFrom`/
`validUntil` separate from `recordedAt`, and retain a source and evidence link.

| Meaning | Predicate | Object type |
|---|---|---|
| Membership or affiliation | `core:memberOf` | `core:Agent` |
| Position or role | `core:hasRole` | `core:Role` |
| Participation in an event | `core:participatesIn` | `core:Event` |
| Organizational command | `organization:subordinateTo` | `core:Organization` |
| Corporate ownership | `organization:subsidiaryOf` | `core:Organization` |

An assignment to a `core:Role` does not classify the agent as that role.
`memberOf`, `hasRole`, and `participatesIn` are distinct predicates. Do not
encode an affiliation as a new event type or as a direct replacement for the
assertion history. `profiles/vietnam/affiliation_shapes.ttl` and the fixture
in `profiles/vietnam/examples/` enforce these object-type distinctions.

## Natural-language semantic queries

The API only selects a query from `semantic_query_templates`, loads its
registered CQ query file, validates the SPARQL as a `SELECT`, and binds typed
parameters separately. It never accepts SPARQL text from the request. The
current offline planner recognizes a small Vietnamese/English phrase set. A
model-backed planner can be injected through `LlmIntentSelector`; it may only
return one of the registered CQ IDs. Entity and time slots are still parsed
and validated by the service.

Set `FOUNDRY_SEMANTIC_QUERY_DATA` to an RDF/Turtle file or a directory of RDF
files to enable `/api/v1/semantic/query`. That endpoint is an optional semantic
projection; the operational PostgreSQL query path stays separate. The default
console does not silently query benchmark fixtures or materialize the event
log into an in-memory graph.

Validate the profile and affiliation fixture with:

```sh
make profile-check
```
