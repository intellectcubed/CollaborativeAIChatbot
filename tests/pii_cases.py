"""Seeded synthetic PII cases. Every string here is invented; none is real chat or call data.

LEAKS: (message, strings that must not appear in any filter output). If the filter drops the
message entirely, the case passes. KEEPS: scheduling messages that must get through with the
scheduling content intact. KNOWN_GAPS: leaks the current rules cannot catch (expected failures);
they document what local NER must close.
"""

LEAKS = [
    ("Pt is a 72 yo male with chest pain at 123 Main Street", ["72", "123 Main", "chest"]),
    ("Transported Mary Johnson to Robert Wood hospital, 54 no crew tonight", ["Mary", "Johnson", "Robert Wood"]),
    ("Call at 45 Oak Avenue. 54 is short a crew tonight 1800-0600.", ["45 Oak", "Oak Avenue"]),
    ("Call me at 908-555-1234 re 54 crew tonight", ["908", "555-1234"]),
    ("Email dana@example.com about 34 crew Friday", ["dana@example.com"]),
    ("See https://example.com/run/998877 for tonight's 42 crew", ["example.com", "998877"]),
    ("Incident #482913 was rough. 35 has no crew Sunday 0600-1800", ["482913"]),
    ("run number 4471 done. 43 off Saturday night", ["4471"]),
    ("DOB 04/12/1951. 54 short an EMT Friday", ["04/12/1951", "1951"]),
    ("Patient John Smith went to the ED. 34 no crew tomorrow 2200-0600", ["John", "Smith"]),
    ("Spoke with Dolores Whitfield about the schedule, 54 covers Friday", ["Dolores", "Whitfield"]),
    ("Cardiac arrest on Elm Street, 88 yom. Nothing to do with the schedule", ["Elm", "88"]),
    ("overdose in the park, narcan given", ["overdose", "narcan"]),
    ("Her address is 17 Birch Lane and 42 can cover Sat night", ["17 Birch", "Birch Lane"]),
    ("Thanks Priya Raman, 43 will cover 1800-0000", ["Priya", "Raman"]),
]

KEEPS = [
    ("54 won't have a crew Wednesday 2200-0100", ["54", "Wednesday", "2200-0100"]),
    ("Can 39 take 2nd calls tonight after 0000?", ["tonight", "0000"]),
    ("We now have cover from 18 to midnight just need midnight to 6:00 covered", ["18", "midnight", "6:00"]),
    ("Tango is 35 Saturday 0600-1800, 34 and 42 swap Friday night", ["Tango", "35", "Saturday", "34", "42", "Friday"]),
    ("Somerville needs a driver Sunday night 1800 to 0600", ["Somerville", "Sunday", "1800", "0600"]),
]

KNOWN_GAPS = [
    ("Smith needs a ride Friday, 34 crew is short", ["Smith"]),
    ("dolores says 54 is out friday", ["dolores"]),
    ("54 no crew tonight, we are transporting a patient", ["transporting"]),  # dropped: an over-block, not a leak; kept to track recall
]
