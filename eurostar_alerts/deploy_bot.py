"""Provision/update Twilio controls using existing secrets; never print credentials."""
from __future__ import annotations
import json
import os
import time
import uuid
from pathlib import Path
from xml.etree import ElementTree

import requests
from twilio.base.exceptions import TwilioRestException
from twilio.request_validator import RequestValidator

from .config import load_config
from .control import seed_controls
from .notifier import twilio_client

METADATA = Path("data/twilio-control.json")


def find_or_create(resources, match, **kwargs):
    existing = next((item for item in resources.stream() if match(item)), None)
    return existing if existing is not None else resources.create(**kwargs)


def verify_webhook(url, token, owner, bot):
    payload = {"From": owner, "To": bot, "Body": "TEST", "MessageSid": "SM" + uuid.uuid4().hex}
    signature = RequestValidator(token).compute_signature(url, payload)
    response = requests.post(url, data=payload, headers={"X-Twilio-Signature": signature}, timeout=20)
    response.raise_for_status()
    replies = [node.text or "" for node in ElementTree.fromstring(response.text).iter("Message")]
    if not any("Eurostar bot is working" in reply for reply in replies):
        raise RuntimeError("Signed webhook test did not return the expected TEST response")
    response = requests.post(url, data=payload, timeout=20)
    if response.status_code not in {401, 403}:
        raise RuntimeError("Webhook did not reject an unsigned request")
    payload["From"] = "whatsapp:+10000000000"
    signature = RequestValidator(token).compute_signature(url, payload)
    response = requests.post(url, data=payload, headers={"X-Twilio-Signature": signature}, timeout=20)
    response.raise_for_status()
    if list(ElementTree.fromstring(response.text).iter("Message")):
        raise RuntimeError("Webhook replied to an unauthorized phone")
    print("Webhook checks passed: signed TEST, signature enforcement, owner restriction.")


def main():
    config = load_config("config.yml")
    client, bot, owner = twilio_client(config.notification)
    sync = find_or_create(
        client.sync.v1.services, lambda item: item.friendly_name == "Eurostar phone controls",
        friendly_name="Eurostar phone controls",
    )
    document = client.sync.v1.services(sync.sid).documents("controls")
    try:
        document.fetch()
    except TwilioRestException as exc:
        if exc.status != 404:
            raise
        client.sync.v1.services(sync.sid).documents.create(unique_name="controls", data=seed_controls(config))
    service = find_or_create(
        client.serverless.v1.services, lambda item: item.unique_name == "eurostar-phone-controls",
        unique_name="eurostar-phone-controls", friendly_name="Eurostar phone controls",
        include_credentials=True,
    )
    service_context = client.serverless.v1.services(service.sid)
    environment = find_or_create(
        service_context.environments, lambda item: item.unique_name == "production",
        unique_name="production", domain_suffix="prod",
    )
    environment_context = service_context.environments(environment.sid)
    variables = {item.key: item for item in environment_context.variables.stream()}
    for key, value in {"SYNC_SERVICE_SID": sync.sid, "OWNER_WHATSAPP": owner, "BOT_WHATSAPP": bot}.items():
        if key in variables:
            environment_context.variables(variables[key].sid).update(value=value)
        else:
            environment_context.variables.create(key=key, value=value)
    function = find_or_create(
        service_context.functions, lambda item: item.friendly_name == "WhatsApp commands",
        friendly_name="WhatsApp commands",
    )
    token = os.environ[config.notification.twilio_auth_token_env]
    with Path("twilio/commands.js").open("rb") as source:
        response = requests.post(
            f"https://serverless-upload.twilio.com/v1/Services/{service.sid}/Functions/{function.sid}/Versions",
            auth=(os.environ[config.notification.twilio_account_sid_env], token),
            data={"Path": "/whatsapp", "Visibility": "protected"},
            files={"Content": ("commands.js", source, "application/javascript")}, timeout=60,
        )
    if not response.ok:
        raise RuntimeError(f"Function upload failed with HTTP {response.status_code}")
    build = service_context.builds.create(function_versions=[response.json()["sid"]], runtime="node22")
    deadline = time.monotonic() + 300
    while build.status == "building" and time.monotonic() < deadline:
        time.sleep(5)
        build = service_context.builds(build.sid).fetch()
    if build.status != "completed":
        raise RuntimeError(f"Twilio build did not complete: {build.status}")
    environment_context.deployments.create(build_sid=build.sid)
    url = f"https://{environment.domain_name}/whatsapp"
    time.sleep(5)
    verify_webhook(url, token, owner, bot)
    metadata = {"sync_service_sid": sync.sid, "serverless_service_sid": service.sid, "webhook_url": url}
    METADATA.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    print(f"Webhook deployed: {url}")
    print("Set this URL as the WhatsApp incoming-message webhook (HTTP POST) in Twilio Console.")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as output:
            output.write(f"Bot deployed and webhook tests passed.\n\nIncoming-message webhook (POST): {url}\n")


if __name__ == "__main__":
    main()
