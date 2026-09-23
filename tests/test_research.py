from datetime import datetime, timezone, timedelta
from types import SimpleNamespace as Obj
from unittest.mock import Mock
import json

import pytest
import requests

from gamedaybot.espn import research
from gamedaybot.espn.analysis import generate_analysis


def player(name='Player One', status='QUESTIONABLE'):
    return Obj(name=name, injuryStatus=status, position='WR', lineupSlot='WR',
               stats={2: {'points': 12, 'projected_points': 10, 'breakdown': {'receivingYards': 80}},
                      3: {'projected_points': 14}})


def league(players=None):
    return Obj(scoringPeriodId=3, teams=[Obj(team_name='Oak', roster=players or [player()])])


def test_manager_context_keeps_team_mapping_and_excludes_account_data():
    teams = [Obj(team_id=1, team_name='Oak', owners=[
        {'firstName': ' Alex ', 'lastName': 'Jones', 'id': 'secret-id', 'email': 'private@example.com'},
        {'firstName': 'Alex', 'lastName': 'Jones'},
        {'displayName': 'CoManager'}, None]),
        Obj(team_id=2, team_name='Maple', owners=[{'firstName': 'Alex', 'lastName': 'Smith'}]),
        Obj(team_id=3, team_name='Birch', owners=None)]
    data = research.fantasy_team_context(Obj(teams=teams))
    assert data == [
        {'id': '1', 'name': 'Oak', 'managers': ['Alex Jones', 'CoManager']},
        {'id': '2', 'name': 'Maple', 'managers': ['Alex Smith']},
        {'id': '3', 'name': 'Birch', 'managers': []}]


def test_manager_names_reach_historical_context_with_current_scope():
    sample = league()
    sample.teams[0].team_id = 1
    sample.teams[0].owners = [{'firstName': 'Tanner', 'lastName': 'Example'}]
    data = research.build_context(sample, 'scores', 'get_final', 2)
    assert data['fantasy_teams'][0]['managers'] == ['Tanner Example']
    assert 'past management' in data['manager_scope']


def article(now, name='Player One', url='https://www.espn.com/nfl/story/_/id/123'):
    return {'headline': name + ' returns to practice', 'description': 'Practice participation is limited.',
            'published': now.isoformat(), 'links': {'web': {'href': url}}}


@pytest.fixture(autouse=True)
def empty_cache(monkeypatch):
    monkeypatch.setattr(research, '_cache', None)


def test_news_filters_dates_relevance_urls_and_duplicates(monkeypatch):
    now = datetime.now(timezone.utc)
    good = article(now - timedelta(hours=1))
    items = [article(now + timedelta(days=1)), article(now - timedelta(days=5)),
             article(now, 'Someone Else'), article(now, url='https://espn.com.evil.test/a'),
             {'headline': 'broken'}, good, good]
    monkeypatch.setattr(research, 'news_feed', lambda: (items, now.isoformat()))
    result, fetched = research.relevant_news(['Player One'], now)
    assert len(result) == 1
    assert result[0]['url'] == good['links']['web']['href']
    assert result[0]['players'] == ['Player One']


def test_news_cache_and_expired_failure_never_returns_stale(mock_requests, monkeypatch):
    mock_requests.get(research.NEWS_URL, json={'articles': []})
    assert research.news_feed() == research.news_feed()
    assert mock_requests.call_count == 1
    monkeypatch.setattr(research, '_cache', (-1000, [{'old':'news'}], 'old'))
    mock_requests.get(research.NEWS_URL, exc=requests.Timeout())
    with pytest.raises(requests.Timeout):
        research.news_feed()


def test_oversized_feed_rejected(mock_requests):
    mock_requests.get(research.NEWS_URL, content=b'x'*1_000_001)
    with pytest.raises(ValueError):
        research.news_feed()


def test_historical_stats_exclude_current_status_and_news(monkeypatch):
    news = Mock(side_effect=AssertionError('Must not fetch current news'))
    monkeypatch.setattr(research, 'news_feed', news)
    data = research.build_context(league(), 'scores', 'get_final', 2)
    entry = data['players'][0]
    assert entry['points'] == 12 and entry['stats']['receivingYards'] == 80
    assert 'current_status' not in entry and 'slot' not in entry
    assert data['historical'] and data['news'] == []
    news.assert_not_called()


def test_current_status_missing_stats_and_news_failure(monkeypatch):
    monkeypatch.setattr(research, 'news_feed', Mock(side_effect=requests.Timeout()))
    data = research.build_context(league(), 'scores', 'get_matchups', 3)
    entry = data['players'][0]
    assert entry['current_status'] == 'QUESTIONABLE'
    assert entry['points'] is None and entry['projected_points'] == 14
    assert 'unavailable' in data['news_status']
    assert data['status_updated_at'] is None


def test_selected_matchup_context_excludes_other_rosters(monkeypatch):
    monkeypatch.setattr(research, 'news_feed', lambda: ([], 'now'))
    box = Obj(home_team=Obj(team_name='Selected'), home_lineup=[player('Selected Player')],
              away_team=None, away_lineup=[])
    data = research.build_context(league(), 'matchup', 'get_matchups', 3, [box])
    assert [p['name'] for p in data['players']] == ['Selected Player']


def test_trade_context_prioritizes_named_players_and_includes_depth(monkeypatch):
    monkeypatch.setattr(research, 'news_feed', lambda: ([], 'now'))
    data = research.build_context(league([player(), player('Other Person')]),
                                  'Oak received Player One', 'get_trade_report', 3)
    assert [p['name'] for p in data['players']] == ['Player One', 'Other Person']


def test_large_context_is_bounded_and_discloses_omissions(monkeypatch):
    monkeypatch.setattr(research, 'news_feed', lambda: ([], 'now'))
    data = research.build_context(league([player('Player '+str(i)) for i in range(60)]),
                                  'scores', 'get_matchups', 3)
    assert len(data['players']) <= 40
    assert len(data['rosters'][0]['players']) == 60
    assert data['omitted_player_count'] == 60 - len(data['players'])
    assert len(json.dumps(data)) <= 32000


def test_model_gets_evidence_and_output_has_deterministic_sources(mock_requests, monkeypatch):
    monkeypatch.setenv('AI_MODEL', 'local')
    monkeypatch.setenv('AI_BASE_URL', 'http://localhost:1234/v1')
    now = datetime.now(timezone.utc)
    mock_requests.get(research.NEWS_URL, json={'articles': [article(now-timedelta(hours=1))]})
    endpoint = 'http://localhost:1234/v1/chat/completions'
    mock_requests.post(endpoint, json={'choices':[{'finish_reason':'stop','message':{'content':'Player One returned to limited practice [N1].'}}]})
    result = generate_analysis('Score Update\nOak 12', 'get_scoreboard_short', week=3, league=league())
    body = mock_requests.last_request.json()
    evidence = json.loads(body['messages'][1]['content'])['research_context']
    assert evidence['unassigned_players'][0]['current_status'] == 'QUESTIONABLE'
    assert evidence['news'][0]['url'] not in result
    assert 'Research Sources' not in result
    assert '[ESPN](' not in result and '[N1]' not in result
    assert evidence['news'][0]['citation_id'] == 'N1'


def test_only_used_verified_citations_are_linked():
    from gamedaybot.espn.analysis import inline_citations
    context={'news':[{'citation_id':'N1','url':'https://www.espn.com/nfl/story/_/id/123'},
                     {'citation_id':'N2','url':'https://www.espn.com/unused'}]}
    text=inline_citations('Practice update [N1]. Other claim [N9]. [Fake](https://evil.test) https://evil.test',context)
    assert '[ESPN](' not in text and '[N1]' not in text
    assert 'unused' not in text and 'evil.test' not in text and '[N9]' not in text
    assert inline_citations('Scores only.',context)=='Scores only.'


def test_discord_preserves_inline_source_link():
    from gamedaybot.chat.discord_format import build_payloads
    link='[ESPN](https://www.espn.com/nfl/story/_/id/123)'
    cards=list(build_payloads('AI Analysis\nPractice update '+link+'.'))[0]['embeds']
    assert len(cards)==1 and link in cards[0]['description']


def test_trade_context_includes_roster_depth_and_unrostered_traded_player(monkeypatch):
    monkeypatch.setattr(research,'news_feed',lambda:([], 'now'))
    team=Obj(team_id=1,team_name='Oak',roster=[player('Current WR')])
    traded=player('Departed WR')
    data=research.build_context(Obj(scoringPeriodId=3,teams=[team]),'Oak sent Departed WR',
                                'get_trade_report',3,trade_actions=[(team,'TRADE_SENT',traded,0)])
    assert data['current_roster_depth'][0]['positions']['WR']['count']==1
    assert data['players'][0]['name']=='Departed WR'
    actions=[(team,'TRADE_SENT',traded,0,'2026-09-18'),(team,'TRADE_RECEIVED',traded,0,'2026-09-19')]
    data=research.build_context(Obj(scoringPeriodId=3,teams=[team]),'Oak sent Departed WR',
                                'get_trade_report',3,trade_actions=actions)
    assert len(data['trade_sides'])==2
    assert data['trade_sides'][0]['sent']==['Departed WR']
    assert data['trade_sides'][1]['received']==['Departed WR']


def test_research_failure_does_not_block_model_or_report(mock_requests, monkeypatch):
    from gamedaybot.espn import analysis
    monkeypatch.setenv('AI_MODEL', 'local')
    monkeypatch.setenv('AI_BASE_URL', 'http://localhost:1234/v1')
    monkeypatch.setattr(analysis, 'build_context', Mock(side_effect=RuntimeError('private-data')))
    mock_requests.post('http://localhost:1234/v1/chat/completions', json={'choices':[{'finish_reason':'stop','message':{'content':'Oak leads.'}}]})
    assert generate_analysis('Scores', 'get_matchups', league=league()).endswith('Oak leads.')
