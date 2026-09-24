import os
if os.environ.get("AWS_EXECUTION_ENV") is not None:
    # For use in lambda function
    import utils.util as util
    from chat.groupme import GroupMe
    from chat.slack import Slack
    from chat.discord import Discord
else:
    # For local use
    import sys
    sys.path.insert(1, os.path.abspath('.'))
    import gamedaybot.utils.util as util
    from gamedaybot.chat.groupme import GroupMe
    from gamedaybot.chat.slack import Slack
    from gamedaybot.chat.discord import Discord
    from gamedaybot.espn.env_vars import get_env_vars
    import gamedaybot.espn.functionality as espn
    import gamedaybot.espn.season_recap as recap
    from gamedaybot.espn.trades import get_trade_report
    from gamedaybot.espn.analysis import generate_analysis
    from gamedaybot.chat.discord_format import TeamReport
    from gamedaybot.espn.trade_notifications import poll_trades


from espn_api.football import League
import json
import logging

logger = logging.getLogger(__name__)
# logger.setLevel(logging.INFO)
logger.setLevel(logging.DEBUG)


def espn_bot(function):
    """
    This function is used to send messages to a messaging platform (e.g. Slack, Discord, or GroupMe) with information
    about a fantasy football league.

    Parameters
    ----------
    function: str
        A string that specifies which type of information to send (e.g. "get_matchups", "get_power_rankings").

    Returns
    -------
    None

    Notes
    -----
    The function uses the following information from the data dictionary:

    str_limit: the character limit for messages on slack.
    bot_id: the id of the GroupMe bot.
        If not provided, defaults to 1.
    slack_webhook_url: the webhook url for the slack bot.
        If not provided, defaults to 1.
    discord_webhook_url: the webhook url for the discord bot.
        If not provided, defaults to 1.
    league_id: the id of the fantasy football league.
    year: the year of the league.
        If not provided, defaults to current year.
    swid: the swid of the league.
        If not provided, defaults to '{1}'.
    espn_s2: the espn s2 of the league.
        If not provided, defaults to '1'.
    close_scores_threshold: the largest projected point difference that still
        counts as a close matchup.
        If not provided, defaults to functionality.CLOSE_SCORES_DEFAULT_THRESHOLD.

    The function creates GroupMe, Slack, and Discord objects, and a League object using the provided information.
    It then uses the specified function to generate a message and sends it through the appropriate messaging platform.

    Possible function values:

    get_matchups: sends the current week's matchups and the projected scores for the remaining games.
    get_monitor: sends a message with a summary of the current week's scores.
    get_scoreboard_short: sends a short version of the current week's scores.
    get_projected_scoreboard: sends the projected scores for the remaining games.
    get_close_scores: sends a message with the scores of games that have a difference of less than 7 points.
    get_power_rankings: sends a message with the power rankings for the league.
    get_trophies: sends a message with the trophies for the league.
    get_standings: sends a message with the standings for the league.
    get_final: sends the final scores and trophies for the previous week.
    get_waiver_report: sends a message with the waiver report for the league.
    init: sends a message to confirm that the bot has been set up.
    """

    data = get_env_vars()
    str_limit = data['str_limit']  # slack char limit

    try:
        bot_id = data['bot_id']
    except KeyError:
        bot_id = 1

    try:
        slack_webhook_url = data['slack_webhook_url']
    except KeyError:
        slack_webhook_url = 1

    try:
        discord_webhook_url = data['discord_webhook_url']
    except KeyError:
        discord_webhook_url = 1

    if (len(str(bot_id)) <= 1 and
        len(str(slack_webhook_url)) <= 1 and
            len(str(discord_webhook_url)) <= 1):
        # Ensure that there's info for at least one messaging platform,
        # use length of str in case of blank but non null env variable
        raise Exception("No messaging platform info provided. Be sure one of BOT_ID, SLACK_WEBHOOK_URL, or DISCORD_WEBHOOK_URL env variables are set")

    league_id = data['league_id']

    try:
        year = int(data['year'])
    except KeyError:
        year = 2026

    try:
        swid = data['swid']
    except KeyError:
        swid = '{1}'

    if swid.find("{", 0) == -1:
        swid = "{" + swid
    if swid.find("}", -1) == -1:
        swid = swid + "}"

    try:
        espn_s2 = data['espn_s2']
    except KeyError:
        espn_s2 = '1'

    try:
        close_scores_threshold = data['close_scores_threshold']
    except KeyError:
        close_scores_threshold = espn.CLOSE_SCORES_DEFAULT_THRESHOLD

    groupme_bot = GroupMe(bot_id)
    slack_bot = Slack(slack_webhook_url)
    discord_bot = Discord(discord_webhook_url)

    if swid == '{1}' or espn_s2 == '1':
        league = League(league_id=league_id, year=year)
    else:
        league = League(league_id=league_id, year=year, espn_s2=espn_s2, swid=swid)

    if function == 'get_trade_followups':
        from gamedaybot.espn.trade_followups import followups
        from gamedaybot.espn.community_state import scope, reserve, state
        key = scope(league)
        with state() as db:
            known = {r[0] for r in db.execute('SELECT key FROM notices WHERE scope=?',(key,))}
        reports = [(tid, text) for tid,text in followups(league) if 'followup:'+tid not in known][:3]
        texts = [text for tid,text in reports if reserve(key,'followup:'+tid)]
        if texts:
            discord_bot.teams = league.teams
            discord_bot.send_message('\n\n'.join(texts))
        return
    if function == 'get_pickem_results':
        from gamedaybot.espn.community import pickem
        from gamedaybot.espn.community_state import scope, state, reserve
        with state() as db:
            active = db.execute('SELECT 1 FROM picks WHERE scope=? LIMIT 1',(scope(league),)).fetchone()
        if active and reserve(scope(league),f'pickem-results:{league.scoringPeriodId}'):
            text = pickem(league, [], 'leaderboard', '', None)
            discord_bot.send_message(text)
        return
    if function == 'get_trade_updates':
        poll_trades(league, data, discord_bot)
        return
    if function == 'get_league_updates':
        from gamedaybot.espn.league_updates import poll_league_updates
        poll_league_updates(league, data, discord_bot)
        return

    try:
        broadcast_message = data['broadcast_message']
    except KeyError:
        broadcast_message = None

    # always let init and broadcast run
    if function not in ["init", "broadcast", "win_matrix", "trophy_recap"] and (league.scoringPeriodId > league.finalScoringPeriod or league.scoringPeriodId < league.firstScoringPeriod):
        logger.info("Not in active season")
        return

    text = ''
    logger.info("Function: " + function)

    from gamedaybot.espn import community

    if function == "get_matchups":
        box_scores = espn.fetch_box_scores(league)
        text = espn.get_matchups(league, box_scores=box_scores)
        if text != util.NO_MATCHUP_DATA:
            text = text + "\n\n" + espn.get_projected_scoreboard(league, box_scores=box_scores)
        text += "\n\n" + community.preview(league, box_scores)
    elif function == "get_monitor":
        text = espn.get_monitor(league)
    elif function == "get_scoreboard_short":
        box_scores = espn.fetch_box_scores(league)
        text = espn.get_scoreboard_short(league, box_scores=box_scores)
        if text != util.NO_MATCHUP_DATA:
            text = text + "\n\n" + espn.get_projected_scoreboard(league, box_scores=box_scores)
    elif function == "get_projected_scoreboard":
        text = espn.get_projected_scoreboard(league)
    elif function == "get_close_scores":
        text = community.monday_watch(league, espn.fetch_box_scores(league), community.nfl_games(league))
    elif function == "get_power_rankings":
        text = espn.get_power_rankings(league)
    elif function == "get_trophies":
        text = espn.get_trophies(league)
    elif function == "get_standings":
        text = espn.get_standings(league) + "\n\n" + community.playoff_picture(league, compact=True)
    elif function == "win_matrix":
        text = recap.win_matrix(league)
    elif function == "trophy_recap":
        text = recap.trophy_recap(league)
        # groupme_bot.send_message(text, file_path='/tmp/season_recap.png')
        # slack_bot.send_message(text, file_path='/tmp/season_recap.png')
        # discord_bot.send_message(text, file_path='/tmp/season_recap.png')
    elif function == "get_final":
        # on Tuesday we need to get the scores of last week
        week = league.current_week - 1
        box_scores = espn.fetch_box_scores(league, week=week)
        scores = espn.get_scoreboard_short(league, week=week, box_scores=box_scores)
        if scores == util.NO_MATCHUP_DATA:
            text = scores
        else:
            text = "Final " + scores
            text = text + "\n\n" + community.awards(league, box_scores, week)
    elif function == "get_waiver_report":
        faab = league.settings.faab
        text = espn.get_waiver_report(league, faab)
    elif function == "get_trade_report":
        text = get_trade_report(league, timezone=data['my_timezone'])
    elif function == "broadcast":
        try:
            text = broadcast_message
        except KeyError:
            # do nothing here, empty broadcast message
            pass
    elif function == "init":
        try:
            text = data["init_msg"]
        except KeyError:
            # do nothing here, empty init message
            pass
    else:
        text = "Something bad happened. HALP"

    if util.has_sendable_content(text):
        report_week = league.current_week - 1 if function == 'get_final' else getattr(league, 'current_week', None)
        analysis = generate_analysis(text, function, timezone=data['my_timezone'], week=report_week,
                                     league=league, box_scores=locals().get('box_scores'))
        if analysis:
            text += '\n\n' + analysis
        logger.debug(text)
        messages = util.str_limit_check(text, str_limit)
        for message in messages:
            groupme_bot.send_message(message)
            slack_bot.send_message(message)
        # Discord formats and paginates the complete report as rich embeds.
        discord_bot.teams = getattr(league, 'teams', [])
        if function in ('get_matchups', 'get_scoreboard_short', 'get_final'):
            text = TeamReport(text, discord_bot.teams, matchups=box_scores,
                              final_scores=function == 'get_final', week=report_week)
        discord_bot.send_message(text)


if __name__ == '__main__':
    from gamedaybot.espn.scheduler import scheduler
    from gamedaybot.chat.interactions import start_interactions
    from gamedaybot.espn.model_readiness import start_model_keeper

    start_interactions()
    start_model_keeper()
    espn_bot("init")
    scheduler()
