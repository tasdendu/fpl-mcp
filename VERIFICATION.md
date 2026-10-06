# Verification

Verified locally on 19 September 2026 with Python 3.12 and MCP SDK 1.30.0.

- 11 automated tests passed.
- Ruff code checks passed.
- Real Streamable HTTP JSON-RPC initialization, tool discovery (16 tools), tool call, source timestamps, invalid input rejection, and Host header rejection were exercised through an in-process ASGI test client.
- Regression cases cover captain and triple-captain EO, incomplete league samples, squad comparison, next-gameweek selection, blank weeks, prior/current transfer hits, cache reuse, missing upstream resources, and unknown private squad state.
- Test data is synthetic. No real player recommendation is embedded in the project.
- Two dependency deprecation warnings appear from Starlette's test client and AnyIO; they do not fail the tests.

Not verified in this environment:

- Live FPL fetches: the official API hostname failed DNS resolution in the build workspace.
- Docker image build and Nginx configuration activation: Docker and a configured HTTPS host were unavailable.
- ChatGPT account connection: requires deployment and a connection in the user's ChatGPT settings.

Run the included `deploy/smoke_test.py` against the real HTTPS endpoint after deployment to verify the remote MCP and upstream FPL path together.
