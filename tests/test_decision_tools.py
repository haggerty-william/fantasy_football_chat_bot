from types import SimpleNamespace as O
from unittest.mock import Mock
import json
import time

import pytest

from gamedaybot.espn import decision_tools as d, research_tools, analysis, analysis_limits


def player(pid, name, points, slots=('WR', 'FLEX'), position='WR'):
    return O(playerId=pid, name=name, stats={2: {'projected_points': points}}, eligibleSlots=list(slots),
             position=position, injuryStatus='ACTIVE', schedule={w: {'team': 'BUF'} for w in range(1,19) if w!=5})


@pytest.fixture
def league(monkeypatch, tmp_path):
    monkeypatch.setenv('COMMUNITY_STATE_PATH', str(tmp_path/'state.sqlite3'))
    monkeypatch.setattr(d, '_free_cache', {})
    monkeypatch.setattr(d, 'rules', lambda l: {'lineup_slots': {'WR': 1, 'FLEX': 1, 'BE': 3}})
    a = O(team_id=1, team_name='Oak', roster=[player(1,'Alpha',20), player(2,'Beta',15)],
          scores=[100], outcomes=['W'], division_id=0)
    b = O(team_id=2, team_name='Maple', roster=[player(3,'Gamma',12), player(4,'Delta',10)],
          scores=[90], outcomes=['L'], division_id=0)
    a.schedule=[b]*4; b.schedule=[a]*4
    return O(league_id=1, year=2026, scoringPeriodId=2, teams=[a,b],
             settings=O(reg_season_count=4,playoff_team_count=1,median_scoring=False,tie_rule='NONE',
                        playoff_seed_tie_rule='TOTAL_POINTS_SCORED', matchup_periods={str(w):[w] for w in range(1,5)}))


def test_optimal_lineup_handles_flex_without_double_counting():
    roster = [player(1,'Dual',20), player(2,'WR only',19,('WR',)), player(3,'RB',5,('FLEX',),'RB')]
    result = d.lineup(roster, {'WR':1,'FLEX':1}, 2)
    assert result['available'] and result['projected_points']==39
    assert {p['id'] for p in result['lineup']}=={'1','2'}
    assert next(p for p in result['lineup'] if p['name']=='Dual')['slot']=='FLEX'


def test_lineup_missing_projection_and_injury_do_not_invent_gain():
    roster=[player(1,'Missing',None),player(2,'Out',30),player(3,'Negative',-2)]
    roster[1].injuryStatus='OUT'
    result=d.lineup(roster,{'WR':1,'FLEX':1},2)
    assert not result['available'] and result['missing_projections']==['Missing']
    assert result['filled_slots']==1 and result['projected_points']==-2


def test_trade_counterfactual_respects_received_direction_and_preserves_rosters(league):
    context={'trade_sides':[{'team':'Oak','sent':['Gamma'],'received':['Alpha'],'completed_at':'today'},
                            {'team':'Maple','sent':['Alpha'],'received':['Gamma'],'completed_at':'today'}]}
    before=[list(t.roster) for t in league.teams]
    result=d.trade_impact(league,context,2)
    assert result['teams'][0]['starter_projection_change']==8
    assert result['teams'][1]['starter_projection_change']==-8
    assert [t.roster for t in league.teams]==before
    context['trade_sides'][0]['received']=['Delta']
    assert 'error' in d.trade_impact(league,context,2)


def test_replacements_exclude_owned_and_cache_source(league):
    league.free_agents=Mock(return_value=[league.teams[0].roster[0],player(9,'Free',22),player(10,'Backup',2)])
    result=d.replacements(league,league.teams[0],2)
    assert result['candidates'][0]['name']=='Free'
    assert result['candidates'][0]['starter_projection_gain']==7
    assert result['candidates'][0]['displaced_starters']==['Beta']
    d.replacements(league,league.teams[1],2)
    league.free_agents.assert_called_once()


def test_workload_missing_metrics_and_percentage_points():
    result=d.workload({'players':[{'id':1,'name':'Alpha','usage':{'weeks':[
        {'week':1,'targets':4,'offense_snap_pct':50}, {'week':2,'targets':8},
        {'week':4,'targets':12,'offense_snap_pct':75}]}}]})
    changes=result['players'][0]['changes']
    assert changes['targets']['prior_average']==6 and changes['targets']['change']==6
    assert changes['offense_snap_pct']['change']==25
    assert changes['offense_snap_pct']['prior_weeks']==[1]
    assert 'carries' not in changes


def test_schedule_only_infers_bye_from_complete_schedule(league):
    league.teams[0].roster[1].schedule={2:{'team':'NYJ'}}
    result=d.schedule_outlook(league,league.teams[0],2)
    assert result['players'][0]['bye_week']==5
    assert result['players'][1]['bye_week'] is None
    assert result['players'][1]['opponents'][-1]['status']=='unknown'
    assert result['fantasy_matchups'][0]['opponent']=='Maple'


def test_playoff_probabilities_are_reproducible_and_total_slots(league):
    a=d.playoff_odds(league,2)
    assert a==d.playoff_odds(league,2)
    assert sum(t['playoff_pct'] for t in a['teams'])==100
    assert a['teams'][0]['playoff_pct']>a['teams'][1]['playoff_pct']
    assert a['teams'][0]['conditional_on_this_week']['win']>=a['teams'][0]['conditional_on_this_week']['lose']


@pytest.mark.parametrize('field,value',[('median_scoring',True),('playoff_seed_tie_rule','H2H_RECORD'),('tie_rule','BENCH_POINTS')])
def test_unsupported_rules_return_unknown_not_fake_odds(league,field,value):
    setattr(league.settings,field,value)
    assert 'error' in d.playoff_odds(league,2)


def test_invalid_remaining_schedule_returns_unknown(league):
    league.teams[1].schedule=[]
    assert 'error' in d.playoff_odds(league,2)


def test_prediction_archive_deduplicates_and_only_compares_completed_weeks(league):
    context={'rosters':[{'team':'Oak'}], 'nfl_games':{'BUF':{'state':'pre'}}}
    for _ in range(2): d.archive_commentary(league,context,2,'get_matchups','local','Oak could win.')
    assert d.review_predictions(league,2)['receipts']==[]
    league.scoringPeriodId=3
    league.teams[0].scores.append(123);league.teams[0].outcomes.append('W')
    rows=d.review_predictions(league,3)['receipts']
    assert len(rows)==1 and rows[0]['text']=='Oak could win.'
    assert rows[0]['subsequent_completed_results'][0]['score']==123
    league.year=2025
    assert d.review_predictions(league,3)['receipts']==[]


def test_new_tool_evidence_reaches_validator(league):
    context={'players':[],'rosters':[]}
    tools=research_tools.ResearchTools(league,context,2,None,time.monotonic()+90)
    result=tools.execute('simulate_playoff_odds','{}')
    assert result['simulations']==600 and context['tool_evidence'][0]['result']==result
    assert 'error' in tools.execute('get_schedule_outlook','{"team_id":"999"}')
    assert 'error' in tools.execute('simulate_playoff_odds','{"url":"http://localhost"}')


def test_timeout_budgets_leave_room_for_delivery(monkeypatch):
    monkeypatch.delenv('AI_ANALYSIS_TIMEOUT_SECONDS',raising=False)
    monkeypatch.delenv('AI_REQUEST_TIMEOUT_SECONDS',raising=False)
    assert analysis_limits.request_timeout()==600
    assert analysis_limits.analysis_timeout()==600
    assert analysis_limits.command_timeout()==660
    monkeypatch.setenv('AI_ANALYSIS_TIMEOUT_SECONDS','9999')
    assert analysis_limits.command_timeout()==660
    monkeypatch.setenv('AI_REQUEST_TIMEOUT_SECONDS','invalid')
    assert analysis_limits.request_timeout()==600


def test_local_request_actually_uses_wider_timeout(monkeypatch):
    monkeypatch.setenv('AI_MODEL','local')
    monkeypatch.setenv('AI_REQUEST_TIMEOUT_SECONDS','180')
    post=Mock(return_value=O(status_code=200,json=lambda:{'choices':[{'finish_reason':'stop','message':{'content':'A close game.'}}]}))
    monkeypatch.setattr(analysis.requests,'post',post)
    assert analysis.generate_analysis('Scores','get_matchups')
    assert post.call_args.kwargs['timeout'][1]==180
