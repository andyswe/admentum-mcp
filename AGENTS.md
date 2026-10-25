An MCP Server for Admentum (school messaging platform), used by a personal agent (for example Hermes). Only the server is in scope; the agent-side setup is the user's (see SPECIFICATION.md section 8).

Status: Testing&Verification
Status transition: Planning->Implementing->Testing&Verification->Maintaining

References:
- documentation/SPECIFICATION.md: the design (rules, data model, tools, Admentum protocol, verification status) and how to maintain it. Read it before changing code.
- README.md: what the project is, setup, usage and how to connect an agent, for users.
- examples/school-inbox/SKILL.md: an example agent skill showing how a personal agent uses the tools. An example only; the server must not depend on it.
