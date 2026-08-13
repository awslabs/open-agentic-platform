"agentcore-code-interpreter": {
	alias: ""
	annotations: {}
	attributes: {
		workload: type: "autodetects.core.oam.dev"
		status: {
			// status.atProvider.codeInterpreterId is the field the provider populates on a
			// successful create, confirmed in the CRD schema for
			// codeinterpreters.bedrockagentcore.aws.upbound.io v1beta1. Gating on it means a
			// dependsOn from the MCP server waits for a real interpreter id rather than for
			// the resource object merely existing.
			healthPolicy: "isHealth: *( context.output.status.atProvider.codeInterpreterId != \"\" ) | false"
			customStatus: #"""
				message: *("codeInterpreterId: " + context.output.status.atProvider.codeInterpreterId) | "provisioning"
				codeInterpreterId: *context.output.status.atProvider.codeInterpreterId | ""
				"""#
		}
	}
	description: "AgentCore Code Interpreter provisioned via Crossplane managed resource with IAM policy"
	labels: {}
	type: "component"
}

template: {
	// There is deliberately NO generated default for the interpreter name.
	//
	// The name is referenced from a second place, the MCP server's
	// AGENTCORE_CODE_INTERPRETER_NAME, and the two must match or the server never resolves
	// an interpreter and its pod never becomes ready. A generated default cannot be
	// referenced: it is computed inside this template and is not addressable from another
	// component or from the application YAML, so a developer relying on it would have to
	// reproduce the formula by hand. Requiring the name means the value a developer types
	// here is the value they type there.
	//
	// The name must also be unique per AWS ACCOUNT, and a collision is not self-healing. A
	// create against a taken name fails with
	//   ConflictException: CodeInterpreter with name '<name>' already exists in this account
	// and the provider then retries that same create forever, leaving the resource
	// permanently Synced=False with an empty atProvider. That is the live state of the
	// interpreter provisioned by the crossplane-agentcore chart on this hub, stuck since
	// 2026-05-27, even though the interpreter itself exists in AWS and is READY.
	//
	// Automatic wiring, which would remove the need for a developer to name this at all, is
	// tracked in issue #55.

	output: {
		apiVersion: "bedrockagentcore.aws.upbound.io/v1beta1"
		kind:       "CodeInterpreter"
		metadata: name: context.name
		spec: {
			forProvider: {
				name:        parameter.interpreterName
				region:      parameter.region
				description: parameter.description
				networkConfiguration: {
					networkMode: parameter.networkMode
				}
			}
			providerConfigRef: name: "default"
		}
	}

	outputs: "\(context.name)-iam-policy": {
		apiVersion: "iam.aws.upbound.io/v1beta1"
		kind:       "Policy"
		metadata: name: "\(context.appName)-\(context.name)-iam-policy"
		spec: {
			forProvider: {
				name: "\(context.appName)-\(context.name)-iam-policy"
				// Least privilege: exactly the four actions code-interpreter-mcp calls, no more.
				// Verified against src/agentcore.js, which constructs only
				// ListCodeInterpretersCommand, StartCodeInterpreterSessionCommand,
				// StopCodeInterpreterSessionCommand and InvokeCodeInterpreterCommand.
				// GetCodeInterpreter, GetCodeInterpreterSession and ListCodeInterpreterSessions
				// are deliberately absent because nothing calls them; add them alongside the
				// code that needs them. CreateCodeInterpreter and DeleteCodeInterpreter are
				// absent because Crossplane provisions under its own credentials.
				//
				// The resource type is "code-interpreter-custom", not "code-interpreter". A
				// custom interpreter's real ARN, read back from the service:
				//
				//   arn:aws:bedrock-agentcore:us-west-2:<acct>:code-interpreter-custom/peeks_hub_agent_core_code_interpreter-S8Z5DTeRiG
				//
				// while the AWS built-in is a different type and partition-owned:
				//
				//   arn:aws:bedrock-agentcore:us-west-2:aws:code-interpreter/aws.codeinterpreter.v1
				//
				// This distinction is not cosmetic. The sibling agentcore-browser component hit
				// exactly this trap: AgentCore's own documented example policy scopes browser
				// actions to "browser/*", which never matches a provisioned browser, and that
				// policy denied every call at runtime.
				//
				// The trailing "-*" is required because AWS appends a suffix to the requested
				// name: this component asks for "peeks_hub_agent_core_code_interpreter" and the
				// service created "peeks_hub_agent_core_code_interpreter-S8Z5DTeRiG".
				//
				// ListCodeInterpreters stays on "*" because it is a collection read and is not
				// authorized against any single interpreter.
				//
				// All of the above was checked with `aws iam simulate-custom-policy` against the
				// real ARN: the four granted actions returned "allowed", while
				// InvokeCodeInterpreter against a DIFFERENT interpreter, plus
				// CreateCodeInterpreter and DeleteCodeInterpreter, all returned "implicitDeny".
				//
				// The account id comes from platform config (the cluster secret's
				// aws_account_id annotation), never from developer OAM, and falls back to "*"
				// so the ARN stays valid if the global is unset.
				policy: """
					{
					  "Version": "2012-10-17",
					  "Statement": [
					    {
					      "Sid": "Discover",
					      "Effect": "Allow",
					      "Action": [
					        "bedrock-agentcore:ListCodeInterpreters"
					      ],
					      "Resource": "*"
					    },
					    {
					      "Sid": "UseThisInterpreter",
					      "Effect": "Allow",
					      "Action": [
					        "bedrock-agentcore:StartCodeInterpreterSession",
					        "bedrock-agentcore:StopCodeInterpreterSession",
					        "bedrock-agentcore:InvokeCodeInterpreter"
					      ],
					      "Resource": "arn:aws:bedrock-agentcore:\(parameter.region):{{ .Values.global.awsAccountId }}:code-interpreter-custom/\(parameter.interpreterName)-*"
					    }
					  ]
					}
					"""
			}
			providerConfigRef: name: "default"
		}
	}

	parameter: {
		// +usage=REQUIRED. Code interpreter name in AWS, and the value to repeat in the MCP server's AGENTCORE_CODE_INTERPRETER_NAME. Letters, digits and underscores, starting with a letter, 48 characters maximum. No hyphens. Must be unique within the AWS account.
		//
		// The pattern below is AWS's own, taken from the CreateCodeInterpreter API reference
		// and confirmed against the live service, which rejects a violation with:
		//   ValidationException: Value 'invalid-name-with-hyphens' at 'name' failed to
		//   satisfy constraint: Member must satisfy regular expression pattern:
		//   [a-zA-Z][a-zA-Z0-9_]{0,47}
		// Validating here turns that into a render-time error naming the offending value,
		// instead of a managed resource that sits Synced=False until someone reads its events.
		interpreterName: string & =~"^[a-zA-Z][a-zA-Z0-9_]{0,47}$"
		// +usage=AWS region. Defaults to the cluster's region so the same OAM Application
		// is portable across regions; override only for a cross-region interpreter.
		region: *"{{ .Values.global.awsRegion }}" | string
		// +usage=Description of the code interpreter
		description: *"AgentCore Code Interpreter" | string
		// +usage=Network mode. PUBLIC is verified working; the CRD does not enumerate the allowed values, so this is left open rather than guessed
		networkMode: *"PUBLIC" | string
	}
}
