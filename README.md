# Claude Code in Codex Desktop

Run **Claude Opus 5.5** and **Claude Fable 5.1** in a separate Codex desktop mode, using your authenticated local Claude Code subscription. Regular Codex keeps its normal settings, tasks, voice, and model picker.

The standalone service leaves the official Codex app and its app-server process chain intact. Claude owns its native tools, automatic permissions, and native Agent subagents. Browser and selected task tools return through Codex's ordinary Responses tool loop.

**macOS, experimental.** The current desktop smoke passed real task list/read/wait/message calls, the in-app browser, and a native Opus subagent. Fable Ultra was verified as Claude `ultracode`. See [verification and boundaries](STANDALONE.md).

## Start

Requires an installed Codex desktop app, Python 3.11+, and Claude Code logged in with a supported Claude subscription. Run `claude` and `/login` if needed. The tested Claude Code version is 2.1.282.

```sh
python3 claude_mode.py launch
```

Or double-click **Start Standalone Claude.command**. **Start Codex with Opus.command** opens the same new mode. A previously installed orange **Codex with Opus** launcher also uses standalone mode after updating this checkout's `dock.py`.

To install the optional Dock launcher:

```sh
python3 dock.py install --icon /path/to/icon.icns
```

An existing installed icon is preserved when `--icon` is omitted. The launcher is an applet; the actual running window belongs to the official Codex app.

To also color the running Claude window's Dock icon, close Claude mode and run:

```sh
python3 app_icon.py install --icon /path/to/icon.icns
```

This opt-in feature makes a local copy at `~/.codex-claude/appearance/Claude Codex.app` and applies a normal macOS Finder custom icon. It preserves the signed executable, resources, app identity, and nested helpers; normal deep signature verification passes. Finder's icon metadata does fail `codesign --strict`'s extra metadata check. The installed app is untouched. A Dock restart may be needed to refresh its cached icon.

Continue starting Claude with the launcher: a pinned copy launched directly after quitting does **not** retain the isolated Claude environment. The launcher and running app remain separate Dock items. Remove only the optional copy with `python3 app_icon.py remove`; this retains chats and the launcher.

Claude mode uses `~/.codex-claude/home` for task/configuration state and `~/.codex-claude/desktop` for desktop state. It can run alongside regular Codex. Its tasks and settings are separate. No login credentials, browser profiles, or regular Codex task history are copied. The desktop initializes its own bundled plugins.

Select Opus or Fable and the desired effort in that window. **Extra High** maps to `xhigh`, **Max** to `max`, and **Ultra** to Claude `ultracode`. Read-only side chats cannot use Ultracode because it needs workflow tools.

An existing regular Codex task cannot be switched into this isolated mode through its model dropdown. Continue the work in a Claude-mode task with the context you want to provide.

## Subscription and model pinning

The service checks that local Claude Code is authenticated with `claude.ai`, a first-party provider, and an eligible subscription. It strips inherited API/provider overrides and never adds an Anthropic API client, API key, or OpenAI inference fallback. A billing amount reported by Claude's CLI is not evidence of a separate API charge; account usage limits and any user-enabled extra usage still apply.

Main inference and native Agent subagents are pinned to the selected Claude model. Recorded parent and child transcripts have verified Opus 5.5; Fable's transcript verified Fable 5.1. Native automatic permission classification is controlled by Claude Code and may use another Anthropic model. It is not covered by the task-model pin.

Unknown models, missing attribution, and requests requiring an OpenAI host approval reviewer are rejected. This routing guarantee is not a network firewall preventing an explicitly requested shell command from contacting another service.

## Tools and permissions

- Native Claude tools and native Agent subagents run in Claude's `auto` permission mode, without `--dangerously-skip-permissions`. Interactive Claude approval prompts are unsupported; denials are reported.
- Codex's OS sandbox does not contain native Claude tools. The runtime uses restricted settings, disables hooks/skills, and supplies only the configured relay MCP server. Read-only tasks use only Read/Glob/Grep, without browser access or native subagents.
- The relay exposes `cua_repl.js`/`js_reset` and task `list_threads`, `read_thread`, `wait_threads`, and `send_message_to_thread` when the host provides them. Other Codex connectors and task-management tools are not currently forwarded.
- Forwarded tools execute through the official Codex core and its permission checks. Host approvals may still require user input. Required model-based review is refused instead of silently invoking an OpenAI reviewer or bypassing a policy.
- Messages can start work only in a local Claude-mode task already using the same selected model. Remote or differently modeled targets are refused. Reading and waiting do not start inference.
- Separate Claude tasks can run concurrently. Native subagents within one task share that task's browser REPL and must coordinate tabs and variable names.
- In-app browser access requires a desktop-owned task. Screenshot/image results reach Claude through the subscription session. Browser policies remain in force; a denial is not permission to switch to a private browser interface.

Read-only side chats retain their parent's Claude model. The service checks that attribution and rejects mismatches. Editing from a side chat is not implemented.

## Stop, restore, or remove

To stop native Claude sessions and the local service:

```sh
python3 claude_mode.py stop
```

**Stop Standalone Claude.command** does the same. Regular Codex remains usable. To stop the service and open regular Codex, use **Restore Standard Codex.command**, or:

```sh
python3 claude_mode.py standard
```

There is no LaunchAgent or startup item. Opening the Claude launcher starts the service again. Its loopback address and local token persist in the private profile so reopening the service does not invalidate an existing desktop window.

To remove the launcher and stop the service, first close the Claude-mode window. Regular Codex may remain open:

```sh
python3 claude_mode.py remove
```

Both **Remove Standalone Claude.command** and **Remove Claude Adapter.command** retain Claude-mode history. To permanently delete that isolated history and settings as well, explicitly run:

```sh
python3 claude_mode.py remove --delete-history
```

Removal checks the directory's ownership marker and refuses to delete an unrelated directory or one still used by the Claude desktop instance. Source code is deliberately retained; you may delete this checkout afterwards. Your Claude CLI, login, ordinary Claude transcripts, and regular Codex conversations are untouched.

**Migrating from the older wrapper:** restore old saved providers once using `python3 manage.py standard` with Codex closed. The new standard command also detects tasks still needing that migration. The old wrapper and its restoration code remain in this checkout for recovery, but the launchers no longer select it. If you want to remove its legacy runtime too, use `python3 manage.py uninstall` with Codex closed; it preserves conversation history and refuses unknown runtime files.

## Updates and limits

Update the official Codex app normally after finishing work and closing both instances. The adapter does not patch the app bundle or freeze a separate application version. If the optional icon copy no longer matches the installed app, the launcher uses the updated official app with the same Claude profile; its icon may revert. Re-run the icon install command to create a matching copy. Start Claude mode through its launcher after updating; do not assume an updater restart preserves the isolated launch environment. Future app-server, model-catalog, and browser-interface compatibility is not guaranteed. Regular Codex remains available if an adapter update is needed.

Voice is unavailable in Claude mode. Use regular Codex for voice. Audio/file attachments, Codex review/compaction commands, remote tasks, and scheduled work are not integrated. Inline image attachments are supported; remote image URLs are not fetched. The advertised Codex context budget is 100k tokens.

Failed/interrupted native requests are not automatically replayed, avoiding repeated tool side effects. An active HTTP disconnect stops native execution. When Codex is executing a forwarded tool, a missing continuation expires after 120 seconds; an already-dispatched host action may still finish. Stop the service to cancel all native sessions immediately.

## Development

```sh
# Local tests, fixture inference:
python3 -m unittest discover -v

# Include the installed official Codex protocol; no paid model calls:
CODEX_ADAPTER_INTEGRATION=1 python3 -m unittest discover -v
```

The legacy `smoke.py` performs real subscription-backed inference. Do not run it expecting a free fixture test. The [standalone report](STANDALONE.md) distinguishes protocol fixtures from real desktop and Claude results.

The tool-forwarding pattern in [jpm8888/claude-codex-proxy](https://github.com/jpm8888/claude-codex-proxy) was useful. This implementation keeps native Claude tools and automatic permissions and was written independently; no source from that repository was copied.

[MIT license](LICENSE) applies to this adapter's code only.
