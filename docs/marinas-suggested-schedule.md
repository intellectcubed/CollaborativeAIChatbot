Here's a suggested week-by-week plan to the final presentation. It's a suggestion, so if you'd sequence things differently that's fine!

| Due | What to have done |
| :- | :- |
| Tue Oct 20 | Write the rubric, fix the labels, and build the pipeline locally: one command reads redacted messages, proposes calendar edits without writing anything, and scores them against your labels. Add tests for the tricky date cases (overnight shifts, "tomorrow" after midnight, daylight saving etc). |
| Tue Oct 27 | Add a second model call that checks each proposal, and have the bot ask the sender one clarifying question when it isn't confident. Enforce in code that people can only change their own squad. Add tricky and adversarial messages to the test set. |
| Tue Nov 3 | Deploy to AWS with Terraform (probably Lambda on a schedule, DynamoDB, Bedrock) in shadow mode: the bot reads the real chat and sends its proposals only to you. Let the chiefs know before it reads anything live. |
| Tue Nov 10 | Score the shadow-mode proposals against what you actually did on the calendar. Turn on live writes for the safest single action, adding a No Crew gap for your own squad. No new features after this. |
| Tue Nov 17 | README with the results table, decision log tidied, demo video, talk rehearsed. |
| Thu Nov 19 | Final presentation. |
