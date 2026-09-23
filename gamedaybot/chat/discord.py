import requests
import json
import logging
from .discord_format import build_payloads
from .discord_images import prepare_payloads

logger = logging.getLogger(__name__)


class DiscordException(Exception):
    pass


class Discord(object):
    """
    A class used to send messages to a Discord channel through a webhook.

    Parameters
    ----------
    webhook_url : str
        The URL of the Discord webhook to send messages to.

    Attributes
    ----------
    webhook_url : str
        The URL of the Discord webhook to send messages to.

    Methods
    -------
    send_message(text: str)
        Sends a message to the Discord channel.
    """

    def __init__(self, webhook_url):
        self.webhook_url = webhook_url
        self.teams = None

    def __repr__(self):
        return "Discord Webhook Url(%s)" % self.webhook_url

    def send_message(self, text):
        """
        Sends a message to the Discord channel.

        Parameters
        ----------
        text : str
            The message to be sent to the Discord channel.

        Returns
        -------
        r : requests.Response
            The response object of the POST request.

        Raises
        ------
        DiscordException
            If there is an error with the POST request.
        """

        headers = {'content-type': 'application/json'}

        if self.webhook_url in (1, "1", '') or not text or not text.strip():
            return None
        for template, files in prepare_payloads(text, teams=self.teams):
            if files:
                r = requests.post(self.webhook_url, data={'payload_json': json.dumps(template)},
                                  files={f'files[{i}]': (name, data, 'image/png') for i, (name, data) in enumerate(files)},
                                  timeout=30)
            else:
                r = requests.post(self.webhook_url,
                                  data=json.dumps(template), headers=headers, timeout=30)
            if r.status_code not in (200, 204):
                logger.error(r.content)
                raise DiscordException(r.content)
        return r
