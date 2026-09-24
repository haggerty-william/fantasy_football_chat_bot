"""Conservative checks for trade lifecycle and future-game factual mixups.

Only explicit named assertions are checked. These checks do not establish that
all claims are true, infer an offer from a simulation, or reject sports opinions.
"""
import re


def _clean(value):
    return value.replace('\u2019', "'").replace('**', '') if isinstance(value, str) else ''


def _records(value):
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _identity(context):
    teams, candidates, players = {}, {}, {}
    for team in _records(context.get('fantasy_teams')):
        tid, name = str(team.get('id')), _clean(team.get('name'))
        if not name:
            continue
        teams[tid] = name
        candidates.setdefault(name.casefold(), set()).add(tid)
        for manager in team.get('managers', []) or []:
            parts = _clean(manager).split()
            if not parts:
                continue
            names = {' '.join(parts), parts[0]}
            if len(parts) > 1:
                names.update((parts[0] + ' ' + parts[-1][0], parts[0] + ' ' + parts[-1][0] + '.'))
            for alias in names:
                candidates.setdefault(alias.casefold(), set()).add(tid)
    for row in _records(context.get('players')):
        if row.get('id') is not None and row.get('name'):
            players[str(row['id'])] = _clean(row['name'])
    for roster in _records(context.get('rosters')):
        for row in roster.get('players', []) or []:
            if isinstance(row, (list, tuple)) and len(row) > 1 and row[0] is not None:
                players.setdefault(str(row[0]), _clean(row[1]))
    aliases = {alias: next(iter(ids)) for alias, ids in candidates.items() if len(ids) == 1}
    return teams, aliases, players


def _trade_rows(context):
    awareness = context.get('trade_awareness', {})
    awareness = awareness if isinstance(awareness, dict) else {}
    pending = _records(awareness.get('pending', {}).get('trades')) if isinstance(awareness.get('pending'), dict) else []
    completed = _records(awareness.get('recent_completed', {}).get('trades')) if isinstance(awareness.get('recent_completed'), dict) else []
    hypothetical = []
    for evidence in _records(context.get('tool_evidence')):
        result = evidence.get('result')
        if not isinstance(result, dict) or 'error' in result:
            continue
        name = evidence.get('tool')
        if name == 'get_pending_trades':
            pending.extend(_records(result.get('trades')))
        elif name == 'get_recent_trades':
            completed.extend(_records(result.get('trades')))
        elif name == 'evaluate_trade_proposal' and result.get('kind') == 'hypothetical_trade':
            hypothetical.extend(_records(result.get('teams')))
    return pending, completed, hypothetical


def _legs(rows, players):
    legs = set()
    for trade in rows:
        for item in _records(trade.get('items')):
            pid = item.get('player_id')
            if pid is None:
                continue
            pid = str(pid)
            if item.get('player_name'):
                players.setdefault(pid, _clean(item['player_name']))
            for field, direction in (('from_team_id', 'sent'), ('to_team_id', 'received')):
                if item.get(field) is not None:
                    legs.add((str(item[field]), pid, direction))
    return legs


def _player_aliases(players):
    candidates = {}
    for pid, name in players.items():
        if not name:
            continue
        candidates.setdefault(name.casefold(), set()).add(pid)
        last = name.split()[-1]
        if len(last) > 3:
            candidates.setdefault(last.casefold(), set()).add(pid)
    return {alias: next(iter(ids)) for alias, ids in candidates.items() if len(ids) == 1}


def _pattern(names):
    names = sorted(set(names), key=len, reverse=True)
    return r'(?<!\w)(?:' + '|'.join(re.escape(name) for name in names) + r')(?!\w)' if names else r'(?!)'


def _clauses(text, protected_names):
    # Initials in manager/player names and punctuation in team names are not
    # sentence boundaries. Restore the exact text before matching assertions.
    protected = re.sub(_pattern(protected_names), lambda match: match.group().replace('.', '\ufff0')
                       .replace('!', '\ufff1').replace('?', '\ufff2'), text, flags=re.I)
    for chunk in re.split(r'(?<=[.!?])\s+|\n+|;|\b(?:but|whereas)\b', protected, flags=re.I):
        yield chunk.replace('\ufff0', '.').replace('\ufff1', '!').replace('\ufff2', '?')


def _qualified(clause):
    return bool(re.search(r"\b(?:if|unless|would|could|might|may|will|assuming|hypothetical|"
                          r"pending|awaiting|hasn't|haven't|didn't|not|never)\b|"
                          r'\b(?:a|the|this) proposed (?:trade|deal|offer)\b', clause, re.I))


def _future_pairs(context):
    future, completed = {}, {}
    cutoff = context.get('week')
    awareness = context.get('schedule_awareness', {})
    awareness = awareness if isinstance(awareness, dict) else {}
    rows = list(_records(awareness.get('matchups')))
    for evidence in _records(context.get('tool_evidence')):
        if evidence.get('tool') in ('get_fantasy_schedule', 'get_week_matchups', 'get_head_to_head'):
            result = evidence.get('result')
            if isinstance(result, dict) and 'error' not in result:
                rows.extend(_records(result.get('matchups')))
    for row in rows:
        tids = {str(tid) for tid in row.get('team_ids', []) or []}
        tids.update(str(team['team_id']) for team in _records(row.get('teams')) if team.get('team_id') is not None)
        weeks = row.get('scoring_weeks')
        if not isinstance(weeks, list):
            continue
        for week in weeks:
            if type(week) is not int:
                continue
            if row.get('state') == 'completed':
                completed.setdefault(week, set()).update(tids)
            elif row.get('state') == 'future' or (type(cutoff) is int and week > cutoff):
                future.setdefault(week, set()).update(tids)
    return {week: teams - completed.get(week, set()) for week, teams in future.items()}


def _game_result(verb, remainder, subjects):
    """Avoid mistaking 'lost the plot' or 'beat the waiver rush' for a score."""
    if verb in ('scored', 'posted'):
        return bool(re.match(r'\s+[-+]?\d+(?:\.\d+)?\b', remainder))
    if verb in ('beat', 'defeated'):
        return bool(re.match(r'\s+(?:' + subjects + r")(?!'s\b)", remainder, re.I))
    if verb in ('won', 'lost'):
        return bool(re.match(r'\s+(?:(?:its|their|the)\s+)?(?:week\s+\d+\b|matchup\b|game\b|'
                             r'(?:in|during)\s+week\b|(?:to|against)\s+' + subjects + ')', remainder, re.I))
    return bool(re.match(r'\s+(?:by\s+' + subjects + r'|(?:in|during)\s+week\b)', remainder, re.I))


def check_transaction_claims(text, context):
    """Return issue codes for explicit pending/hypothetical/future fact errors."""
    if not isinstance(context, dict) or not isinstance(text, str):
        return []
    teams, aliases, players = _identity(context)
    pending_rows, completed_rows, hypothetical_rows = _trade_rows(context)
    pending = _legs(pending_rows, players)
    completed = _legs([row for row in completed_rows if row.get('completed') is True], players)
    hypothetical = set()
    for row in hypothetical_rows:
        for field, direction in (('sent_player_ids', 'sent'), ('received_player_ids', 'received')):
            for pid in row.get(field, []) or []:
                hypothetical.add((str(row.get('team_id')), str(pid), direction))
    by_name = {name.casefold(): tid for tid, name in teams.items()}
    by_player = {name.casefold(): pid for pid, name in players.items()}
    for side in _records(context.get('trade_sides')):
        tid = by_name.get(_clean(side.get('team')).casefold())
        if tid is None:
            continue
        for direction in ('sent', 'received'):
            for name in side.get(direction, []) or []:
                pid = by_player.get(_clean(name).casefold())
                if pid is not None:
                    completed.add((tid, pid, direction))
    player_aliases = _player_aliases(players)
    future = _future_pairs(context)
    subjects = _pattern(aliases)
    player_pattern = _pattern(player_aliases)
    auxiliary = r'\s+(?:(?:has|have|had|just|already|officially|now|successfully|finally|actually)\s+)*'
    transfers = {'received': r'received|acquired|got|landed|added',
                 'sent': r'sent|shipped|gave up|traded away|traded'}
    problems = set()
    for clause in _clauses(_clean(text), [*aliases, *player_aliases]):
        if _qualified(clause):
            continue
        for direction, verbs in transfers.items():
            for match in re.finditer('(' + subjects + ')' + auxiliary + '(?:' + verbs + r')\s+', clause, re.I):
                tid = aliases.get(match.group(1).casefold())
                body = re.split(r'\b(?:for|from|in exchange for|while|who|whose|which)\b|\band\s+' + subjects,
                                clause[match.end():], maxsplit=1, flags=re.I)[0]
                for player in re.finditer(player_pattern, body, re.I):
                    key = (tid, player_aliases[player.group().casefold()], direction)
                    if key in pending and key not in completed:
                        problems.add('pending_trade_as_completed')
                    elif key in hypothetical and key not in pending and key not in completed:
                        problems.add('hypothetical_trade_as_completed')
        for match in re.finditer('(' + subjects + ')' + auxiliary + r'(?:offered|proposed|accepted|agreed to)\s+', clause, re.I):
            tid = aliases.get(match.group(1).casefold())
            body = re.split(r'\band\s+' + subjects, clause[match.end():], maxsplit=1, flags=re.I)[0]
            for player in re.finditer(player_pattern, body, re.I):
                pid = player_aliases[player.group().casefold()]
                keys = {(tid, pid, 'received'), (tid, pid, 'sent')}
                if keys & hypothetical and not keys & (pending | completed):
                    problems.add('hypothetical_trade_as_offer')
        weeks = {int(value) for value in re.findall(r'\bweek\s+(\d{1,2})\b', clause, re.I)}
        for match in re.finditer('(' + subjects + ')' + auxiliary +
                                r'(beat|defeated|won|lost|scored|posted|was beaten|was defeated)\b', clause, re.I):
            tid = aliases.get(match.group(1).casefold())
            if (_game_result(match.group(2).casefold(), clause[match.end():], subjects)
                    and any(tid in future.get(week, set()) for week in weeks)):
                problems.add('future_matchup_as_completed')
    return sorted(problems)
