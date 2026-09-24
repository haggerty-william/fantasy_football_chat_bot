"""ESPN designation changes are incremental, neutral and independent of ownership."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from gamedaybot.espn import injury_updates
from gamedaybot.espn.injury_updates import collect_injuries


NOW = datetime(2026, 9, 24, 15, tzinfo=timezone.utc)


def player(player_id=1, status='ACTIVE', slot='BE'):
    return SimpleNamespace(playerId=player_id, name=f'Player {player_id}', injuryStatus=status,
                           position='WR', slot_position=slot)


def league(*players):
    return SimpleNamespace(teams=[SimpleNamespace(team_id=1, team_name='Oak Club', roster=list(players))])


def scan(league, previous=None, hours=0):
    return collect_injuries(league, previous, NOW + timedelta(hours=hours))


def test_initial_scan_silently_baselines_existing_injuries_across_all_slots():
    roster = league(player(1, 'OUT', 'WR'), player(2, 'QUESTIONABLE', 'BE'), player(3, 'INJURY_RESERVE', 'IR'))
    events, state = scan(roster)
    assert events == []
    assert {key: value['status'] for key, value in state['players'].items()} == {
        '1': 'OUT', '2': 'QUESTIONABLE', '3': 'INJURY_RESERVE'}
    assert json.loads(json.dumps(state)) == state


@pytest.mark.parametrize('slot', ['WR', 'BE', 'IR'])
def test_new_designation_reports_once_and_carries_current_metadata(slot):
    p = player(slot=slot)
    roster = league(p)
    _, state = scan(roster)
    original = deepcopy(state)
    p.injuryStatus = 'QUESTIONABLE'
    events, changed = scan(roster, state, 1)
    assert state == original
    assert len(events) == 1
    event = events[0]
    assert event['id'] == 'injury:1:1'
    assert event['kind'] == 'injury_status'
    assert event['previous_status'] == 'ACTIVE' and event['status'] == 'QUESTIONABLE'
    assert event['player_id'] == '1' and event['player_name'] == 'Player 1'
    assert event['team_id'] == '1' and event['team'] == 'Oak Club'
    assert event['slot'] == slot and event['position'] == 'WR'
    assert event['observed_at'] == '2026-09-24T16:00:00+00:00'
    assert json.loads(json.dumps(events)) == events
    repeated, final = scan(roster, changed, 2)
    assert repeated == []
    assert final['players']['1']['revision'] == 1


def test_designation_removal_and_recurrence_get_distinct_revisions():
    p = player(status='OUT')
    roster = league(p)
    _, state = scan(roster)
    p.injuryStatus = 'ACTIVE'
    recovered, state = scan(roster, state, 1)
    assert recovered[0]['previous_status'] == 'OUT'
    assert recovered[0]['status'] == 'ACTIVE'
    assert 'does not establish full health' in recovered[0]['limitation']
    p.injuryStatus = 'OUT'
    recurring, state = scan(roster, state, 2)
    assert recovered[0]['id'] == 'injury:1:1'
    assert recurring[0]['id'] == 'injury:1:2'


@pytest.mark.parametrize('status', [None, '', 'UNKNOWN', 'UNAVAILABLE', [], {}, True, 0, 'healthy', 'x' * 100])
def test_unknown_status_never_clears_previous_injury(status):
    p = player(status='OUT')
    roster = league(p)
    _, state = scan(roster)
    p.injuryStatus = status
    events, state = scan(roster, state, 1)
    assert events == []
    assert state['players']['1']['status'] == 'OUT'
    p.injuryStatus = 'ACTIVE'
    events, state = scan(roster, state, 2)
    assert events[0]['previous_status'] == 'OUT'


def test_first_known_status_and_newly_rostered_players_are_silently_baselined():
    p = player(status='UNKNOWN')
    roster = league(p)
    _, state = scan(roster)
    assert state['players'] == {}
    p.injuryStatus = 'OUT'
    roster.teams[0].roster.append(player(2, 'INJURY_RESERVE'))
    events, state = scan(roster, state, 1)
    assert events == []
    assert set(state['players']) == {'1', '2'}


def test_team_transfer_and_slot_change_are_quiet_but_later_injury_uses_new_owner():
    p = player(status='QUESTIONABLE')
    roster = league(p)
    _, state = scan(roster)
    roster.teams = [SimpleNamespace(team_id=2, team_name='Pine Club', roster=[p])]
    p.slot_position = 'WR'
    events, state = scan(roster, state, 1)
    assert events == []
    p.injuryStatus = 'OUT'
    events, state = scan(roster, state, 2)
    assert events[0]['team_id'] == '2' and events[0]['team'] == 'Pine Club'
    assert events[0]['previous_status'] == 'QUESTIONABLE'


def test_dropped_player_retains_status_without_announcing_a_drop():
    p = player(status='OUT')
    roster = league(p)
    _, state = scan(roster)
    roster.teams[0].roster = []
    events, state = scan(roster, state, 1)
    assert events == [] and state['players']['1']['status'] == 'OUT'
    roster.teams[0].roster = [p]
    assert scan(roster, state, 2)[0] == []


@pytest.mark.parametrize('before,after', [('NORMAL', 'ACTIVE'), ('IR', 'INJURY_RESERVE'), ('SUSPENDED', 'SUSPENSION')])
def test_equivalent_designation_aliases_do_not_generate_fake_changes(before, after):
    p = player(status=before)
    roster = league(p)
    _, state = scan(roster)
    p.injuryStatus = after
    assert scan(roster, state, 1)[0] == []


def test_duplicate_inconsistent_roster_ownership_is_not_used_as_injury_evidence():
    p = player(status='ACTIVE')
    roster = league(p)
    _, state = scan(roster)
    roster.teams.append(SimpleNamespace(team_id=2, team_name='Pine Club', roster=[player(status='OUT')]))
    events, state = scan(roster, state, 1)
    assert events == [] and state['players']['1']['status'] == 'ACTIVE'


def test_bounded_retention_preserves_rostered_players_and_prevents_recycled_event_ids(monkeypatch):
    monkeypatch.setattr(injury_updates, 'MAX_PLAYERS', 2)
    p = player()
    roster = league(p)
    _, state = scan(roster)
    p.injuryStatus = 'OUT'
    first, state = scan(roster, state, 1)
    roster.teams[0].roster = [player(2), player(3)]
    _, state = scan(roster, state, 2)
    assert set(state['players']) == {'2', '3'}
    roster.teams[0].roster = [p]
    events, state = scan(roster, state, 3)
    assert events == []
    assert len(state['players']) <= 2
    p.injuryStatus = 'ACTIVE'
    later, state = scan(roster, state, 4)
    assert first[0]['id'] == 'injury:1:1'
    assert later[0]['id'] == 'injury:1:2'


@pytest.mark.parametrize('previous', [None, {}, {'version': 2}, {'version': 1, 'players': []}, 'bad'])
def test_malformed_or_missing_state_baselines_quietly(previous):
    events, state = scan(league(player(status='OUT')), previous)
    assert events == [] and state['players']['1']['status'] == 'OUT'


def test_observation_timestamp_normalizes_to_utc_and_rejects_bad_input():
    _, state = collect_injuries(league(), observed_at='2026-09-24T12:00:00-04:00')
    assert state['observed_at'] == '2026-09-24T16:00:00+00:00'
    _, seconds_state = collect_injuries(league(), observed_at=NOW.timestamp())
    assert seconds_state['observed_at'] == NOW.isoformat()
    with pytest.raises(ValueError):
        collect_injuries(league(), observed_at='not a date')
