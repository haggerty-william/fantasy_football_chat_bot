import pytest
import sys
import os
sys.path.insert(1, os.path.abspath('.'))
from gamedaybot.chat.discord import (Discord, DiscordException, )
from gamedaybot.chat.discord_format import build_payloads
from gamedaybot.chat.discord_format import TeamReport
from gamedaybot.commentator_names import ANALYST_NAME, RESPONDER_NAME
from types import SimpleNamespace


def test_report_tables_include_manager_labels_but_ai_prose_is_unchanged():
    teams = [SimpleNamespace(team_name='Oak', team_abbrev='OAK', owners=[{'firstName':'Alex','lastName':'Morgan'}]),
             SimpleNamespace(team_name='Maple', team_abbrev='MAP', owners=[{'firstName':'Alex','lastName':'Jordan'}])]
    report = TeamReport('Current Standings\n1: (2-0) Oak\n2: (1-1) Maple\n\n'
                        'Power Rankings (Playoff %)\n100 (75) - OAK\n90 (50) - MAP\n\n'
                        'AI Analysis\nOak leads Maple.', teams)
    cards = [e for p in build_payloads(report) for e in p['embeds']]
    standings = next(e for e in cards if 'League standings' in e['title'])
    power = next(e for e in cards if 'Power rankings' in e['title'])
    analysis = next(e for e in cards if e['title'] == ANALYST_NAME)
    assert 'Oak (Alex M.)' in standings['description'] and 'Maple (Alex J.)' in standings['description']
    assert 'Oak (Alex M.)' in power['description'] and 'Maple (Alex J.)' in power['description']
    assert analysis['description'] == 'Oak leads Maple.'


def test_display_labels_are_not_duplicated_or_inserted_inside_other_team_names():
    from gamedaybot.chat.discord_format import label_team_names
    teams = [SimpleNamespace(team_name='Oak', owners=[{'firstName':'Tanner','lastName':'Example'}]),
             SimpleNamespace(team_name='Oak Grove', owners=[{'firstName':'Joe','lastName':'Example'}])]
    assert label_team_names('Oak Grove vs Oak', teams) == 'Oak Grove (Joe) vs Oak (Tanner)'
    assert label_team_names('Oak (Tanner)', teams) == 'Oak (Tanner)'


@pytest.mark.usefixtures("mock_requests")
class TestDiscord:
    '''Test DiscordBot class'''

    def setup_method(self):
        self.url = "https://discordapp.com/api/webhooks/123/abc"
        self.test_bot = Discord(self.url)
        self.test_text = "This is a test."

    def test_send_message(self, mock_requests):
        '''Does the message send successfully?'''
        mock_requests.post(self.url, status_code=204)
        assert self.test_bot.send_message(self.test_text).status_code == 204

    def test_bad_bot_id(self, mock_requests):
        '''Does the expected error raise when a bot id is incorrect?'''
        mock_requests.post(self.url, status_code=404)
        with pytest.raises(DiscordException):
            self.test_bot.send_message(self.test_text)

    def test_webhook_receives_embeds(self, mock_requests):
        mock_requests.post(self.url, status_code=204)
        self.test_bot.send_message('Trade Report 2026-09-20:\n\nOak received Player One')
        payload = mock_requests.last_request.json()
        assert 'content' not in payload
        assert payload['allowed_mentions'] == {'parse': []}
        assert '**Oak** received **Player One**' in payload['embeds'][0]['description']

    @pytest.mark.parametrize('model,label', [
        ('google/gemma-4-12b-qat', 'Gemma 4 12B QAT'),
        ('qwen/qwen3.6-35b-a3b', 'Qwen3.6 35B A3B'),
        ('nvidia/NVIDIA-Nemotron-Nano-9B-v2-Q4_K_M.gguf', 'NVIDIA Nemotron Nano 9B V2 Q4_K_M'),
        ('', 'Local model'),
    ])
    def test_announcers_are_sent_in_separate_ordered_messages(self, mock_requests, monkeypatch, model, label):
        monkeypatch.setenv('AI_MODEL', model)
        mock_requests.post(self.url, status_code=204)
        self.test_bot.send_message(
            'Trade Report 2026-09-20:\nOak received Player One\n\n'
            'AI Analysis\nThis addresses Oak\'s thin bench.\n\n'
            'AI Hot Take\nGraham, depth only helps if those backups can produce.')
        posts = [request.json() for request in mock_requests.request_history]
        assert len(posts) == 3
        assert all(len(post['embeds']) == 1 for post in posts)
        analyst, responder = (post['embeds'][0] for post in posts[1:])
        assert [analyst['title'], responder['title']] == [ANALYST_NAME, RESPONDER_NAME]
        assert 'thin bench' in analyst['description']
        assert responder['description'].startswith('Graham,')
        assert posts[0]['embeds'][0]['footer']['text'] == 'GameDayBot • ESPN Fantasy'
        assert all(card['footer']['text'] == 'GameDayBot • ' + label
                   for card in (analyst, responder))

    def test_failed_analyst_delivery_does_not_send_responder(self, mock_requests):
        mock_requests.post(self.url, [{'status_code': 204}, {'status_code': 503}])
        with pytest.raises(DiscordException):
            self.test_bot.send_message(
                'League update\nThe report.\n\nAI Analysis\nThe analysis.'
                '\n\nAI Hot Take\nThe reaction.')
        assert mock_requests.call_count == 2
        assert mock_requests.last_request.json()['embeds'][0]['title'] == ANALYST_NAME

    def test_quiet_report_sends_nothing(self, mock_requests):
        self.test_bot.send_message('  ')
        assert not mock_requests.called

    def test_long_report_posts_every_page(self, mock_requests):
        mock_requests.post(self.url, status_code=204)
        text = 'Trade Report 2026-09-20:\n' + '\n'.join(
            f'Team {i} received Player {i}' for i in range(400))
        self.test_bot.send_message(text)
        expected = list(build_payloads(text))
        assert len(expected) > 1
        assert [r.json() for r in mock_requests.request_history] == expected


def test_scores_and_projections_have_distinct_cards():
    payloads = list(build_payloads(
        'Score Update\nOAK 100.25 -  99.10 MAP\n\n'
        'Approximate Projected Scores\nOAK 120.25 - 119.10 MAP'))
    assert len(payloads) == 1
    scores, projections = payloads[0]['embeds']
    assert scores['title'] == '🏈 Scoreboard'
    assert projections['title'] == '🔮 Projected scores'
    assert 'OAK 100.25 -  99.10 MAP' in scores['description']
    assert scores['description'].startswith('```\n')
    assert scores['color'] != projections['color']


def test_final_scores_and_awards_are_separate():
    cards = list(build_payloads(
        'Final Score Update\nOAK 100 - 99 MAP\n\n'
        'Trophies of the week:\n👑 High score 👑\nOak with 100 points'))[0]['embeds']
    assert len(cards) == 2
    assert cards[1]['title'] == '🏆 Weekly awards'
    assert '**👑 High score 👑**' in cards[1]['description']
    assert 'Oak with 100 points' in cards[1]['description']


def test_custom_single_line_message_is_preserved():
    embed = list(build_payloads('Welcome to the neighborhood!'))[0]['embeds'][0]
    assert embed['description'] == 'Welcome to the neighborhood!'


def test_team_markdown_cannot_break_emphasis():
    embed = list(build_payloads(
        'Trade Report 2026-09-20:\nThe *Stars* received Player_One'))[0]['embeds'][0]
    assert r'**The \*Stars\*** received **Player\_One**' in embed['description']


def test_large_report_stays_within_discord_embed_limits():
    rows = [f'TEAM{i:04} 123.45 - 120.12 OPP{i:04}' for i in range(900)]
    payloads = list(build_payloads('Score Update\n' + '\n'.join(rows)))
    descriptions = []
    for payload in payloads:
        assert 1 <= len(payload['embeds']) <= 10
        total = 0
        for embed in payload['embeds']:
            description = embed.get('description', '')
            assert len(embed['title']) <= 256
            assert len(description) <= 4096
            total += len(embed['title']) + len(description) + len(embed['footer']['text'])
            descriptions.append(description)
        assert total <= 6000
    combined = '\n'.join(descriptions)
    for row in rows:
        assert combined.count(row) == 1


def logo_team(name, abbrev, logo='https://example.com/logo.png'):
    return SimpleNamespace(team_name=name, team_abbrev=abbrev, logo_url=logo)


def test_standings_remain_one_card_in_rank_order():
    teams = [logo_team('Oak', 'OAK'), logo_team('Maple', 'MAP', 'https://example.com/map.png')]
    report = TeamReport('Current Standings\n 1: (2-0) Maple\n 2: (1-1) Oak', teams)
    cards = list(build_payloads(report))[0]['embeds']
    assert len(cards) == 1
    assert cards[0]['description'].index('Maple') < cards[0]['description'].index('Oak')
    assert '1: (2-0) Maple' in cards[0]['description']


def test_multi_team_text_does_not_assign_one_teams_icon_to_whole_report():
    teams = [logo_team('Oak', 'OAK'), logo_team('Maple', 'MAP', 'https://example.com/map.png')]
    card = list(build_payloads('Score Update\nMAP 100 - 99 OAK', teams))[0]['embeds'][0]
    assert 'author' not in card and 'thumbnail' not in card
    assert '**Maple** — 100' in card['description']
    assert '**Oak** — 99' in card['description']


def test_logos_flow_through_webhook(mock_requests):
    bot = Discord('https://discord.com/api/webhooks/test')
    bot.teams = [logo_team('Oak', 'OAK')]
    mock_requests.post(bot.webhook_url, status_code=204)
    bot.send_message('Trade Report 2026-09-20:\nOak received Player One')
    assert mock_requests.last_request.json()['embeds'][0]['author']['name'] == 'Oak'


@pytest.mark.parametrize('url', ['', 'file:///secret', 'javascript:alert(1)', 'https://user:password@example.com/a', 'https://[bad'])
def test_missing_or_invalid_logos_preserve_report(url):
    text = 'Current Standings\n1: (2-0) Oak'
    assert list(build_payloads(text, [logo_team('Oak', 'OAK', url)])) == list(build_payloads(text))


def test_ambiguous_abbreviation_and_partial_name_do_not_get_wrong_icon():
    teams = [logo_team('Oak', 'ABC'), logo_team('Maple', 'ABC')]
    card = list(build_payloads('Score Update\nABC 100 - 99 OAKLAND', teams))[0]['embeds'][0]
    assert 'author' not in card and 'thumbnail' not in card


def test_trophy_labels_stay_with_correct_teams():
    teams = [logo_team('Oak', 'OAK'), logo_team('Maple', 'MAP')]
    text = 'Trophies of the week:\n👑 High score 👑\nOak with 120 points\n💩 Low score 💩\nMaple with 90 points'
    cards = list(build_payloads(text, teams))[0]['embeds']
    high = next(c for c in cards if 'Oak with' in c.get('description', ''))
    low = next(c for c in cards if 'Maple with' in c.get('description', ''))
    assert len(cards)==1
    assert 'author' not in high and 'author' not in low
    assert 'High score' in high['description']
    assert 'Low score' in low['description']


def test_icon_cards_paginate_with_author_names_in_character_budget():
    teams = [logo_team('Team '+str(i)+'x'*180, 'T'+str(i)) for i in range(40)]
    text = 'Current Standings\n' + '\n'.join(f'{i}: (2-0) {t.team_name}' for i,t in enumerate(teams))
    payloads = list(build_payloads(text, teams))
    cards = [c for p in payloads for c in p['embeds']]
    assert len(cards) < 40
    for team in teams:
        assert sum(team.team_name in card['description'] for card in cards) == 1
    for payload in payloads:
        assert len(payload['embeds']) <= 10
        assert sum(len(c['title']) + len(c.get('description','')) + len(c['footer']['text']) +
                   len(c.get('author',{}).get('name','')) for c in payload['embeds']) <= 6000
