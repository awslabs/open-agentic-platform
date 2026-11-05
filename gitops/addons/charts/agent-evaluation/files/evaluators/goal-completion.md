You are evaluating whether an AI agent achieved what the user asked for.

You are given the user's request and the agent's final response.

**User request**
{{input}}

**Agent's final response**
{{output}}

Score how completely the agent achieved the user's goal, from 0 to 1:

- **1.0** — The goal was fully achieved. Every part of the request was addressed,
  and the response is something the user can act on without asking again.
- **0.7** — Substantially achieved. The main request was met, but a secondary
  part was missed or left vague.
- **0.4** — Partially achieved. The response moves towards the goal but leaves
  the user's actual question unanswered, or answers only one of several parts.
- **0.1** — Not achieved. The response is on topic but does not accomplish what
  was asked.
- **0.0** — The agent refused, failed, answered a different question, or returned
  an error.

Judge only goal completion. Specifically:

- Do not reward length, confidence or politeness. A short, correct answer scores
  higher than a long one that circles the question.
- If the user asked the agent to *do* something rather than explain it, judge
  whether it was done. An explanation of how to do the task is not the task.
- If the agent asked a clarifying question instead of acting, that is a 0.7 when
  the request was genuinely ambiguous and a 0.2 when it was not: a necessary
  clarification is good behaviour, an unnecessary one is a stalled goal.
- If the agent says it cannot help because a tool or permission is missing, score
  0.0. The goal was not achieved, whatever the reason, and the number should show
  it so the dashboard reflects what users actually experienced.
- Judge only what is in the response. Do not assume the agent did anything it
  does not say it did.
