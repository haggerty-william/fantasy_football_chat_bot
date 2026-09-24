"""Core explanations must survive large rosters, archives and current context."""
from copy import deepcopy
import json
from types import SimpleNamespace as O
from unittest.mock import Mock

import pytest

from gamedaybot.espn import analyst_evidence, community, nfl_usage, research, transaction_tools
from gamedaybot.espn.analysis_packet import AnalysisPacket


def big_league():
    teams = []
    for index in range(10):
        roster = [O(playerId=index * 100 + pid, name=f'Player {index} Example {pid}', position='WR',
                    lineupSlot='WR' if pid < 9 else 'BE', injuryStatus='ACTIVE', proTeam='NFL' + str(index),
                    stats={1: {'points': 5 + pid, 'projected_points': 10},
                           2: {'points': 7 + pid, 'projected_points': 10}, 3: {'projected_points': 12}})
                  for pid in range(1, 18)]
        teams.append(O(team_id=index + 1, team_name='Neighborhood Team ' + chr(65 + index),
                       owners=[{'firstName': 'Manager' + chr(65 + index), 'lastName': 'Example'}],
                       scores=[100 + index * 2, 115 - index * 2, 9999], outcomes=['W', 'L', 'U'], roster=roster))
    for index, team in enumerate(teams):
        team.schedule = [teams[index ^ 1]] * 14
    return O(league_id=123, year=2026, scoringPeriodId=3, teams=teams, espn_request=O(),
             settings=O(reg_season_count=14, matchup_periods={str(week): [week] for week in range(1, 15)}))


@pytest.fixture
def sources(monkeypatch):
    monkeypatch.setenv('AI_CONTEXT_CHAR_LIMIT', '24000')
    rules = {'lineup_slots': {'WR': 2, 'FLEX': 1, 'BE': 6}, 'playoff_places': 6,
             'scoring': [{'stat_id': index, 'stat': 'Fantasy scoring category with a detailed description', 'points': .1}
                         for index in range(45)]}
    monkeypatch.setattr(analyst_evidence, 'rules', lambda league: deepcopy(rules))
    monkeypatch.setattr(analyst_evidence, 'forecast_memory', lambda *args: [
        {'week': 1, 'commentary': 'A long archived prediction with receipts and old context. ' * 150}
        for _ in range(5)])
    monkeypatch.setattr(analyst_evidence, 'recent_trade_history', lambda *args: [])
    monkeypatch.setattr(nfl_usage, 'usage_context', lambda *args: ({}, {}))
    monkeypatch.setattr(research, 'news_feed', lambda: ([], 'now'))
    monkeypatch.setattr(community, 'nfl_games', lambda league: {
        'NFL' + str(index): {'start': 1790600000, 'state': 'pre', 'completed': False} for index in range(32)})
    offers = {'observed_at': '2026-09-24T00:00:00+00:00', 'visibility': 'Configured ESPN account only',
              'pending': {'status': 'visible_records', 'visible_count': 3, 'trades': [
                  {'state': 'proposed', 'completed': False, 'team_ids': ['1', '2'],
                   'items_complete': True, 'items': [
                       {'player_id': str(pid), 'player_name': 'Offered Player ' + str(pid),
                        'from_team_id': '1', 'to_team_id': '2'} for pid in range(1, 7)]}
                  for _ in range(3)]},
              'recent_completed': {'trades': [], 'scan_complete': True}}
    awareness = Mock(side_effect=lambda *args, **kwargs: deepcopy(offers))
    monkeypatch.setattr(transaction_tools, 'build_trade_awareness', awareness)
    return awareness


@pytest.mark.parametrize('report_type', ['get_standings', 'get_power_rankings', 'get_playoffs'])
def test_ten_team_standings_keep_explanations_and_awareness_within_budget(sources, report_type):
    league = big_league()
    context = research.build_context(league, 'Standings', report_type, 3)
    assert len(json.dumps(context)) <= 24000
    history = context['league_history']
    assert history['performance_context']['through_week'] == 2
    assert len(history['teams']) == 10
    for row in history['teams']:
        performance = row['performance']
        assert performance['weeks'] == 2
        assert performance['points_per_game'] == 107.5
        assert 'schedule_luck_wins' in performance
        assert 'points_per_game_vs_league_average' in performance
        assert 'average_margin' in performance
        assert 'recent_games' in performance
    assert len(context['fantasy_teams']) == 10
    assert len(context['schedule_awareness']['matchups']) == 15
    assert context['trade_awareness']['pending']['visible_count'] == 3
    assert context['trade_awareness']['pending']['trades'][0]['completed'] is False
    assert context['context_omissions']['previous_forecasts'] == 5
    assert context['context_omissions']['scoring_rules'] > 0
    assert context['context_omissions']['nfl_games'] == 10
    assert '9999' not in json.dumps(context)
    retained = sum(len(row['players']) for row in context['rosters'])
    omitted = context['omitted_roster_players']
    assert retained + omitted == 170
    assert sum(row.get('omitted_players', 0) for row in context['rosters']) == omitted
    assert omitted > 0 and retained > 0
    assert len(context['players']) + context['omitted_player_count'] == 170
    packet = json.loads(AnalysisPacket(league, context, 'Standings', report_type, 3, 'now').dumps())
    canonical = packet['research_context']['teams']
    assert len(canonical) == 10
    assert all('performance' in team['history'] for team in canonical.values())


def test_historical_budget_keeps_completed_evidence_without_current_offers(sources):
    league = big_league()
    context = research.build_context(league, 'Historical standings', 'get_standings', 1)
    assert len(json.dumps(context)) <= 24000
    assert context['historical'] is True
    assert context['league_history']['performance_context']['through_week'] == 1
    assert len(context['league_history']['teams']) == 10
    for index, row in enumerate(context['league_history']['teams']):
        assert row['performance']['weeks'] == 1
        assert row['performance']['points_per_game'] == 100 + index * 2
    assert 'trade_awareness' not in context
    assert 'schedule_awareness' not in context
    assert context['news'] == []
    assert all('current_status' not in player for player in context['players'])
    assert '9999' not in json.dumps(context)
    sources.assert_not_called()


def test_smaller_supported_context_budget_preserves_each_teams_core_performance(sources, monkeypatch):
    monkeypatch.setenv('AI_CONTEXT_CHAR_LIMIT', '16000')
    context = research.build_context(big_league(), 'Standings', 'get_standings', 3)
    assert len(json.dumps(context)) <= 16000
    assert len(context['league_history']['teams']) == 10
    assert all(row['performance']['weeks'] == 2 for row in context['league_history']['teams'])
    assert context['league_history']['performance_context']['through_week'] == 2


def test_authoritative_completed_trade_directions_are_never_removed_to_fit():
    sides = [{'team': 'Oak', 'sent': ['Explicit outgoing player ' * 1500], 'received': ['Incoming player']}]
    context = {'players': [], 'omitted_player_count': 0, 'rosters': [], 'league_rules': {},
               'league_history': {}, 'trade_sides': deepcopy(sides)}
    with pytest.raises(ValueError, match='Core report evidence exceeds'):
        research._prune_context(context, 16000, 'get_trade_report')
    assert context['trade_sides'] == sides
