"""Smoke-test for is_in_scope() — block-only WH-question design."""
from __future__ import annotations
import sys
sys.path.insert(0, ".")
from src.utils.scope_guard import is_in_scope

tests = [
    # (message, expected, description)

    # ── MUST BE BLOCKED — WH-questions with no store signal ──────────────────
    ("what is python?",                       False, "off-topic WH 3 words"),
    ("what is python",                        False, "off-topic WH no punct"),
    ("explain how photosynthesis works",      False, "explain WH off-topic"),
    ("who is the prime minister",             False, "who WH off-topic"),
    ("how does photosynthesis work",          False, "how WH off-topic"),
    ("what is the capital of france",         False, "what WH geography"),
    ("tell me about history of india",        False, "tell me WH off-topic"),
    ("define quantum mechanics",              False, "define WH off-topic"),
    ("explain machine learning to me",        False, "explain WH ML off-topic"),
    ("what is cryptocurrency",                False, "what WH crypto"),
    ("who invented electricity",              False, "who WH science"),
    ("why is the sky blue",                   False, "why WH science"),
    ("when did world war 2 end",              False, "when WH history"),
    ("where is eiffel tower located",         False, "where WH geography"),
    ("describe the water cycle",              False, "describe WH science"),
    ("what is the speed of light",            False, "what WH physics"),

    # ── MUST BE ALLOWED — WH-questions with store signal ─────────────────────
    ("what is gst?",                          True,  "what + gst keyword"),
    ("how do i add a product",                True,  "how + product keyword"),
    ("what is my stock balance",              True,  "what + stock + balance"),
    ("who paid yesterday",                    True,  "who + paid keyword"),
    ("how much did ramesh pay",               True,  "how + pay keyword"),
    ("what is total bill amount",             True,  "what + bill + amount"),
    ("explain the khata balance",             True,  "explain + khata keyword"),
    ("how much is outstanding",               True,  "how + outstanding keyword"),
    ("what is ramesh balance",                True,  "what + balance keyword"),
    ("how much did ramesh owe",               True,  "how + owe"),
    ("what is my store gstin",                True,  "what + store + gstin"),
    ("how much credit does naveen have",      True,  "how + credit keyword"),
    ("who are my customers",                  True,  "who + customer keyword"),
    ("what is the total sales today",         True,  "what + sales + total"),

    # ── MUST BE ALLOWED — billing instructions (no keyword, any product name) ─
    ("2 wheat aata add chey",                 True,  "numeric-led billing"),
    ("2 wheat aata",                          True,  "numeric-led product qty"),
    ("3 kg surf excel",                       True,  "numeric-led branded product"),
    ("500 cash",                              True,  "numeric-led payment amount"),
    ("1 horlicks",                            True,  "numeric-led unknown product"),
    ("10 complan",                            True,  "numeric-led brand not in list"),
    ("5 parle g",                             True,  "numeric-led biscuit brand"),
    ("2 lijjat papad",                        True,  "numeric-led regional product"),

    # ── MUST BE ALLOWED — owner instructions without WH opener ───────────────
    ("kottha bill",                           True,  "Telugu: new bill"),
    ("wheat aata add karo",                   True,  "instruction no keyword"),
    ("naveen 500 cash de",                    True,  "customer payment instruction"),
    ("surf excel stock update karo",          True,  "stock instruction"),
    ("horlicks add cheyyi",                   True,  "Telugu add instruction"),
    ("finish bill",                           True,  "finish instruction"),
    ("cancel",                                True,  "single word cancel"),
    ("yes",                                   True,  "yes"),
    ("no",                                    True,  "no"),
    ("done",                                  True,  "done"),
    ("ok",                                    True,  "ok"),
    ("500",                                   True,  "number only"),

    # ── MUST BE ALLOWED — Hindi/Telugu/Tamil phrases ──────────────────────────
    ("ramesh kitna baaki hai",                True,  "Hindi: how much remaining"),
    ("ramesh ka udhar kitna",                 True,  "Hindi: how much credit"),
    ("naveen ka hisab batao",                 True,  "Hindi: show account"),
    ("ramesh yekkuva undi",                   True,  "Telugu: how much remaining"),
    ("ramesh account chupinchu",              True,  "Telugu: show account"),
    ("icchaadu ramesh",                       True,  "Telugu: ramesh paid"),
    ("ramesh kadan pesam",                    True,  "Tamil: ramesh debt amount"),
    ("ramesh evvalavu kadan",                 True,  "Tamil: how much debt"),
    ("hesabu kaatu",                          True,  "Tamil: show account"),

    # ── MUST BE ALLOWED — slash commands ─────────────────────────────────────
    ("/new",                                  True,  "slash command /new"),
    ("/status",                               True,  "slash command /status"),
    ("/history",                              True,  "slash command /history"),
]

passed = 0
failed = 0
for msg, expected, desc in tests:
    result = is_in_scope(msg)
    verdict = "PASS" if result == expected else "FAIL"
    label = "ALLOW" if result else "BLOCK"
    exp_label = "ALLOW" if expected else "BLOCK"
    if result != expected:
        failed += 1
        print(f"  {verdict}  [{label}] expected [{exp_label}]  {msg!r}  ({desc})")
    else:
        passed += 1
        print(f"  {verdict}  [{label}]  {msg!r}  ({desc})")

print(f"\n{passed}/{passed + failed} passed")
sys.exit(0 if failed == 0 else 1)
