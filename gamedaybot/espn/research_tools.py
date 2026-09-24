"""Bounded, read-only player research for local model tool calls."""
import json
import logging
import re
import time

from gamedaybot.espn.research import build_context
from gamedaybot.espn import decision_tools, manager_tools, league_tools, transaction_tools, player_tools
from gamedaybot.espn.analysis_limits import MAX_TOOL_CALLS

logger = logging.getLogger(__name__)
DESCRIPTIONS = {
    'get_player_news': 'Get dated ESPN news highlights for players in supplied rosters or verified player results from another tool. No full articles. Current news is unavailable for historical reports.',
    'get_player_stats': 'Get additional player statistics, calculated trends, targets, carries and snap usage for the report week and recent completed games.',
    'get_player_status': 'Get ESPN player status and NFL game state, including whether the lineup is locked. ACTIVE does not prove full health.',
}
TOOLS = [{'type': 'function', 'function': {
    'name': name, 'description': description,
    'parameters': {'type': 'object', 'properties': {
        'player_ids': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1, 'maxItems': 3,
                       'description': 'Exact ESPN player IDs from supplied rosters or player, draft-pick, lineup, or transaction results from another research tool.'}},
        'required': ['player_ids'], 'additionalProperties': False}}}
    for name, description in DESCRIPTIONS.items()]
PROVIDERS = (decision_tools, manager_tools, league_tools, transaction_tools, player_tools)
PROVIDER_BY_NAME = {name: provider for provider in PROVIDERS for name in provider.NAMES}
TOOLS += [definition for provider in PROVIDERS for definition in provider.TOOLS]
MAX_EVIDENCE_CHARS = 14000
MAX_TOTAL_EVIDENCE_CHARS = 28000


def verified_result_player_ids(result):
    """Identify player IDs only in sanitized providers' explicit player rows.

    Generic team/game/event IDs and arbitrary nested text are not player IDs.
    This walks a small set of application-owned result containers, never model
    arguments, news bodies or user-supplied personality notes.
    """
    if not isinstance(result, dict) or 'error' in result:
        return set()
    player_rows = {'players', 'roster', 'candidates', 'lineup'}
    explicit_rows = player_rows | {'picks', 'items'}
    containers = explicit_rows | {'teams', 'trades', 'transactions', 'offers', 'before', 'after'}
    found = set()
    def visit(node, row_type=None, depth=0):
        if depth > 8 or not isinstance(node, dict):
            return
        value = node.get('player_id') if row_type in explicit_rows else None
        if value is None and row_type in player_rows:
            value = node.get('id')
        if not isinstance(value, bool) and isinstance(value, (str, int)):
            text = str(value)
            if re.fullmatch(r'-?\d{1,10}', text) and int(text) != 0:
                found.add(text)
        for key in containers:
            child = node.get(key)
            if isinstance(child, list):
                for row in child[:200]:
                    visit(row, key, depth + 1)
            elif isinstance(child, dict):
                visit(child, key, depth + 1)
    visit(result)
    return found


class ResearchTools:
    def __init__(self, league, context, week, box_scores, deadline):
        self.league, self.context = league, context
        self.week, self.box_scores, self.deadline = week, box_scores, deadline
        self.allowed = {str(p['id']) for p in context.get('players', []) if p.get('id') is not None}
        self.allowed.update(str(row[0]) for r in context.get('rosters', []) for row in r['players'] if row[0] is not None)
        self.cache = {}
        self.calls = 0
        self.rounds = 0
        self.evidence_chars = 0
        self.decision_cache = {}
        self.discovered_players = {}
        self.packet = None

    def bound_result(self, result):
        size = len(json.dumps(result, ensure_ascii=False))
        if size > MAX_EVIDENCE_CHARS:
            return {'error': 'Result exceeds the evidence budget. Narrow the team, week, player list or page limit.'}
        if self.evidence_chars + size > MAX_TOTAL_EVIDENCE_CHARS:
            return {'error': 'Total evidence budget exhausted. Use the evidence already returned.'}
        if self.packet is not None:
            from gamedaybot.espn.analysis_packet import MAX_PACKET_CHARS
            if len(self.packet.dumps()) + size + 512 > MAX_PACKET_CHARS:
                return {'error': 'The research snapshot is full. Use the evidence already supplied.'}
        self.evidence_chars += size
        return result

    def execute(self, name, raw_arguments):
        # Dispatch only fixed functions. Model-generated URLs, code and extra
        # parameters never reach a network client or an interpreter.
        if isinstance(name, str) and name in PROVIDER_BY_NAME:
            if not isinstance(raw_arguments, str) or len(raw_arguments) > 1000:
                return {'error': 'Invalid tool arguments.'}
            try:
                args = json.loads(raw_arguments)
                key = (name, json.dumps(args, sort_keys=True))
                if key not in self.decision_cache:
                    if self.calls >= MAX_TOOL_CALLS or time.monotonic() > self.deadline - 20:
                        return {'error': 'Research budget exhausted.'}
                    if name != 'get_workload_changes': self.calls += 1
                    provider = PROVIDER_BY_NAME[name]
                    result = provider.execute(name, args, self)
                    result = self.bound_result(result)
                    # A profile, historical lineup, draft or transaction can
                    # introduce a player omitted by initial-context pruning.
                    # Register identity only: do not copy historical ownership
                    # or old points into a current player record.
                    self.allowed.update(verified_result_player_ids(result))
                    self.decision_cache[key] = result
                    self.context.setdefault('tool_evidence', []).append({'tool': name, 'result': result})
                    for p in result.get('candidates', []):
                        self.context.setdefault('players', []).append({**p, 'fantasy_team': 'Unrostered or on waivers'})
                    logger.info('Research tool=%s status=%s', name, 'unavailable' if 'error' in result else 'complete')
                return self.decision_cache[key]
            except Exception:
                logger.warning('Calculated research unavailable: %s', name)
                return {'error': 'Research unavailable; do not infer missing facts.'}
        if not isinstance(name, str) or name not in DESCRIPTIONS:
            return {'error': 'Unknown research tool.'}
        try:
            if not isinstance(raw_arguments, str) or len(raw_arguments) > 1000:
                raise ValueError()
            args = json.loads(raw_arguments)
            ids = args['player_ids']
            if (set(args) != {'player_ids'} or not isinstance(ids, list) or not 1 <= len(ids) <= 3
                    or any(not isinstance(p, str) or p not in self.allowed for p in ids)):
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            return {'error': 'Use one to three exact player IDs from supplied rosters or verified research results.'}
        if self.context.get('historical') and name in ('get_player_news', 'get_player_status'):
            return {'error': 'Current news and statuses are excluded from historical reports.'}
        key = tuple(sorted(set(ids)))
        if key not in self.cache:
            if self.calls >= MAX_TOOL_CALLS or time.monotonic() > self.deadline - 20:
                return {'error': 'Research budget exhausted. Use existing evidence; missing data is unknown.'}
            self.calls += 1
            try:
                self.cache[key] = build_context(self.league, '', 'get_matchups', self.week,
                                               self.box_scores, requested_player_ids=set(ids))
                present = {str(p['id']) for p in self.cache[key].get('players', [])}
                missing = [pid for pid in ids if pid not in present and pid not in self.discovered_players]
                if missing:
                    # Only prevalidated IDs can reach this bounded card read.
                    # Current-roster research above retains authoritative slots
                    # and ownership whenever that evidence is available.
                    player_tools._cards(self, missing)
                discovered = [pid for pid in ids if pid not in present and pid in self.discovered_players]
                if discovered:
                    extra = player_tools.discovered_context(self, discovered)
                    self.cache[key].setdefault('players', []).extend(extra.get('players', []))
                    self.cache[key].setdefault('news', []).extend(extra.get('news', []))
                    base_limitations = self.cache[key].get('limitations', [])
                    if isinstance(base_limitations, str): base_limitations = [base_limitations]
                    self.cache[key]['limitations'] = list(dict.fromkeys([
                        *base_limitations, *extra.get('limitations', [])]))
                    if extra.get('news_fetched_at'):
                        self.cache[key]['news_fetched_at'] = extra['news_fetched_at']
                    if extra.get('news') or not present:
                        self.cache[key]['news_status'] = extra.get('news_status', 'Discovered-player news unavailable.')
            except Exception:
                logger.warning('Player research tool unavailable: %s', name)
                return {'error': 'Source unavailable. Do not infer missing facts.'}
        packet = self.cache[key]
        fields = {'get_player_news': ('id', 'name'),
                  'get_player_status': ('id', 'name', 'current_status', 'game'),
                  'get_player_stats': ('id', 'name', 'fantasy_team', 'points', 'projected_points',
                                       'stats', 'trend', 'previous_weeks', 'usage', 'game')}[name]
        players = [{k: v for k, v in p.items() if k in fields} for p in packet.get('players', [])]
        result = {'source': packet['source'], 'fetched_at': packet['fetched_at'],
                  'week': packet['week'], 'players': players, 'limitations': packet['limitations']}
        if name == 'get_player_news':
            result.update(news=packet.get('news', []), news_status=packet.get('news_status'),
                          news_fetched_at=packet.get('news_fetched_at'))
        if name == 'get_player_stats':
            result['usage_status'] = packet.get('usage_status')
        result = self.bound_result(result)
        if 'error' in result:
            return result
        # Merge exactly what the model receives into the validation evidence.
        for entry in players:
            existing = next((p for p in self.context['players'] if str(p.get('id')) == str(entry['id'])), None)
            if existing is None:
                self.context['players'].append(dict(entry))
            else:
                existing.update(entry)
        for article in result.get('news', []):
            if article not in self.context.setdefault('news', []):
                self.context['news'].append(article)
        logger.info('Research tool=%s players=%s results=%s', name, ','.join(ids), len(players))
        return result
