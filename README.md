# github-actions
Re-usable GitHub Actions workflows for Kausal

## Reusing successful results

`result-lookup.yaml` and `result-record.yaml` implement opt-in reuse within a
repository and caller workflow. A result is identified by its task name, exact
commit SHA, and caller-supplied `identity` string. Include a recipe version in
that identity and bump it when shared workflow changes invalidate old results.
The helpers run without checking out or executing application source.

1. Call `result-lookup.yaml` with `task` and `identity`.
2. Run the task only when its `hit` output is not `'true'`.
3. Call `result-record.yaml` with `if: always()` and dependencies on both the
   lookup and work jobs. Pass `reused` from the lookup, its `source_url`, and
   `successful` computed from all required work jobs' results. This final job
   also acts as the success gate when the work was skipped.

Lookup returns `hit`, a JSON object in `outputs` (default `{}`), and `source_url`.
Record accepts a JSON object in `outputs`. Store only non-secret data: image
references, build IDs, digests, and report URLs. Do not store credentials or
credential-bearing URLs in either outputs or identity.

Metadata is a dedicated `Kausal result: <task>` check run. `external_id` contains
the SHA-256 identity hash; `output.text` contains a versioned JSON record with
the original run ID and outputs. Lookup checks the GitHub Actions app, SHA,
identity, schema, and source workflow. It paginates all check runs, so new or
skipped runs cannot hide the original success. A reused result does not create
another record. Records can disappear with GitHub retention/deletion; absence
is a cache miss, not a failed build.

Callers need `actions: read` and `checks: write`, in addition to permissions
their work already needs. Fork PRs do not publish records. Lookup/API failures
run the work; record/API failures leave successful work successful. Explicit
reruns and manual dispatch bypass lookup. These helpers reuse completed work;
they do not coalesce simultaneous runs that have not yet recorded a result.

### Build wrapper

`build-cached.yaml` has the same inputs, secrets, and outputs as `build.yaml`,
including the newly exported `image_digest`. On a hit it validates the recorded
image metadata and checks that its digest still exists in the registry. On a
miss it calls the existing build workflow. The existing workflow's image-tag
lookup remains a fallback, including for builds predating these records.

On a hit the wrapper restores image outputs, updates image tags for the current
ref, and creates the Sentry release for a deployment. `deployment_env` is always
derived from the current ref and is never cached. The original build workflow
is otherwise unchanged; other repositories opt in by switching to this wrapper.

Paths is the first caller. Its policies are:

- Unit tests: exact SHA plus image reference and build ID.
- UI E2E: exact SHA plus named product/instance suite; one successful run with
  the then-current companion image is sufficient. The report URL is restored.
- Lint: exact tested SHA plus event and diff/PR context, because Reviewdog's
  filtering and reporting depend on that context. PR merge SHAs are not replaced
  by PR head SHAs.

Publish the shared workflows before activating callers referencing them at
`@main`. For an isolated live trial, publish a branch of this repository and
point all three caller references (lookup, record, and cached build) to that
branch. Internal workflow calls are relative and use the wrapper's revision.
Nothing needs to be enabled in the other application repositories first.

### Validation

Run `uv run --with pyyaml python -m unittest discover -s tests`. The tests execute
the actual inline JavaScript with a mocked GitHub API, including metadata round
trips, identity/provenance mismatches, error fallback, and success gating.
Validate workflow syntax with actionlint using `runner-prod` as an allowed
self-hosted runner label.
