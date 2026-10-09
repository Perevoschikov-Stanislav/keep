import dataclasses

import json5
import pydantic
import requests

from keep.contextmanager.contextmanager import ContextManager
from keep.exceptions.provider_exception import ProviderException
from keep.providers.base.base_provider import BaseProvider
from keep.providers.models.provider_config import ProviderConfig


@pydantic.dataclasses.dataclass
class MattermostProviderAuthConfig:
    """Mattermost authentication configuration."""

    webhook_url: pydantic.AnyHttpUrl = dataclasses.field(
        metadata={
            "required": True,
            "description": "Mattermost Webhook Url",
            "sensitive": True,
            "validation": "any_http_url",
        }
    )


class MattermostProvider(BaseProvider):
    """send alert message to Mattermost."""

    PROVIDER_DISPLAY_NAME = "Mattermost"
    PROVIDER_CATEGORY = ["Collaboration"]

    @staticmethod
    def notification_payload(notification, channel_id, addresses):
        """Render already selected fields/actions; action links require confirmation in Keep."""
        fields = [{"title": item["label"], "value": str(item["value"]) if item["value"] is not None else "unknown", "short": True}
                  for item in notification["fields"]]
        links = notification["links"] + [{"label": item["label"], "url": item["keep_url"]}
                                          for item in notification["actions"]]
        attachment = {"title": notification["title"], "text": notification["description"], "fields": fields,
                      "footer": " · ".join(([notification["footer"]] if notification.get("footer") else []) +
                                             [f'[{item["label"]}]({item["url"]})' for item in links])}
        if notification.get("color"):
            attachment["color"] = notification["color"]
        # Addresses are IaC data; the adapter does not infer a role or team from them.
        return {"channel_id": channel_id, "message": " ".join(addresses), "props": {
            "attachments": [attachment], "keep_notification_id": notification["notification_id"],
            "keep_incident_id": notification["incident_id"],
            "keep_projection_revision": notification["projection_revision"]}}

    def __init__(
        self, context_manager: ContextManager, provider_id: str, config: ProviderConfig
    ):
        super().__init__(context_manager, provider_id, config)

    def validate_config(self):
        self.authentication_config = MattermostProviderAuthConfig(
            **self.config.authentication
        )

    def dispose(self):
        """
        No need to dispose of anything, so just do nothing.
        """
        pass

    def _notify(self, message="", attachments=[], channel="", **kwargs: dict):
        """
        Notify alert message to Mattermost using the Mattermost Incoming Webhook API
        https://docs.mattermost.com/developer/webhooks-incoming.html

        Args:
            message (str): The content of the message.
            attachments (list): The attachments of the message.
            channel (str): The channel to send the message
        """
        self.logger.info("Notifying alert message to Mattermost")
        if not message:
            message = attachments[0].get("text")
        webhook_url = self.authentication_config.webhook_url
        payload = {"text": message, **kwargs}

        if channel:
            payload["channel"] = channel

        if attachments:
            try:
                attachments = json5.loads(attachments)
            except Exception:
                pass
            payload["attachments"] = attachments

        response = requests.post(webhook_url, json=payload, verify=False)

        if not response.ok:
            raise ProviderException(
                f"{self.__class__.__name__} failed to notify alert message to Mattermost: {response.text}"
            )

        self.logger.info(
            "Alert message notified to Mattermost", extra={"response": response.text}
        )


if __name__ == "__main__":
    # Output debug messages
    import logging

    logging.basicConfig(level=logging.DEBUG, handlers=[logging.StreamHandler()])
    context_manager = ContextManager(
        tenant_id="singletenant",
        workflow_id="test",
    )
    # Load environment variables
    import os

    mattermost_webhook_url = os.environ.get("MATTERMOST_WEBHOOK_URL")

    # Initalize the provider and provider config
    config = ProviderConfig(
        id="mattermost-test",
        description="Mattermost Output Provider",
        authentication={"webhook_url": mattermost_webhook_url},
    )
    provider = MattermostProvider(
        context_manager, provider_id="mattermost", config=config
    )
    provider.notify(message="Simple alert showing context with name: John Doe")
