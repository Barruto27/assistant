# The adversarial lab

Not part of `python -m unittest discover -s tests`. These run the **real**
classifier and the **real** router against a **copy** of the live database, so
they cost API credits and need `.env`. They live here because a scripted
classifier can prove the plumbing carries an intent and cannot prove the model
picks the right one — and every conversational bug so far has been the second
kind.

```bash
cd /home/assistant/lab            # or anywhere with bot/ and .env alongside
python cases_01.py                # everything in that file
python cases_01.py --only multi   # matching cases only
python cases_01.py --list         # names, no API calls
```

Point it somewhere else with `LAB_SOURCE_DB` and `LAB_WORKING_DB`.

Each case says what it expects and, where it is not obvious, why it exists.
A case that fails prints the whole exchange, so the first thing you see is what
the bot actually said.

- `cases_01.py` — single messages: false confirmations, several instructions in
  one message, ambiguity, dates, noise, email routing.
- `cases_02.py` — multi-turn, untrusted input, destructive requests, questions
  with no answer on file.
- `cases_03.py` — the evening check-in, answered in fragments and corrected.
  Uses `setup()` to stage an outstanding check-in.
- `cases_email.py` — email as third-party text reaching a prompt. Six hostile
  messages plus a genuine one, through the real flagging call and the real
  brief.

Two things to know before trusting a result. Assert on behaviour rather than on
which intent was chosen — several of the early "gaps" were the routing changing
while the reply stayed correct. And a case starts from a copy of real data, so
one task is already `done`: compare against the `before` snapshot rather than
counting rows.
