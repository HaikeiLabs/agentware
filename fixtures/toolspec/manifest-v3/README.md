# kei.tool-manifest/v3 loader fixtures

Shared fixtures for loading a `kei.tool-manifest/v3` file into typed
`ToolSpec` values. The Go, Python, TypeScript and Rust loaders must all pass
this same set. The wire format itself is described in
[`docs/kei-tool-manifest.md`](../../../docs/kei-tool-manifest.md); the
marshallers that write it are `MarshalManifestV3` (Go), `serialize_tool_specs`
(Python), `serializeToolSpecs` (TypeScript) and `marshal_manifest_v3` (Rust).

## Layout

| Path | Contents |
|---|---|
| `index.json` | Every case: `valid[]` with `input`, `expected` and `canonical`; `invalid[]` with `input`, the expected error `code`, an informational `path` and the `defect` |
| `valid/<name>.json` | A manifest the loader must accept |
| `expected/<name>.json` | The typed specs the loader must produce, in the normalized form below |
| `canonical/<name>.json` | What the marshaller writes for that case, when it differs from the input |
| `invalid/<name>.json` | A manifest the loader must reject. Each file has exactly one defect |

Paths in `index.json` are relative to this directory. Tests read
`index.json` rather than listing directories.

## Loader functions

Each language exposes two functions next to its existing marshaller:
`Load` reads a file path, `Parse` takes the document as bytes or text. Both
return the tools in file order.

| Language | Module | Load from a path | Parse bytes or text |
|---|---|---|---|
| Go | `go/toolspec` | `LoadManifestV3(path string) ([]ToolSpec, error)` | `ParseManifestV3(data []byte) ([]ToolSpec, error)` |
| Python | `haikeilabs.agentware.toolspec` | `load_manifest_v3(path) -> list[ToolSpec]` | `parse_manifest_v3(data: bytes \| str) -> list[ToolSpec]` |
| TypeScript | `typescript/src/toolspec` | `loadManifestV3(path: string): ToolSpec[]` | `parseManifestV3(data: string \| Uint8Array): ToolSpec[]` |
| Rust | `agentware_kei::toolspec` | `load_manifest_v3(path) -> Result<Vec<ToolSpec>, ManifestError>` | `parse_manifest_v3(data: &[u8]) -> Result<Vec<ToolSpec>, ManifestError>` |

A rejected manifest produces an error that carries one of the codes below as
a string: a `ManifestError` with a `Code` field (Go, found with `errors.As`),
a `ManifestV3Error` exception with `.code` (Python), a `ManifestV3Error` with
`.code` (TypeScript), or a `ManifestError` with `code()` (Rust). An error
reading the file in `Load` is the language's ordinary I/O error, not a
manifest error. Error messages must not echo field values; the path of the
offending field is enough.

## Contract

The manifest is a closed contract. Every object accepts only the fields
listed here, and any other field is `unknown_field`.

| Object | Fields |
|---|---|
| manifest | `schema` (string, exactly `kei.tool-manifest/v3`), `tools` (array, may be empty) |
| tool | `name`, `description`, `service`, `source` (non-empty strings, required); `operation_class` (`read` or `write`, case-sensitive, required); `route` (object, required); `required_capabilities` (array of strings); `resource_types` (array of objects); `parameters` (object); `enabled` (boolean, default `true`) |
| route | exactly one of `harness_executor` or `connector_binding` |
| `harness_executor` | `executor`, `registration` (non-empty strings) |
| `connector_binding` | `agent_id`, `connector_id` (non-empty strings) |
| resource type | `type` (non-empty string, required), `parent_type` (string) |

Route rules: a `harness_executor` tool carries neither
`required_capabilities` nor `resource_types` (not even empty arrays). A
`connector_binding` tool needs a non-empty `required_capabilities`; its
`resource_types` is optional but, when present, not empty.

Tool names are unique within a manifest. Strings are kept exactly as
written: no trimming and no Unicode normalization (the `unicode-descriptions`
case includes a combining accent that must survive as two code points).

`parameters` is the model's input schema. It is accepted and loaded into
`ToolSpec.Parameters`, but the v3 marshallers do not write it, so it does not
survive a round trip.

### Error codes

| Code | Meaning |
|---|---|
| `invalid_json` | Not valid JSON, or more than one JSON value |
| `invalid_type` | A value has the wrong JSON type, including a top-level value that is not an object and a non-object `parameters` |
| `unsupported_schema` | `schema` is missing or is not `kei.tool-manifest/v3` |
| `unknown_field` | A field outside the contract, including a route kind other than the two above |
| `missing_field` | A required field is absent, or a required string is empty |
| `invalid_operation_class` | `operation_class` is present but not `read` or `write` |
| `invalid_route` | The route does not have exactly one kind, a route field is empty, or the route rules above are broken |
| `duplicate_tool_name` | A tool name repeats an earlier tool's name |

Checks run in this order, and the loader reports the first failure:

1. Parse the JSON (`invalid_json`); the top level must be an object (`invalid_type`).
2. Check `schema` (`unsupported_schema`).
3. Decode strictly (`unknown_field`, `invalid_type`, `missing_field` for `tools`).
4. Validate each tool in file order: `name`, `description`, `service`,
   `source`, `operation_class`, `route`, then the route rules and resource
   types, then the name against earlier tools (`duplicate_tool_name`).

Parity tests must compare `code`. The `path` in `index.json` (a JSON pointer
to the offending field) is diagnostic; a loader may report it but tests must
not require it.

### Stricter than kei-proxy

kei-proxy's `LoadToolManifest` (kei-connector-runtime `runtime/tool_manifest.go`)
accepts every valid case here and rejects every invalid one, except five that
this contract rejects deliberately because the marshallers could not write
them back: `legacy-tool-field` (kei-proxy still accepts `permission`,
`category`, `version` and `tags`), `missing-tools`, `missing-description`,
`parameters-not-object` and `parameters-array`. kei-proxy also enforces name,
source and capability patterns that this loader leaves to the proxy; the valid
cases here satisfy them.

## Normalized expected form

`expected/<name>.json` is `{"tools": [...]}`, one object per tool, with every
field present so that `nil` versus empty does not differ by language:

- `parameters`: the input object, or `{}` when absent.
- `required_capabilities`, `resource_types`: the input arrays, or `[]` when absent.
- `resource_types[]`: `type`, plus `parent_type` only when the input has it.
- `route`: the single route kind with its two fields.
- `enabled`: the input boolean, or `true` when absent.

A parity test converts each loaded `ToolSpec` to this form and compares it
with `expected/<name>.json` as parsed JSON (key order and whitespace do not
matter).

## Round trip

For every valid case, `Marshal(Load(input))` equals `canonical` when both are
parsed as JSON. `canonical` is the input file itself unless the input omits
`enabled` or sets `parameters`; then `canonical/<name>.json` holds the
marshaller's output (`enabled` written out, `parameters` dropped). Compare
parsed JSON, not bytes: marshallers differ in key order and escaping.
