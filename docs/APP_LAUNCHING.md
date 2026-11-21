# Application and website launching

Aurora can open applications and websites from natural chat: "Open Firefox.",
"Launch Cursor.", "Open GitHub.", "Open the Saturday Multiverse website.",
"Open this web address: https://…", "What applications can you open?"

## How it works

Two coordinated paths, both guarded the same way:

1. **Deterministic path (model-free).** Plain requests are parsed by
   `aurora/launcher.py` (`parse_launch_request`) and executed inside
   `ConversationManager._try_launch_request` before any model call. Launching
   therefore works reliably even with models that cannot do native tool
   calling — the model is not involved at all in these turns
   (`source="launch"` in the message history).
2. **Tool path.** Three tools are registered in the standard registry
   (`aurora/conversation/tools.py`) for models that do their own tool calls:
   - `open_website` — a URL or a catalog site name;
   - `launch_application` — a catalog application name;
   - `list_available_applications` — the catalog with availability.

   The model only *names* a catalog entry; `aurora/launcher.py` performs and
   verifies the action (binary availability check → argv-list `Popen` →
   process-alive verification), so a hallucinated command can never run.

Anything that is not a plainly parseable request ("Could you open up about
your feelings?", "open the pod bay doors") goes to the model as ordinary
conversation.

## What can be launched — the catalog

Only entries from the owner-configured catalog (`launcher:` in config.yaml,
defaults in `aurora/config.py`) are launchable. Edit `config.yaml`:

```yaml
launcher:
  enabled: false   # shipped default; set true to allow launches on Aurora's machine
  applications:
    firefox: {command: [firefox], description: Firefox web browser}
    cursor:  {command: [cursor],  description: Cursor code editor}
  websites:
    github: https://github.com
    saturday multiverse: https://your-site.example   # free-form names work
```

Names are matched leniently but conservatively (case/whitespace-normalized,
unique prefix or close match). Ambiguous matches get a "which one?" question
instead of a guess. The dashboard exposes the catalog read-only at
`GET /api/launcher`.

## Which computer? (important)

Aurora acts **only on the machine running the Aurora server** (Portable).
Requests aimed at the dashboard's computer are detected ("open firefox on my
computer", "open this site in my browser here") and **refused honestly** —
nothing is opened anywhere, because opening it on Portable would be the wrong
destination. Opening a page in the *dashboard browser's* machine from the
server is not technically possible today; remote launching would need extra
infrastructure (a small agent on the target machine) and is deliberately
**documented, not implemented**. Aurora's replies state where an action
happened ("on this machine (the one running Aurora)").

## Safety properties

- **Catalog-only execution.** Nothing outside `launcher.applications` /
  `launcher.websites` runs, ever. Unmatched candidates are conversation.
- **No shell.** Commands run as argv lists via `subprocess.Popen` with
  detached stdio; candidates containing shell metacharacters (`;& | $ > …`)
  or path separators are rejected before matching, so config-driven commands
  stay inert and quoted text ("open 'rm -rf /'") can never execute.
- **URL schemes.** Only `http`/`https` URLs are opened (`file:`,
  `javascript:`, … are refused); bare hostnames get `https://` assumed.
- **Explicit intent.** Tool calls require the *current* user utterance to
  ask for it (`has_launch_intent`, quoting-aware) — past topics, proactive
  turns, and the model's own ideas never authorize a launch. This mirrors
  the reminder tools' authorization pattern.
- **Honest errors.** Missing binaries ("not installed on this machine"),
  failed starts, headless machines (no browser session) and disabled
  launching all produce clear refusals, never fake success.

## Tests

`tests/test_launcher.py` (35 tests) covers: parsing (polite forms, URLs,
website/app suffixes, quotes, metacharacters, remote destinations,
conversation fragments), catalog-only execution and fuzzy-match safety,
argv/no-shell verification, scheme validation, availability and failure
reporting, tool authorization gating, the deterministic end-to-end manager
flow (with the real `CompanionApp` on the mock provider), destination
semantics, and the dashboard endpoint.

## Known limitations

- Launching needs a desktop session on Aurora's machine; on a headless
  server, `open_website` reports that no browser could be opened.
- `webbrowser.open` uses the default browser; Aurora does not choose a
  specific browser or window placement.
- No remote/Station launching (see above); no application closing or window
  management — launching only, by design.
