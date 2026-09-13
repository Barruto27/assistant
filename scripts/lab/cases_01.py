"""Round one: the ways a real conversation goes wrong.

Grouped by the kind of failure rather than by feature, because the failures
that have actually happened cut across features - a false confirmation came out
of the query path, a dropped instruction out of the classifier, a wrong row out
of the check-in.
"""

from lab import Case, main


def unchanged(table):
    """Nothing new in that table."""
    def check(conn, before):
        n = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        return None if n == before[table] else f"{table}: {n - before[table]:+d} row(s)"
    return check


def added(table, count):
    def check(conn, before):
        n = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        got = n - before[table]
        return None if got == count else f"expected {count} new {table}, got {got}"
    return check


def nothing_newly_done(conn, before):
    """No task finished during this case.

    Compared against the ids already done, because the lab runs on a copy of
    the real database and one attendance mark is legitimately finished in it.
    """
    now_done = {
        row["id"] for row in conn.execute("SELECT id FROM tasks WHERE status = 'done'")
    }
    newly = now_done - before["done_ids"]
    return f"marked task(s) {sorted(newly)} done" if newly else None


no_new_tasks = unchanged("tasks")
no_reminders = unchanged("reminders")
nothing_done = nothing_newly_done
reminders = lambda count: added("reminders", count)  # noqa: E731


CASES = [
    # ---- claiming things it did not do -----------------------------------
    Case(
        name="claim/reports-doing-something-vague",
        messages=["did some work on the essay earlier"],
        forbids=["marked", "recorded", "logged", "saved it", "noted it as done"],
        check=nothing_done,
        note="The Sep 10 bug: a report of activity came back as a confirmed write.",
    ),
    Case(
        name="claim/asks-about-a-course-it-has-never-heard-of",
        messages=["whats due for NATS 1505"],
        wants=["nats"],
        forbids=["due Monday", "due Tuesday", "assignment 1"],
        check=no_new_tasks,
        note="It must say it has no such course rather than improvise one.",
    ),
    Case(
        name="claim/asks-for-a-weight-it-was-never-told",
        messages=["how much is the DATT final worth"],
        note="A confident wrong percentage is worse than no answer.",
    ),
    Case(
        name="claim/invented-past-conversation",
        messages=["like I told you yesterday, the psyc test moved to the 30th"],
        note="It has no memory of yesterday. It must not pretend it does.",
    ),

    # ---- several things in one message ------------------------------------
    Case(
        name="multi/three-reminders",
        messages=["remind me at 8am, noon and 6pm tomorrow to drink water"],
        check=reminders(3),
        note="Three times, three reminders.",
    ),
    Case(
        name="multi/task-plus-question",
        messages=["add the psyc essay for friday, and whats already due that week"],
        note="A write and a read in one message; both halves must happen.",
    ),
    Case(
        name="multi/contradictory-halves",
        messages=["remind me at 5pm to email the TA, actually make it 6pm"],
        check=reminders(1),
        note="He corrected himself mid-sentence. One reminder, at six.",
    ),
    Case(
        name="multi/five-things",
        messages=[
            "add a psyc reading for monday, a datt sketch for tuesday, remind me "
            "tonight at 9 to start the reading, set a daily goal to stop "
            "doomscrolling, and tell me whats due this week"
        ],
        note="Four writes and a read. The cap is eight, so nothing should drop.",
    ),

    # ---- ambiguity that must not be guessed --------------------------------
    Case(
        name="ambiguous/which-course",
        messages=["mark the reflection done"],
        note="PSYC has reflections; if anything else matches it must ask.",
    ),
    Case(
        name="ambiguous/bare-pronoun",
        messages=["push it back a week"],
        forbids=["done"],
        note="No antecedent at all. Must ask, not pick something.",
    ),
    Case(
        name="ambiguous/relative-date-with-no-anchor",
        messages=["remind me the day before it's due"],
        note="Before what is due? Unanswerable without a referent.",
    ),

    # ---- times and dates ---------------------------------------------------
    Case(
        name="time/in-twenty-minutes",
        messages=["remind me in 20 minutes to check the oven"],
        check=reminders(1),
    ),
    Case(
        name="time/next-friday-is-not-this-friday",
        messages=["essay due next friday for CMDS"],
        note="Off-by-a-week here silently sets the wrong deadline.",
    ),
    Case(
        name="time/a-time-that-already-passed-today",
        messages=["remind me at 9am to call the registrar"],
        note="It is nearly midnight. 9am today is gone; it should mean tomorrow.",
    ),
    Case(
        name="time/end-of-month",
        messages=["remind me on the last day of the month to check my grades"],
    ),

    # ---- things that should not become tasks -------------------------------
    Case(
        name="noise/pure-small-talk",
        messages=["lol"],
        check=no_new_tasks,
    ),
    Case(
        name="noise/venting",
        messages=["im so behind on everything and its week one, this is bad"],
        check=no_new_tasks,
        note="Reassurance is not a task. Nor is it a reason to invent data.",
    ),
    Case(
        name="noise/a-question-shaped-like-a-task",
        messages=["should I start the soundscape assignment tonight?"],
        check=no_new_tasks,
        note="Asking whether to do a thing is not asking to record it.",
    ),
    Case(
        name="noise/empty-ish",
        messages=["?"],
        check=no_new_tasks,
    ),

    # ---- email -------------------------------------------------------------
    Case(
        name="email/asked-plainly",
        messages=["any emails i should know about"],
        intents=["check_email"],
        note="Went to just_chat for days and always answered that it had none.",
    ),
    Case(
        name="email/asked-sideways",
        messages=["did my prof send anything about the quiz"],
        intents=["check_email"],
    ),
    Case(
        name="email/not-really-about-email",
        messages=["remind me to email my prof about the quiz"],
        forbids=["which course", "E103"],
        wants=["remind"],
        note="The word email is in it, but this is a reminder. Asserted on "
             "behaviour, not routing: asking about the time is right whether "
             "it comes from add_reminder or ask_clarification.",
    ),

    # ---- follow-ups --------------------------------------------------------
    Case(
        name="followup/answers-its-question",
        messages=["mark the iclicker done", "the cmds one"],
        note="It asks which course; the one-word answer must land.",
    ),
    Case(
        name="followup/changes-his-mind",
        messages=["add a task to read chapter 3", "actually forget that"],
        note="Retraction. At minimum it must not silently keep the task.",
    ),
]

if __name__ == "__main__":
    raise SystemExit(main(CASES))
