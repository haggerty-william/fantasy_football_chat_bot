import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from gamedaybot.chat import interactions as module
from gamedaybot.espn import command_reports as reports
from gamedaybot.commentator_names import ANALYST_NAME, RESPONDER_NAME


def interaction(guild=123, channel=456):
    return SimpleNamespace(guild_id=guild, channel_id=channel,
                           response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
                           followup=SimpleNamespace(send=AsyncMock()))


@pytest.mark.parametrize('share', [False, True])
def test_commands_defer_and_use_embeds(monkeypatch, share):
    async def run():
        client = module.LeagueClient(123)
        event = interaction()
        def report(*args, **kwargs):
            assert event.response.defer.await_count == 1
            return 'Standings\n1. Team Alpha'
        monkeypatch.setattr(module, 'command_report', report)
        assert {c.name for c in client.tree.get_commands(guild=discord.Object(id=123))} == {'matchup', 'standings', 'recap', 'trades', 'rivalry', 'monday', 'awards', 'playoffs', 'tradefollowup', 'pickem', 'alerts'}
        await client.respond(event, 'standings', share=share)
        event.response.defer.assert_awaited_once_with(thinking=True, ephemeral=not share)
        assert event.followup.send.call_args.kwargs['ephemeral'] is (not share)
        assert event.followup.send.call_args.kwargs['embeds']
        assert not event.followup.send.call_args.kwargs['allowed_mentions'].everyone
    asyncio.run(run())


@pytest.mark.parametrize('share', [False, True])
@pytest.mark.parametrize('has_response', [False, True])
def test_commands_post_each_announcer_separately_in_order(monkeypatch, share, has_response):
    async def run():
        client = module.LeagueClient(123)
        event = interaction()
        report = 'Score Update\nOAK 100 - 99 MAP\n\nAI Analysis\nOak needs more bench depth.'
        if has_response:
            report += '\n\nAI Hot Take\nGraham, the waiver wire is calling.'
        monkeypatch.setattr(module, 'command_report', lambda *a, **k: report)
        await client.respond(event, 'standings', share=share)
        sent = [call.kwargs for call in event.followup.send.await_args_list]
        assert len(sent) == (3 if has_response else 2)
        expected = ['🏈 Scoreboard', ANALYST_NAME] + ([RESPONDER_NAME] if has_response else [])
        assert [post['embeds'][0].title for post in sent] == expected
        assert all(len(post['embeds']) == 1 and post['ephemeral'] is (not share) for post in sent)
        assert all(post['embeds'][0].footer.text == 'GameDayBot • AI commentary' for post in sent[1:])
    asyncio.run(run())


@pytest.mark.parametrize('guild,channel', [(999,456), (123,999)])
def test_wrong_server_or_channel_denied(guild, channel):
    async def run():
        client = module.LeagueClient(123, 456)
        event = interaction(guild, channel)
        await client.respond(event, 'standings')
        event.response.send_message.assert_awaited_once()
        event.response.defer.assert_not_awaited()
    asyncio.run(run())


def test_concurrent_request_during_deferral_is_rejected(monkeypatch):
    async def run():
        client = module.LeagueClient(123)
        first, second = interaction(), interaction()
        async def defer(**kwargs):
            await client.respond(second, 'standings')
        first.response.defer.side_effect = defer
        monkeypatch.setattr(module, 'command_report', lambda *a, **k: 'Standings\nTeam')
        await client.respond(first, 'standings')
        second.response.send_message.assert_awaited_once()
        second.response.defer.assert_not_awaited()
    asyncio.run(run())


def test_upstream_errors_do_not_expose_details(monkeypatch):
    async def run():
        client = module.LeagueClient(123)
        event = interaction()
        monkeypatch.setattr(module, 'command_report', Mock(side_effect=RuntimeError('secret-cookie')))
        await client.respond(event, 'standings')
        assert 'secret' not in event.followup.send.call_args.args[0]
        assert 'unavailable' in event.followup.send.call_args.args[0]
    asyncio.run(run())


def test_webhook_only_mode(monkeypatch):
    monkeypatch.delenv('DISCORD_BOT_TOKEN', raising=False)
    assert module.start_interactions() is None


def test_channel_without_attach_files_still_gets_text(monkeypatch):
    async def run():
        client = module.LeagueClient(123)
        event = interaction()
        event.app_permissions = discord.Permissions.none()
        monkeypatch.setattr(module, 'command_report', lambda *a, **k: 'Current Standings\n1: (1-0) Oak')
        await client.respond(event, 'standings')
        sent = event.followup.send.call_args.kwargs
        assert sent['files'] == []
        assert not sent['embeds'][0].image.url
        assert 'Oak' in sent['embeds'][0].description
    asyncio.run(run())


def test_sync_is_scoped_to_configured_server():
    async def run():
        client = module.LeagueClient(123)
        client.tree.sync = AsyncMock()
        await client.setup_hook()
        assert client.tree.sync.call_args.kwargs['guild'].id == 123
    asyncio.run(run())


def test_timeout_keeps_worker_busy(monkeypatch):
    async def run():
        client = module.LeagueClient(123)
        released = asyncio.Event()
        async def worker(*args, **kwargs):
            await released.wait()
            return 'Standings'
        async def timeout(awaitable, **kwargs):
            raise asyncio.TimeoutError()
        monkeypatch.setattr(module.asyncio, 'to_thread', worker)
        monkeypatch.setattr(module.asyncio, 'wait_for', timeout)
        event = interaction()
        await client.respond(event, 'standings')
        assert 'too long' in event.followup.send.call_args.args[0]
        assert not client._work.done()
        other = interaction()
        await client.respond(other, 'standings')
        other.response.defer.assert_not_awaited()
        released.set()
        await client._work
    asyncio.run(run())


@pytest.fixture
def league(monkeypatch):
    monkeypatch.setenv('LEAGUE_ID', '123')
    alpha = SimpleNamespace(team_id=1, team_name='Alpha Team', team_abbrev='ALP')
    beta = SimpleNamespace(team_id=2, team_name='Beta Team', team_abbrev='BET')
    for i,t in enumerate((alpha,beta),1):
        t.standing=i
        t.wins=1
        t.losses=1
        t.ties=0
        t.schedule=[]
    league = SimpleNamespace(scoringPeriodId=3, finalScoringPeriod=17, teams=[alpha, beta], settings=SimpleNamespace(playoff_team_count=1, reg_season_count=14))
    monkeypatch.setattr(reports, 'League', Mock(return_value=league))
    monkeypatch.setattr(reports.espn, 'fetch_box_scores', Mock(return_value=[SimpleNamespace(home_team=alpha, away_team=beta, home_score=100, away_score=90, home_lineup=[], away_lineup=[])]))
    for name, result in [('get_matchups','Matchups\nAlpha vs Beta'), ('get_scoreboard_short','Score Update\nAlpha 100'),
                         ('get_projected_scoreboard','Projected Scoreboard\nAlpha 110'), ('get_trophies','Awards\nAlpha'),
                         ('get_standings','Standings\nAlpha')]:
        monkeypatch.setattr(reports.espn, name, Mock(return_value=result))
    monkeypatch.setattr(reports, 'generate_analysis', Mock(return_value=''))
    return league


def test_recap_keeps_completed_report_when_analyst_has_nothing_to_add(league):
    text = reports.command_report('recap')
    assert 'Week 2' in text and 'Completed' in text
    assert 'unavailable' not in text and 'NO_ADDITIONAL_INSIGHT' not in text
    reports.espn.fetch_box_scores.assert_called_once_with(league, week=2)
    assert 'Neighborhood awards' in text
    reports.generate_analysis.assert_called_once()


def test_live_recap_omits_final_awards(league):
    text = reports.command_report('recap', week=3)
    assert 'In progress' in text
    reports.espn.get_trophies.assert_not_called()
    assert reports.generate_analysis.call_args.args[1] == 'get_scoreboard_short'


def test_preseason_and_future_week(league):
    league.scoringPeriodId = 1
    assert 'No completed week' in reports.command_report('recap')
    with pytest.raises(reports.ReportInputError):
        reports.command_report('recap', week=2)
    reports.espn.fetch_box_scores.assert_not_called()


def test_team_filter_reuses_snapshot_and_rejects_ambiguity(league):
    assert 'Alpha' in reports.command_report('matchup', team='alp')
    reports.espn.fetch_box_scores.assert_called_once()
    with pytest.raises(reports.ReportInputError, match='multiple'):
        reports.command_report('matchup', team='Team')
    with pytest.raises(reports.ReportInputError, match='No matching'):
        reports.command_report('matchup', team='missing')


def test_every_request_refreshes_league(league):
    reports.command_report('standings')
    reports.command_report('standings')
    assert reports.League.call_count == 2
