# Candidate-matching adversarial benchmark

`tests/fixtures/inkdrop-candidate-matching-benchmark-v1.json` is a fixed,
versioned semantic-gold corpus for the provider-independent matcher. It
deliberately records desired safe outcomes, not whatever the current
implementation happens to return. A score below the committed baseline (see
"Score" below) therefore identifies a matcher gap; it is not a reason to
rewrite the gold.

**This is a curated-corpus score, not a general accuracy claim.** The corpus
is 50 hand-picked adversarial cases chosen because they are hard, not a random
or representative sample of real-world matching traffic. "50/50" means the
matcher gets every one of these 50 deliberately difficult cases right today --
it does not mean matching never fails in production, and it should not be
quoted as an overall success rate.

Run the baseline from the repository root:

```text
python -B tests/inkdrop-candidate-matching-benchmark.py
```

Use `--json` for machine-readable evidence. Without `--require-perfect`, a
valid benchmark run exits zero and reports the score, which is what makes it
useful to run by hand while working on the matcher; malformed data or a
matcher-contract version mismatch always fails closed.

`tests/inkdrop-candidate-matching-benchmark-smoke.py` is the release gate.
Rather than requiring a perfect score, it compares the run above against
`tests/fixtures/inkdrop-candidate-matching-benchmark-baseline.json` and fails
only if the score drops below that committed floor. A hardcoded "must be
100%" gate would break on an unrelated PR the moment someone adds a new,
harder case that nothing has fixed yet; a non-regression gate lets the corpus
grow without that cost; expanding it with a deliberately unsolved case just
means committing a lowered baseline in the same PR, explicitly, instead of
leaving CI red or silently allowing the gap. The wrapper also exists so the
corpus is picked up by the smoke suite's own discovery
(`git ls-files tests/inkdrop-*smoke*.py` in
`tools/inkdrop_run_smoke_suite.py`), which the scorer's filename does not
match, without giving up the plain scoring run above.

Direct cases supply normalized provider-independent inputs accepted by
`core.inkdrop_candidate_matching.candidate_compatibility()`. Cases marked with
the `prowlarr` pipeline instead pass raw result data through
`core.inkdrop_source_providers.prowlarr_candidate_from_result()` first. The
`mangadex` pipeline passes deterministic manga/feed payloads through
`core.inkdrop_source_providers.mangadex_candidates_from_payload()` and requires
exactly one normalized candidate. These paths do not manufacture adapter
confidence. Expected statuses (`compatible`, `review`, `blocked`) and reason
codes use the shared matcher's real contract-v2 vocabulary.

## Score

The corpus scores 50/50 today. The smoke wrapper holds it at or above the
`score_percent` committed in
`tests/fixtures/inkdrop-candidate-matching-benchmark-baseline.json` (currently
100.0) rather than a hardcoded 100% -- see the non-regression gate described
above. Bump that file, in the same PR as the change that causes the move,
whenever the true score changes on purpose.

It first landed at 43/50 against the matcher as it stood, with seven reviewed
gaps recorded rather than papered over. Five mechanisms were behind them:

- A one-word wanted title policed nothing that followed it, so a child series
  built on that name read as a plain match -- `title-001` (Batman Beyond),
  `title-004` (Venom: Lethal Protector), `title-005` (Superman: Son of
  Kal-El). A one-word title now polices its own title segment: the words
  before the release names a unit. What trails the unit marker is still left
  alone, because real accepted packs trail prose no token list will cover,
  and words naming an edition, format or language of the same work ("Vagabond
  Colored Manga V01-41") do not end the title either.
- A chapter target never compared volumes, so the right chapter number in the
  wrong asserted volume passed -- `unit-006`. The issue branch had compared
  print runs since the Love and Rockets fix; the chapter branch now does the
  same, and only against a volume the target itself confirmed.
- `ambiguous_unit_identity` was reported ahead of the precise reason beside it
  -- `legacy-002`. It is now demoted the way `candidate_title_mismatch`
  already was, except when the sources genuinely contradict each other, where
  it is the precise answer rather than a placeholder.
- `VYYYY N` was read as a run year plus issue only when N was the wanted
  issue, so the same release reported `wrong_unit_type` or
  `wrong_issue_number` depending on what was being searched for --
  `legacy-005`. The shape of the title now decides that, not the target.
- A relaunch reuses the title and restarts the numbering, so a 2018 first
  issue satisfied a 1963 run's issue 1 -- `legacy-006`. Comic issue-one
  targets now compare the run year, one-directionally, never on a reprint,
  and never for manga, whose series year and release year are not the same
  quantity.

`tests/inkdrop-run-identity-and-reason-precision-smoke.py` pins those five
mechanisms directly, including the cases each one must *not* fire on.
