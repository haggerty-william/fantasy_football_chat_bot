"""Cross-provider dispatch, discovery follow-ups and one shared research budget."""
from copy import deepcopy
import json
import time
from types import SimpleNamespace as O
from unittest.mock import Mock

import pytest

from gamedaybot.espn import research_tools as dispatch, player_tools, espn_read
from gamedaybot.espn.analysis_limits import MAX_TOOL_CALLS


def player_row(pid=7, name='Player Alpha'):
    return {'id': pid, 'onTeamId': 0, 'player': {'id': pid, 'fullName': name,
            'defaultPositionId': 4, 'eligibleSlots': [4, 3, 23], 'proTeamId': 2,
            'injuryStatus': 'QUESTIONABLE', 'ownership': {'percentOwned': 25},
            'stats': [{'seasonId': 2026, 'statSourceId': 0, 'statSplitTypeId': 1,
                       'scoringPeriodId': 3, 'appliedTotal': 17, 'stats': {'53': 8}},
                      {'seasonId': 2026, 'statSourceId': 1, 'statSplitTypeId': 1,
                       'scoringPeriodId': 3, 'appliedTotal': 12}],
            'email': 'owner-private@example.invalid', 'secretCookie': 'fake-credential-never-forward'}}


@pytest.fixture
def researcher(monkeypatch):
    teams = [O(team_id=1, team_name='Oak', roster=[], owners=[]),
             O(team_id=2, team_name='Pine', roster=[], owners=[])]
    league = O(league_id=123, year=2026, scoringPeriodId=3, current_week=3, teams=teams,
               player_map={7: 'Player Alpha', 8: 'Player Beta'},
               settings=O(matchup_periods={str(w): [w] for w in range(1, 19)}))
    context = {'week': 3, 'players': [], 'rosters': [], 'news': [], 'historical': False}
    return dispatch.ResearchTools(league, context, 3, None, time.monotonic() + 120)


def test_all_32_tools_are_unique_and_every_provider_is_registered():
    names = [item['function']['name'] for item in dispatch.TOOLS]
    assert len(names) == len(set(names)) == 32
    assert set(names) == set(dispatch.DESCRIPTIONS) | set(dispatch.PROVIDER_BY_NAME)
    assert len(dispatch.PROVIDERS) == 5
    for provider in dispatch.PROVIDERS:
        assert all(dispatch.PROVIDER_BY_NAME[name] is provider for name in provider.NAMES)
    assert all(item['function']['parameters']['additionalProperties'] is False for item in dispatch.TOOLS)


@pytest.mark.parametrize('name', sorted(dispatch.PROVIDER_BY_NAME))
def test_every_provider_tool_dispatches_and_records_sanitized_evidence(researcher, monkeypatch, name):
    provider = dispatch.PROVIDER_BY_NAME[name]
    fake = Mock(return_value={'status': 'available', 'marker': name})
    monkeypatch.setattr(provider, 'execute', fake)
    result = researcher.execute(name, '{}')
    assert result['marker'] == name
    fake.assert_called_once_with(name, {}, researcher)
    assert researcher.context['tool_evidence'] == [{'tool': name, 'result': result}]
    assert researcher.execute(name, ' { } ') is result
    fake.assert_called_once()


@pytest.mark.parametrize('entry_tool,args', [('search_players', {'query': 'Alpha'}),
                                          ('get_free_agent_pool', {'limit': 1})])
def test_discovery_allows_stats_news_status_followups_and_shares_fetch(researcher, monkeypatch, entry_tool, args):
    fetch = Mock(return_value={'players': [player_row()]})
    monkeypatch.setattr(espn_read, 'read_league', fetch)
    news = [{'headline': 'Player Alpha limited at practice', 'published_at': '2026-09-23T12:00:00Z',
             'url': 'https://www.espn.com/nfl/story/example', 'players': ['Player Alpha']}]
    monkeypatch.setattr(player_tools, 'relevant_news', lambda *args: (news, '2026-09-24T12:00:00Z'))
    empty_packet = {'source': 'ESPN Fantasy', 'fetched_at': 'now', 'week': 3,
                    'players': [], 'news': [], 'limitations': []}
    base = Mock(side_effect=lambda *args, **kwargs: deepcopy(empty_packet))
    monkeypatch.setattr(dispatch, 'build_context', base)
    result = researcher.execute(entry_tool, json.dumps(args))
    assert result['players'][0]['id'] == '7' and '7' in researcher.allowed
    stats = researcher.execute('get_player_stats', '{"player_ids":["7"]}')
    assert stats['players'][0]['points'] == 17
    assert stats['players'][0]['projected_points'] == 12
    status = researcher.execute('get_player_status', '{"player_ids":["7"]}')
    assert status['players'][0]['current_status'] == 'QUESTIONABLE'
    assert 'points' not in status['players'][0]
    articles = researcher.execute('get_player_news', '{"player_ids":["7"]}')
    assert articles['news'] == news and articles['players'] == [{'id': '7', 'name': 'Player Alpha'}]
    assert articles['news_status'] == 'Available'
    assert articles['news_fetched_at'] == '2026-09-24T12:00:00Z'
    assert any('Discovered players' in note for note in articles['limitations'])
    assert researcher.calls == 2
    assert base.call_count == fetch.call_count == 1
    assert researcher.context['players'][0]['points'] == 17
    raw = json.dumps([result, stats, status, articles, researcher.context])
    assert 'fake-credential-never-forward' not in raw and 'owner-private@example.invalid' not in raw


def test_unknown_discovery_id_is_rejected_until_server_verifies_it(researcher, monkeypatch):
    fetch = Mock(return_value={'players': [player_row(8, 'Wrong Player')]})
    monkeypatch.setattr(espn_read, 'read_league', fetch)
    researcher.execute('search_players', '{"query":"Alpha"}')
    assert not researcher.allowed
    assert 'error' in researcher.execute('get_player_stats', '{"player_ids":["8"]}')
    assert 'error' in researcher.execute('get_player_season_details', '{"player_ids":["8"]}')
    assert fetch.call_count == 1


def test_all_providers_share_twelve_call_budget_and_cached_evidence_is_free(researcher, monkeypatch):
    calls = []
    for provider in dispatch.PROVIDERS:
        monkeypatch.setattr(provider, 'execute', lambda name, args, r: calls.append(name) or {'status': 'ok'})
    names = [name for name in sorted(dispatch.PROVIDER_BY_NAME) if name != 'get_workload_changes']
    assert MAX_TOOL_CALLS == 12
    for name in names[:MAX_TOOL_CALLS]:
        assert 'error' not in researcher.execute(name, '{}')
    assert researcher.calls == len(calls) == 12
    assert 'error' in researcher.execute(names[12], '{}')
    assert researcher.execute(names[0], '{}') == {'status': 'ok'}
    assert researcher.calls == len(calls) == 12


def test_base_player_research_and_provider_research_share_budget(researcher, monkeypatch):
    researcher.allowed = {'7'}
    researcher.calls = 11
    fetch = Mock(return_value={'source': 'ESPN Fantasy', 'fetched_at': 'now', 'week': 3,
                              'players': [{'id': 7, 'name': 'Player Alpha', 'points': 17}], 'news': [], 'limitations': []})
    monkeypatch.setattr(dispatch, 'build_context', fetch)
    assert researcher.execute('get_player_stats', '{"player_ids":["7"]}')['players']
    assert researcher.calls == 12
    assert 'error' in researcher.execute('get_fantasy_schedule', '{}')
    # A different projection of the already-fetched player evidence is free.
    assert 'error' not in researcher.execute('get_player_news', '{"player_ids":["7"]}')
    fetch.assert_called_once()


def test_workload_nested_player_fetch_only_consumes_one_call(researcher, monkeypatch):
    researcher.allowed = {'7'}
    fetch = Mock(return_value={'source': 'ESPN Fantasy', 'fetched_at': 'now', 'week': 3,
                              'players': [{'id': '7', 'name': 'Player Alpha', 'usage': {'weeks': []}}],
                              'news': [], 'limitations': []})
    monkeypatch.setattr(dispatch, 'build_context', fetch)
    assert 'error' not in researcher.execute('get_workload_changes', '{"player_ids":["7"]}')
    assert researcher.calls == 1
    assert 'error' not in researcher.execute('get_player_stats', '{"player_ids":["7"]}')
    assert researcher.calls == 1


@pytest.mark.parametrize('name,args', [
    ('get_fantasy_schedule', {'team_id': '999'}),
    ('get_week_matchups', {'matchup_period': True}),
    ('get_league_rules', {'url': 'https://example.invalid'}),
    ('get_pending_trades', {'team_id': '999'}),
    ('get_recent_trades', {'limit': 99999}),
    ('evaluate_trade_proposal', {'team_a_id': '1'}),
    ('search_players', {'query': 'Alpha', 'cookies': 'private'}),
    ('get_player_season_details', {'player_ids': ['7777']}),
    ('get_nfl_game_summary', {'event_id': '123'}),
    ('get_free_agent_pool', {'offset': -1}),
])
def test_real_provider_argument_validation_blocks_untrusted_reads(researcher, monkeypatch, name, args):
    read = Mock(side_effect=AssertionError('Unexpected source access'))
    monkeypatch.setattr(espn_read, 'read_league', read)
    monkeypatch.setattr(espn_read, 'read_espn', read)
    monkeypatch.setattr(dispatch.league_tools, 'read_league', read)
    assert 'error' in researcher.execute(name, json.dumps(args))
    read.assert_not_called()


@pytest.mark.parametrize('raw', ['[]', 'null', 'false', 'not json', '{' + 'a' * 1001, None])
def test_invalid_argument_envelopes_never_escape_to_source(researcher, monkeypatch, raw):
    read = Mock(side_effect=AssertionError('Unexpected source access'))
    monkeypatch.setattr(dispatch.league_tools, 'read_league', read)
    assert 'error' in researcher.execute('get_league_rules', raw)
    read.assert_not_called()


def test_expired_deadline_blocks_all_new_provider_requests(researcher, monkeypatch):
    researcher.deadline = time.monotonic()
    for provider in dispatch.PROVIDERS:
        monkeypatch.setattr(provider, 'execute', Mock(side_effect=AssertionError('Expired provider was called')))
    for name in dispatch.PROVIDER_BY_NAME:
        assert 'error' in researcher.execute(name, '{}')
    assert researcher.calls == 0


def test_provider_exception_message_not_in_evidence_or_logs(researcher, monkeypatch, caplog):
    monkeypatch.setattr(dispatch.league_tools, 'execute', Mock(side_effect=ValueError('secretCookie=fake-credential')))
    result = researcher.execute('get_league_rules', '{}')
    assert 'error' in result
    assert 'fake-credential' not in json.dumps(result) + caplog.text


def test_oversized_results_replaced_before_reaching_context(researcher, monkeypatch):
    monkeypatch.setattr(dispatch.league_tools, 'execute', lambda *args: {'large': 'x' * (dispatch.MAX_EVIDENCE_CHARS + 1)})
    result = researcher.execute('get_league_rules', '{}')
    assert 'error' in result and 'Narrow' in result['error']
    assert researcher.context['tool_evidence'][0]['result'] == result
    assert len(json.dumps(researcher.context)) < 1000


def test_stored_tool_evidence_is_grouped_in_canonical_team_packet(researcher, monkeypatch):
    from gamedaybot.espn.analysis_packet import AnalysisPacket
    payload = {'teams': [{'team_id': '1', 'team': 'Oak', 'observed': 14}], 'limitations': 'Bounded sample.'}
    monkeypatch.setattr(dispatch.league_tools, 'execute', lambda *args: payload)
    result = researcher.execute('get_scoring_splits', '{}')
    packet = AnalysisPacket(researcher.league, researcher.context, 'Report', 'get_standings', 3, 'now')
    receipt = packet.add_tool_result('test-call', 'get_scoring_splits', result, {})
    assert receipt['status'] == 'stored'
    assert 'research' in packet.teams['1']
    assert packet.dumps().count('Oak') == 1
    assert '"observed":14' in packet.dumps()


def validator_context():
    return {'week': 3, 'players': [{'id': '7', 'name': 'Player Alpha', 'points': 5,
                                  'current_status': 'ACTIVE', 'stats': {'receivingTargets': 2}}],
            'fantasy_teams': [{'id': '1', 'name': 'Oak', 'managers': ['Alice Example']}],
            'rosters': [], 'tool_evidence': []}


def test_validator_uses_explicit_week_points_and_stats_from_season_details():
    from gamedaybot.espn.commentary_checks import check_commentary
    context=validator_context()
    context['tool_evidence']=[{'tool':'get_player_season_details','result':{'players':[
        {'id':'7','name':'Player Alpha','week':3,'points':5,'current_status':'QUESTIONABLE',
         'stats':{'receivingTargets':4},'weekly_stats':[
             {'week':1,'points':12,'breakdown':{'receivingTargets':7}},
             {'week':2,'points':9,'breakdown':{'receivingTargets':8}}]}]}}]
    assert check_commentary('Player Alpha scored 12 fantasy points in Week 1.','',context)==[]
    assert check_commentary('Player Alpha had 7 targets in Week 1.','',context)==[]
    assert check_commentary('Player Alpha is questionable.','',context)==[]
    assert 'player_points_mismatch' in check_commentary('Player Alpha scored 5 points in Week 1.','',context)
    assert 'player_points_mismatch' in check_commentary('Player Alpha scored 12 points in Week 2.','',context)
    assert 'player_usage_mismatch' in check_commentary('Player Alpha had 8 targets in Week 1.','',context)
    assert 'player_usage_mismatch' in check_commentary('Player Alpha had 7 targets in Week 3.','',context)


def test_validator_accepts_historical_box_points_without_overwriting_current_points():
    from gamedaybot.espn.commentary_checks import check_commentary, _entries
    context=validator_context()
    context['tool_evidence']=[{'tool':'get_week_box_scores','result':{'week':1,'teams':[
        {'team_id':'1','team':'Oak','players':[{'id':'7','name':'Player Alpha','points':12,'lineup_slot':'RB'}]}]}}]
    before=deepcopy(context)
    assert check_commentary('Player Alpha scored 12 points in Week 1.','',context)==[]
    assert check_commentary('Player Alpha scored 5 points in Week 3.','',context)==[]
    assert 'player_points_mismatch' in check_commentary('Player Alpha scored 12 points in Week 3.','',context)
    assert _entries(context)['Player Alpha']['points']==5
    assert context==before


def test_tool_only_discovered_player_has_status_health_and_score_guards():
    from gamedaybot.espn.commentary_checks import check_commentary
    context=validator_context()
    context['tool_evidence']=[{'tool':'get_free_agent_pool','result':{'players':[
        {'id':'8','name':'Player Beta','week':3,'points':11,'current_status':'QUESTIONABLE'}]}}]
    assert check_commentary('Player Beta scored 11 fantasy points in Week 3.','',context)==[]
    assert 'player_points_mismatch' in check_commentary('Player Beta scored 5 fantasy points in Week 3.','',context)
    assert 'status_mismatch' in check_commentary('Player Beta is active.','',context)
    assert 'unsupported_health_inference' in check_commentary('Player Beta is healthy.','',context)


def test_profile_current_status_is_used_but_historical_profile_cannot_override_status():
    from gamedaybot.espn.commentary_checks import check_commentary
    context=validator_context()
    context['tool_evidence']=[{'tool':'get_team_profile','result':{'team':'Oak','roster':[
        {'id':'7','name':'Player Alpha','current_status':'QUESTIONABLE'}]}}]
    assert 'status_mismatch' not in check_commentary('Player Alpha is questionable.','',context)
    context['historical']=True
    assert 'current_status_in_historical_recap' in check_commentary('Player Alpha is questionable.','',context)


def nfl_context():
    context=validator_context()
    context['matchups']=[{'week':3,'sides':[{'team':'Oak','score':100}]}]
    context['league_history']={'teams':[{'team':'Oak','performance':{'average_margin':10}}]}
    context['tool_evidence']=[{'tool':'get_nfl_scoreboard','result':{'games':[
        {'week':3,'teams':[{'nfl_team':'BUF','name':'Buffalo Bills','nfl_score':24}]}]}},
        {'tool':'get_nfl_game_summary','result':{'game':{'week':3,'teams':[
            {'nfl_team':'BUF','name':'Buffalo Bills','nfl_score':24}]},
            'leaders':[{'name':'Player Alpha','summary':'24 receiving yards','points':24}]}}]
    return context


def test_nfl_score_and_leader_numbers_do_not_validate_fantasy_point_claims():
    from gamedaybot.espn.commentary_checks import check_commentary
    context=nfl_context()
    assert 'player_points_mismatch' in check_commentary('Player Alpha scored 24 fantasy points.','',context)
    assert 'fantasy_team_points_mismatch' in check_commentary('Oak scored 24 fantasy points in Week 3.','',context)
    assert check_commentary('Oak scored 100 fantasy points in Week 3.','',context)==[]
    assert 'nfl_score_is_not_fantasy_points' in check_commentary('Buffalo Bills scored 24 fantasy points.','',context)
    assert check_commentary('Buffalo Bills scored 24 points.','',context)==[]


def test_nfl_club_defense_comments_do_not_trigger_fantasy_control_guard():
    from gamedaybot.espn.commentary_checks import check_commentary
    context=nfl_context()
    assert 'unsupported_opponent_scoring_control' not in check_commentary('Buffalo Bills need to improve their defense.','',context)
    assert 'unsupported_opponent_scoring_control' in check_commentary('Oak needs to improve its defense.','',context)


def test_new_matchup_tool_validates_explicit_fantasy_week_without_using_nfl_score():
    from gamedaybot.espn.commentary_checks import check_commentary
    context=nfl_context()
    context['tool_evidence'].append({'tool':'get_week_matchups','result':{'matchups':[
        {'scoring_weeks':[1],'teams':[{'team':'Oak','score':24}]}]}})
    assert 'fantasy_team_points_mismatch' not in check_commentary('Oak scored 24 points in Week 1.','',context)
    assert 'fantasy_team_points_mismatch' in check_commentary('Oak scored 24 points in Week 3.','',context)


def test_past_lineup_is_not_judged_using_current_bench_or_ownership():
    from gamedaybot.espn.commentary_checks import check_commentary
    context=validator_context()
    context['players'][0].update(fantasy_team='Oak',slot='BE',game={'state':'post'})
    context['matchups']=[{'week':3,'sides':[]}]
    context['rosters']=[{'team':'Oak','players':[]},{'team':'Pine','players':[]}]
    context['tool_evidence']=[{'tool':'get_week_box_scores','result':{'week':1,'teams':[
        {'team_id':'2','team':'Pine','players':[{'id':'7','name':'Player Alpha','points':12,'lineup_slot':'WR'}]}]}}]
    assert 'bench_points_do_not_contribute' not in check_commentary('Player Alpha powered Pine in Week 1.','',context)
    assert 'ownership_mismatch' not in check_commentary("Pine's WR Player Alpha scored 12 points in Week 1.",'',context)
    assert 'ownership_mismatch' in check_commentary("Oak's WR Player Alpha scored 12 points in Week 1.",'',context)


@pytest.mark.parametrize('tool,args,result', [
    ('get_team_profile', {'team_id': '1'}, {'team_id': '1', 'team': 'Oak', 'roster': [{'id': '7', 'name': 'Player Alpha'}]}),
    ('get_week_box_scores', {'team_id': '1', 'week': 1}, {'week': 1, 'teams': [
        {'team_id': '1', 'team': 'Oak', 'players': [{'id': '7', 'name': 'Player Alpha', 'points': 99}]}]}),
    ('get_draft_board', {}, {'teams': [{'team_id': '1', 'team': 'Oak', 'picks': [{'player_id': '7', 'player': 'Player Alpha'}]}]}),
    ('get_recent_trades', {}, {'trades': [{'id': '999', 'items': [{'player_id': '7', 'player_name': 'Player Alpha', 'from_team_id': '1', 'to_team_id': '2'}]}]}),
    ('get_pending_trades', {}, {'offers': [{'id': '999', 'items': [{'player_id': '7', 'player_name': 'Player Alpha', 'from_team_id': '1', 'to_team_id': '2'}]}]}),
    ('get_transaction_history', {}, {'transactions': [{'id': '999', 'items': [{'player_id': '7', 'type': 'DROP'}]}]}),
])
def test_verified_provider_player_ids_allow_bounded_legacy_followups(researcher, monkeypatch, tool, args, result):
    provider = dispatch.PROVIDER_BY_NAME[tool]
    monkeypatch.setattr(provider, 'execute', lambda *unused: deepcopy(result))
    assert not researcher.allowed
    assert 'error' not in researcher.execute(tool, json.dumps(args))
    assert researcher.allowed == {'7'}
    fetch = Mock(return_value={'players': [player_row()]})
    monkeypatch.setattr(espn_read, 'read_league', fetch)
    monkeypatch.setattr(player_tools, 'relevant_news', lambda *unused: ([], 'now'))
    empty = {'source': 'ESPN Fantasy', 'fetched_at': 'now', 'week': 3, 'players': [], 'news': [], 'limitations': []}
    monkeypatch.setattr(dispatch, 'build_context', lambda *unused, **kwargs: deepcopy(empty))
    stats = researcher.execute('get_player_stats', '{"player_ids":["7"]}')
    assert stats['players'][0]['points'] == 17
    assert stats['players'][0]['fantasy_team'] == 'Unrostered or on waivers'
    assert researcher.execute('get_player_status', '{"player_ids":["7"]}')['players'][0]['current_status'] == 'QUESTIONABLE'
    assert researcher.execute('get_player_news', '{"player_ids":["7"]}')['players'][0]['name'] == 'Player Alpha'
    fetch.assert_called_once()
    assert fetch.call_args.args[1] == ['kona_playercard']
    assert fetch.call_args.kwargs['filters']['players']['filterIds']['value'] == [7]
    assert researcher.calls == 2


def test_registry_uses_explicit_player_fields_not_team_event_stats_or_notes():
    result = {
        'id': '100', 'player_id': '101', 'team_id': '102', 'event_id': '103',
        'teams': [{'id': '104', 'name': 'NFL team', 'team_id': '105',
                   'players': [{'id': '7', 'name': 'Player Alpha'}],
                   'picks': [{'id': '106', 'player_id': '8'}]}],
        'trades': [{'id': '107', 'items': [{'id': '108', 'player_id': '-16001', 'from_team_id': '109'}]}],
        'notes': [{'players': [{'id': '110'}], 'player_id': '111'}],
        'leaders': [{'player_id': '112', 'name': 'NFL-only identity'}],
        'statistics': [{'id': '113'}], 'games': [{'id': '114'}],
    }
    assert dispatch.verified_result_player_ids(result) == {'7', '8', '-16001'}


@pytest.mark.parametrize('value', [True, False, 0, '0', 'NaN', 'http://localhost', '../7', 7.1, None, '7' * 11])
def test_registry_rejects_malformed_player_identifiers(value):
    assert dispatch.verified_result_player_ids({'players': [{'id': value, 'name': 'Invalid'}]}) == set()


def test_rejected_or_oversized_evidence_does_not_register_hidden_ids(researcher, monkeypatch):
    result = {'roster': [{'id': '7', 'name': 'Player Alpha'}], 'padding': 'x' * dispatch.MAX_EVIDENCE_CHARS}
    monkeypatch.setattr(dispatch.league_tools, 'execute', lambda *args: result)
    assert 'error' in researcher.execute('get_team_profile', '{"team_id":"1"}')
    assert not researcher.allowed
    assert dispatch.verified_result_player_ids({'error': 'unavailable', 'players': [{'id': '7'}]}) == set()


def test_draft_identity_allows_season_details_without_implying_current_draft_owner(researcher, monkeypatch):
    result = {'teams': [{'team_id': '1', 'team': 'Oak', 'picks': [{'player_id': '7', 'player': 'Player Alpha'}]}]}
    monkeypatch.setattr(dispatch.league_tools, 'execute', lambda *args: result)
    researcher.execute('get_draft_board', '{}')
    raw = player_row()
    raw['onTeamId'] = 2
    monkeypatch.setattr(espn_read, 'read_league', Mock(return_value={'players': [raw]}))
    detail = researcher.execute('get_player_season_details', '{"player_ids":["7"]}')['players'][0]
    assert detail['fantasy_team'] == 'Pine' and detail['team_id'] == '2'
    assert researcher.context['tool_evidence'][0]['result']['teams'][0]['team'] == 'Oak'


def test_historical_discovered_id_hydration_does_not_attach_current_ownership_or_status(researcher, monkeypatch):
    researcher.week = 1
    researcher.context['historical'] = True
    researcher.context['week'] = 1
    old_lineup = {'week': 1, 'teams': [{'team_id': '1', 'team': 'Oak', 'players': [{'id': '7', 'name': 'Player Alpha', 'points': 12}]}]}
    monkeypatch.setattr(dispatch.league_tools, 'execute', lambda *args: old_lineup)
    researcher.execute('get_week_box_scores', '{"week":1,"team_id":"1"}')
    raw = player_row()
    raw['onTeamId'] = 2
    raw['player']['stats'].append({'seasonId': 2026, 'statSourceId': 0, 'statSplitTypeId': 1,
                                   'scoringPeriodId': 1, 'appliedTotal': 12, 'stats': {}})
    fetch = Mock(return_value={'players': [raw]})
    monkeypatch.setattr(espn_read, 'read_league', fetch)
    monkeypatch.setattr(dispatch, 'build_context', lambda *args, **kwargs: {
        'source': 'ESPN Fantasy', 'fetched_at': 'now', 'week': 1, 'players': [], 'news': [], 'limitations': []})
    news = Mock(side_effect=AssertionError('historical news must not be requested'))
    monkeypatch.setattr(player_tools, 'relevant_news', news)
    player = researcher.execute('get_player_stats', '{"player_ids":["7"]}')['players'][0]
    assert player['points'] == 12 and player['fantasy_team'] == 'Historical ownership unknown'
    assert 'current_status' not in player and 'projected_points' not in player
    assert 'error' in researcher.execute('get_player_status', '{"player_ids":["7"]}')
    assert 'error' in researcher.execute('get_player_news', '{"player_ids":["7"]}')
    news.assert_not_called()
    assert researcher.context['tool_evidence'][0]['result'] == old_lineup


def test_historical_lineup_discovery_preserves_existing_current_roster_evidence(researcher, monkeypatch):
    old = {'week': 1, 'teams': [{'team_id': '2', 'team': 'Pine', 'players': [{'id': '7', 'name': 'Player Alpha', 'points': 99}]}]}
    monkeypatch.setattr(dispatch.league_tools, 'execute', lambda *args: old)
    researcher.execute('get_week_box_scores', '{"week":1,"team_id":"2"}')
    monkeypatch.setattr(dispatch, 'build_context', lambda *args, **kwargs: {
        'source': 'ESPN Fantasy', 'fetched_at': 'now', 'week': 3,
        'players': [{'id': '7', 'name': 'Player Alpha', 'points': 17, 'fantasy_team': 'Oak', 'slot': 'WR'}],
        'news': [], 'limitations': []})
    card = Mock(side_effect=AssertionError('current roster evidence should be retained'))
    monkeypatch.setattr(player_tools, '_cards', card)
    result = researcher.execute('get_player_stats', '{"player_ids":["7"]}')
    assert result['players'][0]['points'] == 17 and result['players'][0]['fantasy_team'] == 'Oak'
    assert researcher.context['tool_evidence'][0]['result']['teams'][0]['players'][0]['points'] == 99
    card.assert_not_called()
