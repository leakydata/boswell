"""Finding a card somebody has just plugged in.

The desktop mounts it wherever it likes and does not tell the server, so the
server looks. Recognition is by content rather than by device node or volume
label: a directory with .bwl files in it was written by a Boswell, wherever
it ended up, and a USB stick that happens to be the right size is not.
"""
import os
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "web"))

import card_scan


def make_card(root, name="CARD", files=("b0000e9cd_0000.bwl",)):
    d = os.path.join(root, name, "boswell")
    os.makedirs(d)
    for f in files:
        open(os.path.join(d, f), "wb").write(b"BSWL")
    return d


def test_a_card_is_found_under_a_mount_root(tmp_path):
    made = make_card(str(tmp_path))
    found = card_scan.find_cards([str(tmp_path)])
    assert [os.path.realpath(f) for f in found] == [os.path.realpath(made)]


def test_the_cards_own_folder_is_accepted_directly(tmp_path):
    # So somebody can point it at the boswell/ directory itself, or at a
    # folder they copied the files into by hand.
    made = make_card(str(tmp_path))
    found = card_scan.find_cards([os.path.dirname(made)])
    assert [os.path.realpath(f) for f in found] == [os.path.realpath(made)]


def test_an_unrelated_drive_is_not_claimed(tmp_path):
    d = os.path.join(str(tmp_path), "HOLIDAY PHOTOS")
    os.makedirs(d)
    open(os.path.join(d, "IMG_0001.JPG"), "wb").write(b"x")
    assert card_scan.find_cards([str(tmp_path)]) == []


def test_an_empty_boswell_directory_is_not_a_card(tmp_path):
    os.makedirs(os.path.join(str(tmp_path), "CARD", "boswell"))
    assert card_scan.find_cards([str(tmp_path)]) == []


def test_a_missing_root_is_not_an_error(tmp_path):
    assert card_scan.find_cards([str(tmp_path / "nope")]) == []


def test_the_same_card_seen_twice_is_reported_once(tmp_path):
    made = make_card(str(tmp_path))
    found = card_scan.find_cards([str(tmp_path), str(tmp_path)])
    assert len(found) == 1


def test_recordings_come_back_in_name_order(tmp_path):
    made = make_card(str(tmp_path), files=("b_0002.bwl", "b_0000.bwl",
                                           "b_0001.bwl", "notes.txt"))
    got = [os.path.basename(p) for p in card_scan.recordings(made)]
    assert got == ["b_0000.bwl", "b_0001.bwl", "b_0002.bwl"], \
        "only .bwl, oldest name first"


def test_the_search_does_not_walk_the_whole_volume(tmp_path):
    # A 16 GB card walked in full is slow, and so is every other mounted
    # filesystem on the machine. The answer is always a level or two down.
    deep = os.path.join(str(tmp_path), "a", "b", "c", "d", "boswell")
    os.makedirs(deep)
    open(os.path.join(deep, "x.bwl"), "wb").write(b"BSWL")
    assert card_scan.find_cards([str(tmp_path)]) == []
