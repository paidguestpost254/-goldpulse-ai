# Find first balanced { ... }
    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object in model output")
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start:i + 1]
                return json.loads(candidate)
    raise ValueError("unbalanced JSON in model output")


def call_gemini(prompt: str, api_key: str, model: str, timeout: int = 45) -> str:
    endpoint = (
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        f"?key={api_key}"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.4,
            "maxOutputTokens": 512,
            "responseMimeType": "application/json",
        },
    }
    r = requests.post(endpoint, json=payload, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as exc:
        raise ValueError(f"unexpected Gemini response: {str(data)[:200]}") from exc


def normalize(raw: dict, price: float, source: str) -> dict:
    direction = str(raw.get("direction", "NEUTRAL")).upper()
    if direction not in ("LONG", "SHORT", "NEUTRAL"):
        direction = "NEUTRAL"
    try:
        conf = float(raw.get("confidence", 0))
    except (TypeError, ValueError):
        conf = 0.0
    conf = max(0, min(100, conf))

    def fnum(k):
        v = raw.get(k)
        if v is None:
            return None
        try:
            return round(float(v), 2)
        except (TypeError, ValueError):
            return None

    sig = {
        "direction": direction,
        "confidence": int(conf),
        "entry": fnum("entry") or round(price, 2),
        "sl": fnum("sl"),
        "tp1": fnum("tp1"),
        "tp2": fnum("tp2"),
        "tp3": fnum("tp3"),
        "rationale": str(raw.get("rationale", ""))[:280],
        "spot_price": round(price, 2),
        "price_source": source,
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }
    return sig


def analyze(api_key: str, model: str) -> dict:
    """Full pipeline: fetch price -> prompt -> Gemini -> normalized signal."""
    started = time.time()
    price, source = fetch_xauusd_price()
    prompt = build_prompt(price, source)
    raw_text = call_gemini(prompt, api_key, model)
    raw = _extract_json(raw_text)
    sig = normalize(raw, price, source)
    sig["elapsed_ms"] = int((time.time() - started) * 1000)
    return sig

python:formatter.py
"""Turn a signal dict into a clean Telegram HTML alert."""
from __future__ import annotations


def _fmt(x) -> str:
    if x is None:
        return "—"
    return f"${x:,.2f}"


def format_signal(sig: dict) -> str:
    d = sig.get("direction", "NEUTRAL")
    conf = sig.get("confidence", 0)
    spot = sig.get("spot_price", 0)

    if d == "LONG":
        headline = "🟢 <b>LONG / BUY (LS)</b>"
        bias = "bullish"
    elif d == "SHORT":
        headline = "🔴 <b>SHORT / SELL (PS)</b>"
        bias = "bearish"
    else:
        headline = "⚪ <b>NEUTRAL — wait &amp; watch</b>"
        bias = "flat"

    lines = [
        f"<b>GOLD (XAUUSD) SIGNAL</b>",
        f"{headline}",
        "",
        f"Spot: <b>{_fmt(spot)}</b>  ·  Bias: {bias}  ·  Confidence: <b>{conf}%</b>",
        "",
    ]
    if d in ("LONG", "SHORT"):
        lines += [
            f"Entry zone: {_fmt(sig.get('entry'))}",
            f"Stop Loss : {_fmt(sig.get('sl'))}",
            f"TP1       : {_fmt(sig.get('tp1'))}",
            f"TP2       : {_fmt(sig.get('tp2'))}",
            f"TP3       : {_fmt(sig.get('tp3'))}",
            "",
        ]
    if sig.get("rationale"):
        lines += [f"<i>{sig['rationale']}</i>", ""]
    lines += [
        f"<i>{sig.get('fetched_at','')}</i>",
        "⏱ GoldPulse AI · not financial advice",
    ]
    return "\n".join(lines)


def format_status(status: dict) -> str:
    return (
        f"Uptime: {status.get('uptime_s',0)}s\n"
        f"Last price: {status.get('last_price')}\n"
        f"Last signal: {status.get('last_direction')}\n"
        f"Auto: {'ON every '+str(status.get('auto_interval_min','?'))+'m' if status.get('auto_on') else 'OFF'}\n"
        f"Analyses run: {status.get('runs',0)}"
    )

python:bot.py
#!/usr/bin/env python3
"""GoldPulse AI — console (CMD) entry point.

Threaded REPL: type commands while an autonomous analyse-and-post loop runs
in the background. Designed to be launched from Windows CMD:

    python bot.py
"""
from __future__ import annotations
import os
import sys
import threading
import time
from datetime import datetime, timezone

from config import get_config
from analyzer import analyze, fetch_xauusd_price, current_session
from formatter import format_signal
import telegram_utils as tg

# ---- Mutable runtime state ----------------------------------------------
_state = {
    "auto_on": False,
    "auto_interval_min": 5,
    "auto_thread": None,
    "stop_event": threading.Event(),
    "last_price": None,
    "last_source": None,
    "last_signal": None,
    "last_signal_at": 0.0,
    "runs": 0,
    "errors": 0,
    "started_at": time.time(),
    "history": [],          # last N signals
    "lock": threading.Lock(),
}

BANNER = r"""
╔══════════════════════════════════════════╗
║   GOLDPULSE AI · XAUUSD Signal Bot       ║
║   Gemini 2.5 Flash + Telegram            ║
╚══════════════════════════════════════════╝
Type `help` for commands. Type `auto 5` to start autonomous mode.
"""


def _log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def _cfg():
    return get_config()


def _require_token():
    cfg = _cfg()
    if not cfg.telegram_bot_token or "paste_your" in cfg.telegram_bot_token:
        _log("No Telegram bot token in .env. Run scripts\\setup.bat (or set TELEGRAM_BOT_TOKEN).")
    if not cfg.gemini_api_key:
        _log("No GEMINI_API_KEY in .env.")


def do_analyze_and_post(force: bool = False) -> dict | None:
    """One full cycle: analyze, dedupe, post. Returns signal or None."""
    cfg = _cfg()
    _require_token()
    _log("Analyzing XAUUSD…")
    try:
        sig = analyze(cfg.gemini_api_key, cfg.gemini_model)
    except Exception as exc:  # noqa: BLE001
        with _state["lock"]:
            _state["errors"] += 1
        _log(f"Analysis failed: {exc}")
        return None

    with _state["lock"]:
        _state["runs"] += 1
        prev_dir = (_state["last_signal"] or {}).get("direction")
        prev_at = _state["last_signal_at"]
        _state["last_price"] = sig["spot_price"]
        _state["last_source"] = sig["price_source"]
        _state["last_signal"] = sig
        _state["history"].append(sig)
        _state["history"] = _state["history"][-15:]

        # De-dup / rate limit: skip posting an OPPOSITE signal too soon,
        # but always post a NEW direction or a strong refresh.
        opposite = (prev_dir in ("LONG", "SHORT")
                    and sig["direction"] in ("LONG", "SHORT")
                    and prev_dir != sig["direction"])
        hours_gap = (time.time() - prev_at) / 3600.0 if prev_at else 999
        if opposite and hours_gap < cfg.min_hours_between_signals and not force:
            _log(f"Opposite signal suppressed ({hours_gap:.1f}h < {cfg.min_hours_between_signals}h)")
            _state["last_signal_at"] = prev_at
            return sig

    # Post
    try:
        text = format_signal(sig)
        tg.send_message(cfg.telegram_bot_token, cfg.telegram_channel, text)
        _log(f"Posted {sig['direction']} signal (conf {sig['confidence']}) → {cfg.telegram_channel}")
        with _state["lock"]:
            _state["last_signal_at"] = time.time()
    except tg.TelegramError as exc:
        _log(f"Telegram post failed: {exc}")
    return sig


def _auto_loop(interval_min: float):
    stop = _state["stop_event"]
    while not stop.is_set():
        do_analyze_and_post()
        stop.wait(interval_min * 60.0)
    _log("Autonomous loop stopped.")


def cmd_auto(parts: list[str]):
    args = [p.lower() for p in parts]
    if args and args[0] == "stop":
        _state["stop_event"].set()
        if _state["auto_thread"] and _state["auto_thread"].is_alive():
            _state["auto_thread"].join(timeout=2)
        _state["auto_on"] = False
        _log("Autonomous mode OFF.")
        return
    minutes = 5.0
    if args:
        try:
            minutes = max(1.0, float(args[0]))
        except ValueError:
            _log("Usage: auto [minutes]")
            return
    if _state["auto_on"]:
        _log(f"Already running every { _state['auto_interval_min']} min. Stop it first.")
        return
    _state["auto_interval_min"] = minutes
    _state["stop_event"].clear()
    t = threading.Thread(target=_auto_loop, args=(minutes,), daemon=True)
    t.start()
    _state["auto_thread"] = t
    _state["auto_on"] = True
    _log(f"Autonomous mode ON — analyzing every {minutes:g} min. Walk away.")


def cmd_analyze(parts):
    do_analyze_and_post(force="--force" in [p.lower() for p in parts])


def cmd_status(parts):
    with _state["lock"]:
        st = {
            "uptime_s": int(time.time() - _state["started_at"]),
            "last_price": _state["last_price"],
            "last_direction": (_state["last_signal"] or {}).get("direction"),
            "auto_on": _state["auto_on"],
            "auto_interval_min": _state["auto_interval_min"],
            "runs": _state["runs"],
            "errors": _state["errors"],
        }
    print(__import__("formatter").format_status(st))
    print(f"Session: {current_session()}")
    print(f"Price source: {_state['last_source']}")


def cmd_history(parts):
    with _state["lock"]:
        hist = list(_state["history"])
    if not hist:
        print("No signals yet this session.")
        return
    for i, s in enumerate(reversed(hist), 1):
        print(f"{i}. [{s['fetched_at']}] {s['direction']} "
              f"conf={s['confidence']} spot=${s['spot_price']} "
              f"SL={s['sl']} TP1={s['tp1']} TP2={s['tp2']} TP3={s['tp3']}")


def cmd_price(parts):
    try:
        p, src = fetch_xauusd_price()
        with _state["lock"]:
            _state["last_price"] = p
            _state["last_source"] = src
        _log(f"XAUUSD ${p:,.2f}  (source {src})  Session {current_session()}")
    except Exception as exc:  # noqa: BLE001
        _log(f"price fetch failed: {exc}")


def cmd_reset(parts):
    with _state["lock"]:
        _state["last_signal_at"] = 0.0
    _log("Cooldown reset — next opposite signal will post immediately.")


HELP = """
Commands:
  analyze [--force]   analyze now and post a signal
  auto [min]          start autonomous mode (default 5 min)
  auto stop           stop autonomous mode
  status              show uptime, last price/signal, auto state
  history             list signals from this session
  price               fetch current XAUUSD spot
  session             current FX session info
  reset cooldown      allow immediate opposite-direction post
  quit | exit         clean exit
"""


def main():
    cfg = _cfg()
    print(BANNER)
    # Warm-up: validate tokens exist & grab price once
    if not cfg.telegram_bot_token or "paste_your" in cfg.telegram_bot_token:
        _log("Heads up: set TELEGRAM_BOT_TOKEN in .env (run scripts\\setup.bat).")
    cmd_price([])

    while True:
        try:
            line = input("\ngold> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        parts = line.split()
        head = parts[0].lower()
        rest = parts[1:]
        try:
            if head in ("quit", "exit"):
                break
            elif head == "help":
                print(HELP)
            elif head == "analyze":
                cmd_analyze(rest)
            elif head == "auto":
                cmd_auto(rest)
            elif head == "status":
                cmd_status(rest)
            elif head == "history":
                cmd_history(rest)
            elif head == "price":
                cmd_price(rest)
            elif head == "session":
                print(current_session(datetime.now(timezone.utc)))
            elif head == "reset":
                cmd_reset(rest)
            else:
                _log(f"Unknown command '{head}'. Type `help`.")
        except Exception as exc:  # noqa: BLE001
            _log(f"error: {exc}")

    if _state["auto_on"]:
        _state["stop_event"].set()
    _log("Bye. GoldPulse AI stopped.")


if __name__ == "__main__":
    main()

bash:scripts/setup.bat
@echo off
REM One-time GoldPulse AI setup (Windows). Creates .env, then asks for your
REM BotFather token, then AUTO-CREATES the Telegram channel and prints its @name.
cd /d "%~dp0.."

where python >nul 2>nul || (echo Python not found. Install Python 3.9+ and re-run. & pause & exit /b 1)

echo === Installing dependencies ===
python -m pip install --quiet -r requirements.txt || (echo pip install failed & pause & exit /b 1)

echo.
echo === Writing .env (keeps your Gemini key) ===
python - <<PY
from config import get_config
cfg = get_config()
if not cfg.gemini_api_key:
    cfg.gemini_api_key = "AQ.Ab8RN6LTZv9OPMdq6Xxf-Ehcace8lgiSO45qgAJaYpyMiDlD0g"
cfg.write_default_env({"GEMINI_API_KEY": cfg.gemini_api_key})
print("Wrote .env with your Gemini key.")
PY

echo.
echo Paste your BotFather token (from https://t.me/BotFather, /newbot):
set /p BTOKEN=Token:

python - <<PY
import os
os.environ["TELEGRAM_BOT_TOKEN"] = %BTOKEN%.strip().strip('"')
import telegram_utils as tg
from config import get_config

cfg = get_config()
tok = os.environ["TELEGRAM_BOT_TOKEN"]

me = tg.get_me(tok)
print("Bot ready:", me.get("username"))

# Create the channel
try:
    ch = tg.create_channel(tok, "GoldPulse AI", "goldpulse_ai_signals",
                           "Auto gold (XAUUSD) LS/PS signals w/ TP")
    uname = "goldpulse_ai_signals"
except tg.TelegramError as e:
    print("createChannel note:", e)
    uname = "goldpulse_ai_signals"

chat = tg.get_chat_by_username("@"+uname, tok)
print("Channel created. Its address is:", "@"+uname)

# Persist the channel into .env
cfg.write_default_env({
    "GEMINI_API_KEY": cfg.gemini_api_key or "",
    "TELEGRAM_BOT_TOKEN": tok,
    "TELEGRAM_CHANNEL": "@"+uname,
})
print("Saved .env. Now run:  python bot.py")
PY

echo.
pause

bash:scripts/setup.sh
#!/usr/bin/env bash
# One-time setup (mac/linux mirror of setup.bat)
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m pip install --quiet -r requirements.txt
python3 - <<'PY'
from config import get_config
cfg = get_config()
if not cfg.gemini_api_key:
    cfg.gemini_api_key = "AQ.Ab8RN6LTZv9OPMdq6Xxf-Ehcace8lgiSO45qgAJaYpyMiDlD0g"
cfg.write_default_env({"GEMINI_API_KEY": cfg.gemini_api_key})
print("Wrote .env with your Gemini key.")
PY
read -rp "Paste your BotFather token: " BTOKEN
export TELEGRAM_BOT_TOKEN="$BTOKEN"
python3 - <<PY
import os
import telegram_utils as tg
from config import get_config
cfg = get_config()
tok = os.environ["TELEGRAM_BOT_TOKEN"]
me = tg.get_me(tok); print("Bot ready:", me.get("username"))
uname = "goldpulse_ai_signals"
try:
    tg.create_channel(tok, "GoldPulse AI", uname, "Auto gold (XAUUSD) LS/PS signals w/ TP")
except tg.TelegramError as e:
    print("createChannel note:", e)
tg.get_chat_by_username("@"+uname, tok)
cfg.write_default_env({"GEMINI_API_KEY": cfg.gemini_api_key or "",
                       "TELEGRAM_BOT_TOKEN": tok,
                       "TELEGRAM_CHANNEL": "@"+uname})
print("Saved .env. Run:  python bot.py")
PY

.gitignore
.env
__pycache__/
*.pyc
.venv/
venv/
.DS_Store

python:tests/test_analyzer.py
import unittest
from analyzer import _extract_json, normalize, build_prompt, current_session
from datetime import datetime, timezone


class TestExtractJson(unittest.TestCase):
    def test_plain_object(self):
        obj = _extract_json('{"a":1,"b":2}')
        self.assertEqual(obj, {"a": 1, "b": 2})

    def test_with_fences_and_prefix(self):
        txt = "Sure! Here is:\n
obj = _extract_json(txt)
        self.assertEqual(obj["direction"], "LONG")

    def test_nested(self):
        obj = _extract_json('{"x":{"y":[1,2,3]}}')
        self.assertEqual(obj["x"]["y"], [1, 2, 3])

    def test_garbage_raises(self):
        with self.assertRaises(ValueError):
            _extract_json("no braces here")


class TestNormalize(unittest.TestCase):
    def test_long_clamps_confidence(self):
        raw = {"direction": "LONG", "confidence": 150, "entry": 2600.5,
               "sl": 2590.1, "tp1": 2610, "tp2": 2620, "tp3": 2630, "rationale": "x"}
        sig = normalize(raw, 2601.0, "test")
        self.assertEqual(sig["direction"], "LONG")
        self.assertEqual(sig["confidence"], 100)
        self.assertAlmostEqual(sig["entry"], 2600.5)
        self.assertIn("fetched_at", sig)

    def test_neutral_defaults(self):
        raw = {"direction": "NEUTRAL", "confidence": 20}
        sig = normalize(raw, 2650.0, "test")
        self.assertIsNone(sig["sl"])
        self.assertIsNone(sig["tp1"])

    def test_bad_direction_coerced(self):
        raw = {"direction": "SIDEWAYS"}
        sig = normalize(raw, 2650.0, "test")
        self.assertEqual(sig["direction"], "NEUTRAL")


class TestPrompt(unittest.TestCase):
    def test_prompt_contains_key_bits(self):
        p = build_prompt(2650.0, "unit")
        self.assertIn("XAUUSD", p)
        self.assertIn("2650.00", p.replace("$", "").replace("$", ""))
        self.assertIn("JSON", p)
        self.assertIn("tp3", p)

    def test_session_is_a_string(self):
        self.assertIsInstance(current_session(datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)), str)


if __name__ == "__main__":
    unittest.main()

python:tests/test_formatter.py
import unittest
from formatter import format_signal


class TestFormatter(unittest.TestCase):
    def test_long_signal(self):
        sig = {"direction": "LONG", "confidence": 72, "entry": 2600.0,
               "sl": 2590.0, "tp1": 2610.0, "tp2": 2620.0, "tp3": 2630.0,
               "rationale": "Breakout.", "spot_price": 2601.0,
               "fetched_at": "2025-01-01 00:00 UTC"}
        out = format_signal(sig)
        self.assertIn("LONG", out)
        self.assertIn("TP1", out)
        self.assertIn("2,610.00", out)
        self.assertIn("Stop Loss", out)

    def test_short_signal(self):
        sig = {"direction": "SHORT", "confidence": 55, "entry": 2700.0,
               "sl": 2710.0, "tp1": 2690.0, "tp2": 2680.0, "tp3": 2670.0,
               "rationale": "", "spot_price": 2699.0, "fetched_at": ""}
        out = format_signal(sig)
        self.assertIn("SHORT", out)
        self