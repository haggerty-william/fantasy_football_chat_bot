"""Bounded, read-only player research for local model tool calls."""
import json
import logging
import time

from gamedaybot.espn.research import build_context
from gamedaybot.espn import decision_tools, manager_tools
from gamedaybot.espn.analysis_limits import MAX_TOOL_CALLS

logger = logging.getLogger(__name__)
DESCRIPTIONS = {
    'get_player_news': 'Get dated ESPN news highlights for players in the report rosters. No full articles. Current news is unavailable for historical reports.',
    'get_player_stats': 'Get additional player statistics, calculated trends, targets, carries and snap usage for the report week and recent completed games.',
    'get_player_status': 'Get ESPN player status and NFL game state, including whether the lineup is locked. ACTIVE does not prove full health.',
}
TOOLS = [{'type': 'function', 'function': {
    'name': name, 'description': description,
    'parameters': {'type': 'object', 'properties': {
        'player_ids': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1, 'maxItems': 3,
                       'description': 'Exact ESPN player IDs from the supplied roster rows or player entries.'}},
        'required': ['player_ids'], 'additionalProperties': False}}}
    for name, description in DESCRIPTIONS.items()]
TOOLS += decision_tools.TOOLS
TOOLS += manager_tools.TOOLS


class ResearchTools:
    def __init__(self, league, context, week, box_scores, deadline):
        self.league, self.context = league, context
        self.week, self.box_scores, self.deadline = week, box_scores, deadline
        self.allowed = {str(p['id']) for p in context.get('players', []) if p.get('id') is not None}
        self.allowed.update(str(row[0]) for r in context.get('rosters', []) for row in r['players'] if row[0] is not None)
        self.cache = {}
        self.calls = 0
        self.decision_cache = {}

    def execute(self, name, raw_arguments):
        # Dispatch only fixed functions. Model-generated URLs, code and extra
        # parameters never reach a network client or an interpreter.
        if isinstance(name, str) and name in decision_tools.NAMES | manager_tools.NAMES:
            if not isinstance(raw_arguments, str) or len(raw_arguments) > 1000:
                return {'error': 'Invalid tool arguments.'}
            try:
                args = json.loads(raw_arguments)
                key = (name, json.dumps(args, sort_keys=True))
                if key not in self.decision_cache:
                    if self.calls >= MAX_TOOL_CALLS or time.monotonic() > self.deadline - 20:
                        return {'error': 'Research budget exhausted.'}
                    if name != 'get_workload_changes': self.calls += 1
                    provider = manager_tools if name in manager_tools.NAMES else decision_tools
                    result = provider.execute(name, args, self)
                    self.decision_cache[key] = result
                    self.context.setdefault('tool_evidence', []).append({'tool': name, 'result': result})
                    for p in result.get('candidates', []):
                        self.context['players'].append({**p, 'fantasy_team': 'Unrostered or on waivers'})
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
            return {'error': 'Use one to three exact player IDs from the supplied report rosters.'}
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
