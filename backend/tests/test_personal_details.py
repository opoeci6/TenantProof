"""Issue #60, the pure part: which personal details are found, which placeholder each gets,
and that swapping back gives the exact text again. No database."""

from app.services.personal_details import (
    Placeholders,
    address_variants,
    caseworker_known,
    name_variants,
)

LETTER = (
    "To: Mei Chen, Unit 9, 77 Fictional Lane, Halifax, NS\n"
    "From: Harbour Ltd. (landlord), 1 Placeholder Road, Halifax, NS B3H 1A1\n"
    "Dear Ms. Chen, call 902-555-0187 or write to mei.chen@example.test.\n"
    "Paid $189.99 on 2026-09-25 14:22, receipt 20260925."
)
CHAT = "[2026-08-14 21:10] 陈梅: 房东您好，我家浴室的墙在漏水。"


def case_placeholders(*extra, shown=()) -> Placeholders:
    return Placeholders(
        known=[
            *name_variants("Mei Chen", "TENANT"),
            *address_variants("Unit 9, 77 Fictional Lane, Halifax, NS", "RENTAL"),
            *extra,
        ],
        shown=shown,
    )


def test_names_addresses_and_contact_details_are_replaced() -> None:
    p = case_placeholders()
    hidden = p.hide(LETTER)
    assert hidden == (
        "To: [TENANT_NAME], [RENTAL_ADDRESS], Halifax, NS\n"
        "From: Harbour Ltd. (landlord), [ADDRESS_1], Halifax, NS [POSTAL_CODE_1]\n"
        "Dear Ms. [TENANT_LAST_NAME], call [PHONE_1] or write to [EMAIL_1].\n"
        "Paid $189.99 on 2026-09-25 14:22, receipt 20260925."
    )


def test_dates_amounts_city_and_a_company_landlord_stay() -> None:
    hidden = case_placeholders(*name_variants("Harbour Ltd.", "LANDLORD")).hide(LETTER)
    for kept in ("Harbour Ltd.", "Halifax, NS", "$189.99", "2026-09-25 14:22", "20260925"):
        assert kept in hidden


def test_swapping_back_gives_the_exact_text() -> None:
    p = case_placeholders(caseworker_known("陈梅", "tenant"))
    for text in (LETTER, CHAT, LETTER.upper(), "Mei\nChen wrote"):
        assert p.reveal(p.hide(text)) == text


def test_a_name_in_another_script_is_hidden_once_the_caseworker_adds_it() -> None:
    assert case_placeholders().hide(CHAT) == CHAT  # the finder cannot know 陈梅 is Mei Chen
    p = case_placeholders(caseworker_known("陈梅", "tenant", "pt_1"))
    assert p.hide(CHAT) == "[2026-08-14 21:10] [TENANT_NAME_2]: 房东您好，我家浴室的墙在漏水。"
    entry = next(e for e in p.entries() if e.value == "陈梅")
    assert (entry.source, entry.term_id) == ("caseworker", "pt_1")


def test_a_full_name_matches_in_any_case_and_a_first_name_only_as_written() -> None:
    p = case_placeholders()
    assert p.hide("MEI CHEN and mei chen") == "[TENANT_NAME_2] and [TENANT_NAME_3]"
    assert p.hide("Hi Mei, said the mei") == "Hi [TENANT_FIRST_NAME], said the mei"
    assert p.hide("Mei Chen您好") == "[TENANT_NAME]您好"  # no space before Chinese text
    assert p.hide("Meiling Chenoweth") == "Meiling Chenoweth"  # whole words only


def test_a_name_that_is_also_a_word_is_matched_only_in_full() -> None:
    p = Placeholders(known=name_variants("Will May", "PERSON"))
    assert p.hide("Will May said it will rain in May") == "[PERSON_NAME] said it will rain in May"


def test_each_spelling_keeps_its_placeholder_and_numbers_follow_first_sight() -> None:
    p = case_placeholders()
    first = p.hide("call 902-555-0187, then 902-555-0199")
    again = p.hide("902-555-0199 or 902-555-0187")
    assert first == "call [PHONE_1], then [PHONE_2]"
    assert again == "[PHONE_2] or [PHONE_1]"


def test_id_and_card_numbers_need_a_valid_check_digit() -> None:
    p = Placeholders()
    assert p.hide("SIN 046 454 286") == "SIN [ID_NUMBER_1]"
    assert p.hide("SIN 046 454 287") == "SIN 046 454 287"
    assert p.hide("card 4111 1111 1111 1111") == "card [CARD_NUMBER_1]"
    assert p.hide("card 4111 1111 1111 1112") == "card 4111 1111 1111 1112"


def test_ids_and_nonces_are_never_touched() -> None:
    text = 'evidence_id="ev_4111111111111111" tag="evidence-0123456789abcdef" E-01 page 1'
    assert Placeholders(known=name_variants("Mei Chen", "TENANT")).hide(text) == text


def test_a_value_the_caseworker_lets_through_is_sent_as_written() -> None:
    p = case_placeholders(shown=["  mei   CHEN "])
    assert p.hide("To: Mei Chen") == "To: Mei Chen"  # and the parts inside it stay too
    assert p.hide("Dear Ms. Chen") == "Dear Ms. [TENANT_LAST_NAME]"


def test_a_placeholder_the_model_made_up_stays_as_written() -> None:
    p = case_placeholders()
    p.hide(LETTER)
    assert p.reveal("[TENANT_NAME] met [PERSON_7]") == "Mei Chen met [PERSON_7]"


def test_the_list_counts_occurrences_and_skips_pieces_never_seen() -> None:
    p = case_placeholders()
    p.hide(LETTER, count=True)
    listed = {e.placeholder: e for e in p.entries()}
    assert listed["[TENANT_NAME]"].count == 1
    assert listed["[TENANT_LAST_NAME]"].count == 1  # "Ms. Chen"
    assert "[TENANT_FIRST_NAME]" not in listed  # "Mei" never stood alone
    assert listed["[PHONE_1]"].source == "found"


def test_an_address_without_a_street_number_is_not_an_address_to_hide() -> None:
    assert address_variants("Halifax, NS", "RENTAL") == []
    assert [k.value for k in address_variants("100 Example Street, Halifax, NS B3H 1A1",
                                              "RENTAL")] == ["100 Example Street"]


def test_a_company_name_is_not_personal_data() -> None:
    assert name_variants("Sample Property Management Ltd.", "LANDLORD") == []
    assert [k.value for k in name_variants("Dr. Priya Nair", "PERSON")] == [
        "Dr. Priya Nair", "Priya", "Nair",
    ]


def test_dates_next_to_a_capitalised_word_are_not_addresses() -> None:
    p = Placeholders()
    for text in (
        "On 15 May Dr. Patel saw the mould.",
        "Hearing set for 3 December Court Room 2.",
        "Seen on 2 Sept. Road works outside.",
    ):
        assert p.hide(text) == text
    assert p.hide("Moved to 12 Oak Drive on 15 May.") == "Moved to [ADDRESS_1] on 15 May."


def test_two_iso_dates_side_by_side_are_not_a_card_number() -> None:
    p = Placeholders()
    for start, end in (("2026-08-14", "2026-08-18"), ("2026-01-01", "2026-12-31")):
        text = f"Lease period: {start} {end} signed"
        assert p.hide(text) == text
    assert p.hide("card 4111 1111 1111 1111") == "card [CARD_NUMBER_1]"


def test_phone_numbers_without_separators_or_with_a_country_code_are_hidden() -> None:
    p = Placeholders()
    assert p.hide("call me 9025550187") == "call me [PHONE_1]"
    assert p.hide("or 19025550188.") == "or [PHONE_2]."
    assert p.hide("WeChat +86 138 0013 8000 after 6") == "WeChat [PHONE_3] after 6"
    assert p.hide("Call +1 902 555 0189") == "Call [PHONE_4]"
    for text in ("Rent 1200 paid", "receipt 20260925", "at 1732665600", "order 20260801123"):
        assert p.hide(text) == text
