"""Check direct team-metric claims against the supplied completed-game sample.

This is deliberately limited to unambiguous wording and resolved subjects. It
does not infer causes or assess an opinion from a bag of statistics.
"""

import math
import re


_NUMBER = r'[-+]?\d+(?:\.\d+)?'
_OPPONENT_MARGIN = re.compile(
    r'\boutscor(?:e|es|ed|ing)\s+(?:(?:their|its|the)\s+)?opponents?\s+by\s+'
    r'(?P<value>' + _NUMBER + r')\s+(?:fantasy\s+)?points?\s+(?:per game|a game|on average)\b', re.I)
_AVERAGE_MARGIN = re.compile(
    r'\baverage (?:scoring |winning )?margin\s*(?:of|is|was|:)?\s*'
    r'(?P<value>' + _NUMBER + r')\s+points?\b', re.I)
_LEAGUE_GAP = re.compile(
    r'(?P<value>' + _NUMBER + r')\s+points?(?:\s+(?:per game|a game|on average))?\s+'
    r'(?P<direction>above|below|better than|more than|less than)\s+(?:the\s+)?league(?:[- ]wide)? average\b', re.I)
_RANK = re.compile(
    r'\b(?:scoring\s+)?rank(?:ed|s)?\s*(?:(?:of|at|is|was)\s+)?#?(?P<rank>\d+)(?:st|nd|rd|th)?\b|'
    r'\b(?P<ordinal>\d+)(?:st|nd|rd|th)\s+(?:in|for)\s+(?:league\s+)?scoring\b', re.I)
_DEFENSE_CONTROL = re.compile(
    r'\b(?:stabili[sz]e|improve|fix|tighten(?: up)?|shore up|strengthen)\s+'
    r'(?:(?:the|their|its|his|her)\s+)?defen[cs]e\b|'
    r'\bplay\s+(?:better\s+)?(?:fantasy\s+)?defen[cs]e\b|'
    r'\b(?:reduce|limit|lower|cut|control)\s+(?:(?:the|their|its)\s+)?'
    r'(?:opponents?[\u2019\x27]?\s+(?:points|scores|scoring)|points against)\b', re.I)
_DST = re.compile(r'\b(?:D/ST|DST|defense/special teams|defen[cs]ive slot)\b', re.I)
_ROSTER_MOVE = re.compile(r'\b(?:start|bench|stream|add|drop|pick up|swap|replace|trade)(?:s|ed|ing)?\b', re.I)


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _different(claim, expected):
    return _finite(expected) and abs(float(claim) - expected) > .011


def _sentences(text, names_pattern):
    # A team such as "Sports Team!!" may contain sentence punctuation.
    spans = [m.span() for m in names_pattern.finditer(text)] if names_pattern else []
    start = 0
    for boundary in re.finditer(r'(?<=[.!?])\s+|\n+', text):
        if '\n' not in boundary.group() and any(a <= boundary.start() - 1 < b for a, b in spans):
            continue
        yield text[start:boundary.start()]
        start = boundary.end()
    yield text[start:]


def _rank_issues(sentence, performance):
    # A generic rank might be a seed or a power rank; require scoring language.
    if not re.search(r'\b(?:scoring|points per game|points-per-game)\b', sentence, re.I):
        return []
    claims = list(_RANK.finditer(sentence))
    if not claims:
        return []
    weeks = _specific_weeks(sentence)
    if len(set(weeks)) > 1:
        return []  # A comparison of several weeks needs more precise binding.
    if weeks:
        game = next((row for row in performance.get('recent_games', []) if row.get('week') == weeks[0]), {})
        expected = game.get('scoring_rank')
        issue = 'weekly_scoring_rank_mismatch'
    else:
        expected = performance.get('points_per_game_rank')
        issue = 'sample_scoring_rank_mismatch'
    return [issue] if any(_different(m.group('rank') or m.group('ordinal'), expected) for m in claims) else []


def _specific_weeks(sentence):
    return [int(m.group(1)) for m in re.finditer(r'\bweek\s+(\d+)\b', sentence, re.I)
            if not re.search(r'\b(?:through|after|as of)\s*$', sentence[:m.start()], re.I)]


def _defense_control(sentence):
    if _DST.search(sentence) and _ROSTER_MOVE.search(sentence):
        return False  # A real fantasy D/ST roster slot is a different subject.
    for match in _DEFENSE_CONTROL.finditer(sentence):
        before = sentence[:match.start()]
        if re.search(r"\b(?:cannot|can't|won't|doesn't|don't|not|never)\b(?:\W+\w+){0,4}\W*$", before, re.I):
            continue
        return True
    return False


def check_team_performance(text, context):
    """Return stable issue names for explicit contradictions, not missing data."""
    if not isinstance(context, dict):
        return []
    rows = context.get('league_history', {}).get('teams', [])
    performance = {row['team'].replace('\u2019', "'").casefold(): row['performance']
                   for row in rows if isinstance(row.get('team'), str) and isinstance(row.get('performance'), dict)}
    if not performance:
        return []
    names = {row['team'].replace('\u2019', "'") for row in rows if isinstance(row.get('team'), str)}
    # Include teams without samples so that mentioning one clears a pronoun's
    # association with the previous, measured team.
    names.update(row['name'].replace('\u2019', "'") for row in context.get('fantasy_teams', [])
                 if isinstance(row.get('name'), str))
    names_pattern = re.compile(r'(?<!\w)(?:' + '|'.join(re.escape(n) for n in sorted(names, key=len, reverse=True))
                               + r')(?!\w)', re.I) if names else None
    problems = set()
    clean = str(text).replace('\u2019', "'").replace('**', '')
    for paragraph in re.split(r'\n\s*\n', clean):
        active = None
        for sentence in _sentences(paragraph, names_pattern):
            if _defense_control(sentence):
                problems.add('unsupported_opponent_scoring_control')
            mentioned = {m.group().casefold() for m in names_pattern.finditer(sentence)} if names_pattern else set()
            # Compare both sides of a direct scoring-rank tie, including a
            # leading pronoun referring to the previous sentence's sole team.
            tie = re.search(r'\btied\s+(?:for|at|in)\b[^,;.!?]{0,70}\b(?:scoring|points per game)\b', sentence, re.I)
            if tie and not re.search(r"\b(?:not|never|aren't|isn't)\s*$", sentence[:tie.start()], re.I):
                tied = set(mentioned)
                if active and re.match(r'^\s*(?:(?:While|Although|But|Yet)\s+)?(?:they|their|it|its)\b', sentence, re.I):
                    tied.add(active)
                # Weekly comparisons and sample comparisons have different scopes.
                weeks = re.findall(r'\bweek\s+(\d+)\b', sentence, re.I)
                ranks = [performance.get(name, {}).get('points_per_game_rank') for name in tied]
                if len(tied) >= 2 and not weeks and all(_finite(rank) for rank in ranks):
                    if len(set(ranks)) > 1 or ('top spot' in tie.group().casefold() and ranks[0] != 1):
                        problems.add('scoring_rank_tie_mismatch')
            if len(mentioned) == 1:
                active = next(iter(mentioned))
            elif mentioned or not re.match(r'^\s*(?:(?:But|Yet|Still|So)\s+)?(?:They|Their|It|Its)\b', sentence, re.I):
                active = None
            data = performance.get(active)
            if not data:
                continue
            problems.update(_rank_issues(sentence, data))
            if _specific_weeks(sentence):
                # Weekly margins/gaps and the multiweek average are distinct.
                # This bounded check only verifies the latter.
                continue
            for pattern in (_OPPONENT_MARGIN, _AVERAGE_MARGIN):
                if any(_different(m.group('value'), data.get('average_margin')) for m in pattern.finditer(sentence)):
                    problems.add('opponent_average_margin_mismatch')
            for match in _LEAGUE_GAP.finditer(sentence):
                direction = -1 if match.group('direction').casefold() in ('below', 'less than') else 1
                if _different(float(match.group('value')) * direction, data.get('points_per_game_vs_league_average')):
                    problems.add('league_average_gap_mismatch')
    return sorted(problems)
