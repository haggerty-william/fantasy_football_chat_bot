from io import BytesIO
from types import SimpleNamespace as Obj
import json

from PIL import Image
import pytest

from gamedaybot.chat import discord_images as images
from gamedaybot.chat.discord_format import TeamReport
from gamedaybot.chat.discord import Discord

URL = 'https://g.espncdn.com/logo.svg'
SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64"><rect width="64" height="64" fill="red"/></svg>'


@pytest.fixture(autouse=True)
def clear_cache():
    images._cache.clear()


def test_svg_is_uploaded_as_png_and_cached(mock_requests):
    mock_requests.get(URL, text=SVG, headers={'Content-Type':'image/svg+xml'})
    team=Obj(team_name='Oak',team_abbrev='OAK',logo_url=URL)
    prepared=images.prepare_payloads(TeamReport('Score Update\nOAK 100', [team]))
    payload,files=prepared[0]
    assert payload['embeds'][0]['author']['icon_url'] == 'attachment://image-0.png'
    assert Image.open(BytesIO(files[0][1])).format == 'PNG'
    images.prepare_payloads(TeamReport('Score Update\nOAK 100', [team]))
    assert mock_requests.call_count == 1


def test_standings_one_message_one_table_with_all_ten_rows(mock_requests):
    mock_requests.get(URL,text=SVG,headers={'Content-Type':'image/svg+xml'})
    teams=[Obj(team_name='Team '+str(i),team_abbrev='T'+str(i),logo_url=URL) for i in range(10)]
    report=TeamReport('Current Standings\n'+'\n'.join(f'{i+1}: (1-1) {t.team_name}' for i,t in enumerate(teams))+
                      '\n\nAI Analysis\nA close race.\n\nResearch Sources\nESPN', teams)
    prepared=images.prepare_payloads(report)
    assert len(prepared)==1
    payload,files=prepared[0]
    assert len(payload['embeds'])==3 and len(files)==1
    assert payload['embeds'][0]['image']['url']=='attachment://image-0.png'
    assert Image.open(BytesIO(files[0][1])).size == (1320,830)
    assert 'description' not in payload['embeds'][0]
    fallback=images.prepare_payloads(report,include_images=False)[0][0]['embeds'][0]
    for team in teams:
        assert team.team_name in fallback['description']


def test_private_or_unavailable_logo_never_leaves_broken_image(mock_requests):
    private='https://mystique-api.fantasy.espn.com/private'
    team=Obj(team_name='Oak',team_abbrev='OAK',logo_url=private)
    payload,files=images.prepare_payloads(TeamReport('Score Update\nOAK 100',[team]))[0]
    assert 'icon_url' not in payload['embeds'][0]['author']
    assert not files and not mock_requests.called
    mock_requests.get(URL,status_code=401)
    assert images.fetch_logo(URL) is None


def test_standalone_projected_scores_use_full_names():
    teams=[Obj(team_name='Oak Street',team_abbrev='OAK',logo_url=''),Obj(team_name='Maple Street',team_abbrev='MAP',logo_url='')]
    payload,_=images.prepare_payloads(TeamReport('Projected Close Scores\nOAK 100.12 - 99.14 MAP',teams),include_images=False)[0]
    card=payload['embeds'][0]
    assert '**Oak Street** — 100.12' in card['description']
    assert '**Maple Street** — 99.14' in card['description']
    assert '```' not in card['description']


def test_external_svg_resources_are_rejected(mock_requests):
    mock_requests.get(URL,text='<svg xmlns="http://www.w3.org/2000/svg"><image href="file:///secret"/></svg>',
                      headers={'Content-Type':'image/svg+xml'})
    assert images.fetch_logo(URL) is None


def test_webhook_uses_multipart_attachment(mock_requests):
    url='https://discord.com/api/webhooks/test'
    mock_requests.post(url,status_code=204)
    bot=Discord(url)
    bot.send_message('Current Standings\n1: (1-0) Oak')
    request=mock_requests.last_request
    assert 'multipart/form-data' in request.headers['Content-Type']
    assert b'payload_json' in request.body and b'image-0.png' in request.body
    assert b'attachment://image-0.png' in request.body


def test_custom_logo_uses_espn_auth_only_on_exact_endpoint(mock_requests, monkeypatch):
    monkeypatch.setenv('ESPN_S2', 'test-cookie')
    monkeypatch.setenv('SWID', '{test-swid}')
    custom='https://mystique-api.fantasy.espn.com/apis/v1/domains/lm/images/e74b3400-aa5a-11f1-bcd0-ffc758441db5'
    stream=BytesIO()
    Image.new('RGB',(40,40),'blue').save(stream,format='JPEG')
    mock_requests.get(custom,content=stream.getvalue(),headers={'Content-Type':'image/jpeg'})
    assert images.fetch_logo(custom).startswith(b'\x89PNG')
    assert 'espn_s2=test-cookie' in mock_requests.last_request.headers['Cookie']
    mock_requests.get(URL,text=SVG,headers={'Content-Type':'image/svg+xml'})
    assert images.fetch_logo(URL)
    assert 'Cookie' not in mock_requests.last_request.headers
    assert images.fetch_logo(custom.replace('espn.com','espn.com.evil.test')) is None
    assert images.fetch_logo(custom.replace('/apis/v1/domains/lm/images/', '/other/')) is None


def test_custom_logo_redirect_does_not_forward_credentials(mock_requests,monkeypatch):
    monkeypatch.setenv('ESPN_S2','test-cookie')
    custom='https://mystique-api.fantasy.espn.com/apis/v1/domains/lm/images/e74b3400-aa5a-11f1-bcd0-ffc758441db5'
    mock_requests.get(custom,status_code=302,headers={'Location':'https://example.com'})
    assert images.fetch_logo(custom) is None
    assert mock_requests.call_count == 1


def test_cached_logo_survives_transient_refresh_failure(mock_requests):
    mock_requests.get(URL,text=SVG,headers={'Content-Type':'image/svg+xml'})
    good=images.fetch_logo(URL)
    images._cache[URL]=(0,good)
    mock_requests.get(URL,status_code=503)
    assert images.fetch_logo(URL)==good


def test_standings_win_percentage_and_games_back_include_ties():
    from gamedaybot.espn.functionality import get_standings
    teams=[Obj(team_name='Leader',wins=3,losses=0,ties=1),
           Obj(team_name='Second',wins=2,losses=1,ties=1),Obj(team_name='New',wins=0,losses=0,ties=0)]
    report=get_standings(Obj(standings=lambda:teams))
    assert '(3-0-1) Leader | Win% 87.5% | GB 0' in report
    assert '(2-1-1) Second | Win% 62.5% | GB 1' in report
    assert '(0-0-0) New | Win% 0.0% | GB 1.5' in report
    assert images.standings_image(report,[],{}) is not None


@pytest.mark.parametrize('count',[1,5])
def test_matchups_are_one_board_with_named_scores_and_no_duplicate_cards(monkeypatch,count):
    teams=[Obj(team_name=f'Team {i}',team_abbrev=f'T{i}',wins=1,losses=0,ties=0,logo_url=URL) for i in range(count*2)]
    boxes=[Obj(home_team=teams[i],away_team=teams[i+1],home_score=100+i,away_score=90+i,
               home_lineup=[Obj(slot_position='QB',points=100+i,game_played=100,projected_points=0)],
               away_lineup=[Obj(slot_position='QB',points=90+i,game_played=100,projected_points=0)])
           for i in range(0,count*2,2)]
    mock_image=BytesIO()
    Image.new('RGB',(40,40),'red').save(mock_image,format='PNG')
    monkeypatch.setattr(images,'fetch_logo',lambda url:mock_image.getvalue())
    text=TeamReport('Matchups\nold duplicate names\n\nScore Update\nold scores\n\n'
                    'Approximate Projected Scores\nold projections\nFetched from ESPN today\n\n'
                    'AI Analysis\nCommentary\n\nResearch Sources\nESPN',teams,matchups=boxes)
    prepared=images.prepare_payloads(text)
    assert len(prepared)==1
    payload,files=prepared[0]
    assert len(payload['embeds'])==3 and len(files)==1
    board=payload['embeds'][0]
    assert 'author' not in board and 'thumbnail' not in board
    assert 'description' not in board
    assert 'Fetched from ESPN today' in board['footer']['text']
    board=images.prepare_payloads(text,include_images=False)[0][0]['embeds'][0]
    for team in teams:
        assert board['description'].count(team.team_name+' (')==1
    assert 'old ' not in board['description']
    assert '100.00' in board['description'] and '90.00' in board['description']
    assert 'Fetched from ESPN today' in board['description']
    assert Image.open(BytesIO(files[0][1])).size==(1320,104+count*168)


def test_failed_image_render_keeps_full_text(monkeypatch):
    monkeypatch.setattr(images,'standings_image',lambda *args:None)
    payload,files=images.prepare_payloads('Current Standings\n1: (2-0) Oak')[0]
    assert not files and 'Oak' in payload['embeds'][0]['description']


def test_historical_recap_is_one_final_board_and_one_awards_card(monkeypatch):
    teams=[Obj(team_name='Oak',team_abbrev='OAK',logo_url=''),Obj(team_name='Maple',team_abbrev='MAP',logo_url='')]
    boxes=[Obj(home_team=teams[0],away_team=teams[1],home_score=100,away_score=90)]
    text=TeamReport('Week 1 · Completed\n\nFinal Score Update\nOAK 100 - 90 MAP\n\n'
                    'Trophies of the week:\n👑 High score 👑\nOak 100\n💩 Low score 💩\nMaple 90\n\n'
                    'AI Analysis\nA recap.',teams,boxes,final_scores=True,week=1)
    prepared=images.prepare_payloads(text)
    assert len(prepared)==1
    cards=prepared[0][0]['embeds']
    assert len(cards)==3
    assert 'Final scores' in cards[0]['title'] and 'Week 1' in cards[0]['title']
    assert 'description' not in cards[0]
    assert 'High score' in cards[1]['description'] and 'Low score' in cards[1]['description']
    fallback=images.prepare_payloads(text,include_images=False)[0][0]['embeds'][0]['description']
    assert 'Projected' not in fallback and '100.00' in fallback


@pytest.mark.parametrize('heading',['Starting Players to Monitor','Trophies of the week:',
                                  'Waiver Report 2026-09-20:','Trade Report 2026-09-20:'])
def test_report_sections_do_not_split_into_per_team_cards(heading):
    teams=[Obj(team_name='Team '+str(i),team_abbrev='T'+str(i),logo_url='https://example.com/a.png') for i in range(10)]
    report=TeamReport(heading+'\n'+'\n'.join(t.team_name+' received Player' for t in teams),teams)
    cards=images.prepare_payloads(report,include_images=False)[0][0]['embeds']
    assert len(cards)==1


def named_teams():
    return [Obj(team_name="Tanny's Illiterate Accountants", team_abbrev='TIA', logo_url='', wins=1, losses=1, ties=0,
                owners=[{'firstName':'Tanner','lastName':'Example'}]),
            Obj(team_name='Sports Team!!', team_abbrev='ST', logo_url='', wins=1, losses=1, ties=0,
                owners=[{'firstName':'Alex','lastName':'Morgan'}]),
            Obj(team_name='San Fartcisco 69ers', team_abbrev='SF', logo_url='', wins=1, losses=1, ties=0,
                owners=[{'firstName':'Alex','lastName':'Carter'}])]


def test_boards_draw_manager_labels_without_duplicate_suffixes(monkeypatch):
    teams=named_teams()
    drawn=[]
    original=images.ImageDraw.ImageDraw.text
    def capture(self,xy,text,*args,**kwargs):
        drawn.append(str(text))
        return original(self,xy,text,*args,**kwargs)
    monkeypatch.setattr(images.ImageDraw.ImageDraw,'text',capture)
    report=TeamReport('Current Standings\n1: (1-1-0) Tanny\'s Illiterate Accountants | Win% 50.0% | GB 0\n'
                      '2: (1-1-0) Sports Team!! | Win% 50.0% | GB 0',teams)
    payload,files=images.prepare_payloads(report)[0]
    assert "Tanny's Illiterate Accountants (Tanner)" in drawn
    assert 'Sports Team!! (Alex M.)' in drawn
    assert not any('(Tanner) (Tanner)' in s for s in drawn)
    assert '50.0%' in drawn and '1-1-0' in drawn
    assert Image.open(BytesIO(files[0][1])).width == 1320
    drawn.clear()
    boxes=[Obj(home_team=teams[0],away_team=teams[1],home_score=100,away_score=90)]
    assert images.matchups_image(boxes,{},teams=teams)
    assert "Tanny's Illiterate Accountants (Tanner)" in drawn
    assert 'Sports Team!! (Alex M.)' in drawn
    assert '100.00' in drawn and '90.00' in drawn


def test_power_rankings_one_named_board_preserves_values_and_text_fallback(monkeypatch):
    teams=named_teams()
    drawn=[]
    original=images.ImageDraw.ImageDraw.text
    def capture(self,xy,text,*args,**kwargs):
        drawn.append(str(text))
        return original(self,xy,text,*args,**kwargs)
    monkeypatch.setattr(images.ImageDraw.ImageDraw,'text',capture)
    report=TeamReport('Power Rankings (Playoff %)\n99.99[🟢12.3%] (80.0) - TIA\n'
                      '84.12[🔻5.4%] (60.0) - ST\n72.31 (50.0) - SF',teams)
    prepared=images.prepare_payloads(report)
    assert len(prepared)==1
    payload,files=prepared[0]
    assert len(payload['embeds'])==1 and len(files)==1
    assert 'description' not in payload['embeds'][0]
    assert 'Sports Team!! (Alex M.)' in drawn
    assert '99.99' in drawn and '80.0%' in drawn and '+12.3%' in drawn and '-5.4%' in drawn
    assert Image.open(BytesIO(files[0][1])).size == (1320,326)
    monkeypatch.setattr(images,'power_rankings_image',lambda *args:None)
    fallback=images.prepare_payloads(report)[0][0]['embeds'][0]['description']
    assert 'Sports Team!! (Alex M.)' in fallback and '99.99' in fallback
