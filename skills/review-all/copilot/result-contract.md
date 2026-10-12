# Result schema version 1

Return one **minified** JSON object (not a Markdown fence), with no repeated
assignment metadata or explanation. Prefer this compact form; every field is
required:

```json
{"dispatchToken":"from start prompt","status":"complete","allMandatoryRead":true,"missingInputs":[],"effectiveModel":null,"sourceFilesRead":["src/lib.rs"],"findings":[]}
```

The opaque `dispatchToken` binds the result to this run, assignment and attempt.
Never reuse another dispatch's token. `allMandatoryRead: true` attests that you
read the entire assignment JSON and every path in its `mandatory` list, including
every numbered part. It is a read receipt, not evidence that the helper can
observe your tool calls. Do not set it from `status` or from a list of filenames.
When context is missing/unread/truncated use `status: "incomplete"`,
`allMandatoryRead: false`, list the blockers in `missingInputs`, and return no
findings. Budget exhaustion also means incomplete.

The coordinator passes this compact object unchanged to `accept --stdin` or
`accept --result FILE`. The helper validates the token and explicit attestation,
fills deterministic identity/chunk/read-input fields from the frozen assignment,
and stores the full validated envelope. Do not echo the mandatory path list.
`sourceFilesRead` still lists original repository-relative files actually reread.

## Legacy full envelope

Older consumers may return the full schema below. All shown fields are required:

```json
{
  "schemaVersion": 1,
  "runId": "from assignment",
  "packageHash": "from assignment",
  "assignmentId": "from assignment id",
  "axis": "from assignment",
  "attempt": 1,
  "status": "complete",
  "chunks": ["exact assignment chunks"],
  "readInputs": ["absolute paths actually read, all mandatory parts"],
  "missingInputs": [],
  "effectiveModel": null,
  "sourceFilesRead": ["repository-relative original source files reread"],
  "findings": []
}
```

`attempt` comes from the coordinator's start command, not the assignment file.
`readInputs` must contain **every exact path in assignment.mandatory**. Extra
reads are allowed. Reading the assignment JSON itself is required to discover
the task, but its path is not a mandatory component and need not be echoed in
`readInputs`; including it is also valid. `accept` checks the mandatory list,
not a guessed list of source snapshots or just the first part of each component.
`status` is `complete` or `incomplete`. For incomplete, list missing inputs or
other blocking context in `missingInputs`; never return partial findings as a
completed assignment. An empty `findings` array IS valid when complete.

## Reviewer findings

Reviewer findings each require:
`id`, `root_cause_key`, `file`, `line` (positive integer), `severity`
(`CRITICAL|IMPORTANT|DEBT|SUGGESTED|QUESTION`), `confidence`
(`HIGH|MEDIUM` or number 0-100), `title`, `evidence`, `impact`, `fix`,
`claim_class` (`static|runtime|data|rendering`), `needs_observation` (string or null).
Use globally unique IDs prefixed by the assignment ID. `pre_existing` (boolean)
is optional and follows the canonical security-audit escape, not a scope bypass.
Only the coordinator can give a deterministic gate `confidence: VERIFIED`.

## Verifier findings

Verifier `findings` contains one adjudication per candidate, with:
`finding_id`, `root_cause_key`, `score` (number 0-100),
`verdict` (`keep|appendix|drop|unverified`), `claim_class`,
`needs_observation` (nonempty string for unverified, otherwise null),
`reason`, `reread_evidence` (a verbatim excerpt reread from original source),
`cross_confirmed_by` (array of names).
The excerpt must exist in a supplied original-source snapshot. Source snapshots
are exact target-revision files, not a reviewer's quoted or edited evidence.
List their original repository-relative names in `sourceFilesRead`.
Missing source prevents completion, never substitutes for a drop verdict.

keep requires score >=75; appendix requires 50-74; drop requires <50.
unverified is orthogonal to score, and runtime/data/rendering claims without
observations must remain unverified. Do not add new candidates or copy another
verifier's score. Corroborating counts alone cannot earn an independence bonus.
