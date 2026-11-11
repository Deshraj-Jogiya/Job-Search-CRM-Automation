"""cover_letter_service.quality_issues: a letter has to read like a letter, not a "you ask X, I did X" list."""

from app.services import cover_letter_service as cls

SHORT = (
    "I am applying for the Analytics Engineer role. The posting asks for large-scale ETL and strong data modeling.\n\n"
    "At Zifatech I modernized Star Schema models in Snowflake and accelerated ingestion by 60%.\n\n"
    "I would welcome an interview to discuss how I can contribute to Reddit."
)

GOOD = (
    "Reddit's communities generate a firehose of votes, comments and moderation events, and every product team "
    "decides what to build from tables an analytics engineer shaped. That is why the Analytics Engineer role at "
    "Reddit stands out to me. I build the models and tests that let people trust a number without asking where "
    "it came from.\n\n"
    "At Zifatech the sales team had stopped trusting its weekly report: totals shifted between runs and nobody "
    "could say why. I traced the drift to star schema models in Snowflake that loaded late-arriving rows twice, "
    "rebuilt the models with explicit grain and keys, and added checks that fail the load instead of publishing a "
    "bad total. Reliability reached the 98% standard the team had set, ingestion ran 60% faster, and the "
    "reporting work I automated cut manual effort by 70%.\n\n"
    "Two other pieces of work map to what this team will need. My dbt project runs 22 data tests on every change "
    "in CI, so a broken model never reaches a dashboard. At Objectways I process large sensor datasets in Python "
    "and PySpark and cut processing time by 30%, which is the scale and care Reddit's event data asks for. Both "
    "habits came from seeing what happens when a report is wrong: people quietly stop using it.\n\n"
    "I would bring Reddit's data team a habit of testing a model before anyone reports on it, and of explaining "
    "the numbers in plain words. I would welcome an interview to talk about the team's modeling work."
)


def test_short_template_letter_is_flagged_for_every_weakness():
    issues = " | ".join(cls.quality_issues(SHORT, "Reddit, Inc."))
    assert "too short" in issues
    assert "paragraphs" in issues
    assert '"i am applying for"' in issues and '"the posting asks"' in issues
    assert "no story paragraph" in issues
    assert "names Reddit, Inc. 1 time" in issues


def test_full_letter_passes():
    assert cls.quality_issues(GOOD, "Reddit, Inc.") == []


def test_company_name_variants_count():
    names = cls._company_names("Amazon Web Services (AWS)")
    assert {"AWS", "Amazon Web Services"} <= set(names)
    assert "Johns Hopkins University" in cls._company_names("The Johns Hopkins University")


def test_last_paragraph_must_ask():
    no_ask = GOOD.replace("I would welcome an interview to talk about the team's modeling work.",
                          "Thank you for reading.")
    assert any("does not ask" in i for i in cls.quality_issues(no_ask, "Reddit"))


def test_greeting_and_sign_off_are_not_counted_as_paragraphs():
    framed = "Dear Hiring Manager,\n\n" + GOOD + "\n\nSincerely,\nDeshraj Jogiya"
    assert cls.quality_issues(framed, "Reddit") == []


def test_legal_names_match_the_brand_a_letter_uses():
    assert "Amazon" in cls._company_names("Amazon.com Services LLC")
    assert "CHAOS" in cls._company_names("CHAOS Industries")


def test_one_mention_of_a_long_name_counts_once():
    once = GOOD.replace("Reddit's", "Acme's").replace("Reddit", "Acme")
    once = once.replace("Acme", "Amazon Web Services", 1).replace("Acme", "the company")
    assert any("names Amazon Web Services (AWS) 1 time" in i for i in cls.quality_issues(once, "Amazon Web Services (AWS)"))
