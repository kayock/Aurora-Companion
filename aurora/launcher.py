"""Application and website launching — the deterministic "open X" path.

Design (mirrors the reminders module):
  * A small, owner-configured catalog is the ONLY thing Aurora can launch:
    `launcher.applications` (name → argv command) and `launcher.websites`
    (name → URL). Nothing outside the catalog is ever executed; the model
    can only name catalog entries, and the app code performs and verifies
    the action itself.
  * Destination semantics are explicit: Aurora acts on the machine running
    the Aurora server (Portable). A dashboard opened from another computer
    cannot be driven from here — requests aimed at the user's own machine
    ("open … on my computer / in my browser here") are refused honestly
    rather than silently executed somewhere else. Remote launching needs
    infrastructure that does not exist yet and is documented, not faked.
  * No shell, ever: commands run as argv lists via subprocess.Popen with
    stdout/stderr detached; names are matched against the catalog and any
    leftover shell metacharacters or path separators in a candidate make
    it unlaunchable rather than dangerous.
  * Intent gating like the reminder tools: a tool call only launches when
    the CURRENT user utterance clearly asks for it — quoted phrases, past
    topics, and the model's own ideas never authorize a launch.

The conversation manager parses plain requests ("open firefox", "open the
Saturday Multiverse website", "what applications can you open?") BEFORE the
model, so launching works reliably even with models that cannot do native
tool calling. The model keeps its full turn whenever the text is not a
plainly parseable launch request.
"""

from __future__ import annotations

import difflib
import re
import shutil
import subprocess
import webbrowser
from dataclasses import dataclass, field
from urllib.parse import urlparse

# Anchored request shapes. Group (1) is the object (possibly a URL).
# Polite forms are part of a clear request; the catalog match itself is the
# authorization — unmatched objects are never executed.
_LEAD = r"(?:hey\s+aurora\s*[,;:]?\s*|aurora\s*[,;:]\s*|aurora\s+(?=open|launch)|could\s+you\s+|can\s+you\s+|would\s+you\s+|please\s+)*"
OPEN_RE = re.compile(rf"^{_LEAD}open\s+(?:up\s+)?(?:the\s+|my\s+)?(.+?)[.!?]?$", re.I)
LAUNCH_RE = re.compile(rf"^{_LEAD}launch\s+(?:the\s+|my\s+)?(.+?)[.!?]?$", re.I)
START_APP_RE = re.compile(rf"^{_LEAD}start\s+(?:the\s+|my\s+)?(.+?\s+(?:app|application|program))[.!?]?$", re.I)
LIST_RE = re.compile(
    r"^what\s+(?:apps?|applications|programs|websites|sites)\s+"
    r"(?:can|could)\s+(?:you|I)\s+(?:open|launch|start)[.!?]?$", re.I)
ASK_URL_RE = re.compile(
    r"^open\s+(?:up\s+)?(?:the\s+|this\s+|that\s+)?(?:web\s+address|website|url|link|site)[.!?]?$", re.I)

# Requests aimed at the user's own machine/browser — a destination Aurora
# cannot reach from the server. Must be refused, not executed on Portable.
REMOTE_TARGET_RE = re.compile(
    r"\b(?:on|from|in|to)\s+(?:my|the|his|her|this)\s+(?:\w+\s+){0,2}"
    r"(?:computer|pc|laptop|notebook|station|desktop|machine|windows|browser)\b"
    r"|\b(?:here|on here)\b", re.I)

URL_RE = re.compile(r"(?:(?:https?://)|(?:www\.))[^\s<>\"']+", re.I)

# Suffix words stripped from a candidate before catalog matching.
_SUFFIX_RE = re.compile(
    r"\s+(?:website|web\s+site|site|webpage|web\s+page|url|link|page|"
    r"app|application|program|software|tool)[.!?]*$", re.I)

_SHELL_META = re.compile(r"[;&|`$><\(\)\[\]\{\}!'\"*?~]")

_HTTP_SCHEMES = ("http", "https")

# Tool-call authorization: the current utterance must clearly ask to open or
# reach something. "start" is deliberately excluded ("start a timer").
LAUNCH_INTENT_RE = re.compile(
    r"\b(?:open|launch|visit|go\s+to|bring\s+up|pull\s+up|navigate|browse)\b"
    r"|\bhttps?://\b|\bwebsite\b|\bsite\b|\bweb\s+address\b|\burl\b", re.I)

_QUOTED_RE = re.compile(r"""['\"“”][^'\"“”]{2,}['\"“”]""", re.S)


def has_launch_intent(text: str) -> bool:
    # Quoted fragments are conversation ("quoting 'open github'"), not intent.
    return bool(text and LAUNCH_INTENT_RE.search(_QUOTED_RE.sub(" ", text)))


LAUNCH_UNAUTHORIZED_MSG = (
    "Not launched: the current message does not clearly ask to open or "
    "launch anything. Applications and websites only start on an explicit "
    "request from the owner.")


@dataclass
class LaunchPlan:
    """A parsed, still-unsigned launch request (resolution happens next)."""
    kind: str                     # "website" | "application" | "list" | "ask_url" | "remote"
    target: str = ""              # raw candidate text or URL
    reason: str = ""              # why it was classified this way


@dataclass
class LaunchResult:
    ok: bool
    message: str
    where: str = "server"         # "server" = the machine running Aurora
    detail: dict = field(default_factory=dict)


def extract_url(text: str) -> str:
    m = URL_RE.search(text)
    return m.group(0).rstrip(".!?)]}'\"") if m else ""


def _normalize(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower().rstrip(".!?")).strip()


def _candidate_from(raw: str) -> str:
    """Strip leading articles, trailing kind words and trailing filler:
    'the Saturday Multiverse website' → 'saturday multiverse'."""
    cand = raw.strip()
    cand = re.sub(r"^(?:the|a|an)\s+", "", cand, flags=re.I)
    cand = _SUFFIX_RE.sub("", cand)
    cand = re.sub(r"\s+(?:now|please|first|then|thanks|thank\s+you)[.!?]*$", "",
                  cand, flags=re.I)
    return _normalize(cand)


def parse_launch_request(text: str) -> LaunchPlan | None:
    """Deterministic classification of an utterance as a launch request.

    Returns None when the text is not a plainly parseable request — the
    model then gets its normal turn. A quoted object that is not itself a
    plain catalog name is deliberately NOT parsed (quoted commands are
    conversation, not instructions).
    """
    text = (text or "").strip()
    if len(text) > 300:
        return None
    if LIST_RE.match(text):
        return LaunchPlan(kind="list")
    m = ASK_URL_RE.match(text)
    if m:
        return LaunchPlan(kind="ask_url")
    m = OPEN_RE.match(text) or LAUNCH_RE.match(text) or START_APP_RE.match(text)
    if m is None:
        return None
    raw = m.group(1).strip()
    # Quoted commands are never instructions ("open 'the pod bay doors'",
    # 'can you "open firefox" in a poem'). Unquoted plain names proceed.
    if re.match(r"""^['\"“”].+['\"“”]$""", raw):
        return None
    if REMOTE_TARGET_RE.search(text):
        # "open github on my computer", "open this site in my browser" —
        # aimed at the user's machine, not at Aurora's; never execute.
        return LaunchPlan(kind="remote", target=raw,
                          reason="request targets the user's own machine")
    url = extract_url(text)
    if url:
        # A URL with a scheme wins outright ("open https://github.com").
        return LaunchPlan(kind="website", target=url)
    cand = _candidate_from(raw)
    if not cand or _SHELL_META.search(cand):
        return None  # not a plain name — never a launch candidate
    if "/" in cand or "\\" in cand:
        return None  # path-like candidates are never launchable
    # a "website"/"site" suffix means the browser even without a URL match;
    # an "app/program" suffix means the application catalog
    if re.search(r"\s+(?:website|web\s+site|site|webpage|web\s+page|url|link|page)\b",
                 raw, re.I):
        return LaunchPlan(kind="website", target=cand)
    if re.search(r"\s+(?:app|application|program)\b", raw, re.I):
        return LaunchPlan(kind="application", target=cand)
    # "open up" / "open about X" conversational fragments are not launches
    if cand in ("up", "about", "it", "that", "this", "them"):
        return None
    return LaunchPlan(kind="unknown", target=cand)


class Launcher:
    """Executes launches from the owner-configured catalog only."""

    def __init__(self, section: dict | None = None, launch_fn=None,
                 browse_fn=None, exists_fn=None) -> None:
        cfg = dict(section or {})
        self.enabled = bool(cfg.get("enabled", True))
        self._applications: dict[str, dict] = {}
        for name, spec in dict(cfg.get("applications", {}) or {}).items():
            if isinstance(spec, list):          # shorthand: firefox: [firefox]
                spec = {"command": spec}
            if isinstance(spec, dict) and spec.get("command"):
                self._applications[_normalize(name)] = {
                    "command": [str(c) for c in spec["command"]],
                    "description": str(spec.get("description", "")),
                }
        self._websites: dict[str, str] = {}
        for name, url in dict(cfg.get("websites", {}) or {}).items():
            if isinstance(url, str) and self._safe_url(url):
                self._websites[_normalize(name)] = url
        # Injection seams for tests (production: real process/browser).
        self._launch_fn = launch_fn or self._popen_launch
        self._browse_fn = browse_fn or (lambda url: webbrowser.open(url))
        self._exists_fn = exists_fn or shutil.which

    # ---------------------------------------------------------------- catalog
    @property
    def applications(self) -> dict[str, dict]:
        return dict(self._applications)

    @property
    def websites(self) -> dict[str, str]:
        return dict(self._websites)

    def available_applications(self) -> list[dict]:
        out = []
        for name, spec in sorted(self._applications.items()):
            ok = all(self._exists_fn(c) for c in spec["command"][:1])
            out.append({"name": name, "description": spec["description"],
                        "available": bool(ok)})
        return out

    def match(self, kind: str, candidate: str) -> tuple[str | None, list[str]]:
        """Catalog lookup with conservative fuzzy matching.
        Returns (exact_or_unique_key, ambiguous_alternatives)."""
        catalog: dict = (self._websites if kind == "website"
                         else self._applications)
        if not candidate:
            return None, []
        cand = _normalize(candidate)
        if cand in catalog:
            return cand, []
        # unique prefix / substring / close match — never guess among many
        hits = [k for k in catalog if k.startswith(cand) or cand in k]
        if len(hits) == 1:
            return hits[0], []
        if len(hits) > 1:
            return None, sorted(hits)
        close = difflib.get_close_matches(cand, list(catalog), n=2, cutoff=0.8)
        if len(close) == 1:
            return close[0], []
        if len(close) > 1:
            return None, sorted(close)
        return None, []

    # ---------------------------------------------------------------- safety
    @staticmethod
    def _safe_url(url: str) -> bool:
        try:
            parsed = urlparse(url)
        except ValueError:
            return False
        if parsed.scheme and parsed.scheme.lower() not in _HTTP_SCHEMES:
            return False
        if not parsed.scheme and "." not in (parsed.path or ""):
            return False
        return bool(parsed.netloc or parsed.path)

    def _ensure_url_scheme(self, url: str) -> str | None:
        url = url.strip()
        if not self._safe_url(url):
            return None
        if "://" not in url:
            url = "https://" + url
        if urlparse(url).scheme.lower() not in _HTTP_SCHEMES:
            return None
        return url

    # ---------------------------------------------------------------- actions
    def _popen_launch(self, command: list[str]) -> int:
        """Start a desktop application detached, without a shell."""
        proc = subprocess.Popen(  # noqa: S603 - argv from the owner's catalog only
            list(command),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )
        return proc.pid

    def launch_application(self, key: str) -> LaunchResult:
        if not self.enabled:
            return LaunchResult(False, "Application launching is disabled in settings.")
        spec = self._applications.get(_normalize(key))
        if spec is None:
            return LaunchResult(False, f"\"{key}\" is not in the application catalog.")
        missing = [c for c in spec["command"][:1] if not self._exists_fn(c)]
        if missing:
            return LaunchResult(
                False, f"{key} is not installed on this machine "
                       f"(command not found: {missing[0]}).",
                detail={"command": spec["command"]})
        try:
            pid = self._launch_fn(spec["command"])
        except Exception as exc:
            return LaunchResult(False, f"Could not start {key}: {exc}",
                                detail={"command": spec["command"]})
        return LaunchResult(
            True, f"Launched {key} on this machine (the one running Aurora).",
            detail={"pid": pid, "command": spec["command"]})

    def open_website(self, url_or_key: str) -> LaunchResult:
        if not self.enabled:
            return LaunchResult(False, "Website opening is disabled in settings.")
        url = self._ensure_url_scheme(url_or_key)
        if url is None:
            catalog_hit, _ = self.match("website", url_or_key)
            if catalog_hit is None:
                return LaunchResult(
                    False, f"\"{url_or_key}\" is not a usable web address "
                           "(only http(s) URLs are opened).")
            url = self._websites[catalog_hit]
        try:
            opened = bool(self._browse_fn(url))
        except Exception as exc:
            return LaunchResult(False, f"Could not open {url}: {exc}",
                                detail={"url": url})
        if not opened:
            return LaunchResult(
                False, "No browser could be opened on this machine — "
                       "it may be running headless (no desktop session).",
                detail={"url": url})
        return LaunchResult(True, f"Opened {url} in the browser on this machine "
                                  "(the one running Aurora).",
                            detail={"url": url})

    # ---------------------------------------------------------------- replies
    def list_reply(self) -> str:
        apps = self.available_applications()
        sites = sorted(self._websites)
        lines = []
        if apps:
            ok = [a for a in apps if a["available"]]
            lines.append("Applications I can open on this machine:")
            lines += [f"- {a['name']}" + (f" — {a['description']}" if a["description"] else "")
                      for a in ok]
            missing = [a["name"] for a in apps if not a["available"]]
            if missing:
                lines.append("(installed in the catalog but not found right now: "
                             + ", ".join(missing) + ")")
        if sites:
            lines.append("Websites I can open on this machine: " + ", ".join(sites))
        if not lines:
            return ("My launch catalog is empty — add applications and websites "
                    "under `launcher:` in config.yaml.")
        return "\n".join(lines)

    def ambiguous_reply(self, kind: str, options: list[str]) -> str:
        label = "websites" if kind == "website" else "applications"
        return (f"Several {label} match that name: " + ", ".join(options) +
                ". Which one did you mean?")

    def remote_reply(self) -> str:
        return ("I can only open things on the machine running me (Portable) — "
                "I can't reach the computer your dashboard is open on. "
                "Remote launching would need extra infrastructure that isn't "
                "set up, so I haven't done anything.")
