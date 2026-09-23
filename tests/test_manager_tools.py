from datetime import datetime, timezone
import json
import time
from types import SimpleNamespace as O
from unittest.mock import Mock

import pytest

from gamedaybot.espn import manager_tools as m
from gamedaybot.espn.research_tools import ResearchTools, TOOLS


def player(pid, points, slot='WR', eligible=None, date=None):
    return O(playerId=pid, name=f'Player {pid}', position='WR', slot_position=slot,
             eligibleSlots=eligible or ['WR', 'FLEX'], stats={1: {'points': points}},
             game_date=date or datetime(2026, 9, 13, tzinfo=timezone.utc), on_bye_week=False)


@pytest.fixture
def researcher():
    teams = [O(team_id=i, team_name=n, scores=[s, 9999], roster=[])
             for i, n, s in [(1, 'Oak', 100), (2, 'Maple', 90), (3, 'Birch', 100), (4, 'Elm', 80)]]
    for a, b in [(0, 1), (2, 3)]:
        teams[a].schedule = [teams[b]]
        teams[b].schedule = [teams[a]]
    roster = [player(1, 8), player(2, 9, 'FLEX'), player(3, 20, 'BE', ['WR'])]
    box = O(home_team=teams[0], away_team=teams[1], home_lineup=roster, away_lineup=[])
    league = O(league_id=1, year=2026, scoringPeriodId=2, finalScoringPeriod=18,
               teams=teams, settings=O(matchup_periods={'1': [1], '2': [2]}),
               box_scores=Mock(return_value=[box]))
    return ResearchTools(league, {'players': [], 'league_rules': {'lineup_slots': {'WR': 1, 'FLEX': 1, 'BE': 2}}},
                         2, None, time.monotonic() + 200)


def test_schedule_luck_ties_and_completed_only(researcher):
    result = researcher.execute('get_schedule_luck', '{}')
    oak = result['teams'][0]
    assert result['through_week'] == 1
    assert oak['all_play_wins'] == 2 and oak['all_play_ties'] == 1
    assert oak['expected_win_equivalents'] == .83
    assert oak['schedule_luck_wins'] == .17
    assert sum(t['schedule_luck_wins'] for t in result['teams']) == pytest.approx(0, abs=.02)


def test_lineup_uses_exact_flex_assignment_and_ignores_current_injuries(researcher):
    roster = researcher.league.box_scores.return_value[0].home_lineup
    roster[2].injuryStatus = 'OUT'  # Today's injury must not remove historical points.
    result = researcher.execute('get_lineup_efficiency', '{"team_id":"1"}')
    assert result['starter_points'] == 17 and result['optimal_points'] == 29
    assert result['points_left_on_bench'] == 12 and result['efficiency_pct'] == 58.6
    researcher.execute('get_lineup_efficiency', '{"team_id":"1"}')
    researcher.league.box_scores.assert_called_once_with(week=1)


def test_lineup_missing_stats_are_not_zero_and_ir_excluded(researcher):
    roster = researcher.league.box_scores.return_value[0].home_lineup
    roster.append(player(99, 100, 'IR'))
    assert m.optimal_actual(roster, {'WR': 1, 'FLEX': 1}, 1) == 29
    roster[2].stats = {}
    result = m.lineup_efficiency(researcher, researcher.league.teams[0])
    assert result['omitted_weeks'] == [1] and result['efficiency_pct'] is None


def test_incomplete_period_rules_and_deadline_do_not_fetch(researcher):
    researcher.league.settings.matchup_periods = {'1': [1, 2]}
    assert 'error' in researcher.execute('get_lineup_efficiency', '{"team_id":"1"}')
    assert 'error' in researcher.execute('get_schedule_luck', '{}')
    researcher.league.box_scores.assert_not_called()
    researcher.deadline = time.monotonic()
    assert 'error' in researcher.execute('get_draft_value', '{"team_id":"1"}')


@pytest.mark.parametrize('args', ['{"team_id":1}', '{"team_id":"99"}', '{"team_id":"1","url":"http://bad"}', '[]'])
def test_argument_validation_prevents_fetch(researcher, args):
    assert 'error' in researcher.execute('get_lineup_efficiency', args)
    researcher.league.box_scores.assert_not_called()


def test_waiver_counts_only_started_points_after_pickup_before_drop(researcher):
    team = researcher.league.teams[0]
    roster = researcher.league.box_scores.return_value[0].home_lineup
    def event(day, action, p):
        return O(date=datetime(2026, 9, day, tzinfo=timezone.utc).timestamp()*1000,
                 actions=[(team, action, p, 7)])
    researcher.league.recent_activity = Mock(return_value=[
        event(10, 'WAIVER ADDED', roster[0]), event(14, 'DROPPED', roster[0]),
        event(10, 'FA ADDED', roster[2]), event(14, 'FA ADDED', roster[1])])
    result = researcher.execute('get_waiver_return', '{"team_id":"1"}')
    assert len(result['pickups']) == 2
    assert result['pickups'][0]['started_points'] == 8
    assert result['pickups'][0]['bid'] == 7
    assert result['pickups'][1]['started_points'] == 0 and result['pickups'][1]['bench_points'] == 20


def test_waiver_reacquisition_does_not_double_count(researcher):
    team = researcher.league.teams[0]
    p = researcher.league.box_scores.return_value[0].home_lineup[0]
    def event(day, action):
        return O(date=datetime(2026, 9, day, tzinfo=timezone.utc).timestamp()*1000, actions=[(team, action, p, 0)])
    researcher.league.recent_activity = Mock(return_value=[event(8, 'FA ADDED'), event(9, 'DROPPED'), event(10, 'FA ADDED')])
    result = m.waiver_return(researcher, team)
    assert len(result['pickups']) == 1 and result['pickups'][0]['started_points'] == 8


def test_draft_position_ranking_preserves_original_team_and_excludes_live(researcher):
    a, b = researcher.league.teams[:2]
    ps = [player(1, 5), player(2, 20), player(3, 15)]
    ps[0].stats[2] = {'points': 9999}
    ps[2].position = 'QB'
    researcher.league.player_info = Mock(return_value=ps)
    researcher.league.draft = [O(playerId=p.playerId, team=t, round_num=1, round_pick=i,
                                bid_amount=0, keeper_status=False) for i, (p, t) in enumerate(zip(ps, [a, b, a]), 1)]
    result = researcher.execute('get_draft_value', '{"team_id":"1"}')
    assert len(result['picks']) == 2
    assert result['picks'][0]['recorded_points'] == 5 and result['picks'][0]['rank_gain'] == -1
    assert result['picks'][1]['rank_gain'] == 0  # QB is not ranked against WRs.
    assert result['picks'][0]['draft_team'] == 'Oak'
    researcher.league.draft[0].bid_amount = 10
    assert 'error' in m.draft_value(researcher, a)


def test_personality_is_scoped_bounded_and_read_only(researcher, monkeypatch, tmp_path):
    path = tmp_path / 'notes.json'
    monkeypatch.setenv('LEAGUE_PERSONALITY_PATH', str(path))
    assert m.personality(researcher)['notes'] == []
    note = {'kind': 'nickname', 'team_ids': ['1'], 'text': 'The Bench Boss'}
    path.write_text(json.dumps({'league_id': 1, 'year': 2026, 'notes': [note]}))
    before = path.read_bytes()
    assert researcher.execute('get_league_personality', '{}')['notes'] == [note]
    assert path.read_bytes() == before
    researcher.context['historical'] = True
    assert m.personality(researcher)['notes'] == []
    researcher.context['historical'] = False
    researcher.league.year = 2025
    assert 'error' in m.personality(researcher)


def test_tools_registered_and_shared_budget_enforced(researcher):
    assert m.NAMES <= {t['function']['name'] for t in TOOLS}
    researcher.calls = 12
    assert 'error' in researcher.execute('get_schedule_luck', '{}')
