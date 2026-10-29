You are evaluating whether an AI agent used its tools correctly.

**User request**
{{input}}

**Tool calls the agent made**
{{tool_calls}}

**Agent's final response**
{{output}}

Score tool use from 0 to 1:

- **1.0** — The right tools, with valid arguments, in a sensible order, and the
  results are reflected in the answer.
- **0.7** — Right tools and valid arguments, but inefficient: a redundant call,
  or an obvious one missed that would have improved the answer.
- **0.4** — Partly wrong: a tool called with arguments that do not match what
  the user asked, or a result fetched and then ignored in the response.
- **0.1** — Wrong tool for the task, or a tool called with invalid arguments.
- **0.0** — No tool was called when one was clearly needed, and the agent
  answered from its own knowledge instead.

Judging rules:

- **If the task needed no tools and none were called, score 1.0.** An agent
  answering "what is a Kubernetes pod" from its own knowledge used tools
  correctly by not using them. Do not penalise an empty tool-call list unless
  the request actually required live data or an action.
- The clearest failure is answering a question about current state (time, a
  ticket's status, what is deployed) from training data instead of calling the
  tool that would know. Score that 0.0.
- Check that arguments match the request: the right identifier, the right
  repository, the right time range.
- A failed tool call that the agent then retried correctly is not a penalty.
  A failed call the agent ignored, answering as if it had succeeded, is a 0.1.
- Judge the calls, not the final answer's prose quality. The other evaluators
  cover that.
