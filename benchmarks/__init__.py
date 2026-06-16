"""Boundary injection benchmark — a bespoke measurable suite.

The kill-condition pivot from AgentDojo (Item 4): AgentDojo's inspect port has no
defense parameter and does not exercise Boundary's differentiators (staging,
taint). Instead, this suite runs injection tasks through the REAL EnvelopeRunner
(defended) vs the bare Agent loop (undefended) and emits {utility,
utility_under_attack, ASR}. Swap the mock client for a real model to score.
"""
