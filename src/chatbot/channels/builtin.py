import logging

from chatbot.channels import OutboundMessage, SendResult

log = logging.getLogger("channels")


class ReplayChannel:
    """Replies to the replay tool. The outbox row is the delivery: the replay page reads it."""

    name = "replay"

    def send(self, msg: OutboundMessage) -> SendResult:
        return SendResult("sent")


class LogChannel:
    """Delivers nowhere. The reply is still in the outbox, so you can see what would have been said."""

    name = "log"

    def send(self, msg: OutboundMessage) -> SendResult:
        log.info("trace=%s reply (%s) not delivered: channel is log", msg.trace_id, msg.kind)
        return SendResult("skipped")
