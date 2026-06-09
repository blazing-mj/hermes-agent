# Needs-MJ Gate Card — Template
*Every card that stops at MJ's gate MUST use exactly this format. No code. No jargon.
If a section can't be filled in plain language, the card is not ready for the gate.*

---

## 🛑 What needs your decision
One sentence. The irreversible action itself, e.g. "Switch the trader's brain from Anthropic API to your Codex subscription."

## ❓ The problem this solves
2–3 sentences, plain words. What was broken/risky/costing money, and how it showed up.

## 🔧 What was changed
Plain-language list (3–5 bullets max). No file paths, no function names — what it *does*, not how.

## ▶️ How it behaves AFTER you approve
What will be different tomorrow, observably. Include what you'd notice if it works AND what you'd notice if it breaks.

## ✅ What you are approving
The exact irreversible action(s), enumerated. Nothing else happens on approval.

## 🚫 What you are NOT approving
Explicit list of things people might assume but that are NOT included (e.g. "no live sends", "no money movement", "doesn't mark the project finished").

## ↩️ If it goes wrong
One sentence: the rollback ("we run X, system returns to today's state in ~N minutes") — already prepared and recorded on the ticket.

## 🔍 Proof it works (for the record, not for you to read)
Links only: validator verdict, test counts, artifacts. MJ never needs to open these.

---

### How MJ responds (in Linear)
- Move card to **Approved** → work continues automatically (webhook wakes the agent).
- Comment + move to **Approved** → continues, honoring your comment.
- Move to **Rejected** (+ comment why) → stops/revises.
- Just comment a question → agent answers in Linear, card stays waiting.

### Card quality bar (enforced by Validator before the card reaches MJ)
1. Zero code, zero file paths, zero jargon in sections 1–7.
2. "NOT approving" section present and honest.
3. Rollback tested or at minimum dry-run-verified, commands recorded on ticket.
4. If MJ would need >60 seconds to decide, the card has failed — rewrite it.
