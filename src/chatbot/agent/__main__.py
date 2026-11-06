"""Agent worker entry point."""

import json
import logging
import os
from pathlib import Path

from chatbot.agent.lab import start_if_enabled
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
    start_if_enabled(llm)
    # The replay page shows these; the agent's environment is not visible to it otherwise.
    status = Path(settings.trace_db_path).with_name("agent_status.json")
    status.write_text(json.dumps({"calendar_url": calendar.base_url if calendar else None,
                                  "allow_writes": os.environ.get("CALENDAR_ALLOW_WRITES") == "1"}))
    run_forever(SqsConsumer(make_client(settings), settings.queue_url), TraceStore(settings.trace_db_path),
                llm, model, intent_model, calendar)
