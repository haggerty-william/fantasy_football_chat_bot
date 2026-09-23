"""Model-facing team grouping, identity references, and tool receipt coverage."""
from copy import deepcopy
import json
import re
from types import SimpleNamespace as Obj

import pytest

from gamedaybot.espn.analysis_packet import AnalysisPacket


@pytest.fixture
def source():
    return {
        'week': 2,
        'fantasy_teams': [
            {'id': '1', 'name': 'Oak Owls', 'managers': ['Tanner Example']},
            {'id': '2', 'name': 'Maple Bears', 'managers': ['Alex Morgan']},
        ],
        'rosters': [
            {'team': 'Oak Owls', 'players': [[11, 'Player Alpha', 'WR', 'WR', 15, 14, 'ACTIVE', 'BUF']]},
            {'team': 'Maple Bears', 'players': [[22, 'Player Beta', 'RB', 'BE', 8, 9, 'ACTIVE', 'KC']]},
        ],
        'players': [
            {'id': 11, 'name': 'Player Alpha', 'fantasy_team': 'Oak Owls', 'points': 15},
            {'id': 22, 'name': 'Player Beta', 'fantasy_team': 'Maple Bears', 'points': 8},
        ],
        'league_history': {
            'teams': [
                {'team': 'Oak Owls', 'completed_record': {'W': 1, 'L': 0}, 'note': 'Tanner Example runs Oak Owls.'},
                {'team': 'Maple Bears', 'completed_record': {'W': 0, 'L': 1}},
            ],
            'meetings': [{'teams': ['Oak Owls', 'Maple Bears'], 'scores': [100, 90]}],
        },
        'trade_sides': [
            {'team': 'Oak Owls', 'received': ['Player Alpha'], 'sent': ['Player Beta']},
            {'team': 'Maple Bears', 'received': ['Player Beta'], 'sent': ['Player Alpha']},
        ],
        'current_roster_depth': [{'team': 'Oak Owls', 'positions': {'WR': {'count': 1}}}],
        'highlights': [{'team': 'Oak Owls', 'player': 'Player Alpha', 'points': 15}],
        'matchups': [{'sides': [{'team': 'Oak Owls', 'score': 100}, {'team': 'Maple Bears', 'score': 90}]}],
        'news': [{'summary': 'Oak Owls and Maple Bears: Tanner Example faces Alex Morgan.'}],
    }


def make_packet(source):
    return AnalysisPacket(None, source,
                          'Oak Owls and Tanner Example face Maple Bears and Alex Morgan. Oak Owls leads.',
                          'get_trade_report', 2, '2026-09-23T12:00:00Z')


def resolve(data, path):
    for key in path:
        data = data[key]
    return data


def assert_single_identities(serialized):
    for name in ('Oak Owls', 'Maple Bears', 'Tanner Example', 'Alex Morgan'):
        assert serialized.count(name) == 1, name


def test_names_are_canonical_once_with_team_data_grouped_and_resolvable(source):
    packet = make_packet(source)
    serialized = packet.dumps()
    assert_single_identities(serialized)
    data = json.loads(serialized)
    context = data['research_context']
    oak = context['teams']['1']
    assert oak['name'] == 'Oak Owls' and oak['managers'] == ['Tanner Example']
    assert oak['rosters'][0]['players'][0][0] == 11
    assert oak['player_details'][0]['name'] == 'Player Alpha'
    assert oak['history']['completed_record'] == {'W': 1, 'L': 0}
    assert oak['trades'][0]['received'] == ['Player Alpha']
    assert oak['roster_depth'][0]['positions']['WR']['count'] == 1
    assert oak['highlights'][0]['points'] == 15
    assert context['matchups'][0]['sides'][0]['team_id'] == '1'
    assert context['league_history']['meetings'][0]['teams'] == ['[team:1]', '[team:2]']
    assert 'fantasy_teams' not in context and 'rosters' not in context and 'players' not in context
    for tid in re.findall(r'\[team:([^\]]+)\]', serialized):
        assert tid in context['teams']
    for tid, index in re.findall(r'\[manager:([^:]+):(\d+)\]', serialized):
        assert context['teams'][tid]['managers'][int(index)]


def test_serializer_and_tool_attachment_do_not_mutate_internal_context(source):
    before = deepcopy(source)
    packet = make_packet(source)
    result = {'team': 'Oak Owls', 'nested': {'note': 'Tanner Example runs Oak Owls.'}, 'players': [{'id': 11}]}
    original_result = deepcopy(result)
    packet.add_tool_result('call1', 'get_schedule_outlook', result, {'team_id': '1'})
    packet.dumps()
    assert source == before
    assert result == original_result


def test_manager_directory_survives_unavailable_research():
    league = Obj(teams=[Obj(team_id=1, team_name='Oak Owls', owners=[
        {'firstName': 'Tanner', 'lastName': 'Example', 'email': 'private@example.com', 'id': 'private-account'}])])
    packet = AnalysisPacket(league, None, 'Oak Owls leads.', 'get_standings', 2, 'now')
    context = packet.data['research_context']
    assert context['teams']['1'] == {'name': 'Oak Owls', 'managers': ['Tanner Example']}
    assert 'unavailable' in context['limitations'][0].lower()
    assert packet.data['espn_report'] == '[team:1] leads.'
    assert 'private@example.com' not in packet.dumps() and 'private-account' not in packet.dumps()


def test_targeted_tool_evidence_has_one_copy_and_resolvable_receipt(source):
    packet = make_packet(source)
    receipt = packet.add_tool_result('schedule1', 'get_schedule_outlook', {
        'team': 'Oak Owls', 'fantasy_matchups': [{'opponent': 'Maple Bears', 'week': 3}],
        'note': 'Tanner Example manages Oak Owls; Alex Morgan manages Maple Bears.',
    }, {'team_id': '1'})
    assert receipt['status'] == 'stored'
    assert len(receipt['evidence_paths']) == 1
    evidence = resolve(packet.data, receipt['evidence_paths'][0])
    assert evidence['tool'] == 'get_schedule_outlook'
    assert evidence['data']['fantasy_matchups'][0]['opponent_id'] == '2'
    assert receipt['evidence_paths'][0][:3] == ['research_context', 'teams', '1']
    request = packet.dumps() + json.dumps(receipt)
    assert_single_identities(request)
    assert request.count('fantasy_matchups') == 1


def test_multi_team_and_player_tools_group_data_keep_shared_limitations(source):
    packet = make_packet(source)
    trade_receipt = packet.add_tool_result('trade1', 'simulate_trade_impact', {
        'teams': [{'team': 'Oak Owls', 'gain': 4}, {'team_id': 2, 'gain': -4}],
        'limitations': 'Estimates for Oak Owls and Maple Bears.',
    }, {})
    assert trade_receipt['status'] == 'stored'
    assert len(trade_receipt['evidence_paths']) == 3
    assert all(resolve(packet.data, path)['tool'] == 'simulate_trade_impact'
               for path in trade_receipt['evidence_paths'])
    team_evidence = {path[2]: resolve(packet.data, path)['data'] for path in trade_receipt['evidence_paths']
                     if path[:2] == ['research_context', 'teams']}
    assert team_evidence['1']['teams'][0]['gain'] == 4
    assert team_evidence['2']['teams'][0]['gain'] == -4
    player_receipt = packet.add_tool_result('stats1', 'get_player_stats', {
        'players': [{'id': 11, 'points': 15}, {'id': 22, 'points': 8}],
        'limitations': ['Observed points only.'],
    }, {'player_ids': ['11', '22']})
    player_evidence = {path[2]: resolve(packet.data, path)['data'] for path in player_receipt['evidence_paths']
                       if path[:2] == ['research_context', 'teams']}
    assert player_evidence['1']['players'][0]['id'] == 11
    assert player_evidence['2']['players'][0]['id'] == 22
    assert all(resolve(packet.data, path) for path in player_receipt['evidence_paths'])
    assert_single_identities(packet.dumps() + json.dumps([trade_receipt, player_receipt]))


def test_unknown_player_or_team_is_retained_without_guessing_an_owner(source):
    packet = make_packet(source)
    receipt = packet.add_tool_result('unknown1', 'get_player_stats', {
        'players': [{'id': 999, 'name': 'Unassigned Player', 'points': 7}],
    }, {'team_id': '999'})
    assert len(receipt['evidence_paths']) == 1
    assert receipt['evidence_paths'][0][:2] == ['research_context', 'shared_research']
    evidence = resolve(packet.data, receipt['evidence_paths'][0])
    assert evidence['data']['players'][0]['id'] == 999
    assert all(not team.get('research') for team in packet.teams.values())


def test_reused_model_call_ids_keep_earlier_evidence_receipts_valid(source):
    packet = make_packet(source)
    receipts = [packet.add_tool_result('call1', 'get_player_stats', {
        'players': [{'id': 11, 'points': points}],
    }, {'player_ids': ['11']}) for points in (15, 17)]
    paths = [receipt['evidence_paths'][0] for receipt in receipts]
    assert paths[0] != paths[1]
    assert [resolve(packet.data, path)['data']['players'][0]['points'] for path in paths] == [15, 17]
    assert_single_identities(packet.dumps() + json.dumps(receipts))


@pytest.mark.parametrize('arguments', [{}, {'team_id': '1'}, {'team_id': '999'}])
def test_tool_error_receipts_mark_evidence_unavailable(source, arguments):
    packet = make_packet(source)
    receipt = packet.add_tool_result('error1', 'get_schedule_outlook', {'error': 'Source unavailable.'}, arguments)
    assert receipt['status'] == 'unavailable'
    assert len(receipt['evidence_paths']) == 1
    assert resolve(packet.data, receipt['evidence_paths'][0])['data']['error'] == 'Source unavailable.'


def test_manager_preference_is_scoped_to_management_commentary(source):
    instruction = make_packet(source).data['writing_reminder']
    assert 'management decisions' in instruction
    assert 'trades, waivers, drafts, lineups' in instruction
    assert 'Team names are fine for scores and standings' in instruction
    assert 'Do not force manager names elsewhere' in instruction
