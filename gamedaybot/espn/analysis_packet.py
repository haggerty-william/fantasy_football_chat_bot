"""One canonical team record per model request, including later tool evidence.

Internal source/validation objects stay unchanged. Names occur only in the team
directory; report text and cross-team relations use explicit ID references.
"""
from copy import deepcopy
import json
import re

from gamedaybot.espn.research import fantasy_team_context


class AnalysisPacket:
    def __init__(self, league, context, report, report_type, week, snapshot):
        self.source = context or {}
        directory = fantasy_team_context(league) if league is not None else []
        if not directory:
            directory = self.source.get('fantasy_teams', [])
        self.teams = {str(t['id']): {'name': t['name'], 'managers': list(t.get('managers', []))}
                      for t in directory}
        self.by_name = {t['name'].casefold(): tid for tid, t in self.teams.items()}
        self.player_teams = {}
        self.tool_results = 0
        aliases = {}
        for tid, team in self.teams.items():
            aliases[team['name']] = f'[team:{tid}]'
            for index, name in enumerate(team['managers']):
                aliases.setdefault(name, f'[manager:{tid}:{index}]')
        self.aliases = {name.casefold(): ref for name, ref in aliases.items() if name}
        self.pattern = (re.compile(r'(?<!\w)(?:' + '|'.join(re.escape(n) for n in sorted(aliases, key=len, reverse=True) if n)
                                   + r')(?!\w)', re.I) if aliases else None)
        for roster in self.source.get('rosters', []):
            tid = self.team_id(roster.get('team'))
            if tid is not None:
                for row in roster.get('players', []):
                    self.player_teams[str(row[0])] = tid
        context_data = self._context()
        self.data = {'schema_version': 2, 'report_type': report_type,
                     'snapshot_generated_at': snapshot, 'report_week': week,
                     'research_context': context_data,
                     'espn_report': self.references(report),
                     'writing_reminder': 'Resolve team IDs using research_context.teams. Team names are fine for scores and standings. When judging management decisions (trades, waivers, drafts, lineups), address the supplied manager by name and make the team association clear. Do not force manager names elsewhere. Use unique first names; disambiguate shared first names with last initials or full names. Never print internal ID references in final commentary.'}

    def team_id(self, name):
        return self.by_name.get(name.casefold()) if isinstance(name, str) else None

    def references(self, value):
        """Convert names to references without rewriting the canonical directory."""
        if isinstance(value, str):
            return self.pattern.sub(lambda m: self.aliases[m.group().casefold()], value) if self.pattern else value
        if isinstance(value, list):
            return [self.references(item) for item in value]
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                tid = self.team_id(item) if key in ('team', 'fantasy_team', 'draft_team', 'opponent') else None
                if tid is not None:
                    result[key + '_id' if key != 'fantasy_team' else 'team_id'] = tid
                else:
                    result[key] = self.references(item)
            return result
        return value

    def _context(self):
        excluded = {'fantasy_teams', 'rosters', 'players', 'league_history', 'trade_sides',
                    'current_roster_depth', 'highlights', 'tool_evidence'}
        data = self.references({k: v for k, v in self.source.items() if k not in excluded})
        data['teams'] = self.teams
        data['manager_scope'] = self.source.get('manager_scope',
            'Current ESPN managers at fetch time; Discord identities and past management are unknown.')
        for source_key, target_key, owner_key in (
                ('rosters', 'rosters', 'team'), ('players', 'player_details', 'fantasy_team'),
                ('trade_sides', 'trades', 'team'), ('current_roster_depth', 'roster_depth', 'team'),
                ('highlights', 'highlights', 'team')):
            for row in self.source.get(source_key, []):
                tid = self.team_id(row.get(owner_key))
                if tid is None:
                    data.setdefault('unassigned_' + source_key, []).append(self.references(row))
                    continue
                detail = self.references({k: v for k, v in row.items() if k != owner_key})
                self.teams[tid].setdefault(target_key, []).append(detail)
                if source_key == 'players' and row.get('id') is not None:
                    self.player_teams[str(row['id'])] = tid
        history = self.source.get('league_history', {})
        data['league_history'] = self.references({k: v for k, v in history.items() if k != 'teams'})
        for row in history.get('teams', []):
            tid = self.team_id(row.get('team'))
            detail = self.references({k: v for k, v in row.items() if k != 'team'})
            if tid is not None:
                self.teams[tid]['history'] = detail
            else:
                data['league_history'].setdefault('unassigned_teams', []).append(self.references(row))
        if not self.source:
            data['limitations'] = ['Detailed research unavailable. Team identities are supplied independently; use only the report for statistics.']
        return data

    def add_tool_result(self, call_id, name, result, arguments):
        """Store new evidence beside its team; tool messages are small receipts.

        The initial JSON message is refreshed before the next inference. This
        keeps one directory and one copy of each tool result across all rounds.
        """
        result = deepcopy(result)
        unavailable = 'error' in result
        self.tool_results += 1
        record_key = f'{self.tool_results}:{call_id}'
        paths = []
        grouped = {}
        target = self.team_id(result.get('team'))
        if target is None and isinstance(arguments, dict):
            proposed = arguments.get('team_id')
            target = proposed if isinstance(proposed, str) and proposed in self.teams else None
        if target is not None:
            result.pop('team', None)
            grouped[target] = result
            result = {}
        else:
            for key in ('teams', 'players'):
                remaining = []
                for row in result.get(key, []):
                    tid = self.team_id(row.get('team') or row.get('fantasy_team'))
                    if tid is None and key == 'teams' and str(row.get('team_id')) in self.teams:
                        tid = str(row['team_id'])
                    if tid is None and key == 'players':
                        tid = self.player_teams.get(str(row.get('id')))
                    if tid is None:
                        remaining.append(row)
                    else:
                        detail = {k: v for k, v in row.items() if k not in ('team', 'fantasy_team', 'team_id')}
                        grouped.setdefault(tid, {}).setdefault(key, []).append(detail)
                if key in result:
                    if remaining: result[key] = remaining
                    else: result.pop(key)
        for tid, evidence in grouped.items():
            self.teams[tid].setdefault('research', {})[record_key] = {'tool': name, 'data': self.references(evidence)}
            paths.append(['research_context', 'teams', tid, 'research', record_key])
        if result:
            self.data['research_context'].setdefault('shared_research', {})[record_key] = {
                'tool': name, 'data': self.references(result)}
            paths.append(['research_context', 'shared_research', record_key])
        return {'status': 'unavailable' if unavailable else 'stored', 'evidence_paths': paths,
                'instruction': 'Read this evidence in the refreshed initial JSON snapshot; use its limitations.'}

    def dumps(self):
        return json.dumps(self.data, separators=(',', ':'), ensure_ascii=False)
