"""Conversations: what goes back to the model, and how a thread is named."""

from app import assistant
from app.main import _thread_title


def q(body, failed=False):
    return {"role": "q", "body": body, "failed": failed}


def a(body, failed=False):
    return {"role": "a", "body": body, "failed": failed}


def test_a_follow_up_carries_what_was_already_said():
    """"And at Durban?" only means anything with the question before it."""
    turns = assistant.conversation(
        [q("What sold best in August?"), a("Sugraone, R 143 615,00.")],
        "And at Durban?")
    assert turns == [
        {"role": "user", "content": "What sold best in August?"},
        {"role": "assistant", "content": "Sugraone, R 143 615,00."},
        {"role": "user", "content": "And at Durban?"},
    ]


def test_a_cold_question_is_just_the_question():
    assert assistant.conversation(None, "What is on the floor?") == [
        {"role": "user", "content": "What is on the floor?"}]


def test_an_error_is_not_something_the_assistant_said():
    """A failed turn is the app's message, not the model's. Feeding it back
    invites an apology for something that never happened."""
    turns = assistant.conversation(
        [q("What sold best?"), a("The assistant is busy right now.", failed=True)],
        "Try again")
    assert turns == [{"role": "user", "content": "Try again"}]


def test_two_turns_of_the_same_side_never_go_back_to_back():
    """The API refuses it, and a question that never got an answer is noise."""
    turns = assistant.conversation([q("First"), q("Second"), a("Answer")], "Third")
    assert [t["role"] for t in turns] == ["user", "assistant", "user"]
    assert turns[-1]["content"] == "Third"


def test_a_conversation_that_starts_mid_answer_is_trimmed_to_a_question():
    turns = assistant.conversation([a("An answer with no question above it")], "Now what?")
    assert turns == [{"role": "user", "content": "Now what?"}]


def test_only_the_last_few_turns_travel():
    """The book is what the answer should mostly be read from, not the chat."""
    long = [t for i in range(20) for t in (q(f"q{i}"), a(f"a{i}"))]
    turns = assistant.conversation(long, "and now?")
    assert len(turns) <= assistant.HISTORY_TURNS + 1
    assert turns[-1]["content"] == "and now?"
    assert turns[0]["role"] == "user"


def test_a_very_long_turn_is_cut_before_it_is_sent_back():
    turns = assistant.conversation([q("x" * 9000), a("y" * 9000)], "short one")
    assert all(len(t["content"]) <= assistant.HISTORY_CHARS for t in turns)


def test_a_thread_is_named_after_the_question_that_started_it():
    assert _thread_title("  What sold best  in August? ") == "What sold best in August?"
    long = _thread_title("Which grapes " + "and more " * 30)
    assert len(long) <= 70 and long.endswith("…")
    assert _thread_title("   ") == "New conversation"
