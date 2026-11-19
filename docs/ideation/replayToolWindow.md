## Replay tool window
I want to redesign the replay tool window. I do like the overall structure. Along the left, going up and down, we have a smaller portion of the window where I can post a message. I can select who it's coming from, I can select the time, and then there's a box for me to type in the message. Send message processor: good.
I don't like that the output window appears and disappears. It appears because it kind of shifts stuff around.
- Below the message processor I want to add the output. Output will be either "message sent to chat" and it'll have the message, or it'll give a message about what action it took: add shift, remove shift, ask things like that.
- Below that I like the traces. It should have a history of traces. The thing is we need to paginate. Right now we have maybe 2. Let's see if we can squeeze 10 in. After 10 let's put pagination: next page, next page, previous, previous, right at the bottom.
- The main panel of this window is where the trace goes. I agree with that because there's a lot of information and it's all relevant. I do like that it disappears with each new request. My problem is that I need to add the ability to also have visibility into the context. When we recently added the feature to save messages, we want to include those messages in subsequent requests to set up the context. Since this is my developer workbench here, I want to be able to control that. I should be able to add to it a message, delete a message, and those messages should be based on time. I need some sort of interface to have visibility and then tweak it as I wish. I'm thinking that's going to need some real estate. Should that be in the main part of the screen where I already have the trace?
One idea that I have is maybe to show just a small summary on the trace page. In the middle screen let's turn it into tabs. The first tab, which is the default, will be Trace, or I can click it. It'll have a second tab. The second tab is the History, Historical Context, I guess. The thinking is I can type a message in the message window and if I hit send, it'll just take the history as is because it's tracked. If I wanted to tweak it, I could hit the tab and then I can add and remove messages, clear them all, and things of that nature.

## Batch messages
On the topic of the UI I also want to add a few more features, the main one being that I have a historical set of evals that I want to be able to run at some point in a batch. These are files that I have locally on my computer.

I'd like to open a file and then have it play those messages. Each file is the archived version of the message from GroupMe so it will have the sender, the date, and all the information we need. It has the message itself. The only thing is there's no chatbot so these were live logs but we can still follow those commands.

I want to be able to load one of those files and say, "Play all messages," and it will just play all of them, updating the calendar until it's finished. Or I can say, "Play from, starting at this date, ending at this date," and it'll scan through those messages and only run those dates.

Additionally outside of those messages, I want to be able to set up my own test cases, which are more comprehensive and longer scenarios. These will be a series of messages that I can put together that will also have the history. The idea is I would clear the whole history, then load this new file that I just created, and let's say it has 20 different messages. Just play those 20 messages. Each step of the way will have tracing so I can look at the traces and I can also compare what it's doing to the calendar.

I need to design all of these features into this UI and I'm thinking maybe tabs are the way to go. Tabs on the large part, the middle, the right pane, basically the largest part of the screen. Each one has a tab. 

## view/edit prompt
I like the behavior of the View and Edit Prompt button. I can go to the Intent or Tier 1. In the future we might have more stages and I can click on that and then I can tweak just that prompt. I can run it, I can ask an LLM for help, and it shows me all the information. Then when I'm happy I can close it.

However the large pop-up is very ugly on the screen and it's very confusing. Maybe that would also become a tab so that it's its own page. That's perhaps not ideal. I like the idea of a pop-up but unless there's a better idea I think I want it to be its own tab.

Right now it sounds like we would have several tabs. The Trace one is the default. I can click into the others depending upon what I want to do. Let me know if you have any other suggestions besides. This should be all of the requirements.

---

## Decisions (finalized)

This is the developer workbench. Production never edits history, never clears it, and is not driven from here.

**Layout**
- Left column, top to bottom: live calendar toggle, composer (From, Sent at, Message, Send), output panel (fixed height, so it never shifts the page), then the trace list: 10 per page with Previous and Next at the bottom.
- Middle column is tabs: **Trace** (default), **History**, **Batch**, **Prompts**. The prompt editor is a tab, not a pop-up. Leaving the Prompts tab with unsaved changes asks Yes (save) / No (discard).

**History tab**
- Stored, not one-off. Edits change the test database's chat history directly; the next message uses whatever is stored.
- Shows every row for a chosen squad, with time, speaker, text and source. Rows can be added, edited and deleted. Bot lines are editable like member lines, so a scenario can put any chatbot reply into the context.
- A preview shows the exact history section the next message from that squad will receive. Full transparency.
- "Clear history" is its own button, separate from "Clear test data" and from loading a scenario.
- All text goes through the PII filter before it is stored.

**Batch tab**
- Loads a scenario file from `test_cases/scenarios/`. Loading a scenario adds its history rows and fills the queue. It does not clear anything.
- Runs touch the calendar when the Live calendar toggle is on.
- Controls: Play all, Run from/to (date range), Run one day, Pause/Resume, Step (one message), Stop.
- Each message waits for its trace to finish before the next is sent. Each row links to its trace.
- Not yet: calendar snapshots and before/after comparison; GroupMe archive import (deferred to last); saving batch results (deferred).

**Scenario file format** (`test_cases/scenarios/<name>.json`)
    {"description": "...", "history": [{"kind", "squad", "speaker", "addressee", "sent_at", "text"}], "messages": [{"member", "sent_at", "text"}]}

`member` is a roster pseudonym (Member-N), never a sender ID, so no real identifiers are committed.
