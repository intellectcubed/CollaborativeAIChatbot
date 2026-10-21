import pytest

from chatbot.config import load_roster


def write(tmp_path, text, name="roster.csv"):
    p = tmp_path / name
    p.write_text(text)
    return p


def test_csv_with_name_and_squad_only(tmp_path):
    r = load_roster(write(tmp_path, "Alex Example,34\nSam Sample,54\n"))
    people = r.people()
    assert [(m.member, m.squad, m.names) for m in people] == [("Member-1", "34", ("Alex Example",)), ("Member-2", "54", ("Sam Sample",))]
    assert r.lookup("csv-2").member == "Member-2"  # synthetic id when the roster has none


def test_csv_with_sender_ids_aliases_and_header(tmp_path):
    r = load_roster(write(tmp_path, "name,squad,sender_ids,aliases\nDiane Chrinka,42,111;222,Diane Chrinko\n"))
    [m] = r.people()
    assert m.names == ("Diane Chrinka", "Diane Chrinko") and m.sender_ids == ("111", "222")
    assert r.lookup("111") is r.lookup("222") and len(r.people()) == 1
    assert {"Chrinka", "Chrinko"} <= r.name_tokens()


def test_csv_rejects_rows_without_squad(tmp_path):
    with pytest.raises(ValueError):
        load_roster(write(tmp_path, "Alex Example\n"))


def test_blank_lines_ignored_and_json_still_works(tmp_path):
    assert len(load_roster(write(tmp_path, "\nAlex Example,34\n\n")).people()) == 1
    j = write(tmp_path, '{"members": [{"sender_id": "1", "member": "Member-1", "squad": "34", "names": ["A B"]}]}', "r.json")
    assert load_roster(j).lookup("1").squad == "34"


def test_rows_sharing_a_sender_id_merge_into_one_person(tmp_path):
    r = load_roster(write(tmp_path, "Diane Chrinka,42,111,\nOther Person,34,333,\nDiane Chrinko,42,111,Diane C\n"))
    assert [m.member for m in r.people()] == ["Member-1", "Member-2"]
    diane = r.lookup("111")
    assert diane.member == "Member-1" and diane.names == ("Diane Chrinka", "Diane Chrinko", "Diane C")


def test_shared_sender_id_with_different_squads_is_an_error(tmp_path):
    with pytest.raises(ValueError):
        load_roster(write(tmp_path, "A One,42,111\nA Uno,54,111\n"))
