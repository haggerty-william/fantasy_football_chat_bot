"""Shared time budgets so Discord waits longer than the local analyst."""
import os

RESEARCH_ROUNDS = 4
CALLS_PER_ROUND = 3
MAX_TOOL_CALLS = RESEARCH_ROUNDS * CALLS_PER_ROUND
MAX_OUTPUT_TOKENS = 1400
MAX_RESPONSE_CHARS = 6000


def seconds(name, default, low, high):
    try: return max(low, min(high, int(os.environ.get(name, str(default)))))
    except ValueError: return default


def analysis_timeout():
    return seconds('AI_ANALYSIS_TIMEOUT_SECONDS', 600, 120, 600)


def request_timeout():
    return seconds('AI_REQUEST_TIMEOUT_SECONDS', 600, 30, 600)


def command_timeout():
    return analysis_timeout() + 60
