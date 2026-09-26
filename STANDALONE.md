# Standalone service verification

September 25, 2026: macOS Codex desktop build 10954, bundled Codex CLI 0.155.0-alpha.16.4, Claude Code 2.1.282.

September 26 update: desktop 26.924.20706 (build 11431) moved its bundled core to `Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex`. Discovery now supports that signed executable and the older layout. Launch validation also runs when the Claude service is already healthy, preventing the former attempt to execute `None`. All 39 tests passed against bundled CLI 0.158.0-alpha.2, including fixture-based provider, effort, side-chat, parallel-task, and rollback integration. The installed launcher was opened against the updated app; this update check did not repeat the subscription-backed tool smoke below.

**Result: separate Claude desktop mode works with the official process chain. Real Claude task coordination and the in-app browser passed.** Regular Codex stays open with its original provider and tasks. The user accepted separate modes rather than a mixed GPT/Claude provider dropdown.

## Architecture

```text
Official Codex desktop, separate data directory
  └─ Official bundled Codex server and signed helpers
       ├─ Local Responses request → independent Python service → local Claude CLI
       │                                                        ├─ native tools
       │                                                        └─ native Agent subagents
       └─ Normal Responses function call ← relay MCP call from Claude
          → official host tool and permission checks
          → next Responses request → waiting native MCP call
```

The service is not between the desktop and its core. No app patch, CLI override, signature workaround, raw desktop IPC client, or OpenAI model impersonation is used.

The app's built-in `CODEX_ELECTRON_USER_DATA_PATH`, `CODEX_HOME`, and `--user-data-dir` paths isolate Claude settings and tasks at `~/.codex-claude`. Merely adding Claude to the stock model catalog does not select its provider; app-server also rejected the CLI's named `--profile` option. A separate home supplies the provider directly.

Task workspace/model/permissions come from authenticated core metadata and the isolated core's read-only task registry. Side chats use the host-provided parent task ID and must keep the parent's model and read-only permissions. Prompt text never supplies trusted task authority.

## Verification

| Check | Actual evidence |
| --- | --- |
| Official process chain | Regular and isolated desktop instances both directly launched `/Applications/ChatGPT.app/Contents/Resources/codex`. No Python wrapper was between either desktop and its core. |
| Real task list/read | The user started an Opus desktop task; its actual host tool results listed and read that task successfully. |
| Real task message | Opus sent `CLAUDE-COORD-OK` to its own task; the host returned the task ID and recorded delivery. No production task was resumed. |
| Real cross-task wait | The user started Fable 5.1 at Ultra. Its `wait_threads(timeoutMs:0)` returned the Opus task's completed turn, idle status, and no error. The initial self-wait correctly failed because Codex forbids a task waiting on itself. |
| In-app browser | Opus opened `https://example.com` in `iab`, received the page state with heading `Example Domain`, and closed its own tab. The host then returned an empty tab list. |
| Native subagent | The Opus task used native `Agent`. Parent and child transcript model fields both contained only `claude-opus-5-5`; the child used `SubagentHandback`. |
| Fable effort | The task registry stored `ultra`; native session state stored `ultracode`. Native transcript model fields contained only `claude-fable-5-1`. |
| Concurrent sessions | Earlier subscription-backed standalone probes overlapped two Opus sessions. The final protocol test overlaps two independent tasks using fixture inference and verifies distinct selected models. The two desktop smoke tasks were sequential. |
| Side chats | The real bundled core's ephemeral, read-only fork with explicit Fable selection passed fixture inference. Native tools were restricted to Read/Glob/Grep. A parent/model mismatch is refused. Interactive side-chat UI behavior was not separately live-tested. |
| Browser images | The earlier live standalone Chrome probe clicked a local fixture and returned a screenshot image to Claude. Final transport tests preserve inline host images and frame them correctly for later native turns. The final in-app-browser smoke checked page content, not a screenshot. |
| No model fallback | Rejection tests prevent OpenAI/unknown models, attribution mismatches, unauthorized tokens, required host model reviewers, and messages to unverified/differently modeled tasks from invoking inference. Live native transcripts recorded the selected Claude models. No API-key client is present. |
| Retry/cancellation | Tests verify exact host call IDs, image continuations, final-result replay without repeated native work, cancellation on wrong results, and rejection of duplicate HTTP requests without cancelling the original turn. |
| Removal | Tests refuse unrelated directories and running Claude windows, retain history by default, and delete only the marked isolated directory when `--delete-history` is explicit. |

The initial isolated launch used a deeply nested workspace path, exceeding macOS's UNIX socket limit. It had no tasks, was closed, and was replaced with the shorter home above. The launcher now rejects overly long paths before creating state. The replacement socket initialized successfully and the real coordination tests then passed.

## Boundaries

- Claude mode sees its own task history. It does not automatically coordinate regular Codex tasks in another desktop profile.
- Native automatic permission classification is owned by Claude and can use a different Anthropic model. Selected-model pinning covers task/subagent inference.
- Forwarded browser/task calls retain host permissions. The adapter refuses host model-based review requirements; it does not promise every host action will auto-approve.
- Only browser and list/read/wait/message tools are forwarded. Native Claude Agent handles subagents; Codex's host subagent tools are not forwarded.
- Voice is unavailable in Claude mode; regular Codex retains voice. No claim is made that every background feature of the desktop is intercepted by this provider.
- An already-dispatched host action can finish after cancellation. Pending host continuations expire after 120 seconds. Interrupted native requests require a new user message rather than automatic replay.
- No edits were made to the official app bundle, signatures, standard Codex configuration, or standard task providers for this standalone setup.

Official reference: [custom provider and model-catalog configuration](https://learn.chatgpt.com/docs/config-file/config-reference). Local app/CLI behavior, rather than CLI profile documentation alone, established the actual desktop launch path.
