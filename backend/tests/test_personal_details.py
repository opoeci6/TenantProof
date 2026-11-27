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
    # A street named after a month is still an address when the number can't be a day.
    assert p.hide("123 March Road") == "[ADDRESS_2]"
    assert p.hide("1200 August Drive") == "[ADDRESS_3]"


def test_two_iso_dates_side_by_side_are_not_a_card_number() -> None:
    p = Placeholders()
    for start, end in (("2026-08-14", "2026-08-18"), ("2026-01-01", "2026-12-31"),
                       ("14-08-2026", "18-08-2026")):  # fmt: skip
        text = f"Lease period: {start} {end} signed"
        assert p.hide(text) == text
    assert p.hide("card 4111 1111 1111 1111") == "card [CARD_NUMBER_1]"


def test_phone_numbers_without_separators_or_with_a_country_code_are_hidden() -> None:
    p = Placeholders()
    assert p.hide("call me 9025550187") == "call me [PHONE_1]"
    assert p.hide("or 19025550188.") == "or [PHONE_2]."
    assert p.hide("WeChat +86 138 0013 8000 after 6") == "WeChat [PHONE_3] after 6"
    assert p.hide("Call +1 902 555 0189") == "Call [PHONE_4]"
    for text in ("Rent 1200 paid", "receipt 20260925", "at 1732665600", "order 20260801123",
                 "Credit +850.00 2026-09-01"):  # fmt: skip
        assert p.hide(text) == text
    # A phone number never runs on into the next line's address.
    assert p.hide("Tel +44 20 7946 0958\n12 Oak Drive, Halifax") == (
        "Tel [PHONE_5]\n[ADDRESS_1], Halifax"
    )


# --- #166: the caseworker hides a value the case already hides


def listed(p: Placeholders) -> list[tuple[str, str, str, int, str | None]]:
    return [(e.value, e.placeholder, e.source, e.count, e.term_id) for e in p.entries()]


def test_a_term_for_a_known_value_is_listed_on_it_and_hides_exactly_as_before() -> None:
    plain = case_placeholders()
    terms = [caseworker_known("mei  chen", "person", "pt_full"),  # the full name, as typed
             caseworker_known("Chen", "tenant", "pt_last"),  # the last name: "Ms. Chen"
             caseworker_known("Mei", "tenant", "pt_first")]  # never on its own  # fmt: skip
    p = case_placeholders(*terms)
    assert p.hide(LETTER, count=True) == plain.hide(LETTER, count=True)
    assert p.hide(CHAT) == plain.hide(CHAT)
    without = {e[0]: e for e in listed(plain)}
    rows = {e[0]: e for e in listed(p)}
    assert rows["Mei Chen"] == ("Mei Chen", "[TENANT_NAME]", "caseworker", 1, "pt_full")
    assert rows["Chen"] == ("Chen", "[TENANT_LAST_NAME]", "caseworker", 1, "pt_last")
    assert rows["Mei"] == ("Mei", "[TENANT_FIRST_NAME]", "caseworker", 0, "pt_first")
    assert {v: e[1] for v, e in rows.items() if v in without} == {
        v: e[1] for v, e in without.items()}  # no placeholder moved


def test_an_unseen_piece_shows_no_number_that_later_text_could_change() -> None:
    # Two first names share the stem: whichever is read first gets [TENANT_FIRST_NAME].
    p = Placeholders(known=[*name_variants("Mei Chen", "TENANT"),
                            *name_variants("Meiling Chen", "TENANT"),
                            caseworker_known("Mei", "tenant", "pt_1")])  # fmt: skip
    p.hide(LETTER, count=True)
    assert ("Mei", "", "caseworker", 0, "pt_1") in listed(p)
    # An address piece matches in any case, so another spelling could be read first.
    q = case_placeholders(caseworker_known("UNIT 9", "address", "pt_2"))
    assert q.hide("Mei Chen, Unit 9, 77 Fictional Lane") == "[TENANT_NAME], [RENTAL_ADDRESS]"
    assert ("Unit 9", "", "caseworker", 0, "pt_2") in listed(q)


def test_a_merged_term_matches_exactly_as_the_case_value_does() -> None:
    # Known limit, kept on purpose (#166): the case's first name matches only as written, and a
    # term merged onto it does too, so a placeholder number never moves. Not less hidden than
    # without the term; a wider match is a separate decision.
    text = "Mei: hi. MEI CHEN signed. mei said ok. Ms. Chen and ms. chen."
    plain = case_placeholders()
    p = case_placeholders(caseworker_known("mei", "tenant", "pt_1"),
                          caseworker_known("CHEN", "tenant", "pt_2"))  # fmt: skip
    assert p.hide(text, count=True) == plain.hide(text, count=True) == (
        "[TENANT_FIRST_NAME]: hi. [TENANT_NAME_2] signed. mei said ok. "
        "Ms. [TENANT_LAST_NAME] and ms. chen."
    )
    # Without the terms (after Remove) the same values are the case's own again.
    assert {(e.value, e.source, e.term_id) for e in plain.entries()} >= {
        ("Mei", "case", None), ("Chen", "case", None)}
    assert {(e.value, e.source, e.term_id) for e in p.entries()} >= {
        ("Mei", "caseworker", "pt_1"), ("Chen", "caseworker", "pt_2")}


def test_a_restored_run_keeps_its_numbers_and_new_values_get_the_next_free_one() -> None:
    first = Placeholders()
    assert first.hide("call 902-555-0187, then 9025550188") == "call [PHONE_1], then [PHONE_2]"
    saved = first.snapshot()

    second = Placeholders()  # a resume builds it again, and sees a different value first
    assert second.hide("call 902-555-0199") == "call [PHONE_1]"
    second.restore(saved)
    assert second.hide("call 902-555-0187, then 9025550188") == "call [PHONE_1], then [PHONE_2]"
    assert second.hide("and 902-555-0123") == "and [PHONE_3]"  # next free number, no collision
    assert second.reveal("[PHONE_2] [PHONE_3]") == "9025550188 902-555-0123"
