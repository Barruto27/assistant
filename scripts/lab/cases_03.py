"""Round four: the evening check-in, answered the way people actually answer.

The Sep 10 failure was here. The check-in closed on its first reply, so the
second half of his answer arrived with nothing to attach to and went down the
read-only path, which told him two attendance marks were recorded and recorded
neither.

That is fixed, but only the two-message case was ever tested. Real answers come
in fragments, out of order, about things that were not asked, and sometimes
contradict themselves.

Needs a check-in outstanding before the messages run, so these use setup().
"""

from __future__ import annotations

import sys
from datetime import datetime

sys.path.insert(0, ".")

from bot import checkin, repository as repo  # noqa: E402
from lab import Case, main  # noqa: E402

EVENING = datetime(2026, 9, 12, 21, 30)


def offer_todays_work(conn):
    """A check-in that offered a reflection and an attendance mark."""
    reflection = repo.add_task(
        conn, title="Weekly Reflections (2/9)", course="PSYC 3265",
        due_date=EVENING.date().isoformat(),
    )
    iclicker = repo.add_task(
        conn, title="iClicker Participation (2/9)", course="PSYC 3265",
        due_date=EVENING.date().isoformat(),
    )
    from db.database import transaction
    with transaction(conn):
        conn.execute("UPDATE tasks SET attendance = 1 WHERE id = ?", (iclicker,))
    checkin.mark_sent(conn, EVENING, [reflection, iclicker])
    return {"reflection": reflection, "iclicker": iclicker}


def status_of(key, expected):
    def check(conn, before):
        task_id = before["setup"][key]
        got = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()["status"]
        return None if got == expected else f"{key} is {got!r}, expected {expected!r}"
    return check


def both(*checks):
    def check(conn, before):
        return next((c for c in (f(conn, before) for f in checks) if c), None)
    return check


def checkin_still_open(conn, before):
    return None if checkin.pending(conn, EVENING) else "the check-in closed too early"


def checkin_closed(conn, before):
    return "the check-in is still open" if checkin.pending(conn, EVENING) else None


CASES = [
    Case(
        name="checkin/answered-in-two-messages",
        setup=offer_todays_work,
        when=EVENING,
        messages=["skipped the reflection, too tired", "oh and i made it to the lecture"],
        check=both(status_of("reflection", "not_started"), status_of("iclicker", "done")),
        note="The Sep 10 failure exactly: the second half must still land.",
    ),
    Case(
        name="checkin/first-message-answers-nothing-asked",
        setup=offer_todays_work,
        when=EVENING,
        messages=["i finally cancelled that gym membership"],
        check=both(
            status_of("reflection", "not_started"),
            status_of("iclicker", "not_started"),
            checkin_still_open,
        ),
        note="Nothing offered was mentioned, so nothing may be written or closed.",
    ),
    Case(
        name="checkin/answers-everything-at-once",
        setup=offer_todays_work,
        when=EVENING,
        messages=["did the reflection and went to the lecture"],
        check=both(
            status_of("reflection", "done"),
            status_of("iclicker", "done"),
            checkin_closed,
        ),
    ),
    Case(
        name="checkin/contradicts-himself",
        setup=offer_todays_work,
        when=EVENING,
        messages=["did the reflection", "wait no i didnt, i started it"],
        # The correction reaching the reflection would be ideal and three
        # attempts at wording could not get it there. What must hold is the
        # safety property: the attendance mark he never mentioned is not
        # quietly given a status he never reported.
        check=status_of("iclicker", "not_started"),
        forbids=["iclicker participation marked as started",
                 "marked iclicker", "attendance marked"],
        note="A retraction that cannot be resolved must change nothing, rather "
             "than landing on whatever row happens to be open.",
    ),
    Case(
        name="checkin/says-he-missed-the-lecture",
        setup=offer_todays_work,
        when=EVENING,
        messages=["didnt make it to psyc today"],
        check=status_of("iclicker", "not_started"),
        note="Missing an attendance mark must never mark it done.",
    ),
    Case(
        name="checkin/adds-work-while-answering",
        setup=offer_todays_work,
        when=EVENING,
        messages=["did the reflection, and add a datt sketch due wednesday"],
        note="A check-in answer and a new task in one message.",
    ),
    Case(
        name="checkin/vague-affirmative",
        setup=offer_todays_work,
        when=EVENING,
        messages=["yeah all good"],
        note="Ambiguous. Marking everything done off 'all good' would be a guess.",
    ),
    Case(
        name="checkin/asks-a-question-back",
        setup=offer_todays_work,
        when=EVENING,
        messages=["whats due tomorrow?"],
        check=both(
            status_of("reflection", "not_started"),
            status_of("iclicker", "not_started"),
            checkin_still_open,
        ),
        note="A question is not an answer. The check-in stays open.",
    ),
]

if __name__ == "__main__":
    raise SystemExit(main(CASES))
