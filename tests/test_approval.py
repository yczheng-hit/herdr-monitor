import pytest

from herdr_monitor.approval import Decision, classify

RULE = "─" * 40

CLAUDE_BASH = f"""earlier output
{RULE}
 Bash command

   rm -rf build && make all
   Rebuild artifacts

 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, and don't ask again for make commands in /repo
   3. No

 Esc to cancel · Tab to amend · ctrl+e to explain
"""

CLAUDE_LEGACY = f"""{RULE}
│ Edit file                                   │
│ src/app.py                                  │
│ Do you want to make this edit to app.py?    │
│ ❯ 1. Yes                                    │
│   2. No, and tell Claude what to do differently (esc) │
"""

CODEX = """• Proposed command
  $ git push origin main

Would you like to run the following command?
› 1. Yes, proceed (y)
  2. Yes, and don't ask again for this command (a)
  3. No, and tell Codex what to do differently (esc)

  Press enter to confirm or esc to cancel
"""

CODEX_ALLOW = """  $ curl https://example.invalid
Allow command?
› Yes
  No
"""


def eligible(agent, rule, text, extra=()):
    return classify(agent, rule, text, extra).eligible


@pytest.mark.parametrize("agent,rule,text", [
    ("claude", "bash_permission_prompt", CLAUDE_BASH),
    ("claude", "generic_permission_prompt", CLAUDE_BASH),
    ("claude", "live_blocked_form", CLAUDE_BASH),
    ("claude", "legacy_no_prompt_blocker", CLAUDE_LEGACY),
    ("codex", "live_strong_blocker", CODEX),
    ("codex", "weak_blocker", CODEX_ALLOW),
])
def test_accepts_complete_menu_with_selected_one_time_yes(agent, rule, text):
    decision = classify(agent, rule, text)
    assert decision.eligible and len(decision.fingerprint) == 64


def test_extra_rule_still_requires_menu():
    assert eligible("claude", "custom", CLAUDE_BASH, ("custom",))
    assert not eligible("claude", "custom", CLAUDE_BASH)
    assert not eligible("claude", "custom", "Working…", {"custom"})


def test_direct_rule_name_is_not_sufficient():
    assert not eligible("claude", "bash_permission_prompt", "Do you want to proceed?\n")
    assert not eligible("claude", "bash_permission_prompt", "")


def test_normalizes_ansi_and_controls_with_stable_fingerprint():
    noisy = CLAUDE_BASH.replace("❯ 1. Yes", "\x1b[36m❯\x1b[0m 1.\tYes\x07").replace("\n", "\r\n")
    assert classify("claude", "bash_permission_prompt", noisy) == classify("claude", "bash_permission_prompt", CLAUDE_BASH)


def test_fingerprint_includes_preceding_command_payload():
    other = CLAUDE_BASH.replace("rm -rf build", "rm -rf /")
    a, b = (classify("claude", "bash_permission_prompt", t) for t in (CLAUDE_BASH, other))
    assert a.eligible and b.eligible and a.fingerprint != b.fingerprint
    # Output above the dialog frame is not part of the menu identity.
    moved = CLAUDE_BASH.replace("earlier output", "different history")
    assert classify("claude", "bash_permission_prompt", moved).fingerprint == a.fingerprint
    assert classify("codex", "x", CODEX).fingerprint != classify("codex", "x", CODEX.replace("main", "prod")).fingerprint


def test_reason_never_contains_terminal_text():
    decision = classify("claude", "bash_permission_prompt", CLAUDE_BASH.replace("1. Yes", "1. No"))
    assert isinstance(decision, Decision) and "rm -rf" not in decision.reason and not decision.fingerprint


@pytest.mark.parametrize("mutate", [
    lambda t: t.replace("❯ 1. Yes", "  1. Yes").replace("  3. No", "❯ 3. No"),          # No selected
    lambda t: t.replace("❯ 1. Yes", "  1. Yes").replace("  2. Yes, and", "❯ 2. Yes, and"),  # always allow
    lambda t: t.replace("❯ 1. Yes", "  1. Yes"),                                          # no marker
    lambda t: t.replace("  3. No", "❯ 3. No"),                                            # multiple markers
    lambda t: t.replace("❯ 1. Yes", "> 1. Yes"),                                          # unknown glyph for Claude
    lambda t: t.replace("❯ 1. Yes", "❯ 1. Yes, and don't ask again"),                     # first slot permanent
    lambda t: t.replace("❯ 1. Yes", "❯ 1. Yes, delete everything"),                       # unknown wording
    lambda t: t.replace("  3. No\n", ""),                                                 # no denial
    lambda t: t.replace("  3. No\n", "  3. No\n   4. Maybe\n"),                           # unknown option
    lambda t: t.replace("  2. Yes, and don't ask again for make commands in /repo\n", ""),  # numbering gap
    lambda t: t.replace(" Esc to cancel · Tab to amend · ctrl+e to explain\n", ""),       # no cancel evidence
    lambda t: t.replace("Do you want to proceed?", "Do you want to continue?"),            # unknown question
    lambda t: t + "\n> next instruction\n",                                               # fresh input prompt
    lambda t: t + "\n❯ \n",                                                               # fresh Claude prompt
    lambda t: t + "\nWhich option do you prefer?\n",                                      # later question
    lambda t: t + "\nsome later output\n",                                                # later output
    lambda t: t.replace("rm -rf build && make all", "rm -rf build && make all …"),          # clipped payload
    lambda t: t.replace("Rebuild artifacts", "[... 12 more lines]"),                     # truncated
    lambda t: t.replace("Rebuild artifacts", "```"),                                     # quoted example
    lambda t: t.replace("Bash command", "AskUserQuestion"),                              # wrong UI type
    lambda t: t.replace("Bash command", "Trust the files in this folder"),             # trust dialog
    lambda t: t.replace("   3. No\n", "   3. No\n   Also select model to continue\n"),
    lambda t: t.replace("❯ 1. Yes", "❯ 1. Yes ❯"),                                         # stray marker
    lambda t: "\n".join(["filler"] * 200) + t,                                            # unbounded region
    lambda t: t.replace(RULE + "\n", "") .replace("earlier output", "\n".join(["x"] * 70)),  # unframed payload
])
def test_rejects_unsafe_claude_variants(mutate):
    assert not eligible("claude", "bash_permission_prompt", mutate(CLAUDE_BASH))


@pytest.mark.parametrize("text", [
    CODEX.replace("› 1. Yes, proceed (y)", "  1. Yes, proceed (y)"),
    CODEX.replace("› 1. Yes, proceed (y)", "  1. Yes, proceed (y)").replace("  2. Yes, and", "› 2. Yes, and"),
    CODEX.replace("  3. No, and tell Codex what to do differently (esc)\n", ""),
    CODEX.replace("› 1.", "❯ 1."),
    CODEX.replace("Would you like to run the following command?", "Would you like to continue?"),
    CODEX + "\n› write tests\n",
    CODEX_ALLOW.replace("› Yes", "  Yes"),
    CODEX_ALLOW.replace("  No", "› No"),
    CODEX_ALLOW.replace("› Yes", "› Yes, always"),
    "Would you like to run the following command?\nyes\n",
    "Sign in with ChatGPT?\n› Yes\n  No\n",
])
def test_rejects_unsafe_codex_variants(text):
    assert not eligible("codex", "live_strong_blocker", text)


def test_rejects_old_menu_followed_by_input_and_unsupported_agents():
    stale = CLAUDE_BASH + "\n" + RULE + "\n❯ continue the work\n" + RULE + "\n"
    assert not eligible("claude", "bash_permission_prompt", stale)
    assert not eligible("gemini", "bash_permission_prompt", CLAUDE_BASH)
    assert not eligible("codex", "x", CLAUDE_BASH)
    assert not eligible("claude", "x", CODEX)


def test_quoted_menu_in_ordinary_question_is_rejected():
    text = "Here is an example dialog:\n" + CLAUDE_BASH + "\nShould I explain each option?\n"
    assert not eligible("claude", "live_blocked_form", text)


def test_pure_module_has_no_side_effect_imports():
    import herdr_monitor.approval as approval
    assert {"os", "subprocess", "asyncio"}.isdisjoint(vars(approval))


# Mirrors picc-permission-modes FilePermissionDialog.renderBashPanel / renderPanel.
PI_BASH = f"""older pi output

{RULE}
Bash command

  rm -rf build && make all
  Rebuild artifacts

This command requires approval

Do you want to proceed?
▶ 1. Yes
  2. Yes, and don't ask again for: make *
  3. No

Esc to cancel · Tab to amend · ctrl+e to explain
"""

PI_EDIT = f"""{RULE}
Edit file
src/app.py

- old line
+ new line

Do you want to make this edit to src/app.py?
▶ 1. Yes
  2. Yes, allow all edits during this session (shift+tab)
  3. No

Esc to cancel · Tab to amend
"""

PI_CREATE = PI_EDIT.replace("Edit file", "Create file").replace(
    "Do you want to make this edit to src/app.py?", "Do you want to create this file: src/app.py?")


@pytest.mark.parametrize("text", [PI_BASH, PI_EDIT, PI_CREATE])
def test_pi_permission_panels_accepted_for_self_reported_block(text):
    decision = classify("pi", "self_reported_blocked", text)
    assert decision.eligible and decision.fingerprint


def test_pi_rejects_variants():
    # Selection moved to persistent grant or No.
    moved = PI_BASH.replace("▶ 1. Yes", "  1. Yes").replace("  2. Yes, and", "▶ 2. Yes, and")
    assert not eligible("pi", "self_reported_blocked", moved)
    denied = PI_BASH.replace("▶ 1. Yes", "  1. Yes").replace("  3. No", "▶ 3. No")
    assert not eligible("pi", "self_reported_blocked", denied)
    # Other agents' marker, missing cancel hint, untitled read / Allow-Block dialogs.
    assert not eligible("pi", "self_reported_blocked", PI_BASH.replace("▶", "❯"))
    assert not eligible("pi", "self_reported_blocked", PI_BASH.replace("Esc to cancel · ", ""))
    read = f"{RULE}\n\nRead outside workspace\n\n▶ 1. Yes\n  2. Yes, during this session\n  3. No\n\n↑↓ navigate · enter select · esc cancel · shift+tab allow all\n"
    assert not eligible("pi", "self_reported_blocked", read)
    allow_block = f"{RULE}\n\nAllow tool?\n▶ 1. Allow\n  2. Block\n\nesc cancel\n"
    assert not eligible("pi", "self_reported_blocked", allow_block)
    # Plan review / question dialogs are not permission questions.
    plan = PI_BASH.replace("Do you want to proceed?", "Would you like to proceed with this plan?")
    assert not eligible("pi", "self_reported_blocked", plan)
    # A later pi input prompt makes the menu stale.
    assert not eligible("pi", "self_reported_blocked", PI_BASH + "\n> next prompt\n")


def test_pi_fingerprint_changes_with_command():
    other = PI_BASH.replace("rm -rf build && make all", "make clean")
    assert classify("pi", "self_reported_blocked", PI_BASH).fingerprint != \
        classify("pi", "self_reported_blocked", other).fingerprint
