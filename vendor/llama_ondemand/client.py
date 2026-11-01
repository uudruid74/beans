# PROVENANCE (mirror of .provenance/ledger.jsonl; ledger wins)
# written-by: unknown (pre-provenance) | 2026-10-09 | 85e8dc6
# modified-by: codex/gpt-5.6-sol | 2026-10-26 | 4f14980 | redirect SSRF fix and provenance block added
# reviewed-by: claude-code/claude-opus-5-5 | 2026-10-26 | APPROVE_WITH_NITS
# reviewed-by: grok-build/grok-4.7 | 2026-10-26 | APPROVE_WITH_NITS
# END PROVENANCE
"""Chat-completion plumbing: parse llama.cpp responses and talk to a server on loopback."""
import ipaddress
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

MAX_RESPONSE_BYTES = 4 * 1_048_576

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.S)


class ClientError(Exception):
    """The model server could not be reached or answered with an unusable body."""


class ContextTooLarge(ClientError):
    """The prompt plus max_tokens does not fit the server context; retrying will not help."""


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class ChatResult:
    text: str
    reasoning: str
    prompt_n: int
    completion_n: int
    prompt_tps: float
    gen_tps: float
    wall_s: float
    finish_reason: str


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def parse_response(raw, wall_s):
    """Turn a llama.cpp /v1/chat/completions body into a ChatResult."""
    try:
        data = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        choice = data["choices"][0]
        message = choice["message"]
    except (UnicodeDecodeError, ValueError, KeyError, IndexError, TypeError) as exc:
        raise ClientError("response is not a chat completion") from exc
    if not isinstance(message, dict):
        raise ClientError("response is not a chat completion")
    text = message.get("content") or ""
    reasoning = message.get("reasoning_content") or ""
    if not isinstance(text, str) or not isinstance(reasoning, str):
        raise ClientError("response content must be text")
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    timings = data.get("timings") if isinstance(data.get("timings"), dict) else {}
    prompt_n = int(_number(usage.get("prompt_tokens")) or _number(timings.get("prompt_n")))
    completion_n = int(_number(usage.get("completion_tokens")) or _number(timings.get("predicted_n")))
    prompt_tps = _number(timings.get("prompt_per_second"))
    gen_tps = _number(timings.get("predicted_per_second"))
    wall_s = max(float(wall_s), 1e-9)
    if not gen_tps and completion_n:
        gen_tps = completion_n / wall_s
    finish = choice.get("finish_reason") if isinstance(choice, dict) else None
    return ChatResult(text, reasoning, prompt_n, completion_n, round(prompt_tps, 2), round(gen_tps, 2),
                      round(wall_s, 3), finish if isinstance(finish, str) else "")


def require_loopback(base_url):
    """Return the normalised base URL, refusing anything but plain http on loopback."""
    parts = urllib.parse.urlsplit(base_url)
    if parts.scheme != "http":
        raise ClientError("base URL must use http on loopback")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ClientError("base URL must not carry credentials, a query or a fragment")
    host = parts.hostname or ""
    if host != "localhost":
        try:
            if not ipaddress.ip_address(host).is_loopback:
                raise ClientError("base URL host must be loopback")
        except ValueError as exc:
            raise ClientError("base URL host must be loopback") from exc
    return base_url.rstrip("/")


class LlamaClient:
    """Direct HTTP client for a llama.cpp server on this machine's loopback."""

    def __init__(self, base_url, model="local", timeout=900.0, opener=None, clock=time.monotonic):
        self.base_url = require_loopback(base_url)
        self.model = model
        self.timeout = timeout
        # No proxy handler: a loopback request must never be routed through a proxy.
        self._open = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _RejectRedirects()).open
        self._clock = clock

    def chat(self, messages, **options):
        body = {"model": self.model, "messages": messages, "stream": False, **options}
        request = urllib.request.Request(self.base_url + "/v1/chat/completions",
                                         data=json.dumps(body).encode("utf-8"),
                                         headers={"Content-Type": "application/json"}, method="POST")
        started = self._clock()
        try:
            response = self._open(request, timeout=self.timeout)
            try:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            finally:
                close = getattr(response, "close", None)
                if close is not None:
                    close()
        except urllib.error.HTTPError as exc:
            if 300 <= exc.code < 400:
                raise ClientError("server redirect refused (HTTP %s)" % exc.code) from exc
            body = exc.read(4096).decode("utf-8", "replace")
            if exc.code == 400 and "context" in body.lower():
                raise ContextTooLarge("prompt exceeds the server context") from exc
            raise ClientError("server answered %s" % exc.code) from exc
        except OSError as exc:
            raise ClientError("request failed: %s" % exc.__class__.__name__) from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ClientError("response exceeds the byte limit")
        return parse_response(raw, self._clock() - started)


def strip_reasoning(text):
    """Drop thinking blocks, including an opening tag the chat template already emitted."""
    text = _THINK_BLOCK.sub("", text)
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1]
    if "<think>" in text:
        text = text.split("<think>", 1)[0]
    return text
