---
name: codex-fusion-auto
description: Automatically synthesize Claude's coding plan with independent Codex analysis when Codex Fusion hook context is present. Use whenever Codex Fusion injects context, including coding tasks, architecture, debugging, refactors, migrations, security-sensitive changes, API design, and substantial code review.
user-invocable: false
---

# Codex Fusion — automatic synthesis

When the conversation contains **AUTOMATIC CODEX FUSION CONTEXT** (injected by the
`codex-fusion-userprompt.sh` hook), treat Codex as an independent peer reviewer whose analysis
you must reconcile with your own **before making any edits**. The context may contain one Codex
report or several bounded Codex sub-agent reports.

## Process
1. Form your own plan first — do not anchor on Codex.
2. Compare against Codex's analysis and explicitly identify:
   - **Consensus** — where you and Codex agree.
   - **Disagreements** — where you differ, and which side you choose and why.
   - **Codex-only insights** — useful points Codex raised that you missed.
   - **Claude-only concerns** — risks/considerations Codex missed.
   - **Final decision** — your chosen approach.
   If multiple Codex Fusion sub-agent reports are present, synthesize by evidence quality, source
   references, and risk coverage — not by vote count. Preserve a minority concern when it has the
   strongest evidence.
3. You remain the final judge. Do **not** blindly obey Codex; reject its suggestions when you have
   a sound reason, and say so.
4. Prefer **minimal, testable** changes.
5. After editing, run the relevant **tests / lint / typecheck** for the project.

## Post-diff review (Stop hook)
If a **POST-DIFF REVIEW** from Codex appears, it may aggregate several review sub-agents. Address
every serious issue (correctness, security, data-loss, concurrency, broken tests) before finalizing,
or explicitly justify why each is not a real problem. Treat stronger evidence as more important
than the number of agents that reported it.

## Subagent adversarial verification (SubagentStop hook)
If you are running **as a subagent** and a **SUBAGENT ADVERSARIAL VERIFICATION** message appears,
Codex has independently checked your final report against the repository and found concrete
counter-evidence. Before returning to your parent agent:
1. Re-check each flagged claim **against the repository**, not against your memory of what you did.
2. Fix what is genuinely wrong, and correct or withdraw any claim you cannot support. Silently
   dropping an overstated claim is better than defending it.
3. Where Codex is wrong, say so explicitly and cite the evidence that refutes it.
4. Never restate a "done" / "tests pass" / "verified" claim you have not actually confirmed — that
   specific failure is what this check exists to catch.

If you are the **parent** agent, treat a verified subagent report as checked but not proven: the
verifier saw the report and the diff, not the subagent's full reasoning. Independently confirm any
subagent claim you are about to build on.

## Required final summary
End your response with a short **Codex Fusion summary**:
- Whether Codex was consulted automatically.
- What Codex suggested (key points).
- What you accepted vs. rejected, and why.
- Files changed.
- Tests / checks run and their result.
- Remaining risks or follow-ups.
