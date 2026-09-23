"""Regression coverage for richer evidence and conservative commentary checks."""
from datetime import datetime,timezone,timedelta
from types import SimpleNamespace as O
from unittest.mock import Mock
import json
import pytest
import requests
from gamedaybot.espn import research, nfl_usage, analyst_evidence as evidence
from gamedaybot.espn.commentary_checks import check_commentary, fallback_highlights


@pytest.fixture(autouse=True)
def isolated(tmp_path,monkeypatch):
    monkeypatch.setenv('COMMUNITY_STATE_PATH',str(tmp_path/'state.sqlite3'))
    monkeypatch.setattr(nfl_usage,'_cache',{})
    monkeypatch.setattr(evidence,'_cache',{})
    monkeypatch.setattr(research,'news_feed',lambda:([], 'now'))


def athlete(pid,name,slot='WR'):
    return O(playerId=pid,name=name,position='WR',lineupSlot=slot,slot_position=slot,proTeam='BUF',
             injuryStatus='QUESTIONABLE',points=20,projected_points=15,
             stats={1:{'points':10,'projected_points':12,'breakdown':{'receivingTargets':5}},
                    2:{'points':20,'projected_points':15,'breakdown':{'receivingTargets':8}}})


def sample():
    a=O(team_id=1,team_name='Oak',team_abbrev='OAK',roster=[athlete(1,'Player Alpha'),athlete(2,'Player Beta','BE')],
        scores=[100,105],outcomes=['W','L'],schedule=[])
    b=O(team_id=2,team_name='Maple',team_abbrev='MAP',roster=[athlete(3,'Player Gamma')],scores=[90,110],outcomes=['L','W'],schedule=[])
    a.schedule=[b,b];b.schedule=[a,a]
    settings=O(_raw_scoring_settings={'scoringItems':[{'statId':53,'points':.5}]},
               scoring_format=[{'id':53,'label':'Receptions'}],matchup_periods={'1':[1],'2':[2],'3':[3]},
               playoff_team_count=1,reg_season_count=14,median_scoring=False,tie_rule='NONE',playoff_seed_tie_rule='TOTAL_POINTS')
    return O(league_id=123,year=2026,scoringPeriodId=3,teams=[a,b],settings=settings)


def test_rules_use_actual_sparse_slot_ids():
    league=sample()
    league.espn_request=O(get_league=Mock(return_value={'settings':{'rosterSettings':{'lineupSlotCounts':{'0':1,'20':6}}}}))
    rules=evidence.rules(league)
    assert rules['lineup_slots']=={'QB':1,'BE':6}
    assert rules['scoring'][0]['points']==.5
    evidence.rules(league)
    league.espn_request.get_league.assert_called_once()


def test_trends_exclude_current_week_byes_and_future():
    p=athlete(1,'Player Alpha')
    p.stats[3]={'points':999,'projected_points':1}
    trend=evidence.trends(p,3,3)
    assert trend['sample_games']==2 and trend['average_points']==15 and trend['mean_vs_projection']==1.5
    assert trend['population_stddev']==5
    p.stats[2]={'points':0,'projected_points':15,'breakdown':{}}
    assert evidence.trends(p,3,3)['sample_games']==1


def test_history_has_correct_cutoff_and_meetings():
    l=sample()
    h=evidence.history(l,l.teams,1)
    assert h['teams'][0]['completed_record']=={'W':1,'L':0,'T':0}
    assert len(h['meetings'])==1
    assert h['meetings'][0]['scores']==[100,90]


def test_full_rosters_games_trends_usage_and_trade_depth(monkeypatch):
    from gamedaybot.espn import community
    l=sample()
    monkeypatch.setattr(community,'nfl_games',lambda l:{'BUF':{'start':0,'state':'post','completed':True}})
    monkeypatch.setattr(nfl_usage,'usage_context',lambda *a:({'1':{'weeks':[{'week':2,'targets':8,'offense_snap_pct':90}]}},{'status':'ok'}))
    d=research.build_context(l,'Oak received Player Alpha','get_trade_report',3,
            trade_actions=[(l.teams[0],'TRADE_RECEIVED',l.teams[0].roster[0],0)])
    assert len(d['rosters'])==1 and len(d['rosters'][0]['players'])==2
    assert d['players'][0]['name']=='Player Alpha'
    assert d['players'][0]['game']['lineup_locked']
    assert d['players'][0]['usage']['weeks'][0]['targets']==8
    assert d['players'][0]['trend']['average_points']==15
    assert d['current_roster_depth'][0]['positions']['WR']['count']==2
    assert len(json.dumps(d))<=32000


def test_usage_id_join_and_week_filter(monkeypatch):
    base={k:{'rows':[],'source':k,'fetched_at':'now'} for k in nfl_usage.COLUMNS}
    base['players']['rows']=[{'espn_id':'123','gsis_id':'g1','pfr_id':'p1'}]
    def stat(w,targets):
        return {**{k:'' for k in nfl_usage.COLUMNS['stats_player']},'player_id':'g1','season':'2026','week':str(w),
                'season_type':'REG','targets':str(targets),'target_share':'.25'}
    base['stats_player']['rows']=[stat(1,4),stat(2,8),stat(3,99)]
    base['snap_counts']['rows']=[{'pfr_player_id':'p1','season':'2026','week':'2','game_type':'REG','offense_snaps':'50','offense_pct':'.8'}]
    monkeypatch.setattr(nfl_usage,'dataset',lambda kind,year:base[kind])
    result,metadata=nfl_usage.usage_context(2026,[123,999],2)
    assert set(result)=={'123'}
    row=result['123']['weeks'][-1]
    assert row['week']==2 and row['targets']==8 and row['offense_snap_pct']==80 and row['target_share_pct']==25
    assert result['123']['change_from_previous_week']['targets']==4
    assert metadata['missing_feeds']==[]


def test_usage_unavailable_does_not_guess_by_name(monkeypatch):
    monkeypatch.setattr(nfl_usage,'dataset',Mock(side_effect=requests.Timeout()))
    result,metadata=nfl_usage.usage_context(2026,[123],2)
    assert result=={} and 'Unavailable' in metadata['status']


def test_usage_cache_persists_and_rejects_stale_data(mock_requests,monkeypatch):
    url=nfl_usage.ROOT+'players/players.csv'
    mock_requests.get(url,text='espn_id,gsis_id,pfr_id\n123,g1,p1\n')
    result=nfl_usage.dataset('players',2026)
    monkeypatch.setattr(nfl_usage,'_cache',{})
    assert nfl_usage.dataset('players',2026)==result and mock_requests.call_count==1
    from gamedaybot.espn.community_state import state
    with state() as db: db.execute('UPDATE research_cache SET fetched=0')
    monkeypatch.setattr(nfl_usage,'_cache',{})
    mock_requests.get(url,exc=requests.Timeout())
    with pytest.raises(requests.Timeout): nfl_usage.dataset('players',2026)


def test_usage_filters_wrong_season_and_postseason(mock_requests):
    columns=nfl_usage.COLUMNS['snap_counts']
    text=','.join(columns)+'\np1,2025,2,REG,50,.8\np1,2026,2,POST,50,.8\np1,2026,1,REG,40,.7\n'
    mock_requests.get(nfl_usage.ROOT+'snap_counts/snap_counts_2026.csv',text=text)
    result=nfl_usage.dataset('snap_counts',2026)
    assert len(result['rows'])==1 and result['rows'][0]['week']=='1'


def packet():
    return {'players':[{'name':'Player Alpha','fantasy_team':'Oak','points':20,'projected_points':15,
                         'current_status':'QUESTIONABLE','previous_weeks':[{'week':1,'points':10}],
                         'game':{'state':'post','completed':True,'lineup_locked':True}}],
            'rosters':[{'team':'Oak','players':[[1,'Player Alpha','WR','WR',20,15,'QUESTIONABLE','BUF']]},
                       {'team':'Maple','players':[]}],
            'highlights':[{'player':'Player Alpha','team':'Oak','week':2,'points':20,'vs_projection':5,'final':True}]}


@pytest.mark.parametrize('text,reason',[
    ('Player Alpha scored 99 points.','unsupported_number'),
    ('Player Alpha scored 15 points.','player_points_mismatch'),
    ('Player Alpha is out.','status_mismatch'),
    ('Player Alpha is healthy.','unsupported_health_inference'),
    ('Start Player Alpha this week.','locked_player_advice'),
    ('If Player Alpha sits, Oak could struggle.','locked_player_advice'),
    ('Maple\'s WR Player Alpha is a risk.','ownership_mismatch'),
    ('Patrick Mahomes scored 20 points.','unsupported_named_subject'),
    ('Oak clinched a playoff spot.','unsupported_playoff_claim'),
])
def test_commentary_rejects_known_failure_modes(text,reason):
    assert reason in check_commentary(text,'Score Update\nOak 100 - 90 Maple',packet())


def test_supported_commentary_and_fallback():
    d=packet()
    assert check_commentary('Player Alpha scored 20 points. Oak leads 100 to 90.','Oak 100 - 90 Maple',d)==[]
    assert '20.00' in fallback_highlights(d) and '+5.00' in fallback_highlights(d)


def test_historical_recap_rejects_todays_injury_status():
    d=packet();d['historical']=True
    assert 'current_status_in_historical_recap' in check_commentary('Player Alpha is questionable.','Recap',d)


def test_trade_direction_guard():
    d=packet();d['trade_sides']=[{'team':'Oak','sent':['Player Alpha'],'received':[]}]
    assert 'trade_direction_mismatch' in check_commentary('Oak received Player Alpha.','Trade',d)


def manager_trade_packet():
    d=packet()
    d['players'].append({'name':'Player Beta','points':10})
    d['trade_sides']=[{'team':'Oak','sent':['Player Beta'],'received':['Player Alpha']},
                      {'team':'Maple','sent':['Player Alpha'],'received':['Player Beta']}]
    d['fantasy_teams']=[{'id':'1','name':'Oak','managers':['Tanner Example','Casey Lake']},
                        {'id':'2','name':'Maple','managers':['Alex Morgan']},
                        {'id':'3','name':'Birch','managers':['Alex Carter']}]
    return d


@pytest.mark.parametrize('subject,received,sent',[
    ('Tanner','Player Alpha','Player Beta'),
    ('Tanner Example','Player Alpha','Player Beta'),
    ('Tanner E.','Player Alpha','Player Beta'),
    ('Tanner (Oak)','Player Alpha','Player Beta'),
    ('Casey','Player Alpha','Player Beta'),
    ('Casey Lake','Player Alpha','Player Beta'),
    ('Casey L. (Oak)','Player Alpha','Player Beta'),
    ('Alex Morgan','Player Beta','Player Alpha'),
    ('Alex M','Player Beta','Player Alpha'),
    ('Alex M.','Player Beta','Player Alpha'),
])
def test_trade_direction_checks_manager_names_and_comanagers(subject,received,sent):
    d=manager_trade_packet()
    assert 'trade_direction_mismatch' not in check_commentary(
        f'{subject} received {received} in exchange for {sent}.','Trade',d)
    assert 'trade_direction_mismatch' in check_commentary(
        f'{subject} received {sent} in exchange for {received}.','Trade',d)
    assert 'trade_direction_mismatch' in check_commentary(f'{subject} sent {received}.','Trade',d)


def test_trade_direction_does_not_guess_ambiguous_first_names_or_initials():
    d=manager_trade_packet()
    assert 'trade_direction_mismatch' not in check_commentary('Alex received Player Alpha.','Trade',d)
    d['fantasy_teams'][1]['managers']=['Alex Crawford']
    assert 'trade_direction_mismatch' not in check_commentary('Alex C. received Player Alpha.','Trade',d)
    assert 'trade_direction_mismatch' in check_commentary('Alex Crawford received Player Alpha.','Trade',d)


def test_trade_direction_does_not_assign_a_shared_full_manager_name():
    d=manager_trade_packet()
    d['fantasy_teams'][1]['managers']=['Tanner Example']
    assert 'trade_direction_mismatch' not in check_commentary('Tanner Example received Player Alpha.','Trade',d)
    assert 'trade_direction_mismatch' in check_commentary('Maple received Player Alpha.','Trade',d)


def test_trade_direction_checks_later_claims_by_the_same_manager():
    d=manager_trade_packet()
    text='Alex M. received Player Beta. Alex M. received Player Alpha.'
    assert 'trade_direction_mismatch' in check_commentary(text,'Trade',d)


def test_trade_direction_keeps_two_manager_clauses_separate():
    d=manager_trade_packet()
    text='Tanner received Player Alpha and Alex Morgan received Player Beta.'
    assert 'trade_direction_mismatch' not in check_commentary(text,'Trade',d)
    text='Tanner received Player Alpha and Alex Morgan received Player Alpha.'
    assert 'trade_direction_mismatch' in check_commentary(text,'Trade',d)


def test_context_budget_preserves_compact_rosters_before_details(monkeypatch):
    from gamedaybot.espn import community
    l=sample();l.teams[0].roster=[athlete(i,f'Player Number {i}') for i in range(60)]
    monkeypatch.setattr(community,'nfl_games',Mock(side_effect=requests.Timeout()))
    monkeypatch.setattr(nfl_usage,'usage_context',lambda *a:({}, {'status':'unavailable'}))
    monkeypatch.setenv('AI_CONTEXT_CHAR_LIMIT','16000')
    d=research.build_context(l,'scores','get_matchups',3)
    assert len(json.dumps(d))<=16000
    assert sum(len(r['players']) for r in d['rosters'])==61
    assert d['omitted_player_count']>0


def test_forecasts_only_record_before_first_kickoff():
    l=sample()
    box=O(home_team=l.teams[0],away_team=l.teams[1],home_lineup=l.teams[0].roster,away_lineup=l.teams[1].roster)
    assert evidence.forecast_memory(l,[box],{'BUF':{'start':0}},3)==[]
    from gamedaybot.espn.community_state import state
    with state() as db: assert db.execute('SELECT COUNT(*) FROM analyst_forecasts').fetchone()[0]==0
    evidence.forecast_memory(l,[box],{'BUF':{'start':datetime.now(timezone.utc).timestamp()+1000}},3)
    with state() as db: assert db.execute('SELECT COUNT(*) FROM analyst_forecasts').fetchone()[0]==1

def test_rejected_ai_uses_calculated_highlights(mock_requests,monkeypatch):
    from gamedaybot.espn import analysis
    monkeypatch.setenv('AI_MODEL','local')
    monkeypatch.setenv('AI_BASE_URL','http://localhost:1234/v1')
    monkeypatch.setattr(analysis,'build_context',lambda *a,**kw:packet())
    mock_requests.post('http://localhost:1234/v1/chat/completions',json={'choices':[{'finish_reason':'stop','message':{'content':'Player Alpha scored 999 points.'}}]})
    text=analysis.generate_analysis('Score Update\nOak 100 - 90 Maple','get_scoreboard_short',week=2,league=sample())
    assert text.startswith('Data Highlights') and '999' not in text and '20.00' in text

def test_usage_claims_bound_to_player_and_explicit_week():
    d=packet();d['week']=3
    d['players'][0]['usage']={'weeks':[{'week':1,'targets':4},{'week':2,'targets':8}]}
    assert check_commentary('Player Alpha had 8 targets in Week 2.','',d)==[]
    assert 'player_usage_mismatch' in check_commentary('Player Alpha had 8 targets in Week 1.','',d)

def test_live_players_do_not_receive_current_week_usage(monkeypatch):
    feeds={k:{'rows':[],'source':k,'fetched_at':'now'} for k in nfl_usage.COLUMNS}
    feeds['players']['rows']=[{'espn_id':'1','gsis_id':'g1','pfr_id':'p1'},{'espn_id':'2','gsis_id':'g2','pfr_id':'p2'}]
    for pid in (1,2):
        for week in (1,2):
            feeds['snap_counts']['rows'].append({'pfr_player_id':f'p{pid}','season':'2026','week':str(week),'game_type':'REG','offense_snaps':'50','offense_pct':'.8'})
    monkeypatch.setattr(nfl_usage,'dataset',lambda kind,year:feeds[kind])
    result,_=nfl_usage.usage_context(2026,[1,2],2,{'1':2,'2':1})
    assert result['1']['latest_week']==2
    assert result['2']['latest_week']==1

def test_creative_rivalry_headline_is_not_mistaken_for_new_player():
    assert 'unsupported_named_subject' not in check_commentary('The Neighborhood Grudge Match is close.','',packet())

def test_upcoming_starters_take_priority_over_finished_injury_labels(monkeypatch):
    from gamedaybot.espn import community
    l=sample()
    l.teams[0].roster[0].injuryStatus='OUT'
    l.teams[0].roster[1].proTeam='LAR'
    l.teams[0].roster[1].slot_position='WR'
    l.teams[0].roster[1].injuryStatus='NORMAL'
    monkeypatch.setattr(community,'nfl_games',lambda l:{'BUF':{'start':0,'state':'post','completed':True},
        'LAR':{'start':datetime.now(timezone.utc).timestamp()+3600,'state':'pre','completed':False}})
    monkeypatch.setattr(nfl_usage,'usage_context',lambda *a:({},{}))
    d=research.build_context(l,'scores','get_matchups',3)
    assert d['players'][0]['name']=='Player Beta'

def test_trade_exchange_clause_does_not_reverse_the_recipients():
    d=packet()
    d['players'].append({'name':'Player Beta','points':10})
    d['trade_sides']=[{'team':'Oak','sent':['Player Beta'],'received':['Player Alpha']}]
    assert 'trade_direction_mismatch' not in check_commentary('Oak received Player Alpha in exchange for Player Beta.','Trade',d)
    assert 'trade_direction_mismatch' in check_commentary('Oak received Player Beta in exchange for Player Alpha.','Trade',d)


def test_trade_opinions_and_negated_consistency_are_not_hard_failures():
    assert 'insufficient_consistency_sample' not in check_commentary('Player Alpha is not yet a proven producer.','Trade',packet())


def test_bad_trade_draft_is_corrected_before_delivery(mock_requests,monkeypatch):
    from gamedaybot.espn import analysis
    monkeypatch.setenv('AI_MODEL','local')
    monkeypatch.setenv('AI_BASE_URL','http://localhost:1234/v1')
    d=packet();d['trade_sides']=[{'team':'Oak','sent':['Player Alpha'],'received':[]}]
    monkeypatch.setattr(analysis,'build_context',lambda *a,**kw:d)
    def result(text): return {'choices':[{'finish_reason':'stop','message':{'content':text}}]}
    mock_requests.post('http://localhost:1234/v1/chat/completions',[
        {'json':result('Oak received Player Alpha.')},
        {'json':result('Oak sent Player Alpha. The loss could hurt its depth.')}])
    text=analysis.generate_analysis('Trade Report\nOak sent Player Alpha','get_trade_report',week=2,league=sample())
    assert text.startswith('AI Analysis') and 'Oak sent Player Alpha.' in text
    assert mock_requests.call_count==2
    assert 'authoritative completed exchanges' in mock_requests.last_request.json()['messages'][-1]['content']


def test_failed_correction_explains_fallback(mock_requests,monkeypatch):
    from gamedaybot.espn import analysis
    monkeypatch.setenv('AI_MODEL','local')
    monkeypatch.setenv('AI_BASE_URL','http://localhost:1234/v1')
    monkeypatch.setattr(analysis,'build_context',lambda *a,**kw:packet())
    mock_requests.post('http://localhost:1234/v1/chat/completions',[
        {'json':{'choices':[{'finish_reason':'stop','message':{'content':'Player Alpha scored 999 points.'}}]}},
        {'exc':requests.Timeout()}])
    text=analysis.generate_analysis('Scores','get_scoreboard_short',week=2,league=sample())
    assert 'AI draft could not be verified' in text and '999' not in text
    assert mock_requests.call_count==2


def test_targeted_research_recovers_player_details_without_fake_roster_share(monkeypatch):
    monkeypatch.setattr(nfl_usage,'usage_context',lambda *a:({},{}))
    d=research.build_context(sample(),'','get_matchups',2,requested_player_ids={'2'})
    assert [p['id'] for p in d['players']]==[2]
    assert d['players'][0]['stats']['receivingTargets']==8
    assert 'share_of_starter_points_pct' not in d['players'][0]
    assert d['historical'] and 'current_status' not in d['players'][0]


@pytest.mark.parametrize('text', ['No one has clinched anything yet.', 'Oak has not yet clinched.',
                                 'Nobody has been eliminated.', "Oak hasn't clinched."])
def test_negated_playoff_claim_is_not_a_clinch_announcement(text):
    assert 'unsupported_playoff_claim' not in check_commentary(text,'Standings',packet())


def test_negated_claim_does_not_hide_a_second_positive_clinch():
    assert 'unsupported_playoff_claim' in check_commentary('Oak has not clinched, but Maple has clinched.','Standings',packet())


def test_live_bench_points_cannot_explain_team_lead():
    d=packet();d['matchups']=[{'week':2}]
    d['players'][0].update(slot='BE',game={'state':'post'})
    assert 'bench_points_do_not_contribute' in check_commentary('Player Alpha is providing a massive boost for Oak.','Scores',d)
    assert 'bench_points_do_not_contribute' not in check_commentary('Player Alpha could provide a boost next week.','Scores',d)
    assert 'bench_points_do_not_contribute' not in check_commentary('Player Alpha provided unused bench points.','Scores',d)
