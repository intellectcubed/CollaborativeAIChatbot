# Including context
We want to enhance the system to remember context between calls.  When in a group chat, sometimes messages are atomic, sometimes they are part of an ongoing conversation that takes multiple turns.  This can take the form of an individual finishing a thought in multiple messages.  For example: msg 1: "We are struggling to get a full crew together", then msg2: "Right now, one of my members needs to end his shift at 1am" then msg3: "This is for tonight and tomorrow as well". -- These messages, taken together paint the full picture: treat this as 3 seperate requests for the same thing - so as of now, it means update tonight and tomorrow night to reflect that this squad will not have a crew from 0100 - 0600 (2 calendar calls).  Then they might say (msg4): Tomorrow we're good, so just for tonight.  If we happen to get all of these together, then just make one calendar call.  If, however, we got the first 3, made adjustments, then got the 4th, knowing what took place in the other 3 messages, we'll know that we need to add the crew for tonight back on the calendar.

Sometimes, the LLM might ask a clarifying question, and the user will respond at some point in the future with the answer.  Sometimes squad n member 1 will make a statement and squad n member 2 will continue that statement.

As stated previously, activity is usually most relevant with the hour.  If there was a thread going for the past history. If somebody says something they likely will not continue this on the same thread in 2 hours. They might pick it up again and start a different topic or continue but they would probably repeat some of it. What I meant earlier was XYZ.

Therefore we want to store a history of conversations but perhaps not any longer than for a day. Then when we send a message to the LLM, I want a portion of the message to be the context of the texts that led up to this particular request. Moreover I think it's important to differentiate recent texts versus older texts. If someone said something within the past hour, it should have a greater weight than something that was said 2 hours ago. But I'd like to include the context from up to, let's say, even 3 or 4 hours because it would still be the same day. 

## Implementation
We will have a Postgres database where we will store each request no matter who it's from. Then when we create the LLM call, we'll add a section there for context, I look to you to suggest the best way to implement that. 

Additionally this system is currently running within Docker so I would create a Postgres database within Docker and then create the tables necessary. I might decide to move this to AWS instead of self-hosting and in that case I would use RDS. But for now let's just create a Postgres database. 

The Postgres database I want to create will be either production (which is actual messages) or testing. Essentially there'll be two separate tables or even separate databases. 

## Update the prompts
Update the prompts to include the chat history.  we want to segregate chats into recent more important, and older ones.  initially we'll define up to one hour ago (relative to the timestamp on the message) as more important, then anything from 2-4 hours old to be less important.  

## Database access
When a message is received, save it to the database and read from the database up to 4 hours ago.  Use the timestamp on the message and not the current timestamp. 

When we are in the development (local running in Docker with the replay tool), we will repeat messages.  Therefore, messages can be repeated, and the timestamp is not always going to be the same.  So we will do a vetting of messages before writing them to the database.  Get a Shah hash of the message, look up that hash (not the timestamp) and insert it into the table (or leave that one as is).  We will not do this in production (don't even include the hash in the table).
