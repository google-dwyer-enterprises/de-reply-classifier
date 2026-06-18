"""Cross-provider LLM cost+quality bench (Anthropic / OpenAI / Gemini).

One call_llm() abstraction over the three SDKs that returns text + token usage +
computed dollar cost, so each feature's existing prompt can be replayed across
providers and compared on cost-per-item and agreement-with-current-output.

Pricing is per 1M tokens (in/out), June 2026 snapshot (see PRICING). Run with no
args for a plumbing smoke test (one trivial call per cheap-tier model).
"""
from __future__ import annotations

import os
import time

from dotenv import load_dotenv
load_dotenv(".env")

# per-1M-token (input, output) USD — June 2026
PRICING = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "gpt-5.4-nano": (0.20, 1.25),
    "gpt-5.4-mini": (0.75, 4.50),
    "gpt-5.4": (2.50, 15.00),
    "gpt-5.5": (5.00, 30.00),
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-3.5-flash": (1.50, 9.00),
    "gemini-3.1-pro-preview": (2.00, 12.00),
}

CHEAP = {
    "anthropic": "claude-haiku-4-5",
    "openai": "gpt-5.4-nano",
    "gemini": "gemini-3.1-flash-lite",
}

# Tier ladder: walk UP from the cheapest until cost exceeds today's Haiku.
# (label, provider, model). "under/over budget" is judged per-feature on measured cost.
LADDER = [
    ("Haiku 4.5 (current)", "anthropic", "claude-haiku-4-5"),
    ("GPT-5.4 nano", "openai", "gpt-5.4-nano"),
    ("GPT-5.4 mini", "openai", "gpt-5.4-mini"),
    ("GPT-5.4", "openai", "gpt-5.4"),
    ("GPT-5.5", "openai", "gpt-5.5"),
    ("Gemini 3.1 Flash-Lite", "gemini", "gemini-3.1-flash-lite"),
    ("Gemini 3.5 Flash", "gemini", "gemini-3.5-flash"),
    ("Gemini 3.1 Pro", "gemini", "gemini-3.1-pro-preview"),
]

_clients: dict = {}


def _anthropic():
    if "anthropic" not in _clients:
        import anthropic
        _clients["anthropic"] = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _clients["anthropic"]


def _openai():
    if "openai" not in _clients:
        from openai import OpenAI
        _clients["openai"] = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    return _clients["openai"]


def _gemini():
    if "gemini" not in _clients:
        from google import genai
        _clients["gemini"] = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    return _clients["gemini"]


def cost(model: str, in_tok: int, out_tok: int) -> float:
    pi, po = PRICING.get(model, (0.0, 0.0))
    return in_tok / 1e6 * pi + out_tok / 1e6 * po


_TRANSIENT = ("503", "502", "500", "504", "429", "unavailable", "overloaded",
              "timeout", "rate limit", "ratelimit", "high demand")


def call_llm(provider: str, model: str, system: str, user: str, max_out: int = 1024) -> dict:
    """Return {text, in_tok, out_tok, cost, ms, error}. Never raises — errors captured.
    Retries up to 4x with backoff on transient (5xx/429/overload) errors."""
    t0 = time.time()
    for attempt in range(4):
        try:
            return _call_once(provider, model, system, user, max_out, t0)
        except Exception as e:
            msg = str(e).lower()
            if attempt < 3 and any(s in msg for s in _TRANSIENT):
                time.sleep(2 ** attempt + 0.5)
                continue
            return {"text": None, "in_tok": 0, "out_tok": 0, "cost": 0.0,
                    "ms": int((time.time() - t0) * 1000), "error": f"{type(e).__name__}: {str(e)[:160]}"}


def _call_once(provider, model, system, user, max_out, t0):
    if True:
        if provider == "anthropic":
            r = _anthropic().messages.create(
                model=model, max_tokens=max_out, system=system,
                messages=[{"role": "user", "content": user}],
            )
            text = "".join(b.text for b in r.content if b.type == "text")
            in_tok, out_tok = r.usage.input_tokens, r.usage.output_tokens

        elif provider == "openai":
            # Responses API; minimal reasoning to keep these classifier-style tasks cheap.
            r = _openai().responses.create(
                model=model, instructions=system, input=user,
                max_output_tokens=max_out, reasoning={"effort": "none"},
            )
            text = r.output_text
            in_tok, out_tok = r.usage.input_tokens, r.usage.output_tokens

        elif provider == "gemini":
            from google.genai import types
            cfg = dict(system_instruction=system, max_output_tokens=max_out)
            if "pro" in model:
                # Pro REQUIRES thinking mode (thinking_budget=0 is rejected). Give output
                # headroom so the answer isn't truncated by thinking tokens; the measured
                # cost then reflects the (billed) thinking tokens — the real cost of Pro here.
                cfg["max_output_tokens"] = max(max_out, 4096)
            else:
                cfg["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
            r = _gemini().models.generate_content(
                model=model, contents=user,
                config=types.GenerateContentConfig(**cfg),
            )
            text = r.text or ""
            um = r.usage_metadata
            in_tok = um.prompt_token_count or 0
            out_tok = (um.candidates_token_count or 0) + (getattr(um, "thoughts_token_count", 0) or 0)
        else:
            raise ValueError(provider)

        return {"text": text, "in_tok": in_tok, "out_tok": out_tok,
                "cost": cost(model, in_tok, out_tok), "ms": int((time.time() - t0) * 1000), "error": None}


if __name__ == "__main__":
    sys_p = "You are a terse assistant. Reply with exactly one word."
    usr = "Classify the sentiment of 'I love this product': positive, negative, or neutral?"
    for prov, model in CHEAP.items():
        r = call_llm(prov, model, sys_p, usr, max_out=2000)
        if r["error"]:
            print(f"{prov:10} {model:24} ERROR {r['error']}")
        else:
            print(f"{prov:10} {model:24} in={r['in_tok']:4} out={r['out_tok']:4} "
                  f"${r['cost']*1000:.4f}/1k  {r['ms']}ms  -> {r['text'][:40]!r}")
