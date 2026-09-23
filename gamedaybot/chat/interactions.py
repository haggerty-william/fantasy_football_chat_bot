"""Optional Discord gateway client for the neighborhood league."""

import asyncio
from io import BytesIO
import logging
import os
from threading import Thread
import time

import discord
from discord import app_commands
from discord.ext import tasks

from gamedaybot.chat.discord_format import build_payloads
from gamedaybot.chat.discord_images import prepare_payloads
from gamedaybot.espn.command_reports import command_report, ReportInputError

logger = logging.getLogger(__name__)


class LeagueClient(discord.Client):
    def __init__(self, guild_id, channel_id=None):
        super().__init__(intents=discord.Intents.none(), allowed_mentions=discord.AllowedMentions.none())
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tree = app_commands.CommandTree(self)
        self._work = None
        self._deferring = False
        guild = discord.Object(id=guild_id)

        @self.tree.command(name='matchup', description='Current matchup, scores, and projections.', guild=guild)
        @app_commands.describe(team='ESPN team name or abbreviation; omit for all matchups',
                               share='Show the result to the channel instead of just you')
        @app_commands.checks.cooldown(1, 15.0)
        async def matchup(interaction: discord.Interaction, team: str = None, share: bool = False):
            await self.respond(interaction, 'matchup', share=share, team=team)

        @self.tree.command(name='standings', description='Fetch the latest league standings.', guild=guild)
        @app_commands.describe(share='Show the result to the channel instead of just you')
        @app_commands.checks.cooldown(1, 15.0)
        async def standings(interaction: discord.Interaction, share: bool = False):
            await self.respond(interaction, 'standings', share=share)

        @self.tree.command(name='recap', description='Weekly scores and local AI analysis; defaults to the last completed week.', guild=guild)
        @app_commands.describe(week='Week number; omit for the last completed week',
                               share='Show the result to the channel instead of just you')
        @app_commands.checks.cooldown(1, 30.0)
        async def recap(interaction: discord.Interaction, week: app_commands.Range[int, 1, 25] = None,
                        share: bool = False):
            await self.respond(interaction, 'recap', share=share, week=week)

        @self.tree.command(name='trades', description='Recent completed trades with fantasy verdicts and roster predictions.', guild=guild)
        @app_commands.describe(days='Look back this many days (latest five trades)',
                               share='Show the result to the channel instead of just you')
        @app_commands.checks.cooldown(1, 30.0)
        async def trades(interaction: discord.Interaction, days: app_commands.Range[int, 1, 30] = 7,
                         share: bool = False):
            await self.respond(interaction, 'trades', share=share, days=days)

        def add_report(command, description):
            async def callback(interaction: discord.Interaction, share: bool = False):
                await self.respond(interaction, command, share=share)
            callback = app_commands.checks.cooldown(1, 30.0)(callback)
            self.tree.command(name=command, description=description, guild=guild)(callback)

        for command, description in (
            ('rivalry', 'Closest projected matchup, past meetings and a rivalry forecast.'),
            ('monday', 'Remaining starters and points needed to take the lead.'),
            ('awards', 'Last completed week: bench blunders, escapes, upsets and bad luck.'),
            ('playoffs', 'Playoff seeds, bubble games and conservative clinch calculations.'),
            ('tradefollowup', 'Revisit trades after two full completed weeks.')):
            add_report(command, description)

        @self.tree.command(name='pickem', description='Pick fantasy matchup winners or view your picks and leaderboard.', guild=guild)
        @app_commands.describe(team='Team you predict will win; omit to view your picks', share='Share your picks and the leaderboard')
        @app_commands.checks.cooldown(1, 10.0)
        async def pickem(interaction: discord.Interaction, team: str = None, share: bool = False):
            await self.respond(interaction, 'pickem', share=share, team=team,
                               user=interaction.user.id, name=interaction.user.display_name)

        @self.tree.command(name='alerts', description='Opt in to private pre-kickoff injury alerts for your team.', guild=guild)
        @app_commands.describe(team='Your ESPN team name or abbreviation', enabled='Set false to unsubscribe')
        @app_commands.checks.cooldown(1, 10.0)
        async def alerts(interaction: discord.Interaction, team: str = None, enabled: bool = True):
            await self.respond(interaction, 'alerts', team=team, enabled=enabled, user=interaction.user.id)

        @self.tree.error
        async def on_command_error(interaction, error):
            message = 'The command could not be completed. Please try again shortly.'
            if isinstance(error, app_commands.CommandOnCooldown):
                message = f'Please wait {error.retry_after:.0f} seconds before requesting that report again.'
            else:
                logger.warning('Discord command failed (%s)', type(error).__name__)
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)

    async def setup_hook(self):
        await self.tree.sync(guild=discord.Object(id=self.guild_id))
        logger.warning('Discord slash commands registered for the configured server')

    @tasks.loop(minutes=10)
    async def alert_loop(self):
        from gamedaybot.espn.team_alerts import pending_alerts
        from gamedaybot.espn.community_state import reserve, scope
        from gamedaybot.espn.command_reports import get_env_vars, League
        def collect():
            data = get_env_vars(require_chat=False)
            auth = {} if data['swid'] == '{1}' or data['espn_s2'] == '1' else {'swid':data['swid'], 'espn_s2':data['espn_s2']}
            league = League(league_id=data['league_id'],year=data['year'],**auth)
            return scope(league), pending_alerts(league)
        try:
            scope_id, batches = await asyncio.to_thread(collect)
            for user_id, keys, title, description, logo in batches:
                # Reserve before DM; ambiguous delivery must not produce repeated pings.
                claimed = await asyncio.to_thread(lambda: [reserve(scope_id,key) for key in keys])
                if not all(claimed):
                    continue
                try:
                    user = await self.fetch_user(int(user_id))
                    embed = discord.Embed(title=title[:256],description=description[:4000],color=0xE67E22)
                    from gamedaybot.chat.discord_images import fetch_logo
                    icon = await asyncio.to_thread(fetch_logo, logo) if logo else None
                    files = []
                    if icon is not None:
                        files.append(discord.File(BytesIO(icon), filename='team.png'))
                        embed.set_thumbnail(url='attachment://team.png')
                    await user.send(embed=embed, files=files, allowed_mentions=discord.AllowedMentions.none())
                except discord.HTTPException as error:
                    logger.warning('Team alert DM unavailable (%s); delivery not retried',type(error).__name__)
        except Exception as error:
            logger.warning('Team alert scan unavailable (%s)',type(error).__name__)

    @alert_loop.before_loop
    async def before_alert_loop(self):
        await self.wait_until_ready()

    async def close(self):
        self.alert_loop.cancel()
        await super().close()

    async def on_ready(self):
        if not self.alert_loop.is_running():
            self.alert_loop.start()
        logger.warning('Discord interactions ready')

    async def respond(self, interaction, command, share=False, **options):
        if interaction.guild_id != self.guild_id or (
                self.channel_id and interaction.channel_id != self.channel_id):
            await interaction.response.send_message('Use this command in the configured league channel/server.', ephemeral=True)
            return
        if self._deferring or (self._work is not None and not self._work.done()):
            await interaction.response.send_message('Another report is being fetched. Please try again shortly.', ephemeral=True)
            return
        self._deferring = True
        try:
            await interaction.response.defer(thinking=True, ephemeral=not share)
            self._work = asyncio.create_task(asyncio.to_thread(command_report, command, **options))
        finally:
            self._deferring = False
        # Keep the gateway responsive during synchronous ESPN and local LLM calls.
        # Shield the worker so a timeout cannot free its slot while its thread runs.
        self._work.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        try:
            from gamedaybot.espn.analysis_limits import command_timeout
            text = await asyncio.wait_for(asyncio.shield(self._work), timeout=command_timeout())
        except ReportInputError as error:
            await interaction.followup.send(str(error), ephemeral=not share)
            return
        except asyncio.TimeoutError:
            await interaction.followup.send('The data source is taking too long. Please try again later.', ephemeral=not share)
            return
        except Exception as error:
            logger.warning('League report unavailable (%s)', type(error).__name__)
            await interaction.followup.send('ESPN data is unavailable right now. Please try again shortly.', ephemeral=not share)
            return
        permissions = getattr(interaction, 'app_permissions', None)
        can_attach = not isinstance(permissions, discord.Permissions) or permissions.attach_files
        for payload, files in await asyncio.to_thread(prepare_payloads, text, include_images=can_attach):
            if isinstance(permissions, discord.Permissions) and not permissions.attach_files:
                files = []
                for embed in payload['embeds']:
                    embed.pop('image', None)
                    embed.pop('thumbnail', None)
                    if 'author' in embed:
                        embed['author'].pop('icon_url', None)
            await interaction.followup.send(
                embeds=[discord.Embed.from_dict(e) for e in payload['embeds']],
                files=[discord.File(BytesIO(data), filename=name) for name, data in files],
                allowed_mentions=discord.AllowedMentions.none(), ephemeral=not share)


def start_interactions():
    """Run the optional gateway beside the scheduler without changing broadcasts."""
    token = os.environ.get('DISCORD_BOT_TOKEN', '').strip()
    if not token:
        logger.warning('Discord commands disabled: DISCORD_BOT_TOKEN is not configured')
        return None
    try:
        guild_id = int(os.environ['DISCORD_GUILD_ID'])
        channel_id = int(os.environ['DISCORD_COMMAND_CHANNEL_ID']) if os.environ.get('DISCORD_COMMAND_CHANNEL_ID') else None
        if guild_id <= 0 or (channel_id is not None and channel_id <= 0):
            raise ValueError()
    except (KeyError, ValueError):
        logger.error('Discord commands disabled: configure valid server/channel IDs')
        return None

    def run():
        while True:
            try:
                LeagueClient(guild_id, channel_id).run(token, log_handler=None)
            except discord.LoginFailure:
                logger.error('Discord bot token rejected; update the Secret and restart')
                return
            except Exception as error:
                logger.warning('Discord connection failed (%s); retrying in 60 seconds', type(error).__name__)
            time.sleep(60)

    worker = Thread(target=run, name='discord-interactions', daemon=True)
    worker.start()
    return worker
