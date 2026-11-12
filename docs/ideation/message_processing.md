hold this thought, I want to ask a question about this.  

I want to clarify some behavior I have planned to add.  When a message comes from the webhook, the first thing is to look at who it is from, determine which squad they represent.  Then, go to the database, get some recent messages relating to that squad and include them in the context of what will be sent for interpretation later.  

So we will probably save each message, so that I always have a window of context to send to the LLM.  I wouldn't use anthing older than a few minutes.  The thinking is if there is a multi step conversation, it will probably not last more than 30 minutes.

## Calling LLMs
### Return schema
actions: ['add crew', 'get shift info', 'no crew', 'obliterate crew', 'assign tango']
```json
{
  "trace_id": "…",
  "actions": [
    {"action": "remove_crew", "squad": 54, "date": "2025-03-05",
     "start": "2200", "end": "0100", "confidence": 0.95, "question": ""}
  ],
  "reasoning": "Squad 54 cannot staff part of Wednesday night.",
  "meta": {
    "model": "claude-haiku-5-5", "prompt_version": "7a61fb24",
    "input_tokens": 1136, "output_tokens": 167, "cost_usd": 0.000197,
    "latency_ms": 2840, "request_id": "req_…"
  }
}
```

**Notes**
The model is asked for actions and reasoning. Everything under meta is added by our code, and one result can sum the intent and Tier 1 calls with a per-call breakdown.

#### What changes in the code

prompts/tier1/schema.json and the Command model change field names and add confidence.
The prompt changes: the overnight rule, whole-shift handling and the clarify guidance.
The rendering code maps remove_crew to the “Call NoCrew on calendar…” text.
The meta block comes from the proxy, so it belongs with that work.



Here is how I see the processing of a message going: 
Is this a duplicate message? Yes - stop
Is this an answer to something that I previously asked for clarification?  

### Example 1: Conversation:

Here is an example. Assume we have (US) and member 1 from squad 4 (4.1) and member 2 from squad 4 (4.2):
(4.1): "I won't have a crew tomorrow".  
(US): "Do you mean for the whole shift, or just part?".  
(4.1) "I don't have the times yet but there will be a gap", 
(US): "Ok, let us know when you can".  (20 minutes later) 
(4.2): I won't be there for the first hour
(US): So only the first hour of the shift.  Morning or evening?
(4.2): Evening
LLM will assume that for tomorrow night the first hour of the evening shift will have no crew for squad 4



## Example 2
If I get a message like this: (from member of 34) "34 has no crew from 1800 - 0100 tomorrow night
- intent: yes
- Stand alone message send to LLM, get back json
