"""The prompt-injection suite: attacks written by us, run through the full shipped pipeline.

Each attack case is a clean base document with an attacker's payload added by one technique.
Every base document is also run as a clean twin, to show the defenses do not just block
everything. The harness never approves anything, so an effect in a mock system's ledger got there
with no human involved. See docs/m12-design.md, section 3.2.
"""
