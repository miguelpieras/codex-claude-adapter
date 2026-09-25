# Codex Claude Adapter

Run **Claude Opus 5.5 and Fable 5.1 through your local Claude Code login** in the Codex desktop model picker, while native Codex tasks keep their normal provider.

Experimental, macOS-only, and unofficial. This project is not affiliated with OpenAI or Anthropic. It contains adapter code only; neither product's binaries, credentials, model catalog, nor proprietary prompts are distributed. Their respective access requirements and terms still apply.

**Known desktop limitation:** on the tested macOS desktop build, launching through this Python wrapper prevents the built-in `codex_app` MCP server from passing the desktop's code-signing checks. Task coordination tools such as read, wait and message are then unavailable, including in native Codex tasks. Browser helper startup has been repaired and verified separately; that does not establish task-tool availability. Use the standard launcher below when task coordination is required. The adapter does not bypass the desktop's signature checks.

## What it does

- Adds **Claude Opus 5.5 · Claude Code** and **Claude Fable 5.1 · Claude Code** to the model picker during an opt-in launch.
- Switches idle tasks between native Codex, Opus and Fable while retaining saved conversation history.
- Runs separate Claude sessions for concurrent Claude tasks.
- Uses Claude Code's native tools and subagents. Subagents are pinned to the same selected Claude model.
- Exposes the desktop's configured browser tools to Claude, including page interactions and screenshot results.
- Makes UI side chats inherit the selected Claude model, with read-only tools.
- Restores saved task providers before returning to stock Codex or removing runtime files.

It does **not** patch the app bundle or replace the global Codex provider. Claude inference uses the official local CLI. Inherited API-key/provider overrides are stripped, subscription authentication is checked, and there is no API-key or OpenAI inference fallback in that backend.

## Requirements

- macOS with Codex desktop installed and opened at least once while signed in, so its model catalog is available.
- Python **3.11+**. Only the standard library is used; no `pip install` is needed.
- Claude Code installed and signed into an eligible Claude subscription, with access to the model you select.
- A native Codex global provider. Other custom global providers are not supported.

The implementation has been tested with Codex CLI **0.155.0-alpha.16.4** bundled with the desktop app, and Claude Code **2.1.282**. It uses experimental desktop APIs; compatibility with other versions is not established.

## Start

```sh
git clone https://github.com/miguelpieras/codex-claude-adapter.git
cd codex-claude-adapter
claude auth status
python3 manage.py status
```

Let active tasks finish, then **quit Codex completely** and run:

```sh
python3 manage.py launch
```

Choose **Claude Opus 5.5 · Claude Code** or **Claude Fable 5.1 · Claude Code** in the dropdown. The `Start Codex with Opus.command` launcher does the same thing.

The launcher passes `CODEX_CLI_PATH` to that app process only. Normal Codex launches do not inherit it. Claude default preferences are stored separately from normal Codex settings.

For a one-click Dock icon, run `python3 dock.py install`. This creates **Codex with Opus.app** in this checkout and pins it without opening or stopping Codex. Click it after fully quitting standard Codex. If this adapter is already running, the icon brings Codex forward. If standard Codex is running, it asks you to quit after your tasks finish. Keep the checkout in place while using the icon.

Choose your own icon with `python3 dock.py install --icon /path/to/icon.icns`. Rebuilding the launcher preserves its selected icon.

Remove just the shortcut with `python3 dock.py remove`. Full adapter uninstall also removes this app and its Dock entry. Your original Codex application and Dock icon are retained.

Application discovery checks `/Applications` and `~/Applications` for `Codex.app` or a Codex distribution named `ChatGPT.app`, verifying the Codex bundle identifier. The ordinary ChatGPT app is not supported. Optional overrides:

```sh
export CODEX_ADAPTER_APP='/custom/location/Codex.app'
export CODEX_ADAPTER_CLAUDE="$HOME/.local/bin/claude"
export CODEX_ADAPTER_PYTHON='/path/to/python3'
python3 manage.py launch
```

`CODEX_HOME` is respected. Generated state lives in `$CODEX_HOME/claude-adapter`, or `~/.codex/claude-adapter` by default. Keep this source checkout available while using the adapter. No LaunchAgent or persistent background service is installed.

## Models and effort

Both model entries offer Low, Medium, High, Extra High, Max and Ultra. These selections reach the local Claude CLI as follows:

| Codex choice | Claude Code receives |
| --- | --- |
| Low / Medium / High | `--effort low` / `medium` / `high` |
| Extra High | `--effort xhigh` |
| Max | `--effort max` |
| Ultra | `--effort ultracode` |

Codex controls the visible **Ultra** label. For Claude models it means **Ultracode**, Claude's Extra High reasoning plus automatic dynamic workflows; it is distinct from Max, Claude's deepest reasoning level. The adapter retains the original selection even when Codex normalizes Ultra before making its provider request. Unknown settings fail explicitly instead of silently changing to Medium. Defaults in the catalog are Medium for Opus 5.5 and High for Fable 5.1; an explicit task preference takes precedence.

Model changes start a fresh native Claude session with the saved Codex conversation context. Same-model continuations resume their native session. Concurrent Opus and Fable tasks keep separate settings and subagent model pins. Read-only side chats inherit the model but cannot run Ultracode workflows: select Extra High or Max in those chats.

Ultracode uses Claude's normal automatic permissions and workflow limits. The adapter does not automatically approve a denied workflow or provide Claude's interactive workflow UI. A selected mode is not proof that a particular task needs or has run a workflow.

[Claude documents effort and Ultracode here](https://code.claude.com/docs/en/model-config#adjust-effort-level). [Fable is included within Max plan limits](https://support.claude.com/en/articles/15424964-claude-fable-models-on-your-plan); other tiers can require usage credits. The adapter uses your existing login and never enables credits, buys credits or supplies an API key. Billing still follows your Claude account settings, including any usage credits you already enabled.

## Restore or remove

**Quit Codex first.** Neither command force-stops running work.

```sh
# Restore saved tasks and open stock Codex:
python3 manage.py standard

# Or restore tasks and remove all generated adapter state:
python3 manage.py uninstall
```

The equivalent Finder launchers are `Restore Standard Codex.command` and `Remove Claude Adapter.command`.

Restoration uses the bundled app-server's task APIs, including for archived tasks. It verifies that no saved task still references this provider before removal. If restoration fails, state is retained so you can retry. No backups or snapshots are created, and the task database is never edited directly.

After successful uninstall, delete this source checkout if you no longer want it. The command deliberately does not delete source code, Git history, or sibling folders. Your Codex conversations, ordinary Claude Code transcripts, Claude installation and login are retained.

Normal wrapper shutdown also attempts restoration. **After a crash or forced quit, run `python3 manage.py standard` before opening stock Codex.** Do not delete the checkout or runtime directory first: saved tasks may still require migration.

## Codex app updates

The original Codex app remains unmodified and can use its normal updater. The adapter finds the installed app and its bundled CLI at launch; it does not ship or freeze a separate Codex version.

For the safest update, finish active tasks, quit Codex, then run `python3 manage.py standard` (or **Restore Standard Codex.command**) to restore saved task providers using the currently installed version. Update through the stock app. After the update, quit and use **Codex with Opus** to re-enable the adapter; an updater's automatic restart should not be assumed to preserve the launcher override.

Compatibility with future releases is not guaranteed: the app-server, model catalog and browser interfaces are experimental. A Codex update may require an adapter update before Opus works again. Continue in stock Codex if that happens. Updating Codex does not update this source checkout or its separately installed Claude CLI.

## Tools, permissions and limits

**Claude Code owns native tool execution and approvals.** It runs in `auto` permission mode, without `--dangerously-skip-permissions`. Requests requiring interactive approval are denied and reported because this adapter does not yet display Claude permission prompts. Its automatic permission classifier may be another Anthropic model; the selected model pin applies to the task and native subagents, not that classifier.

Claude normally runs in safe mode with strict MCP configuration. When the browser bridge is available, it uses restricted mode instead: user/project/local settings are ignored, hooks and skills are disabled, and the only explicitly configured MCP server is the browser bridge. Native tools and agents remain available through `--tools default`; restricted mode confines file tools to the working directories. Codex project instructions are supplied as conversation context. Native tool progress appears as commentary; it is not executed a second time by Codex.

Codex's OS sandbox and approval reviewer do **not** govern native Claude tools. Read-only tasks and ephemeral side chats restrict native tools to Read/Glob/Grep. Side-chat editing and subagents are currently unavailable. Choose named Codex permission profiles before starting a Claude task; changing profiles mid-task is unsupported.

### Browser access

The bridge discovers the installed **Unified Computer Use** plugin's `cua_repl.js` and `js_reset` tools for the current task. Their documentation and screenshot/image results pass directly to Claude through MCP. Calls use the bundled app-server's `mcpServer/tool/call` API, attributed to the actual Codex task, turn and selected Claude model. No browser profile or cookies are copied, and no browser debugging connection is opened by the adapter.

Ask Claude to use the **Codex in-app browser**. The embedded browser requires a task attached to the desktop UI. Connected Chrome is also available through the same tools when enabled in Codex. If a browser is unavailable, Claude must report that instead of claiming the work succeeded. The plugin also exposes computer-use APIs; its existing access restrictions still apply.

**Claude's automatic permissions govern requests to this MCP bridge.** Browser-runtime policies remain enforced, but calls do not go through Codex's model-side tool approval loop. The adapter refuses browser access when the host metadata requires OpenAI model-based review; it does not disable that requirement or pretend to be an OpenAI model. Interactive Claude permission prompts remain unsupported.

Each turn receives a separate, short-lived local token. Other tools/servers and caller-supplied task identities cannot be selected through this endpoint. Stop revokes the token; an already-dispatched browser action may still finish and is never automatically retried. Browser tools are unavailable in read-only tasks and side chats. Parallel tasks have separate identities; native subagents within one task share its browser REPL and must coordinate tabs and variable names. Browser contents/screenshots used by Claude are sent to Claude as tool results through the subscription session.

The bridge has no persistent browser service or separate installation. Standard launch and uninstall retain the same restoration behavior.

Other limitations:

- Local interactive text tasks only; no remote-host or scheduled-task integration.
- Other Codex app tools and connectors are not forwarded to Claude.
- Voice is blocked in Claude tasks because it uses OpenAI. Native Codex tasks retain their native route.
- General Codex image/audio attachments are not supported. Claude can inspect local files with its own tools.
- Codex review and compaction commands are not integrated. The advertised Codex context budget is 100k tokens.
- Stop a running turn before changing its provider. A failed/interrupted request is not automatically replayed, to avoid repeating tool side effects.
- The CLI's usage limits still apply. No performance, billing savings, or compatibility beyond the tested versions is promised.

The no-fallback property describes adapter inference routing. It is not a network firewall preventing arbitrary user-requested shell commands from calling other services.

## How it works

```text
Codex desktop (process-local CLI override)
  └─ Python JSON-RPC wrapper
      ├─ native task → bundled Codex → native provider
      └─ Opus task   → task-specific custom provider
                      → authenticated loopback Responses endpoint
                      → local Claude Code session and native tools
                         └─ browser MCP → bundled app-server → installed browser runtime
```

Desktop auxiliary servers and proxy/daemon/schema commands pass directly to the bundled CLI; they do not take the adapter ownership lock. This repairs browser helper startup. The separate desktop task-tools MCP server remains affected by the code-signing limitation above. The wrapper adds a temporary model catalog, explicitly selects the provider on task creation/resume/fork, and reloads an idle task when its provider changes. It verifies the selected provider before sending a turn. A local token authenticates the loopback endpoint. Original model/provider choices are retained for rollback.

Runtime files are private to the local user. They contain session identifiers, request digests, restoration preferences and cached final answers; they are not suitable for publishing. Normal conversation data also remains in each product's own history. No credentials are copied into this repository.

## Development and verification

```sh
# Portable tests; fake Claude executable, no subscription usage:
python3 -m unittest discover -v

# macOS protocol integration with your installed Codex binary.
# Temporary Codex home and fake inference; no real tasks or model calls:
CODEX_ADAPTER_INTEGRATION=1 python3 -m unittest test_adapter.ProtocolTests -v

# Explicit live smoke: consumes Claude subscription usage:
python3 smoke.py
```

The integration test exercises the real app-server protocol for model-picker preferences, Opus/Fable switching, all advertised effort choices, preserved history, side chats, parallel work, concurrent native helper startup, normal shutdown, and crash recovery including archived tasks. Portable tests cover environment isolation, authentication failure, concurrency, cancellation, session continuation and refusal of other models.

The auxiliary-server test checks ordinary app-server RPC with an isolated configuration. It does not test the desktop's `codex_app` MCP transport or its signed-process authorization. That desktop transport currently fails with `missing-code-signing-identity` / `Codex app tools pipe closed` under the wrapper.

Browser transport tests cover task/turn attribution, token isolation and revocation, read-only restrictions, image results, host denials and rejection of calls requiring OpenAI review. Full in-app-browser verification requires an adapter-launched desktop task; isolated app-server tasks do not have an embedded browser panel.

A live subscription-backed Opus session has successfully used Codex's browser tools to open a temporary local page in Chrome, click a button, read a generated value, receive a screenshot and close its test tab. Embedded-panel behavior and browser use by native subagents remain unverified.

Live verification has covered two overlapping Claude sessions, native Read/Write effects in temporary directories, and one native Agent subagent. Main-agent and subagent transcripts identified Opus 5.5. A desktop-launched adapter has passed native browser and app discovery after fixing auxiliary-server routing. Fable 5.1 has answered a bounded live check with `--effort ultracode`; large dynamic workflows and Fable browser actions have not been live-tested. Protocol tests alone do not establish UI compatibility across releases.

## Related project

[`jpm8888/claude-codex-proxy`](https://github.com/jpm8888/claude-codex-proxy) takes a different approach: a Responses API proxy with Codex tool/browser/plugin translation. This adapter focuses on per-task desktop routing, native Claude execution and reversible operation. Both use the local Claude CLI; this project does not include code from that repository.

## License

[MIT](LICENSE), covering this adapter's code only.
