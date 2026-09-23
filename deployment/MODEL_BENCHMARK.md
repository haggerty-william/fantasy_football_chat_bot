# Local model comparison — September 21, 2026

Selected bot default: `google/gemma-4-12b-qat`.

Gemma is the practical choice for this bot on the current machine: substantially
lower latency than Qwen, successful structured tool calls, and more usable output
than the smaller Nemotron on important trade/standings details. This small test does
not establish a universal model ranking or show that Gemma is factually reliable
without verification. All three models made mistakes.

## Method

The bot's inference and model keeper were paused during isolated tests. Only one
candidate was loaded at a time. Each used the installed quantization, a 32,768-token
context, the same production temperature/token limits, reasoning disabled, and the
same request/correction budgets. A short warm-up preceded each model's reports.
Load time is excluded from report latency.

Four real production prompts were frozen: a completed trade, standings, an
unfinished rivalry, and a historical recap. Each ran twice per model: 24 full
report trials. ESPN/nflverse research results were frozen too, so every model saw
the same evidence. Checks and at most one correction mirrored the bot. Two additional
focused trade-tool probes per model tested actual function calls and interpretation
of the same calculated lineup changes: 30 comparative trials in total.

The full-report prompt included all nine tools. None of the models requested a
tool in those eight full-report trials, despite the trade instruction. All three
called `simulate_trade_impact` correctly in both focused probes. Tool availability
therefore does not guarantee that a model will choose to use it in a large prompt.

## Full-report results

| Installed model | Passed bot checks, including correction | Passed first draft | Median total latency |
|---|---:|---:|---:|
| Qwen3.6 35B-A3B, Q4_K_M | 7/8 | 6/8 | 48.8 s |
| Gemma 4 12B QAT, Q4_0 | 8/8 | 8/8 | 16.8 s |
| Nemotron 3 Nano 4B, Q4_K_M | 8/8 | 8/8 | 4.3 s |

Passing the bot's checks is **not an accuracy score**. Manual review found errors
the deterministic checks missed:

* Qwen treated bench performances as scoring contributions, assigned an award to
  the wrong team, and added unsupported player characterizations. Its failed
  standings correction was triggered by a false-positive check on “no one has
  clinched,” rather than an actual positive clinch claim.
* Gemma also confused bench performances with contributions, reversed a bench
  swap in one recap, omitted a losing team from one standings summary, and made
  an incorrect tight-end depth claim in one trade response.
* Nemotron reversed trade direction, misplaced the playoff cutoff, and confused
  timing/roles in rivalry commentary. Its two recaps were comparatively restrained.

## Focused tool probes

| Model | Valid tool calls | Output passed checks | Median latency |
|---|---:|---:|---:|
| Qwen | 2/2 | 2/2 | 33.7 s |
| Gemma | 2/2 | 2/2 | 22.0 s |
| Nemotron | 2/2 | 2/2, one correction | 8.9 s |

Gemma explained the +4.17 / -3.83 projected lineup changes in both probes, although
one response overstated the comparison with the combined incoming assets. Qwen
preserved those numbers but added questionable slot/roster explanations. Nemotron
misassigned player projections/depth in one response and fell back to merely
retelling the exchange after correction in the other.

## Decision and follow-up

Gemma offers the best operational balance observed here. Qwen did not demonstrate
a clear enough accuracy advantage to justify roughly three times the median wait;
Nemotron's speed came with serious trade/standings mistakes. The selected default
is provisional and should be revisited with more saved reports.

The comparison also prompted shared fixes: the prompt explicitly labels
bench/reserve points as non-scoring, the playoff guard permits negated clinch
statements while still rejecting unsupported positive claims, and a targeted
check rejects clear assertions that live bench points are helping a team's score.
These changes were
made **after** the comparative trials; the table above preserves the original
results. Four separate Gemma follow-up reports passed the automated checks with a
20.9-second median, but manual review still found a bench contribution error; this
led to the additional targeted check. Follow-up runs are stored with the artifacts.

Deployment verification confirmed Gemma as the active default, with AI analysis and
automatic model loading enabled. A read-only live rivalry report completed in 26.9
seconds and rendered as one Discord payload. Its commentary still credited bench
players through wording the targeted check missed. The guard is incomplete; this
verification establishes delivery and configuration, not factual correctness.

Raw prompts and outputs contain league data and are kept locally under the ignored
`data/model-benchmark/2026-09-21/` directory. The reusable runner is
`scripts/benchmark_models.py`. No benchmark message was posted to Discord.
