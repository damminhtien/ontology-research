# Ontology Changelog

Generated from `registry/releases.json` by `tools/manage_ontology.py release`. Do not edit by hand.

## core

### 1.0.0 (2026-09-17T12:02:11Z) - MAJOR

Commit: `4227a35`

- ~ [PATCH] Event: comment: updated
- ~ [PATCH] InformationObject: comment: updated
- ~ [MAJOR] LocationAssertion: comment: updated; parents removed: ['https://damminhtien.github.io/ontology-research/ontology/core#Event']; parents added: ['https://damminhtien.github.io/ontology-research/ontology/core#InformationObject']
- ~ [MAJOR] hasConfidence: domain: 'https://damminhtien.github.io/ontology-research/ontology/core#Event' -> 'https://damminhtien.github.io/ontology-research/ontology/core#Entity'
- ~ [MAJOR] hasSource: domain: 'https://damminhtien.github.io/ontology-research/ontology/core#Event' -> 'https://damminhtien.github.io/ontology-research/ontology/core#Entity'
- ~ [MAJOR] validFrom: domain: 'https://damminhtien.github.io/ontology-research/ontology/core#Event' -> 'https://damminhtien.github.io/ontology-research/ontology/core#InformationObject'
- ~ [MAJOR] validUntil: domain: 'https://damminhtien.github.io/ontology-research/ontology/core#Event' -> 'https://damminhtien.github.io/ontology-research/ontology/core#InformationObject'

#### Migration

A statement is an information object, never an event. Domain changes: core:validFrom/core:validUntil now core:InformationObject, core:hasSource/core:hasConfidence now core:Entity and core:LocationAssertion subclassed core:InformationObject instead of core:Event, so an assertion carrying validity, provenance and confidence is not inferred as an event. Re-run make validate and re-materialize derived graphs.

### 0.1.0 (2026-08-26T07:05:33Z) - NONE

Commit: `72415c7`

- Initial baseline release.

## sensor

### 0.1.0 (2026-08-27T14:02:27Z) - NONE

Commit: `4cbf25d`

- Initial baseline release.

## tracking

### 0.1.0 (2026-08-27T14:02:27Z) - NONE

Commit: `4cbf25d`

- Initial baseline release.

## assertion

### 1.0.0 (2026-09-17T12:02:35Z) - MAJOR

Commit: `4227a35`

- ~ [MAJOR] literalValue: range: 'http://www.w3.org/2001/XMLSchema#string' -> 'http://www.w3.org/2000/01/rdf-schema#Literal'

#### Migration

assertion:literalValue range widened xsd:string -> rdfs:Literal so a typed (xsd:decimal) or language-tagged literal object can be mapped without losing its datatype. Existing data is unchanged; the SHACL shape now requires a literal rather than a string, and object representation is hasObject XOR literalValue.

### 0.1.0 (2026-09-17T12:02:07Z) - NONE

Commit: `4227a35`

- Initial baseline release.

## identity

### 0.1.0 (2026-09-12T11:22:11Z) - NONE

Commit: `4393292`

- Initial baseline release.

## location

### 0.1.0 (2026-08-27T14:02:26Z) - NONE

Commit: `4cbf25d`

- Initial baseline release.

## organization

### 0.1.0 (2026-08-27T14:02:26Z) - NONE

Commit: `4cbf25d`

- Initial baseline release.

