# Social Post Draft — Jingu SWE-bench Results

## X / Twitter Post

```
We got SWE-bench to 23/30 with a simple idea:

Don't try to make the agent think better.
Give it structured recovery paths when it thinks wrong.

Results:
• Claude Sonnet 4.5: 16 → 19 (+3)
• Claude Sonnet 4.6: 19 → 22 (+3)
• Claude Opus 4.6 + Jingu: 23/30

Key finding:
Jingu provides a consistent +3 uplift across model strengths.
Model upgrades and governance are additive, not substitutes.

---

The surprising part:

We tried multiple "smarter reasoning" approaches:
- Better prompts
- Hypothesis generation
- Direction reconsideration

None worked.

The only thing that worked:
→ structured retry + routing based on failure signals

---

Real example (django-11477):

Attempt 1:
- Fixes reverse lookup, misses forward matching
- 1/3 tests pass

Jingu detects incomplete fix → routes to DESIGN

Attempt 2:
- Switches to root cause (RegexPattern.match)
- 3/3 tests pass → resolved

Same model. Same problem. Different outcome because of governance.

---

Repo + reproducible results:
https://github.com/ylu999/jingu-swebench

Demo: python scripts/demo_jingu_retry.py
Deep dive: docs/benchmark_deep_dive.md
```

## First Reply (for credibility)

```
Reproduce in 3 commands:

./scripts/reproduce_benchmark.sh --model sonnet-4-6 --attempts 2   # → 22/30
./scripts/reproduce_benchmark.sh --model sonnet-4-6 --attempts 1   # → 19/30
./scripts/reproduce_benchmark.sh --model opus-4-6   --attempts 2   # → 23/30

Full methodology + scripts in repo.
```

## Attach Images

1. `charts/four_cell_bar.png` (required)
2. `charts/instance_attribution.png` (optional)

## FAQ Answers

**Q: Isn't this just running it twice?**
No — attempts alone don't explain it. The +3 comes from structured retry + routing (failure classification → phase-specific recovery), not just retry.

**Q: Why not just use a stronger model?**
Stronger models help (+3), but governance adds another +3 on top. They're orthogonal — you get both.

**Q: Is this overfitting SWE-bench?**
The mechanism is general: detect failure → classify → route → retry with constraints. SWE-bench gives a measurable testbed. The 5 dead lines we tried (and validated as zero uplift) show this isn't cherry-picked.
