# Agent Guardrails and Policies — Research Appendix

Supporting research for [`agent-guardrails.md`](agent-guardrails.md). The
design's decisions are stated there and stand without this file. What is here is
the evidence behind three of them, moved out of the design so the decisions stay
short:

| Appendix | Supports |
|---|---|
| [A. Bifrost plugins and images](#a-bifrost-plugins-and-images) | §D1 — why the guard runs at the gateway and not in Bifrost |
| [B. Dogwood](#b-dogwood) | §D6 — why Dogwood is the target shape, offline first |
| [C. Relationship to #152](#c-relationship-to-152) | §3.9 — what this design takes from, and owes, the tool-hop design |

Every source reference is to the upstream file at the version named beside it.
Section references such as §D1 or §3.10 point into the design document.
Revision history is in the PR thread, not here.

---

## A. Bifrost plugins and images
