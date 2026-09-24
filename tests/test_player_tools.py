"""Player discovery, temporal scope, fixed reads and bounded NFL summaries."""
import json
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from gamedaybot.espn import player_tools as tools


def stat(week, points=None, source=0, year=2026, stats=None):
    row = {'seasonId': year, 'statSplitTypeId': 1, 'scoringPeriodId': week,
           'statSourceId': source, 'stats': stats or {}}
    if points is not None:
        row['appliedTotal'] = points
    return row


def raw_player(pid=101, name='Player Alpha', owner=1, status=None):
    row = {'id': pid, 'onTeamId': owner,
           'player': {'id': pid, 'fullName': name, 'defaultPositionId': 4,
                      'eligibleSlots': [4, 3, 23], 'proTeamId': 2,
                      'injuryStatus': 'QUESTIONABLE',
                      'ownership': {'percentOwned': 27.5, 'percentStarted': 8.1},
                      'stats': [stat(0, 33), stat(1, 12), stat(2, 21), stat(3, 5),
                                stat(4, 900), stat(3, 15, source=1), stat(4, 16, source=1)],
                      'secretCookie': 'do-not-return', 'email': 'private@example.invalid'}}
    if status is not None:
        row['status'] = status
    return row


@pytest.fixture
def researcher(monkeypatch):
    team = SimpleNamespace(team_id=1, team_name='Oak', roster=[])
    league = SimpleNamespace(year=2026, scoringPeriodId=3, finalScoringPeriod=18, teams=[team],
                             player_map={101: 'Player Alpha', 'Player Alpha': 101,
                                         102: 'Player Beta', 'Player Beta': 102,
                                         103: 'Player Alpha Jr.', 'Player Alpha Jr.': 103})
    researcher = SimpleNamespace(league=league, context={'historical': False, 'players': []},
                                  week=3, deadline=time.monotonic() + 100, allowed={'101'})
    monkeypatch.setattr(tools, '_read_league', Mock(return_value={'players': [raw_player()]}))
    monkeypatch.setattr(tools, '_read_public', Mock(side_effect=AssertionError('unexpected public request')))
    return researcher


def test_search_discovers_names_outside_rosters_and_registers_exact_ids(researcher):
    tools._read_league.return_value = {'players': [raw_player(102, 'Player Beta', owner=0)]}
    result = tools.execute('search_players', {'query': 'beta'}, researcher)
    assert result['players'] == [{'id': '102', 'name': 'Player Beta', 'position': 'WR',
                                  'nfl_team': 'BUF', 'fantasy_team': 'Unrostered or on waivers'}]
    assert '102' in researcher.allowed
    assert researcher.discovered_players['102'].name == 'Player Beta'
    assert tools._read_league.call_args.args[2]['players']['filterIds']['value'] == [102]
    assert 'do-not-return' not in json.dumps(result)
    assert 'private@example' not in json.dumps(result)


def test_search_handles_ambiguity_with_bounded_exact_first_matches(researcher):
    result = tools.execute('search_players', {'query': 'Player Alpha', 'limit': 1}, researcher)
    assert result['matched_names'] == 2 and result['omitted_matches'] == 1
    assert result['players'][0]['id'] == '101'
    tools.execute('search_players', {'query': 'Player Alpha', 'limit': 1}, researcher)
    tools._read_league.assert_called_once()


def test_search_zero_results_does_not_request_player_cards(researcher):
    result = tools.execute('search_players', {'query': 'Nobody Exists'}, researcher)
    assert result['players'] == []
    tools._read_league.assert_not_called()


def test_unrequested_ids_in_upstream_response_are_not_registered(researcher):
    tools._read_league.return_value = {'players': [raw_player(), raw_player(999, 'Wrong Player')]}
    tools.execute('search_players', {'query': 'Player Alpha', 'limit': 1}, researcher)
    assert '999' not in researcher.allowed and '999' not in researcher.discovered_players


def test_details_preserve_actuals_projection_and_current_ownership_scope(researcher):
    result = tools.execute('get_player_season_details', {'player_ids': ['101']}, researcher)
    player = result['players'][0]
    assert player['team_id'] == '1' and player['fantasy_team'] == 'Oak'
    assert player['points'] == 5 and player['projected_points'] == 15
    assert player['percent_owned'] == 27.5 and player['current_status'] == 'QUESTIONABLE'
    assert player['eligible_slots'] == ['WR', 'RB/WR', 'RB/WR/TE']
    assert player['season_observed']['points'] == 33
    future = next(row for row in player['weekly_stats'] if row['week'] == 4)
    assert future['projected_points'] == 16 and 'points' not in future
    assert '900' not in json.dumps(result)


def test_historical_details_exclude_future_current_and_season_leakage(researcher):
    researcher.week = 2
    # The week/period comparison must protect callers even without the flag.
    result = tools.execute('get_player_season_details', {'player_ids': ['101']}, researcher)
    player = result['players'][0]
    assert player['points'] == 21
    assert [row['week'] for row in player['weekly_stats']] == [1, 2]
    assert player['fantasy_team'] == 'Historical ownership unknown'
    for field in ('current_status', 'percent_owned', 'eligible_slots', 'team_id', 'nfl_team',
                  'season_observed', 'season_projection', 'projected_points'):
        assert field not in player
    assert all('projected_points' not in row for row in player['weekly_stats'])


def test_missing_numeric_fields_and_sdk_negative_ownership_are_unknown(researcher):
    raw = raw_player()
    raw['player']['stats'] = [stat(3), stat(4, 1000), stat(3, 99, year=2025)]
    raw['player']['ownership'] = {'percentOwned': -1, 'percentStarted': float('nan')}
    tools._read_league.return_value = {'players': [raw]}
    player = tools.execute('get_player_season_details', {'player_ids': ['101']}, researcher)['players'][0]
    assert 'points' not in player and 'projected_points' not in player
    assert player['percent_owned'] is None and player['percent_started'] is None
    assert '99' not in json.dumps(player) and '1000' not in json.dumps(player)


def test_stat_field_ids_are_named_and_nonfinite_stats_are_omitted(researcher):
    raw = raw_player()
    raw['player']['stats'] = [stat(3, stats={'3': 111, '24': 3, '99': float('inf')})]
    tools._read_league.return_value = {'players': [raw]}
    player = tools.execute('get_player_season_details', {'player_ids': ['101']}, researcher)['players'][0]
    assert player['stats']['passingYards'] == 111
    assert '99' not in player['stats']


def test_newly_discovered_ids_expand_with_same_card_cache(researcher):
    tools._read_league.return_value = {'players': [raw_player(102, 'Player Beta', owner=0)]}
    tools.execute('search_players', {'query': 'beta'}, researcher)
    player = tools.execute('get_player_season_details', {'player_ids': ['102']}, researcher)['players'][0]
    assert player['name'] == 'Player Beta' and player['points'] == 5
    tools._read_league.assert_called_once()


def test_three_full_season_players_fit_central_evidence_budget(researcher):
    players = [raw_player(101 + i, 'Example Player ' + str(i), 0) for i in range(3)]
    for raw in players:
        raw['player']['stats'] = [stat(week, 25, source=source, stats={str(k): 20 for k in tools.PLAYER_STATS_MAP})
                                  for week in range(0, 26) for source in (0, 1)]
    tools._read_league.return_value = {'players': players}
    researcher.allowed.update({'102', '103'})
    result = tools.execute('get_player_season_details', {'player_ids': ['101', '102', '103']}, researcher)
    assert len(json.dumps(result, ensure_ascii=False)) < 14000
    assert all(len(player['stats']) <= 12 for player in result['players'])
    assert all(len(player['season_observed']['stats']) <= 12 for player in result['players'])


def test_discovered_context_supports_existing_stats_status_news_tools(researcher, monkeypatch):
    tools.execute('get_player_season_details', {'player_ids': ['101']}, researcher)
    news = Mock(return_value=([{'headline': 'Player Alpha practiced', 'published_at': '2026-09-23'}], 'now'))
    monkeypatch.setattr(tools, 'relevant_news', news)
    packet = tools.discovered_context(researcher, ['101'])
    assert packet['players'][0]['points'] == 5
    assert packet['players'][0]['current_status'] == 'QUESTIONABLE'
    assert packet['news'][0]['headline'] == 'Player Alpha practiced'
    tools.discovered_context(researcher, ['101'])
    news.assert_called_once()


def test_historical_discovered_context_does_not_read_news(researcher, monkeypatch):
    researcher.week = 2
    tools.execute('get_player_season_details', {'player_ids': ['101']}, researcher)
    news = Mock(side_effect=AssertionError('historical news leak'))
    monkeypatch.setattr(tools, 'relevant_news', news)
    packet = tools.discovered_context(researcher, ['101'])
    assert not packet['news'] and 'current_status' not in packet['players'][0]
    news.assert_not_called()


def test_free_agent_filters_are_bounded_and_do_not_claim_completeness(researcher):
    a, b = raw_player(102, 'Player Beta', 0, 'WAIVERS'), raw_player(103, 'Player Gamma', 0, 'FREEAGENT')
    b['player']['ownership']['percentOwned'] = 75
    tools._read_league.return_value = {'players': [a, b]}
    result = tools.execute('get_free_agent_pool', {'position': 'WR', 'offset': 20, 'limit': 2,
                                                  'max_percent_owned': 50, 'min_projected_points': 10}, researcher)
    assert result['scanned'] == 2 and result['returned'] == 1 and result['next_offset'] == 22
    assert result['partial_coverage'] is True
    assert result['players'][0]['availability'] == 'WAIVERS'
    assert result['players'][0]['fantasy_team'] == 'Unrostered or on waivers'
    filters = tools._read_league.call_args.args[2]['players']
    assert filters['filterSlotIds']['value'] == [4] and filters['limit'] == 2
    assert filters['filterStatus']['value'] == ['FREEAGENT', 'WAIVERS']


@pytest.mark.parametrize('week', [2, 4])
def test_free_agents_cannot_answer_past_or_future_ownership(researcher, week):
    researcher.week = week
    assert 'error' in tools.execute('get_free_agent_pool', {}, researcher)
    tools._read_league.assert_not_called()


def test_free_agent_page_hard_stop(researcher):
    tools._read_league.return_value = {'players': [raw_player(100 + i, owner=0) for i in range(30)]}
    result = tools.execute('get_free_agent_pool', {'offset': 100, 'limit': 20}, researcher)
    assert result['scanned'] == 20 and len(result['players']) == 20 and result['next_offset'] is None


def test_free_agent_result_cannot_override_explicit_current_owner(researcher):
    tools._read_league.return_value = {'players': [raw_player(101, owner=1), raw_player(102, owner=0)]}
    result = tools.execute('get_free_agent_pool', {}, researcher)
    assert [player['id'] for player in result['players']] == ['102']


def schedule(complete=True):
    weeks = range(1, 19) if complete else range(1, 6)
    return {'settings': {'proTeams': [{'id': 2, 'proGamesByScoringPeriod': {
        str(week): [{'homeProTeamId': 2, 'awayProTeamId': 1, 'date': 1789516800000 + week * 604800000}]
        for week in weeks if week != 4}}]}}


@pytest.mark.parametrize('complete,expected', [(True, 4), (False, None)])
def test_nfl_schedule_byes_need_complete_regular_season(researcher, complete, expected):
    tools._read_public.side_effect = None
    tools._read_public.return_value = schedule(complete)
    result = tools.execute('get_player_nfl_schedule', {'player_ids': ['101'], 'start_week': 3, 'weeks': 3}, researcher)
    player = result['players'][0]
    assert player['verified_bye_week'] == expected
    assert player['schedule'][0]['opponent'] == 'ATL'
    assert player['schedule'][0]['kickoff'].endswith('+00:00')
    assert player['schedule'][1]['bye'] is (True if complete else None)
    assert tools._read_public.call_args.args[1] == tools.FANTASY + '2026'
    assert tools._read_public.call_args.args[2] == {'view': 'proTeamSchedules_wl'}


def test_historical_player_schedule_is_unavailable(researcher):
    researcher.week = 2
    assert 'error' in tools.execute('get_player_nfl_schedule', {'player_ids': ['101']}, researcher)
    tools._read_public.assert_not_called()


def test_current_affiliation_cannot_reconstruct_prior_player_schedule(researcher):
    assert 'error' in tools.execute('get_player_nfl_schedule', {'player_ids': ['101'], 'start_week': 1}, researcher)
    tools._read_public.assert_not_called()


def test_corrupt_complete_schedule_does_not_establish_bye(researcher):
    data = schedule()
    games = data['settings']['proTeams'][0]['proGamesByScoringPeriod']
    games['2'][0]['date'] = games['1'][0]['date']
    tools._read_public.side_effect = None
    tools._read_public.return_value = data
    result = tools.execute('get_player_nfl_schedule', {'player_ids': ['101'], 'start_week': 4}, researcher)
    assert result['players'][0]['verified_bye_week'] is None
    assert result['players'][0]['schedule'][0]['bye'] is None


def test_discovered_news_does_not_outlive_short_research_budget(researcher, monkeypatch):
    tools.execute('get_player_season_details', {'player_ids': ['101']}, researcher)
    researcher.deadline = time.monotonic() + 10
    news = Mock(side_effect=AssertionError('insufficient deadline'))
    monkeypatch.setattr(tools, 'relevant_news', news)
    packet = tools.discovered_context(researcher, ['101'])
    assert packet['players'] and packet['news'] == []
    news.assert_not_called()


def nfl_event(event_id='401000001', week=3, state='post'):
    return {'id': event_id, 'date': '2026-09-24T20:00Z', 'season': {'year': 2026, 'type': 2},
            'week': {'number': week}, 'status': {'type': {'state': state, 'completed': state == 'post', 'detail': 'Final'}},
            'competitions': [{'competitors': [
                {'team': {'abbreviation': 'BUF', 'displayName': 'Buffalo Bills'}, 'homeAway': 'home', 'score': '24'},
                {'team': {'abbreviation': 'ATL', 'displayName': 'Atlanta Falcons'}, 'homeAway': 'away', 'score': '17'}]}]}


def scoreboard(week=3, state='post'):
    return {'season': {'year': 2026, 'type': 2}, 'week': {'number': week}, 'events': [nfl_event(week=week, state=state)]}


def test_scoreboard_verifies_scope_and_registers_event_ids(researcher):
    tools._read_public.side_effect = None
    tools._read_public.return_value = scoreboard()
    result = tools.execute('get_nfl_scoreboard', {}, researcher)
    assert result['games'][0]['teams'][0]['nfl_score'] == 24
    assert result['games'][0]['completed'] is True
    assert researcher.player_tools_cache['nfl_events']['401000001'] == {'week': 3, 'season': 2026}
    assert tools._read_public.call_args.args[2] == {'dates': 2026, 'seasontype': 2, 'week': 3, 'limit': 100}


def test_pregame_scoreboard_omits_default_zero_or_stale_scores(researcher):
    tools._read_public.side_effect = None
    tools._read_public.return_value = scoreboard(state='pre')
    result = tools.execute('get_nfl_scoreboard', {'week': 3}, researcher)
    assert all(row['nfl_score'] is None for row in result['games'][0]['teams'])


@pytest.mark.parametrize('field,value', [('season', {'year': 2025, 'type': 2}), ('week', {'number': 9})])
def test_scoreboard_scope_mismatch_is_not_delivered(researcher, field, value):
    data = scoreboard()
    data[field] = value
    tools._read_public.side_effect = None
    tools._read_public.return_value = data
    assert 'error' in tools.execute('get_nfl_scoreboard', {}, researcher)


def test_mixed_week_event_is_omitted_even_with_correct_feed_header(researcher):
    data = scoreboard()
    data['events'][0]['week']['number'] = 8
    tools._read_public.side_effect = None
    tools._read_public.return_value = data
    assert tools.execute('get_nfl_scoreboard', {}, researcher)['games'] == []


def test_historical_scoreboard_rejects_later_week_without_read(researcher):
    researcher.week = 2
    assert 'error' in tools.execute('get_nfl_scoreboard', {'week': 3}, researcher)
    tools._read_public.assert_not_called()


def game_summary():
    event = nfl_event()
    event['week'] = 3
    return {'header': event, 'article': {'story': 'DO NOT SEND FULL ARTICLE'},
            'injuries': [{'private': 'do not return'}],
            'boxscore': {'teams': [{'team': {'abbreviation': 'BUF'}, 'statistics': [
                {'name': 'totalYards', 'label': 'Total Yards', 'displayValue': '420'}] * 35}] * 3},
            'leaders': [{'team': {'abbreviation': 'BUF'}, 'leaders': [
                {'name': 'passing', 'displayName': 'Passing', 'leaders': [
                    {'athlete': {'id': '101', 'displayName': 'Player Alpha'}, 'displayValue': '20/30, 280 YDS, 2 TD'}]}] * 8}] * 3,
            'scoringPlays': [{'text': 'Verified touchdown', 'homeScore': i, 'awayScore': 0,
                             'period': {'number': 4}, 'clock': {'displayValue': '2:00'}} for i in range(10)]}


def test_summary_is_bounded_and_no_article_odds_or_current_injuries_escape(researcher):
    tools._read_public.side_effect = [scoreboard(), game_summary()]
    tools.execute('get_nfl_scoreboard', {}, researcher)
    result = tools.execute('get_nfl_game_summary', {'event_id': '401000001'}, researcher)
    assert len(result['team_statistics']) == 2
    assert len(result['team_statistics'][0]['statistics']) == 30
    assert len(result['leaders']) == 12
    assert len(result['recent_scoring_plays']) == 6
    assert result['recent_scoring_plays'][0]['home_score'] == 4
    assert 'FULL ARTICLE' not in json.dumps(result) and 'private' not in json.dumps(result)
    assert tools._read_public.call_args.args[1] == tools.SUMMARY
    assert tools._read_public.call_args.args[2] == {'event': '401000001'}


def test_summary_requires_discovered_event_and_matching_scope(researcher):
    assert 'error' in tools.execute('get_nfl_game_summary', {'event_id': '401000001'}, researcher)
    tools._read_public.assert_not_called()
    tools._read_public.side_effect = [scoreboard(), {**game_summary(), 'header': {'id': 'wrong'}}]
    tools.execute('get_nfl_scoreboard', {}, researcher)
    # Failed lookups are cached too; use the known event after dropping only that failure.
    researcher.player_tools_cache.pop(('result', 'get_nfl_game_summary', '{"event_id": "401000001"}'))
    assert 'error' in tools.execute('get_nfl_game_summary', {'event_id': '401000001'}, researcher)


@pytest.mark.parametrize('name,args', [
    ('search_players', {'query': ''}), ('search_players', {'query': 'a'}),
    ('search_players', {'query': 'player', 'limit': True}),
    ('search_players', {'query': 'player', 'url': 'https://example.invalid'}),
    ('get_player_season_details', {'player_ids': ['999']}),
    ('get_player_season_details', {'player_ids': [101]}),
    ('get_player_season_details', {'player_ids': ['101', '101']}),
    ('get_player_season_details', {'player_ids': ['../secret']}),
    ('get_free_agent_pool', {'offset': 101}), ('get_free_agent_pool', {'limit': 21}),
    ('get_free_agent_pool', {'position': 'WR; DROP TABLE'}),
    ('get_free_agent_pool', {'max_percent_owned': float('nan')}),
    ('get_player_nfl_schedule', {'player_ids': ['101'], 'weeks': 7}),
    ('get_nfl_scoreboard', {'week': True}), ('get_nfl_scoreboard', {'week': 19}),
    ('get_nfl_game_summary', {'event_id': '1?url=localhost'}),
    ('get_nfl_game_summary', {'event_id': 123}),
    ('get_nfl_game_summary', {'event_id': '123', 'headers': {}}),
])
def test_invalid_arguments_never_reach_network(researcher, name, args):
    assert 'error' in tools.execute(name, args, researcher)
    tools._read_league.assert_not_called()
    tools._read_public.assert_not_called()


def test_deadline_and_upstream_errors_are_safe(researcher):
    researcher.deadline = time.monotonic()
    assert 'error' in tools.execute('search_players', {'query': 'beta'}, researcher)
    tools._read_league.assert_not_called()
    researcher.deadline = time.monotonic() + 60
    tools._read_league.side_effect = RuntimeError('secret-credential')
    result = tools.execute('search_players', {'query': 'beta'}, researcher)
    assert 'secret' not in json.dumps(result) and 'error' in result


def test_actual_read_helpers_use_shared_bounded_transport(monkeypatch, researcher):
    from gamedaybot.espn import espn_read
    # Undo fixture mocks only for this transport-contract test.
    monkeypatch.undo()
    league_read = Mock(return_value={'players': [raw_player()]})
    public_read = Mock(return_value=scoreboard())
    monkeypatch.setattr(espn_read, 'read_league', league_read)
    monkeypatch.setattr(espn_read, 'read_espn', public_read)
    assert tools.execute('get_player_season_details', {'player_ids': ['101']}, researcher)['players']
    assert tools.execute('get_nfl_scoreboard', {}, researcher)['games']
    assert league_read.call_args.args == (researcher.league, ['kona_playercard'])
    assert league_read.call_args.kwargs['deadline'] == researcher.deadline
    assert league_read.call_args.kwargs['max_bytes'] == 2_000_000
    assert public_read.call_args.args == (tools.SCOREBOARD,)
    assert 'cookies' not in public_read.call_args.kwargs
    assert public_read.call_args.kwargs['timeout'] == 8
