"""Pure, fail-closed permission-menu recognition.

This is a UI-type classifier, not a command safety review: an eligible menu may
still run a destructive command. Reasons are fixed strings that never echo
terminal content.
"""

from dataclasses import dataclass
import hashlib
import re
import unicodedata


@dataclass(frozen=True)
class Decision:
    eligible: bool
    reason: str
    fingerprint: str = ""


_CLAUDE_RULES = frozenset({"bash_permission_prompt", "generic_permission_prompt",
                           "live_blocked_form", "legacy_no_prompt_blocker"})
_MAX_LINES = 160
_MAX_PAYLOAD = 60
_MAX_MENU = 30
# Documented cursor glyphs: Claude's claude.toml uses "❯"; Codex renders "›";
# pi's picc-permission-modes FilePermissionDialog renders "▶".
_SELECTED = {"claude": "❯", "codex": "›", "pi": "▶"}

_PERMISSION = {
    "claude": re.compile(
        r"(?:do you want to|would you like to) (?:proceed|allow\b.+|"
        r"make (?:this|these) edits?(?: to .+)?|create\b.+|edit\b.+)\?", re.I),
    "codex": re.compile(
        r"would you like to (?:run the following command|make the following edits|"
        r"apply(?: the following| these)? (?:changes|edits|patch))\?|allow command\?", re.I),
    # Only pi's fixed-question panels (Bash, edit, create); untitled read and
    # Allow/Block dialogs, plan review and select/input prompts have no such question.
    "pi": re.compile(
        r"do you want to (?:proceed|make this edit to .+|create this file: .+)\?", re.I),
}
_QUESTION = re.compile(r".*\?\s*$")
_OPTION = re.compile(r"(?P<marker>[❯›▶>])?\s*(?:(?P<number>\d+)[.)]\s*)?(?P<label>(?:yes|no)\b.*)", re.I)
_MARKED = re.compile(r"[❯›▶>]")
_NUMBERED = re.compile(r"(?:[❯›▶>]\s*)?\d+[.)]\s")
_ONCE = frozenset({"yes", "yes, proceed", "yes, allow once", "yes, run it", "yes, make the edits",
                   "yes, apply the changes", "yes, apply"})
_PERSISTENT = re.compile(r"don't ask|do not ask|always|session|remember|allow all|from now on", re.I)
_NO = re.compile(r"no(?:, (?:and tell (?:claude|codex) what to do differently|cancel))?", re.I)
_LEGACY_DENY = re.compile(r"no, and tell claude what to do differently", re.I)
_HOTKEY = re.compile(r"\s*\((?:[a-z]|esc|shift\+tab)\)$", re.I)
_CANCEL = re.compile(r"\besc(?:ape)?\s+to\s+cancel\b", re.I)
_HINT = re.compile(r"(?:press )?(?:enter|esc|tab|ctrl\+\w|↑|↓|↑/↓|up/down|use arrow keys)\b[^?]*", re.I)
_CLIPPED = re.compile(r"\b(?:truncated|output omitted|clipped|\d+ more lines?|lines? hidden)\b|"
                      r"(?:\.\.\.|…)\s*$|^\s*(?:\.\.\.|…)", re.I | re.M)
_NON_PERMISSION = re.compile(r"askuserquestion|\btrust\b|sign[ -]?in|log[ -]?in|authenticat|"
                             r"select (?:a )?model|choose an option|enter to select", re.I)
_ESCAPE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-_]")
_BOX = re.compile(r"^[│┃║|]\s?|\s?[│┃║|]$")
_RULE = re.compile(r"[─━┄┈═╌╍┌┐└┘╭╮╰╯┬┴┼┤├\-\s]{3,}")


def _normalize(text: str) -> list[str]:
    text = _ESCAPE.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(c for c in text if c in "\n\t" or not unicodedata.category(c).startswith("C"))
    lines = [_BOX.sub("", re.sub(r"[\t ]+", " ", line).strip()).strip() for line in text.split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return lines


def classify(agent: str, rule: str, text: str,
             extra_rules: set[str] | tuple[str, ...] = ()) -> Decision:
    """Accept only a complete latest permission menu whose unique selection is one-time Yes."""
    def reject(reason: str) -> Decision:
        return Decision(False, reason)

    if agent not in _PERMISSION:
        return reject("unsupported agent")
    if agent == "claude" and rule not in _CLAUDE_RULES and rule not in set(extra_rules):
        return reject("unsupported detection rule")
    if not isinstance(text, str) or not text.strip() or len(text) > 512 * 1024:
        return reject("missing or oversized evidence")
    lines = _normalize(text)
    if len(lines) > _MAX_LINES:
        return reject("evidence exceeds bounded region")
    questions = [i for i, line in enumerate(lines) if _QUESTION.match(line)]
    if not questions:
        return reject("no permission question")
    start = questions[-1]
    if not _PERMISSION[agent].fullmatch(lines[start]):
        return reject("latest question is not a known permission question")

    # The payload is everything since the dialog boundary (rule, box edge, or earlier
    # prompt) so a changed command produces a different fingerprint. Claude dialogs
    # are always framed; Codex shows its payload after the question, so a capped
    # context above it only affects de-duplication.
    first, framed = start, False
    while first > 0 and start - first < _MAX_PAYLOAD:
        previous = lines[first - 1]
        if _RULE.fullmatch(previous) or _MARKED.match(previous):
            framed = True
            break
        first -= 1
    if agent in {"claude", "pi"} and first > 0 and not framed:
        return reject("permission payload exceeds bounded region")
    region, menu = lines[first:start], lines[start + 1:]
    if len(menu) > _MAX_MENU:
        return reject("permission menu exceeds bounded region")
    payload = "\n".join(region)
    if "```" in payload or "~~~" in payload:
        return reject("quoted permission example")
    if _CLIPPED.search("\n".join(region + lines[start:])):
        return reject("evidence is clipped")
    if _NON_PERMISSION.search("\n".join(region + lines[start:])):
        return reject("non-permission interaction")

    options, positions, preamble = [], [], True
    for index, line in enumerate(menu):
        match = _OPTION.fullmatch(line)
        if match and (preamble or index == positions[-1] + 1):
            preamble = False
            options.append(match)
            positions.append(index)
        elif match:
            return reject("permission options are not contiguous")
        elif _MARKED.match(line) or _NUMBERED.match(line):
            return reject("unknown option or later input prompt")
        elif not preamble and line and not (_RULE.fullmatch(line) or _HINT.fullmatch(line)):
            return reject("unexpected output after permission menu")
        elif preamble and _QUESTION.match(line):
            return reject("later question before options")
    if not options:
        return reject("permission options are incomplete")
    trailer = "\n".join(menu[positions[-1] + 1:])
    glyphs = sum(line.count("❯") + line.count("›") + line.count("▶") + line.startswith(">")
                 for line in lines[start:])
    selected = [o for o in options if o["marker"]]
    if glyphs != len(selected) or len(selected) != 1 or selected[0]["marker"] != _SELECTED[agent]:
        return reject("permission menu requires exactly one known selection marker")
    numbers = [o["number"] for o in options]
    if any(numbers) and numbers != [str(i) for i in range(1, len(options) + 1)]:
        return reject("permission options are incomplete")
    labels = [_HOTKEY.sub("", o["label"]).strip() for o in options]
    if len(labels) not in (2, 3) or not _NO.fullmatch(labels[-1]) or any(_NO.fullmatch(x) for x in labels[:-1]):
        return reject("permission menu lacks one explicit final denial")
    if len(labels) == 3 and not (labels[1].lower().startswith("yes") and _PERSISTENT.search(labels[1])):
        return reject("unknown permission option")
    if agent == "claude" and not (_CANCEL.search(trailer) or _LEGACY_DENY.fullmatch(labels[-1])):
        return reject("permission menu lacks cancellation evidence")
    if agent == "pi" and not (_CANCEL.search(trailer) and labels[-1].lower() == "no"):
        return reject("permission menu lacks cancellation evidence")
    if selected[0] is not options[0] or labels[0].lower() not in _ONCE:
        return reject("selected option is not a known one-time Yes")

    canonical = "\n".join([agent, rule, *region, *lines[start:]])
    return Decision(True, "one-time Yes selected in a complete permission menu",
                    hashlib.sha256(canonical.encode("utf-8")).hexdigest())
