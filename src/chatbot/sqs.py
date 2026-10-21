"""SQS access. Locally the endpoint is ElasticMQ; in AWS it is real SQS and only the endpoint and credentials change."""

import json

import boto3

from chatbot.config import Settings
from chatbot.models import Event, RedactedText


def make_client(settings: Settings):
    return boto3.client("sqs", endpoint_url=settings.sqs_endpoint_url)


class UnredactedEventError(ValueError):
    """An event whose text is not RedactedText was offered to the queue."""


def _check(event: Event):
    if not isinstance(event.text, RedactedText):
        raise UnredactedEventError("event text must be RedactedText")


class SqsPublisher:
    def __init__(self, client, queue_url: str):
        self.client = client
        self.queue_url = queue_url

    def publish(self, event: Event) -> str:
        _check(event)
        resp = self.client.send_message(
            QueueUrl=self.queue_url,
            MessageBody=event.model_dump_json(),
            MessageAttributes={"trace_id": {"DataType": "String", "StringValue": event.trace_id}},
        )
        return resp["MessageId"]


class InMemoryPublisher:
    """For tests: keeps the JSON bodies it would have sent."""

    def __init__(self):
        self.bodies: list[str] = []
        self.fail = False

    def publish(self, event: Event) -> str:
        _check(event)
        if self.fail:
            raise ConnectionError("queue down")
        self.bodies.append(event.model_dump_json())
        return f"mem-{len(self.bodies)}"

    def events(self):
        return [json.loads(b) for b in self.bodies]


class SqsConsumer:
    """Long-polls the queue. A message is deleted only when the caller says it is finished."""

    def __init__(self, client, queue_url: str, wait_seconds: int = 10):
        self.client = client
        self.queue_url = queue_url
        self.wait_seconds = wait_seconds

    def receive(self, max_messages: int = 1) -> list[tuple[str, Event]]:
        resp = self.client.receive_message(
            QueueUrl=self.queue_url, MaxNumberOfMessages=max_messages, WaitTimeSeconds=self.wait_seconds
        )
        return [(m["ReceiptHandle"], Event.model_validate_json(m["Body"])) for m in resp.get("Messages", [])]

    def delete(self, receipt: str):
        self.client.delete_message(QueueUrl=self.queue_url, ReceiptHandle=receipt)
