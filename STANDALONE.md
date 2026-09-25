# Standalone service investigation

Investigated on September 25, 2026, with macOS desktop build 10954, bundled Codex CLI 0.155.0-alpha.16.4, and local Claude Code 2.1.282.

**Result: the transport is viable; the desktop integration is not yet verified.** A temporary local prototype passed real Claude subscription, native subagent, and browser tests. The installed launcher still uses the existing wrapper. No standalone service, LaunchAgent, provider configuration, or app modification was installed.

## Proposed process and tool flow

Keep the official desktop process launching the official bundled Codex server and its signed helpers directly. Run the adapter as a separate authenticated loopback Responses provider. The service launches the user's authenticated Claude CLI; it never becomes an ancestor of the desktop's Codex server or helpers.

For a forwarded host tool, Claude calls an explicitly configured local MCP tool. The service emits a normal Responses function call. Codex executes it through its own tool and permission system, then returns the result in its next Responses request. The service returns that result to the waiting MCP call in the same native Claude process. Claude retains its native tools, automatic permission mode, and native Agent subagents.

The Responses tool-call pattern in [claude-codex-proxy](https://github.com/jpm8888/claude-codex-proxy) was useful. Its use of `--dangerously-skip-permissions` is unsuitable for this adapter. The investigation used an independently implemented MCP relay and the existing subscription-only native runtime, without copying that permission setting or its structured-output agent loop.

## What passed

| Check | Evidence and limit |
| --- | --- |
| Responses tool continuation | Real Opus called three synthetic read/wait/message functions, passing a random nonce through their results. These were local fixtures, not real Codex task tools. |
| Native subagent | One native Agent read a fixture using Read. Parent and child transcripts both recorded only `claude-opus-5-5`. Host subagent tools were not exposed. |
| Concurrent native sessions | Two independently bound Opus tasks overlapped without sharing their native session. |
| Browser interaction | Through Codex's normal `cua_repl.js` tool, Opus opened a local fixture in Chrome, clicked a button, and read the random result. |
| Screenshot round trip | The host returned an image block; the MCP relay delivered an image block to Claude, which described its contents. The test tab was closed. |
| Subscription and permissions | The existing native runtime checked claude.ai subscription authentication, stripped inherited API/provider overrides, pinned the native model, and used `--permission-mode auto`. No API-key client or OpenAI inference fallback was added. |
| Refusal checks | Seven deterministic local tests passed, including wrong token, unbound task, OpenAI model ID, mismatched attribution, required host model review, namespace handling, and inline image conversion. No inference ran for rejected requests. |

The browser test initially timed out with an inherited `CODEX_CLI_PATH` wrapper override. It passed after that override was removed. Its first successful interaction still omitted screenshots because the test catalog declared text-only input; declaring image support fixed image delivery. These are separate findings from desktop task-tool authorization.

Two temporary workspace trust entries written by Codex during the initial tests were removed, preserving other configuration values. The final browser test used an existing trusted workspace and left the global configuration byte-for-byte unchanged. Test HTTP servers and their native processes were stopped. Normal model/provider settings and the installed rollback launcher were retained.

## What remains unverified

- **Actual read/wait/message access.** The running desktop was still launched through the Python wrapper. The isolated test server was launched by a test harness, so it also does not establish the official desktop ancestry. Task tools were absent. Synthetic function tests cannot establish their availability, actual message delivery, or signed-peer authorization.
- **Provider selection in the stock app.** A catalog entry supplies a model, not its provider. The existing wrapper explicitly changes the provider on task start/resume/fork. A standalone endpoint alone cannot preserve the mixed dropdown's routing. Merely adding an Opus catalog entry while leaving the OpenAI provider selected is unsafe. A supported provider-selection path must be established first.
- **Automatic approval across both systems.** Claude's native automatic permissions were retained. Host permissions still apply to forwarded tools. The prototype refuses requests requiring host model-based review rather than bypassing that policy or invoking an OpenAI reviewer. It cannot promise that every host action will be automatically approved.
- **Production task binding and lifecycle.** Tests explicitly bound each task's workspace, model and effort before inference. A deployable service still needs a trustworthy stock-app binding path, side-chat handling, cancellation across disconnected tool continuations, restart recovery, and provider rollback. Those capabilities must not be inferred from user prompt text.
- **Fable, effort switching and side chats on this new transport.** The existing wrapper's checks do not establish these behaviors for the standalone design. Only Opus at Low was used in these bounded live probes.

## Next verification boundary

First quit the adapter-launched desktop and run the existing **Restore Standard Codex.command**. Confirm the stock desktop launches its official Codex server directly and that real task tools are available again. Then use a supported custom-provider configuration to test the standalone relay from an actual desktop-owned task, including read, wait, an authorized message, browser images, native subagents and provider/model isolation.

Keep the working standard setup and current rollback path until those checks pass. Do not patch signature checks, impersonate an OpenAI model, modify the app bundle, or route native Codex/voice traffic through the Claude endpoint to hide a routing problem.

Official configuration references: [custom providers and model catalogs](https://learn.chatgpt.com/docs/config-file/config-reference), [CLI configuration profiles](https://learn.chatgpt.com/docs/config-file/config-advanced#profiles). CLI profile support by itself does not prove equivalent desktop profile selection.
