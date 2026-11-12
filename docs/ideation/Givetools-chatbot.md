# Give tools to the chatbot
I have a server that manipulates an actual calendar that is shared among the Collaborative.  it has an http interface and supports the following operations: 

- GET {{base_url}}/?action=get_schedule_day&date={{shift_timestamp}}
- Response:
```json
{
  "success": true,
  "action": "get_schedule_day",
  "date": "20261102",
  "day_schedule": "{\"day\": \"Monday 2026-11-02\", \"shifts\": [{\"name\": \"18:00 - 00:00 Shift\", \"start_time\": \"18:00\", \"end_time\": \"00:00\", \"segments\": [{\"start_time\": \"18:00\", \"end_time\": \"00:00\", \"squads\": [{\"id\": 42, \"territories\": [35, 42, 54], \"active\": true}, {\"id\": 43, \"territories\": [34, 43], \"active\": true}]}], \"tango\": 42}, {\"name\": \"00:00 - 06:00 Shift\", \"start_time\": \"00:00\", \"end_time\": \"06:00\", \"segments\": [{\"start_time\": \"00:00\", \"end_time\": \"06:00\", \"squads\": [{\"id\": 42, \"territories\": [34, 35, 42, 43, 54], \"active\": true}]}], \"tango\": 42}]}"
}
```

- GET {{base_url}}/?action=addShift&date={{shift_timestamp}}&shift_start=0600&shift_end=1800&squad=43&preview=False
- Response:
```json
{
  "success": true,
  "changeId": "74a77007-e0e5-41db-ac6d-df6b1a977b6f",
  "action": "addShift",
  "date": "20261101"
}
```
- GET {{base_url}}/?action=noCrew&date={{shift_timestamp}}&shift_start=2300&shift_end=0000&squad=43&preview=False
Response:
```shell
{
  "success": true,
  "changeId": "e511e114-b025-4539-b5e4-f2dacfab72fc",
  "action": "noCrew",
  "date": "20261102"
}
```

I want to enhance the chatbot to allow it to make changes to the calendar.  Note: calling these APIs causes backups - each modifying call returns a change_id - there is a rollback that will undo the change it made.  Should we write an MCP server?  Also, how do we incorporate it?

## Verify before continuing
If the message interpreted indicates a schedule change, the LLM should first call get_schedule_day and interpret the response to validate if a crew says they will not have a crew for a date/time, they were actually scheduled.

**Question** Suppose we get a request to designate no-crew from 1700 - 1800 on a given date.  I need to check to make sure the crew was actually scheduled during at least part of that shift (if it is on the calendar from 1900 - 0600 for example, then the request is meaningless).  It seems that this is very deterministic - we have hard numbers against we can check.  However, the logic might not be so simple.  To solve this problem, should we write computer logic to figure this out, or send the schedule and the request to the LLM and ask the LLM to figure it out?

## Multiple changes
Note: a message/conversation might result in several actions.  Example: "Swich 43 from Tuesday night to Wednesday night for the next three weeks"

## Apply changes
If, after determining the actions to take, the system can implement the changes by calling the tools.

**Question** I want to be able to finetune at which confidence level it makes changes.  How should this be configured?

---
## Future enhancements
If the System makes a mistake, a human will correct it by sending a command to undo (use the rollback function).  Such cases should also be used for the system to self heal
