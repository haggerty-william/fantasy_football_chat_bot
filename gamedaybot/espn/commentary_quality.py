"""Conservative editorial checks, separate from factual commentary validation.

These catch obvious table narration and stock filler, not every weak opinion.
An unfamiliar substantive sentence is deliberately allowed: deterministic word
matching cannot judge humor or establish that a causal explanation is true.
"""

import re


_STOP_WORDS = set("""a an and are as at be been both by currently for from has have
in is it its of on or that the their these they this to was were with while
team teams league standings points record records score scores""".split())
_FILLER = re.compile(
    r"\b(?:maintain(?:ing)? (?:their |the )?momentum|keep(?:ing)? (?:their |the )?momentum|"
    r"fight(?:ing)? for (?:position(?:ing)?|a playoff spot)|"
    r"look(?:ing)? to (?:bounce back|turn things around)|"
    r"(?:difficult|tough|long) (?:path|road) (?:to|ahead)|"
    r"(?:anything can happen|only time will tell|every (?:game|week) counts)|"
    r"(?:remain|remains|will be) (?:a team|teams) to watch)\b"
)
_TABLE_NARRATION = re.compile(
    r"\b(?:top seed|number (?:one|two|three|four|five|six|seven|eight|nine|ten)|"
    r"(?:leading|leads?|atop) (?:the )?(?:league|standings|table)|"
    r"(?:top|bottom|middle) of (?:the )?(?:standings|table|pack)|"
    r"(?:holds?|sits? (?:at|in)|occup(?:y|ies)) (?:the )?(?:\w+ )?(?:seed|place|spot|position)|"
    r"(?:tied|perfect|undefeated|winless|record)|"
    r"(?:outside|inside) (?:the )?mathematical bounds|games? back)\b"
)
_SCORE_NARRATION = re.compile(
    r"\b(?:scored|posted|finished with|won|beat|defeated|lost|leads?|trails?|"
    r"projected to (?:score|win|lose))\b"
)
_INTERPRETATION = re.compile(
    r"\b(?:because|despite|driven by|thanks to|hinges? on|explains?|suggests?|"
    r"all[- ]play|schedule luck|points against|opponent scoring|"
    r"bench(?:ed)? points|left .* on the bench|hindsight|"
    r"target share|snap share|workload|roster depth|"
    r"(?:not enough|insufficient|missing) (?:data|evidence)|"
    r"(?:cannot|can't) (?:explain|tell|judge)|"
    r"(?:small|limited) sample)\b"
)


def _clean(text):
    text = re.sub(r"\[([^\]]+)\]\(https?://[^)]+\)", r"\1", str(text or ""))
    return text.replace("\u2019", "'").replace("**", "").casefold()


def _tokens(text):
    return set(re.findall(r"[a-z]+(?:'[a-z]+)?|\d+(?:\.\d+)?", text)) - _STOP_WORDS


def _team_names(context):
    if not isinstance(context, dict):
        return []
    rows = context.get('fantasy_teams', [])
    canonical = context.get('teams', {})
    if isinstance(canonical, dict):
        rows = [*rows, *canonical.values()]
    return [_clean(row['name']) for row in rows
            if isinstance(row, dict) and isinstance(row.get('name'), str) and row['name']]


def check_analysis_value(text, report, report_type, context=None):
    """Return stable issue names for clearly redundant trailing commentary.

    A report remains independently readable; its commentary need not enumerate
    its rows. Require several anchored restatements before rejecting narration,
    and allow a useful explanation to quote whatever numbers it needs. Factual
    support (including playoff finality) is checked elsewhere.
    """
    clean = _clean(text)
    source = _clean(report)
    source_tokens = _tokens(source)
    names = _team_names(context)
    sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+|\n+', clean) if s.strip()]
    restatements = 0
    filler = 0
    substantive = 0
    for sentence in sentences:
        tokens = _tokens(sentence)
        # Causal/analytical vocabulary merely exempts a sentence here. It is
        # never treated as proof that the explanation is grounded or correct.
        if _INTERPRETATION.search(sentence):
            substantive += 1
            continue
        if _FILLER.search(sentence):
            filler += 1
            continue
        overlap = tokens & source_tokens
        records = re.findall(r'\b\d+[-\u2013]\d+(?:[-\u2013]\d+)?\b', sentence)
        anchored = (any(name in sentence for name in names) or len(overlap) >= 2
                    or any(record in source for record in records))
        numeric = bool(re.search(r'\d', sentence))
        narration = _TABLE_NARRATION.search(sentence)
        if report_type != 'get_standings':
            narration = narration or (numeric and _SCORE_NARRATION.search(sentence))
        copied = len(tokens) >= 5 and len(overlap) / len(tokens) >= .85
        if anchored and (narration or copied):
            restatements += 1
        elif len(tokens) >= 4:
            # No forced vocabulary or joke requirement: an unrecognized
            # observation gets the benefit of the doubt.
            substantive += 1

    if substantive:
        return []
    problems = []
    if restatements >= 2:
        problems.append('report_restatement_without_analysis')
    if filler:
        problems.append('generic_analysis_filler')
    return problems
