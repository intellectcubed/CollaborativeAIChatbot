"""Agent worker entry point."""

import logging

from chatbot.agent.worker import make_calendar, make_llm, run_forever
from chatbot.config import Settings
from chatbot.policy import load_policy
from chatbot.sqs import SqsConsumer, make_client
from chatbot.tracing.store import TraceStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

if __name__ == "__main__":
    settings = Settings.from_env()
    llm, model, intent_model = make_llm()
    calendar = make_calendar()
    log = logging.getLogger("agent")
    log.info("agent started, model=%s, intent=%s, polling %s", model, intent_model or "off", settings.queue_url)
    log.info("calendar=%s, mode=%s", calendar.base_url if calendar else "off", load_policy().effective_mode)
    run_forever(SqsConsumer(make_client(settings), settings.queue_url), TraceStore(settings.trace_db_path),
                llm, model, intent_model, calendar)
