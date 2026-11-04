from chatbot.agent.stages.apply import Apply
from chatbot.agent.stages.dedupe import DedupeCheck
from chatbot.agent.stages.gate import Gate
from chatbot.agent.stages.intent_check import IntentCheck
from chatbot.agent.stages.interpret import Interpret
from chatbot.agent.stages.receive import Receive
from chatbot.agent.stages.reply import Reply
from chatbot.agent.stages.respond import Respond
from chatbot.agent.stages.validate import Validate


def default_stages():
    return [DedupeCheck(), Receive(), IntentCheck(), Interpret(), Validate(), Gate(), Apply(), Respond(), Reply()]
