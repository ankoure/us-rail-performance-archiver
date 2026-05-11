## Summary

<!-- 1–3 sentences: what changed and why. Link the issue if one exists (e.g. `Closes #12`). -->

## Changes

<!-- Bulleted list of the concrete changes in this PR. -->
-
-

## Area affected

<!-- Tick all that apply. -->
- [ ] Fetcher / HTTP client
- [ ] Decoder (protobuf)
- [ ] Landing zone writer (jsonl + raw bins)
- [ ] Parquet rollup
- [ ] Config / `feeds.yaml`
- [ ] Scheduler / orchestration
- [ ] CI / tooling
- [ ] Docs

## How was this tested?

<!-- Commands run, feeds exercised, manual checks. If you skipped a test, say why. -->
- [ ] `uv run ruff check archiver`
- [ ] `uv run ruff format --check archiver`
- [ ] Unit tests pass (`uv run pytest`)
- [ ] Ran against a real feed (specify which):

## Checklist

- [ ] PR scope is focused — no drive-by refactors mixed in.
- [ ] New behavior is covered by a test, or there's a note explaining why not.
- [ ] No credentials, API keys, or personal data committed.
- [ ] `feeds.yaml` changes preserve existing entries and ordering.

## Notes for reviewers

<!-- Anything reviewers should look at first, known follow-ups, or open questions. -->
