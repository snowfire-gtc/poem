"""LLM-backed generation of the chronicle's literary texts (§13 step 22).

The simulation core stays deterministic (design.md §4.1): template phrases in
``chronicle_report`` remain the canonical, reproducible rendering.  This
module adds an *optional* prose layer: when an LLM provider is configured the
server asks it to rewrite the raw turn facts into fresh verses / epics /
laments / chronicle prose in the style of the world's chronicles.

Guarantees honoured here:

* **Off by default.**  Without ``POEM_LLM_ENABLED=1`` (or an explicit
  ``LlmConfig(enabled=True)``) nothing outside the stdlib is touched and no
  network call is ever made — reports stay byte-identical to the template
  rendering.
* **Deterministic prompts.**  A prompt is built only from stable game data
  (civilization/war/law ids, names, game-year values, genres).  Wall-clock
  time never enters (§20.4), so the same finalized turn always produces the
  same prompt and therefore the same cache key.
* **Caching.**  Generated texts are memoised by ``sha256(prompt)``; a second
  request for identical facts costs zero tokens and returns the identical
  text, preserving cross-engine determinism (§ test_ruler_reports).
* **Graceful degradation.**  Any provider failure, timeout, empty or unsafe
  answer falls back to the deterministic template text — the turn pipeline
  can never be broken by the LLM being down.
* **Thin client untouched** (ui-client.md principle 1): all generation is
  server-side; clients keep displaying shipped texts verbatim.

Provider transports (only the stdlib ``urllib`` is used — the package has no
hard dependencies):

* ``openai``    — OpenAI-compatible ``POST {base_url}/chat/completions``
  (works with OpenAI, Azure-style gateways, Ollama, llama.cpp-server, vLLM…).
* ``http``      — same endpoint shape under a fully custom ``base_url``.
* ``command``   — local CLI bridge: the prompt is piped to the configured
  command's stdin and its stdout is taken as the answer (e.g. ``llama-cli``).

Environment configuration (see guide_server.md):

    POEM_LLM_ENABLED=1
    POEM_LLM_PROVIDER=openai|http|command
    POEM_LLM_MODEL=...
    POEM_LLM_API_KEY=...            (or POEM_LLM_API_KEY_FILE=/run/secrets/…)
    POEM_LLM_BASE_URL=https://api.openai.com/v1
    POEM_LLM_COMMAND=...            (provider=command)
    POEM_LLM_TIMEOUT=20
    POEM_LLM_STYLE=chronicle        (extra style hint appended to the system
                                     prompt)
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass

ENV_PREFIX = "POEM_LLM"

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_TIMEOUT = 20.0


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _env(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(f"{ENV_PREFIX}_{name}")
    return val if val not in (None, "") else default


def _truthy(val: str | None) -> bool:
    return str(val or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class LlmConfig:
    enabled: bool = False
    provider: str = "openai"          # openai | http | command
    model: str = ""
    api_key: str = ""
    base_url: str = DEFAULT_BASE_URL
    command: tuple[str, ...] = ()     # argv for provider == "command"
    timeout: float = DEFAULT_TIMEOUT
    style: str = ""                   # extra style hint for the system prompt

    @classmethod
    def from_env(cls) -> "LlmConfig":
        cfg = cls(
            enabled=_truthy(_env("ENABLED")),
            provider=(_env("PROVIDER") or "openai").strip().lower(),
            model=_env("MODEL") or "",
            base_url=(_env("BASE_URL") or DEFAULT_BASE_URL).rstrip("/"),
            timeout=float(_env("TIMEOUT") or DEFAULT_TIMEOUT),
            style=_env("STYLE") or "",
        )
        key = _env("API_KEY")
        if not key:
            key_file = _env("API_KEY_FILE")
            if key_file and os.path.isfile(key_file):
                key = open(key_file, encoding="utf-8").read().strip()
        cfg.api_key = key or ""
        cmd = _env("COMMAND")
        cfg.command = tuple(cmd.split()) if cmd else ()
        return cfg


# ---------------------------------------------------------------------------
# Prompt building — pure function of stable game facts
# ---------------------------------------------------------------------------

SYSTEM_PROMPT_TEMPLATE = (
    "Ты — придворный летописец игрового мира «Поэма». Пиши на русском языке.\n"
    "Задача: превратить сухие факты одного игрового хода в короткий "
    "литературный текст жанра «{genre}» ({kind_desc}) от лица правителя "
    "цивилизации «{civ}».\n"
    "Жёсткие правила:\n"
    "{rules}"
    "- Обратные кавычки и экранирование не нужны; текст должен читаться "
    "как готовая глава хроники.\n"
    "{style}"
)

KIND_DESCRIPTIONS = {
    "wars": "эпическое сказание о войне или плач по изнурённой земле",
    "peace": "лирическое слово о заключённом мире",
    "breakthroughs": "стихи о науке, ремесле и смене эпох",
    "discoveries": "проза странствий: известия разведчиков и послов",
    "unrest": "предостережение правителю о нарастающем ропоте народа",
    "laws": "летописная проза об указах, отменах и корнях закона",
    "folklore": "песня или сказ, которые поют на улицах",
}

_DEFAULT_RULES = (
    "- Только художественный текст: без пояснений, заголовков и кавычек "
    "вокруг всего ответа.\n"
    "- Не более четырёх строк; строки разделяй переводом строки.\n"
    "- Называй указанные имена и числа ровно так, как они даны в фактах; "
    "не выдумывай новых имён и не переводи счёт времени на ходы — только "
    "годы и сезоны.\n"
)


def build_prompt(genre: str, kind: str, facts: dict, *, civ_label: str,
                 style: str = "") -> str:
    """Compose the deterministic user+system prompt pair for one section.

    ``facts`` must contain only stable game data (ids, names, years).  The
    result is a single string so that its sha256 is a valid cache key.
    """
    rules = _DEFAULT_RULES
    style_line = f"- Дополнительный стиль: {style}\n" if style else ""
    system = SYSTEM_PROMPT_TEMPLATE.format(
        genre=genre, kind_desc=KIND_DESCRIPTIONS.get(kind, kind),
        civ=civ_label, rules=rules, style=style_line)
    user = "Факты хода:\n" + json.dumps(facts, ensure_ascii=False,
                                        sort_keys=True, default=str)
    return system + "\n=====\n" + user


def prompt_key(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Sanitising model output
# ---------------------------------------------------------------------------


def sanitize(text: str) -> str:
    """Normalise a model answer into plain chronicle lines."""
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    # strip a whole-answer wrapping quote pair
    if len(t) >= 2 and t[0] in "«\"'" and t[-1] in "»\"'":
        t = t[1:-1].strip()
    lines = [ln.strip() for ln in t.splitlines()]
    lines = [ln for ln in lines if ln][:4]
    return "\n".join(lines)


def looks_safe(text: str) -> bool:
    """Reject answers that clearly are not a literary piece."""
    if not text or len(text) > 2000:
        return False
    lowered = text.lower()
    if lowered.startswith(("извините", "i can't", "as an ai", "как языковая")):
        return False
    return True


# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------


class LlmTransportError(RuntimeError):
    pass


def _post_json(url: str, payload: dict, headers: dict, timeout: float) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise LlmTransportError(f"LLM HTTP request failed: {exc}") from exc


def _chat_completion(cfg: LlmConfig, prompt: str) -> str:
    system, _, user = prompt.partition("\n=====\n")
    payload = {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.7,
        "max_tokens": 300,
    }
    headers = {}
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"
    data = _post_json(f"{cfg.base_url}/chat/completions", payload,
                      headers, cfg.timeout)
    try:
        return str(data["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError) as exc:
        raise LlmTransportError(f"unexpected LLM response shape: {exc}") \
            from exc


def _command_bridge(cfg: LlmConfig, prompt: str) -> str:
    if not cfg.command:
        raise LlmTransportError("LLM command provider: POEM_LLM_COMMAND unset")
    exe = cfg.command[0]
    if shutil.which(exe) is None:
        raise LlmTransportError(f"LLM command not found: {exe}")
    try:
        proc = subprocess.run(list(cfg.command), input=prompt.encode("utf-8"),
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              timeout=cfg.timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LlmTransportError(f"LLM command failed: {exc}") from exc
    if proc.returncode != 0:
        raise LlmTransportError(f"LLM command exited {proc.returncode}")
    return proc.stdout.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Provider façade with caching
# ---------------------------------------------------------------------------


class LlmTextProvider:
    """Ask an LLM for a literary rewrite; cache; fall back on any failure."""

    def __init__(self, config: LlmConfig | None = None,
                 transport=None, cache: dict[str, str] | None = None):
        self.config = config or LlmConfig.from_env()
        self._transport = transport          # callable(cfg, prompt) -> str
        self._cache: dict[str, str] = cache if cache is not None else {}
        self._lock = threading.Lock()
        self.calls = 0                        # diagnostics for tests/server

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled)

    def _default_transport(self) -> str:
        p = self.config.provider
        if p in ("openai", "http"):
            return lambda cfg, prompt: _chat_completion(cfg, prompt)
        if p == "command":
            return lambda cfg, prompt: _command_bridge(cfg, prompt)
        raise LlmTransportError(f"unknown LLM provider '{p}'")

    def generate(self, genre: str, kind: str, facts: dict, *,
                 civ_label: str, fallback: str) -> str:
        """Return an LLM-written text for these facts, or ``fallback``.

        Never raises: every error path degrades to the deterministic
        template text so that turn finalisation cannot fail because of the
        LLM.
        """
        if not self.enabled:
            return fallback
        prompt = build_prompt(genre, kind, facts, civ_label=civ_label,
                              style=self.config.style)
        key = prompt_key(prompt)
        with self._lock:
            cached = self._cache.get(key)
        if cached is not None:
            return cached
        try:
            transport = self._transport or self._default_transport()
            raw = transport(self.config, prompt)
            text = sanitize(raw)
            if not looks_safe(text):
                raise LlmTransportError("LLM answer rejected by safety filter")
        except Exception:
            return fallback
        with self._lock:
            self.calls += 1
            self._cache[key] = text
        return text


_default_provider: LlmTextProvider | None = None
_default_lock = threading.Lock()


def get_default_provider() -> LlmTextProvider:
    """Process-wide provider initialised lazily from the environment."""
    global _default_provider
    with _default_lock:
        if _default_provider is None:
            _default_provider = LlmTextProvider()
        return _default_provider


def set_default_provider(provider: LlmTextProvider | None) -> None:
    """Install (or reset with ``None``) the process-wide provider."""
    global _default_provider
    with _default_lock:
        _default_provider = provider
