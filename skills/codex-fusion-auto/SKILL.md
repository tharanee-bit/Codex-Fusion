---
name: codex-fusion-auto
description: Automatically synthesize Claude's coding plan with independent Codex analysis when Codex Fusion hook context is present. Use whenever Codex Fusion injects context, including coding tasks, architecture, debugging, refactors, migrations, security-sensitive changes, API design, and substantial code review.
user-invocable: false
---

# Codex Fusion — automatic synthesis

When the conversation contains **AUTOMATIC CODEX FUSION CONTEXT** (injected by the
`codex-fusion-userprompt.sh` hook), treat Codex as an independent peer reviewer whose analysis
you must reconcile with your own **before making any edits**.

## Process
1. Form your own plan first — do not anchor on Codex.
2. Compare against Codex's analysis and explicitly identify:
   - **Consensus** — where you and Codex agree.
   - **Disagreements** — where you differ, and which side you choose and why.
   - **Codex-only insights** — useful points Codex raised that you missed.
   - **Claude-only concerns** — risks/considerations Codex missed.
   - **Final decision** — your chosen approach.
3. You remain the final judge. Do **not** blindly obey Codex; reject its suggestions when you have
   a sound reason, and say so.
4. Prefer **minimal, testable** changes.
5. After editing, run the relevant **tests / lint / typecheck** for the project.

## Post-diff review (Stop hook)
If a **POST-DIFF REVIEW** from Codex appears, address every serious issue (correctness, security,
data-loss, concurrency, broken tests) before finalizing, or explicitly justify why each is not a
real problem.

## Required final summary
End your response with a short **Codex Fusion summary**:
- Whether Codex was consulted automatically.
- What Codex suggested (key points).
- What you accepted vs. rejected, and why.
- Files changed.
- Tests / checks run and their result.
- Remaining risks or follow-ups.
