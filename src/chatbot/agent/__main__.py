"""Agent worker entry point."""

import logging

from chatbot.agent.worker import make_llm, run_forever
from chatbot.config import Settings
from chatbot.sqs import SqsConsumer, make_client
from chatbot.tracing.store import TraceStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

if __name__ == "__main__":
    settings = Settings.from_env()
    llm, model = make_llm()
    logging.getLogger("agent").info("agent started, model=%s, polling %s", model, settings.queue_url)
    run_forever(SqsConsumer(make_client(settings), settings.queue_url), TraceStore(settings.trace_db_path), llm, model)
