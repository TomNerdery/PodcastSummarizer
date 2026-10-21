"""
Tests for claude_call.py: which credential is used, and what happens on errors.

Run from the repo root:
    python3 -m unittest discover -s tests -v

No network and no real credential are needed. The API path is tested with a
stubbed requests.post, and the subscription path with a fake `claude` program
put first on PATH, which records how it was called and answers in the same
JSON shape the real CLI uses.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import claude_call  # noqa: E402
import summarize  # noqa: E402
import tts_elevenlabs  # noqa: E402

FAKE_KEY = "test-only-fake-api-key"
FAKE_TOKEN = "test-only-fake-oauth-token"

FAKE_CLAUDE = textwrap.dedent("""\
    #!/usr/bin/env python3
    import json, os, sys
    prompt = sys.stdin.read()
    with open(os.environ["FAKE_CLAUDE_LOG"], "w") as f:
        json.dump({"argv": sys.argv[1:], "prompt": prompt,
                   "has_api_key": "ANTHROPIC_API_KEY" in os.environ,
                   "has_token": bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")),
                   "max_out": os.environ.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS"),
                   "max_thinking": os.environ.get("MAX_THINKING_TOKENS")}, f)
    mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
    if mode == "ok":
        out = {"type": "result", "is_error": False, "api_error_status": None,
               "stop_reason": "end_turn", "result": "FAKE REPLY to: " + prompt[:20]}
        code = 0
    elif mode == "auth":
        out = {"type": "result", "is_error": True, "api_error_status": 401,
               "result": "Failed to authenticate. API Error: 401 OAuth access token is invalid."}
        code = 1
    elif mode == "limit":
        out = {"type": "result", "is_error": True, "api_error_status": 429,
               "result": "Claude usage limit reached."}
        code = 1
    elif mode == "other":
        out = {"type": "result", "is_error": True, "api_error_status": 500,
               "result": "API Error: 500 Internal server error"}
        code = 1
    elif mode == "garbage":
        sys.stderr.write("something crashed")
        sys.exit(2)
    print(json.dumps(out))
    sys.exit(code)
    """)


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        return self._body


OK_BODY = {"stop_reason": "end_turn",
           "content": [{"type": "thinking", "thinking": "..."},
                       {"type": "text", "text": "API REPLY"}]}


class ClaudeCallTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        bindir = Path(self.tmp.name) / "bin"
        bindir.mkdir()
        fake = bindir / "claude"
        fake.write_text(FAKE_CLAUDE)
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        self.log = Path(self.tmp.name) / "call.json"
        self.base_env = {
            "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
            "FAKE_CLAUDE_LOG": str(self.log),
        }
        # Never let a developer's own .env leak into these tests.
        self.patches = [mock.patch.object(claude_call, "_load_env", lambda: None)]
        for p in self.patches:
            p.start()
        claude_call._announced = False

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def env(self, **extra):
        """A clean environment with only what the test names."""
        return mock.patch.dict(os.environ, {**self.base_env, **extra}, clear=True)

    def call(self):
        return claude_call.complete("hello there, model", model="claude-x",
                                    max_tokens=200, system="be brief")

    def called(self) -> dict:
        return json.loads(self.log.read_text())

    # ------------------------ the four cases ------------------------

    def test_1_token_only_uses_the_cli(self):
        err = io.StringIO()
        with self.env(CLAUDE_CODE_OAUTH_TOKEN=FAKE_TOKEN), \
                mock.patch.object(claude_call.requests, "post") as post, \
                contextlib.redirect_stderr(err):
            self.assertEqual(claude_call.backend(), claude_call.SUBSCRIPTION)
            out = self.call()
        post.assert_not_called()
        self.assertEqual(out, "FAKE REPLY to: hello there, model")
        c = self.called()
        argv = c["argv"]
        self.assertEqual(argv[:2], ["-p", "--model"])
        self.assertEqual(argv[2], "claude-x")
        self.assertIn("--output-format", argv)
        self.assertEqual(argv[argv.index("--output-format") + 1], "json")
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertEqual(argv[argv.index("--system-prompt") + 1], "be brief")
        self.assertNotIn("--bare", argv)
        self.assertEqual(c["prompt"], "hello there, model")   # prompt on stdin
        self.assertTrue(c["has_token"])
        self.assertFalse(c["has_api_key"])
        self.assertEqual(c["max_out"], "200")
        log = err.getvalue()
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", log)
        self.assertNotIn(FAKE_TOKEN, log)

    def test_2_key_only_uses_the_api_unchanged(self):
        err = io.StringIO()
        with self.env(ANTHROPIC_API_KEY=FAKE_KEY), \
                mock.patch.object(claude_call.requests, "post",
                                  return_value=_Resp(200, OK_BODY)) as post, \
                contextlib.redirect_stderr(err):
            self.assertEqual(claude_call.backend(), claude_call.API)
            out = self.call()
        self.assertEqual(out, "API REPLY")
        self.assertFalse(self.log.exists(), "the CLI must not run on the API path")
        url = post.call_args.args[0]
        kw = post.call_args.kwargs
        self.assertEqual(url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(kw["headers"]["x-api-key"], FAKE_KEY)
        self.assertEqual(kw["headers"]["anthropic-version"], "2023-06-01")
        self.assertEqual(kw["json"], {
            "model": "claude-x", "max_tokens": 200, "system": "be brief",
            "messages": [{"role": "user", "content": "hello there, model"}]})
        log = err.getvalue()
        self.assertIn("ANTHROPIC_API_KEY", log)
        self.assertNotIn(FAKE_KEY, log)

    def test_3_both_set_refuses(self):
        with self.env(ANTHROPIC_API_KEY=FAKE_KEY, CLAUDE_CODE_OAUTH_TOKEN=FAKE_TOKEN), \
                mock.patch.object(claude_call.requests, "post") as post:
            with self.assertRaises(SystemExit) as cm:
                self.call()
        post.assert_not_called()
        self.assertFalse(self.log.exists())
        msg = str(cm.exception.code)
        self.assertIn("both", msg)
        self.assertNotIn(FAKE_KEY, msg)
        self.assertNotIn(FAKE_TOKEN, msg)

    def test_4_neither_set_refuses(self):
        with self.env(), mock.patch.object(claude_call.requests, "post") as post:
            with self.assertRaises(SystemExit) as cm:
                self.call()
        post.assert_not_called()
        self.assertIn("no Claude credential", str(cm.exception.code))

    # ------------------------ edges ------------------------

    def test_empty_values_count_as_unset(self):
        with self.env(ANTHROPIC_API_KEY="", CLAUDE_CODE_OAUTH_TOKEN=FAKE_TOKEN), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(claude_call.backend(), claude_call.SUBSCRIPTION)
            self.call()
        self.assertFalse(self.called()["has_api_key"],
                         "an empty ANTHROPIC_API_KEY must not reach the CLI")

    def test_token_without_cli_refuses(self):
        with self.env(CLAUDE_CODE_OAUTH_TOKEN=FAKE_TOKEN, PATH="/nonexistent"):
            with self.assertRaises(SystemExit) as cm:
                claude_call.backend()
        self.assertIn("not on PATH", str(cm.exception.code))

    # --------- summarize.py keeps its error classes on both paths ---------

    def _summary(self):
        with contextlib.redirect_stderr(io.StringIO()):
            return summarize.generate_script("some transcript", "t", "d")

    def test_summary_cli_auth_failure_stops_the_run(self):
        with self.env(CLAUDE_CODE_OAUTH_TOKEN=FAKE_TOKEN, FAKE_CLAUDE_MODE="auth"):
            with self.assertRaises(summarize.SummaryQuotaExceeded):
                self._summary()

    def test_summary_cli_limit_stops_the_run(self):
        with self.env(CLAUDE_CODE_OAUTH_TOKEN=FAKE_TOKEN, FAKE_CLAUDE_MODE="limit"):
            with self.assertRaises(summarize.SummaryQuotaExceeded):
                self._summary()

    def test_summary_cli_other_error_is_one_episode(self):
        with self.env(CLAUDE_CODE_OAUTH_TOKEN=FAKE_TOKEN, FAKE_CLAUDE_MODE="other"):
            with self.assertRaises(RuntimeError) as cm:
                self._summary()
        self.assertNotIsInstance(cm.exception, summarize.SummaryQuotaExceeded)

    def test_summary_cli_crash_is_one_episode(self):
        with self.env(CLAUDE_CODE_OAUTH_TOKEN=FAKE_TOKEN, FAKE_CLAUDE_MODE="garbage"):
            with self.assertRaises(RuntimeError) as cm:
                self._summary()
        self.assertNotIsInstance(cm.exception, summarize.SummaryQuotaExceeded)

    def test_summary_cli_passes_the_system_prompt_and_ceiling(self):
        with self.env(CLAUDE_CODE_OAUTH_TOKEN=FAKE_TOKEN):
            self._summary()
        c = self.called()
        self.assertEqual(c["argv"][2], summarize.SUMMARY_MODEL)
        self.assertEqual(c["max_out"], "16000")
        self.assertIsNone(c["max_thinking"], "the summary keeps the model's own thinking")
        self.assertIn("TRANSCRIPT:\nsome transcript", c["prompt"])
        system = c["argv"][c["argv"].index("--system-prompt") + 1]
        self.assertTrue(system.strip(), "the summary instructions must be sent")

    def test_summary_api_empty_balance_stops_the_run(self):
        body = '{"type":"error","error":{"type":"invalid_request_error",' \
               '"message":"Your credit balance is too low"}}'
        with self.env(ANTHROPIC_API_KEY=FAKE_KEY), \
                mock.patch.object(claude_call.requests, "post",
                                  return_value=_Resp(400, body)):
            with self.assertRaises(summarize.SummaryQuotaExceeded):
                self._summary()

    def test_summary_api_server_error_is_one_episode(self):
        with self.env(ANTHROPIC_API_KEY=FAKE_KEY), \
                mock.patch.object(claude_call.requests, "post",
                                  return_value=_Resp(500, "overloaded")):
            with self.assertRaises(RuntimeError) as cm:
                self._summary()
        self.assertNotIsInstance(cm.exception, summarize.SummaryQuotaExceeded)
        self.assertEqual(str(cm.exception), "Claude error 500: overloaded")

    # --------- casting still falls back to a random voice on failure ---------

    def test_smart_match_cli_reply_is_used(self):
        cands = [{"voice_id": v, "name": v, "description": ""} for v in "abcd"]
        with self.env(CLAUDE_CODE_OAUTH_TOKEN=FAKE_TOKEN), \
                mock.patch.object(claude_call, "_call_cli",
                                  return_value='{"voice_id": "c", "reason": "fits"}'), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(tts_elevenlabs.smart_match("script", cands, "a"), "c")

    def test_smart_match_cli_turns_thinking_off(self):
        cands = [{"voice_id": v, "name": v, "description": ""} for v in "abcd"]
        with self.env(CLAUDE_CODE_OAUTH_TOKEN=FAKE_TOKEN), \
                contextlib.redirect_stderr(io.StringIO()):
            tts_elevenlabs.smart_match("script", cands, "a")
        c = self.called()
        self.assertEqual(c["max_thinking"], "0")
        self.assertEqual(c["max_out"], "200")

    def test_smart_match_cli_failure_falls_back(self):
        cands = [{"voice_id": v, "name": v, "description": ""} for v in "abcd"]
        with self.env(CLAUDE_CODE_OAUTH_TOKEN=FAKE_TOKEN, FAKE_CLAUDE_MODE="auth"), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertIn(tts_elevenlabs.smart_match("script", cands, "a"), set("abcd"))

    def test_reply_parser_is_still_shared(self):
        self.assertIs(summarize.join_text, tts_elevenlabs.join_text)


if __name__ == "__main__":
    unittest.main()
