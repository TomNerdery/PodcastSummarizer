#!/usr/bin/env python3
"""
One way to ask Claude a question, with two possible credentials behind it.

Both Claude calls in this project (the summary in summarize.py and the voice
casting in tts_elevenlabs.py) go through `complete()` here, so the choice of
credential is made in exactly one place.

**The two backends.** Exactly one of these must be set, in the environment or
in .env:

    ANTHROPIC_API_KEY        the default. A metered key from the Anthropic
                             Console, sent straight to the Messages API. This
                             is what the project has always done, unchanged.

    CLAUDE_CODE_OAUTH_TOKEN  opt-in. A token from `claude setup-token`, which
                             bills your own Claude subscription instead of an
                             API key. That token does NOT work on the Messages
                             API; it only works through the Claude Code CLI, so
                             this backend runs the unmodified `claude` binary as
                             a subprocess (`claude -p`) with no tools.

**Both set, or neither, is refused at startup.** Neither is obvious. Both is
the dangerous one: Claude Code gives ANTHROPIC_API_KEY priority over the OAuth
token, so leaving both in place quietly bills the metered key while you
believe you are on your subscription. Nothing would warn you, so this does.

**Never add `--bare` to the CLI call.** It looks like the right flag for
scripted use, but bare mode never reads the OAuth token, so every call would
fail to authenticate.

The token is personal: each operator mints their own with their own
subscription. See the README section "Use your Claude subscription instead of
an API key", and https://code.claude.com/docs/en/legal-and-compliance.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

import requests

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

API_KEY_VAR = "ANTHROPIC_API_KEY"
TOKEN_VAR = "CLAUDE_CODE_OAUTH_TOKEN"

API = "api"
SUBSCRIPTION = "subscription"

_announced = False


class ClaudeError(RuntimeError):
    """A Claude call that came back with an error instead of an answer.

    `account_out` is True when the account itself cannot serve calls (no
    credit, plan limit reached, credential rejected), so every later call will
    fail the same way. summarize.py turns that into SummaryQuotaExceeded, which
    stops the whole run instead of spending one video's retries.
    """

    def __init__(self, status, body: str, account_out: bool = False):
        self.status = status
        self.body = body
        self.account_out = account_out
        super().__init__(f"Claude error {status}: {body}")


def _load_env() -> None:
    # Imported here, not at the top: tts_elevenlabs imports this module, so a
    # top-level import the other way round would be circular.
    from tts_elevenlabs import HERE, load_dotenv
    load_dotenv(HERE / ".env")


def backend() -> str:
    """Which credential is in use: API or SUBSCRIPTION. Exits if it is unclear.

    Prints the credential's NAME once, never its value. Someone reading a bill
    later needs to read that line, not guess.
    """
    global _announced
    _load_env()
    has_key = bool(os.environ.get(API_KEY_VAR))
    has_token = bool(os.environ.get(TOKEN_VAR))
    if has_key and has_token:
        sys.exit(
            f"ERROR: both {API_KEY_VAR} and {TOKEN_VAR} are set.\n"
            f"Set exactly one. With both, Claude Code bills {API_KEY_VAR} (it\n"
            "takes priority), so you would pay per token while thinking you are\n"
            "on your subscription. To use your subscription, remove\n"
            f"{API_KEY_VAR} from the environment AND from .env."
        )
    if not has_key and not has_token:
        sys.exit(
            f"ERROR: no Claude credential. Set exactly one of:\n"
            f'  {API_KEY_VAR}="your_key_here"      (metered API key, the default)\n'
            f'  {TOKEN_VAR}="your_token"   (your own Claude subscription;\n'
            "                                       see the README)\n"
            "in the environment or in the .env file."
        )
    if has_token and not shutil.which("claude"):
        sys.exit(
            f"ERROR: {TOKEN_VAR} is set but the `claude` command is not on PATH.\n"
            "The subscription token only works through Claude Code. Install it\n"
            "(https://code.claude.com/docs) or use the Docker image, which\n"
            "includes it."
        )
    choice = SUBSCRIPTION if has_token else API
    if not _announced:
        if choice == API:
            print(f"Claude credential: {API_KEY_VAR} (metered API)", file=sys.stderr)
        else:
            print(f"Claude credential: {TOKEN_VAR} (Claude subscription, via the "
                  "claude CLI)", file=sys.stderr)
        _announced = True
    return choice


def _max_tokens_error() -> RuntimeError:
    return RuntimeError("Claude hit max_tokens; the reply is truncated. "
                        "Raise max_tokens or ask for less.")


def join_text(body: dict) -> str:
    """The text of a Claude Messages reply, whatever else is in it.

    Every caller here used to read content[0]["text"], which assumes the first
    content block is the answer. It is not: a model with thinking on opens with
    a `thinking` block that has no "text" key at all. That assumption raised
    KeyError on roughly half the calls the moment the summary model moved to
    Sonnet 5, so this lives in one place now and both callers use it.
    """
    if body.get("stop_reason") == "max_tokens":
        raise _max_tokens_error()
    parts = [b.get("text", "") for b in body.get("content", [])
             if b.get("type") == "text"]
    text = "\n".join(p for p in parts if p).strip()
    if not text:
        kinds = ", ".join(sorted({b.get("type", "?") for b in body.get("content", [])}))
        raise RuntimeError(
            f"Claude returned no text (stop_reason={body.get('stop_reason')}, "
            f"blocks=[{kinds or 'none'}])")
    return text


def api_account_is_out(status: int, body: str) -> bool:
    """True when the account is the problem, not the request (API key path).

    **Status alone cannot decide this.** An empty balance arrives as
    `400 invalid_request_error` — the same status as a genuinely malformed
    request — so the message text has to be read. That is unusual enough to be
    worth stating: everywhere else in this codebase a 4xx maps cleanly.

    402 and 429 are included on the same reasoning the ElevenLabs check uses:
    both mean the next call fails too, so burning a retry on them is wrong even
    though a rate limit, unlike an empty wallet, will clear on its own.
    """
    if status in (402, 429):
        return True
    low = body.lower()
    return status == 400 and ("credit balance is too low" in low
                              or "billing" in low)


# Phrases the CLI uses when the subscription cannot serve the call: a rejected
# or expired token, or a plan limit. Deliberately narrow. A bare "limit" would
# also match a max-output-tokens message, which is one request's problem.
_CLI_ACCOUNT_PHRASES = (
    "authenticate", "oauth", "/login", "not logged in", "invalid api key",
    "usage limit", "hit your limit", "limit reached", "rate limit",
    "credit balance", "billing",
)


def cli_account_is_out(status, text: str) -> bool:
    """True when the subscription itself cannot serve calls (CLI path).

    Wider than the API-key check on purpose: an expired or revoked token
    (401/403) fails every later call too, and the token expires after a year,
    so that is a failure this path will actually meet.
    """
    if status in (401, 402, 403, 429):
        return True
    low = text.lower()
    return any(p in low for p in _CLI_ACCOUNT_PHRASES)


def _call_api(prompt: str, model: str, max_tokens: int, system: str | None,
              timeout: int) -> str:
    """Today's path, unchanged: a raw POST to the Messages API."""
    headers = {
        "x-api-key": os.environ[API_KEY_VAR],
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system is not None:
        payload["system"] = system
    resp = requests.post(ANTHROPIC_URL, headers=headers, json=payload, timeout=timeout)
    if resp.status_code != 200:
        body = resp.text[:500]
        raise ClaudeError(resp.status_code, body,
                          api_account_is_out(resp.status_code, body))
    return join_text(resp.json())


def _call_cli(prompt: str, model: str, max_tokens: int, system: str | None,
              timeout: int, thinking: bool = True) -> str:
    """Run the unmodified Claude Code CLI once, with no tools, and read `result`."""
    cmd = [
        "claude", "-p",
        "--model", model,
        "--output-format", "json",
        "--tools", "",                 # no tools: one question, one answer
        # Replace Claude Code's own coding-assistant prompt. An empty one
        # matches the API path, which sends no system prompt for casting.
        "--system-prompt", system or "",
        "--setting-sources", "",       # ignore any user or project settings
        "--strict-mcp-config",         # and any MCP servers they configure
        "--no-session-persistence",
        "--disable-slash-commands",
        # NOT --bare. Bare mode never reads CLAUDE_CODE_OAUTH_TOKEN.
    ]
    env = dict(os.environ)
    # Belt and braces: backend() already refused a set API key, but an empty
    # one, or ANTHROPIC_AUTH_TOKEN, would still outrank the OAuth token.
    env.pop(API_KEY_VAR, None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(max_tokens)
    if not thinking:
        env["MAX_THINKING_TOKENS"] = "0"
    env["DISABLE_AUTOUPDATER"] = "1"
    # An empty working directory, so no CLAUDE.md from wherever this was
    # launched ends up in the prompt.
    with tempfile.TemporaryDirectory(prefix="claude-call-") as cwd:
        try:
            proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                                  env=env, cwd=cwd, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise ClaudeError("timeout", f"claude CLI did not answer in {timeout}s")
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        detail = (proc.stderr or proc.stdout or "").strip()[-500:]
        raise ClaudeError(f"exit {proc.returncode}", detail or "no output",
                          cli_account_is_out(None, detail))
    status = data.get("api_error_status")
    result = str(data.get("result") or "")
    if data.get("is_error") or proc.returncode != 0:
        status = status or f"exit {proc.returncode}"
        raise ClaudeError(status, result[:500], cli_account_is_out(status, result))
    if data.get("stop_reason") == "max_tokens":
        raise _max_tokens_error()
    text = result.strip()
    if not text:
        raise RuntimeError(
            f"Claude returned no text (stop_reason={data.get('stop_reason')})")
    return text


def complete(prompt: str, *, model: str, max_tokens: int,
             system: str | None = None, timeout: int = 120,
             thinking: bool = True) -> str:
    """Ask Claude once and return the reply text.

    `thinking=False` matters on the subscription path only. The API path sends
    no thinking setting, so Haiku answers straight away there; the CLI turns
    thinking on by default, and on a 200-token casting call that thinking used
    the whole ceiling and the call failed (found in a live test). False turns
    it back off, so both paths send the same kind of request.

    Raises ClaudeError (a RuntimeError) when Claude answers with an error; its
    `account_out` says whether every later call will fail too. Network errors
    on the API path still surface as requests exceptions, as they always have.
    """
    if backend() == SUBSCRIPTION:
        return _call_cli(prompt, model, max_tokens, system, timeout, thinking)
    return _call_api(prompt, model, max_tokens, system, timeout)
