import shlex
import re
import traceback
import types
from enum import Enum

from aqt import dialogs, mw
from aqt.qt import QApplication, QTimer, qconnect
from aqt.utils import showCritical, tooltip
from aqt.webview import AnkiWebView

from . import config_util, data, generate_grid, logger, util


# mimic `AnkiWebViewKind`
# https://github.com/ankitects/anki/blob/1a68c9f5d5bcc197b641fe7405e5d9a4823928f3/qt/aqt/webview.py#L34-L59
class KanjiGridWebViewKind(Enum):
    DEFAULT = "kanjigrid webview"

STUDY_DECK_NAME_PREFIX = "unseen kanjis from grid group "
TEMP_STUDY_DECK_NAME_PREFIX = "Temp - " + STUDY_DECK_NAME_PREFIX
startup_update_scheduled = False
study_deck_note_watch_timer = None
study_deck_note_watch_last_id = None

def init_webview() -> None:
    webview = AnkiWebView()
    try:
        webview.set_kind(KanjiGridWebViewKind.DEFAULT)
    except Exception:  # noqa: BLE001
        logger.error_log("Failed to set webview kind", traceback.format_exc())
    return webview

def open_search_link(wv: AnkiWebView, config: types.SimpleNamespace, char: str) -> None:
    link = util.get_search(config, char)
    # aqt.utils.openLink is an alternative
    wv.eval(f"window.open('{link}', '_blank');")

def open_note_browser(deckname: str, fields_list: list, additional_search_filters: str, search_string: str) -> None:
    fields_string = ""
    for i, field in enumerate(fields_list):
        if i != 0:
            fields_string += " OR "
        fields_string += field + ":*" + search_string + "*"
    if len(fields_list) > 1:
        fields_string = "(" + fields_string + ")"
    browser = dialogs.open("Browser", mw)
    browser.form.searchEdit.lineEdit().setText("deck:\"" + deckname + "\" " + fields_string + " " + additional_search_filters)
    browser.onSearchActivated()

def on_copy_cmd(char: str) -> None:
    QApplication.clipboard().setText(char)

def on_browse_cmd(char: str, config: types.SimpleNamespace, deckname: str) -> None:
    open_note_browser(deckname, config.fieldslist, config.searchfilter, char)

def study_units_for_group(units: dict, config: types.SimpleNamespace, group_index: int) -> list:
    if config.groupby <= 0:
        return []

    grouping = data.groupings[config.groupby - 1]
    if group_index < len(grouping.groups):
        return [units[c] for c in grouping.groups[group_index].characters if c in units]

    grouped_chars = set("".join(group.characters for group in grouping.groups))
    return [unit for unit in units.values() if unit.value not in grouped_chars]

def unseen_card_ids_for_study_units(study_units: list) -> list:
    card_ids = []
    for unit in study_units:
        if unit.seen_cards_count == 0 and unit.unseen_cards_count > 0:
            card_ids.extend(unit.unseen_card_ids)
    return sorted(set(card_ids))

def cid_search(card_ids: list) -> str:
    if len(card_ids) == 0:
        return "cid:0 is:new"
    return "cid:" + ",".join(str(card_id) for card_id in card_ids) + " is:new"

def card_ids_from_cid_search(search: str) -> set:
    card_ids = set()
    for match in re.finditer(r"\bcid:([0-9,]+)", search or ""):
        for card_id in match.group(1).split(","):
            try:
                parsed_id = int(card_id)
            except ValueError:
                continue
            if parsed_id:
                card_ids.add(parsed_id)
    return card_ids

def save_deck(deck: dict) -> None:
    if hasattr(mw.col.decks, "save"):
        mw.col.decks.save(deck)
    else:
        mw.col.decks.update(deck)

def empty_filtered_deck(deck_id: int) -> None:
    if hasattr(mw.col.sched, "emptyDyn"):
        mw.col.sched.emptyDyn(deck_id)
    elif hasattr(mw.col.sched, "empty_filtered_deck"):
        mw.col.sched.empty_filtered_deck(deck_id)

def rebuild_filtered_deck(deck_id: int) -> None:
    if hasattr(mw.col.sched, "rebuildDyn"):
        mw.col.sched.rebuildDyn(deck_id)
    elif hasattr(mw.col.sched, "rebuild_filtered_deck"):
        mw.col.sched.rebuild_filtered_deck(deck_id)
    else:
        raise RuntimeError("This Anki version does not expose a filtered deck rebuild API.")

def refresh_deck_layout() -> None:
    if hasattr(mw, "deckBrowser") and hasattr(mw.deckBrowser, "refresh"):
        mw.deckBrowser.refresh()
    elif hasattr(mw, "reset"):
        mw.reset()

def study_deck_name(group_name: str, temporary: bool) -> str:
    name = STUDY_DECK_NAME_PREFIX + "\"" + group_name + "\""
    if temporary:
        name = "Temp - " + name
    return name

def study_group_name(config: types.SimpleNamespace, group_index: int) -> str:
    if config.groupby <= 0:
        return "All"

    grouping = data.groupings[config.groupby - 1]
    if group_index < len(grouping.groups):
        return grouping.groups[group_index].name
    return grouping.leftover_group

def study_deck_group_name(deck_name: str):
    if deck_name.startswith(TEMP_STUDY_DECK_NAME_PREFIX):
        return deck_name[len(TEMP_STUDY_DECK_NAME_PREFIX):].strip('"')
    if deck_name.startswith(STUDY_DECK_NAME_PREFIX):
        return deck_name[len(STUDY_DECK_NAME_PREFIX):].strip('"')
    return None

def filtered_deck_by_name(name: str):
    deck = mw.col.decks.by_name(name)
    if deck and deck.get("dyn"):
        return deck
    return None

def filtered_study_deck_for_group(config: types.SimpleNamespace, group_index: int):
    group_name = study_group_name(config, group_index)
    deck = filtered_deck_by_name(study_deck_name(group_name, False))
    if deck is not None:
        return deck
    return filtered_deck_by_name(study_deck_name(group_name, True))

def study_group_index(config: types.SimpleNamespace, group_name: str):
    if config.groupby <= 0:
        return 0 if group_name == "All" else None

    grouping = data.groupings[config.groupby - 1]
    for index, group in enumerate(grouping.groups):
        if group.name == group_name:
            return index
    if grouping.leftover_group == group_name:
        return len(grouping.groups)
    return None

def create_or_update_filtered_deck(name: str, search: str, limit: int) -> int:
    if hasattr(mw.col.decks, "newDyn"):
        new_deck = mw.col.decks.newDyn(name)
        if isinstance(new_deck, dict):
            deck = new_deck
        else:
            deck = mw.col.decks.get(new_deck)
    else:
        deck_id = mw.col.decks.id(name)
        deck = mw.col.decks.get(deck_id)
        deck["dyn"] = 1

    if not deck.get("dyn"):
        raise RuntimeError(f"A normal deck named \"{name}\" already exists.")

    deck["terms"] = [[search, limit, 0]]
    deck["resched"] = True
    save_deck(deck)
    empty_filtered_deck(deck["id"])
    rebuild_filtered_deck(deck["id"])
    return deck["id"]

def save_last_temp_study_deck_id(deck_id: int) -> None:
    saved_config = types.SimpleNamespace(**config_util.get_config(mw))
    saved_config.laststudydeckid = deck_id
    config_util.set_config(mw, saved_config)

def save_study_deck_note_watermark(note_id: int = None) -> None:
    saved_config = types.SimpleNamespace(**config_util.get_config(mw))
    saved_config.studydecklastnoteid = current_max_note_id() if note_id is None else note_id
    config_util.set_config(mw, saved_config)

def forget_last_temp_study_deck_id() -> None:
    saved_config = types.SimpleNamespace(**config_util.get_config(mw))
    if getattr(saved_config, "laststudydeckid", 0) == 0:
        return
    saved_config.laststudydeckid = 0
    config_util.set_config(mw, saved_config)

def remove_deck(deck_id: int) -> None:
    try:
        mw.col.decks.remove([deck_id])
    except TypeError:
        mw.col.decks.remove(deck_id)

def cleanup_temp_study_deck() -> None:
    try:
        saved_config = types.SimpleNamespace(**config_util.get_config(mw))
        if not getattr(saved_config, "makestudydecktemporary", True):
            return

        deck_id = getattr(saved_config, "laststudydeckid", 0)
        if not deck_id:
            return

        deck = mw.col.decks.get(deck_id)
        if not deck or not deck.get("dyn") or not deck.get("name", "").startswith(TEMP_STUDY_DECK_NAME_PREFIX):
            forget_last_temp_study_deck_id()
            return

        empty_filtered_deck(deck_id)
        remove_deck(deck_id)
        forget_last_temp_study_deck_id()
        if hasattr(mw, "reset"):
            mw.reset()
    except Exception:  # noqa: BLE001
        logger.error_log("Failed to clean up Kanji Grid temp study deck", traceback.format_exc())

def open_deck_for_study(deck_id: int) -> None:
    mw.col.decks.select(deck_id)
    if hasattr(mw, "onOverview"):
        mw.onOverview()
    elif hasattr(mw, "moveToState"):
        mw.moveToState("overview")

    def start_review() -> None:
        if hasattr(mw, "onStudy"):
            mw.onStudy()
        elif hasattr(mw, "moveToState"):
            mw.moveToState("review")

    QTimer.singleShot(250, start_review)

def create_study_deck_for_group(group_index: int, config: types.SimpleNamespace, units: dict, temporary: bool) -> tuple:
    card_ids = unseen_card_ids_for_study_units(study_units_for_group(units, config, group_index))
    if len(card_ids) == 0:
        return (None, 0, "")

    group_name = study_group_name(config, group_index)
    deck_name = study_deck_name(group_name, temporary)
    deck_id = create_or_update_filtered_deck(deck_name, cid_search(card_ids), len(card_ids))
    save_study_deck_note_watermark()
    return (deck_id, len(card_ids), deck_name)

def on_create_study_deck_cmd(group_index_text: str, config: types.SimpleNamespace, units: dict) -> None:
    try:
        config_util.set_config(mw, config)
        group_index = int(group_index_text)
        deck_id, card_count, deck_name = create_study_deck_for_group(group_index, config, units, temporary=False)
        if deck_id is None:
            tooltip("No new cards found for this block.")
            return
        refresh_deck_layout()
        tooltip(f"Created {deck_name} with {card_count} new card(s).")
    except Exception as exception:  # noqa: BLE001
        logger.error_log("Failed to create Kanji Grid study deck", traceback.format_exc())
        showCritical("Failed to create study deck:\n" + str(exception))

def on_study_cmd(group_index_text: str, config: types.SimpleNamespace, units: dict) -> None:
    try:
        config_util.set_config(mw, config)
        group_index = int(group_index_text)
        temporary = getattr(config, "makestudydecktemporary", True)
        group_name = study_group_name(config, group_index)
        permanent_deck = filtered_deck_by_name(study_deck_name(group_name, False))
        if permanent_deck is not None:
            deck_id, card_count, deck_name = create_study_deck_for_group(group_index, config, units, temporary=False)
            temporary = False
        else:
            deck_id, card_count, deck_name = create_study_deck_for_group(group_index, config, units, temporary=temporary)
        if deck_id is None:
            tooltip("No new cards found for this block.")
            return
        if temporary:
            save_last_temp_study_deck_id(deck_id)
        else:
            forget_last_temp_study_deck_id()
        open_deck_for_study(deck_id)
        tooltip(f"Loaded {card_count} cards into {deck_name}.")
    except Exception as exception:  # noqa: BLE001
        logger.error_log("Failed to start Kanji Grid temp study", traceback.format_exc())
        showCritical("Failed to start temp study:\n" + str(exception))

def update_existing_study_decks(config: types.SimpleNamespace = None) -> int:
    return update_matching_study_decks(config)

def update_matching_study_decks(config: types.SimpleNamespace = None, group_indexes: set = None, reset_ui: bool = True) -> int:
    if config is None:
        config = types.SimpleNamespace(**config_util.get_config(mw))
    if not hasattr(config, "did"):
        config.did = mw.col.conf["curDeck"]
        if getattr(config, "defaultdeck", ""):
            selected_deck = mw.col.decks.by_name(config.defaultdeck)
            if selected_deck:
                config.did = selected_deck["id"]
    if not hasattr(config, "fieldslist"):
        config.fieldslist = shlex.split(getattr(config, "defaultfield", "").lower())
    config.timetravel_enabled = False
    config.timetravel_time = 0
    config.usetextsource = False
    config.usejitenapi = False
    config.usegsmsource = False
    config.usegsmapi = False

    data.init_groups()
    units = None
    updated = 0
    for deck in mw.col.decks.all():
        group_name = study_deck_group_name(deck.get("name", ""))
        if group_name is None or not deck.get("dyn"):
            continue

        group_index = study_group_index(config, group_name)
        if group_index is None:
            continue
        if group_indexes is not None and group_index not in group_indexes:
            continue

        if units is None:
            units = generate_grid.kanjigrid(mw, config)

        card_ids = unseen_card_ids_for_study_units(study_units_for_group(units, config, group_index))
        deck["terms"] = [[cid_search(card_ids), len(card_ids), 0]]
        deck["resched"] = True
        save_deck(deck)
        empty_filtered_deck(deck["id"])
        rebuild_filtered_deck(deck["id"])
        updated += 1
    if updated > 0 and reset_ui:
        refresh_deck_layout()
    return updated

def update_existing_study_decks_with_tooltip(config: types.SimpleNamespace = None) -> None:
    try:
        count = update_existing_study_decks(config)
        tooltip(f"Updated {count} Kanji Grid study deck(s).")
    except Exception as exception:  # noqa: BLE001
        logger.error_log("Failed to update Kanji Grid study decks", traceback.format_exc())
        showCritical("Failed to update study decks:\n" + str(exception))

def update_existing_study_decks_on_startup() -> None:
    global startup_update_scheduled
    startup_update_scheduled = False
    try:
        config = types.SimpleNamespace(**config_util.get_config(mw))
        if getattr(config, "updatestudydecksonstartup", True):
            poll_added_notes_for_study_decks()
    except Exception:  # noqa: BLE001
        logger.error_log("Failed to update Kanji Grid study decks on startup", traceback.format_exc())

def schedule_existing_study_decks_startup_update(*args, **kwargs) -> None:
    global startup_update_scheduled
    if startup_update_scheduled:
        return
    startup_update_scheduled = True
    QTimer.singleShot(7000, update_existing_study_decks_on_startup)

def current_max_note_id() -> int:
    return int(mw.col.db.scalar("select coalesce(max(id), 0) from notes") or 0)

def touched_groups_for_notes(note_ids: list, config: types.SimpleNamespace):
    chars = set()
    for note_id in note_ids:
        try:
            note = mw.col.get_note(note_id)
        except Exception:  # noqa: BLE001
            continue
        chars.update(note_kanji_for_saved_fields(note, config))

    if len(chars) == 0:
        return None

    data.init_groups()
    if config.groupby <= 0:
        return {0}

    lookup = group_index_by_kanji(config)
    leftover_index = len(data.groupings[config.groupby - 1].groups)
    return {lookup.get(char, leftover_index) for char in chars}

def append_card_ids_to_filtered_deck(deck: dict, card_ids: set) -> None:
    if not card_ids:
        return

    existing_card_ids = set(mw.col.db.list("select id from cards where did = ?", deck["id"]))
    for term in deck.get("terms", []):
        if len(term) > 0:
            existing_card_ids.update(card_ids_from_cid_search(term[0]))

    merged_card_ids = sorted(existing_card_ids.union(card_ids))
    deck["terms"] = [[cid_search(merged_card_ids), len(merged_card_ids), 0]]
    deck["resched"] = True
    save_deck(deck)
    empty_filtered_deck(deck["id"])
    rebuild_filtered_deck(deck["id"])

def append_new_notes_to_study_decks(note_ids: list, config: types.SimpleNamespace) -> int:
    if len(note_ids) == 0:
        return 0

    data.init_groups()
    group_lookup = group_index_by_kanji(config) if config.groupby > 0 else {}
    leftover_index = len(data.groupings[config.groupby - 1].groups) if config.groupby > 0 else 0
    deck_card_ids = {}

    for note_id in note_ids:
        try:
            note = mw.col.get_note(note_id)
        except Exception:  # noqa: BLE001
            continue

        chars = note_kanji_for_saved_fields(note, config)
        if len(chars) == 0:
            continue

        group_indexes = {group_lookup.get(char, leftover_index) for char in chars} if config.groupby > 0 else {0}
        target_deck = None
        for group_index in sorted(group_indexes):
            target_deck = filtered_study_deck_for_group(config, group_index)
            if target_deck is not None:
                break
        if target_deck is None:
            continue

        card_ids = set(mw.col.db.list("select id from cards where nid = ? and type = 0", note_id))
        if not card_ids:
            continue
        deck_card_ids.setdefault(target_deck["id"], set()).update(card_ids)

    updated = 0
    for deck_id, card_ids in deck_card_ids.items():
        deck = mw.col.decks.get(deck_id)
        if deck and deck.get("dyn"):
            append_card_ids_to_filtered_deck(deck, card_ids)
            updated += 1

    if updated > 0:
        refresh_deck_layout()
    return updated

def poll_added_notes_for_study_decks() -> None:
    global study_deck_note_watch_last_id
    try:
        if mw is None or mw.col is None:
            return

        max_id = current_max_note_id()
        if study_deck_note_watch_last_id is None:
            saved_config = types.SimpleNamespace(**config_util.get_config(mw))
            study_deck_note_watch_last_id = getattr(saved_config, "studydecklastnoteid", 0) or max_id

        if max_id <= study_deck_note_watch_last_id:
            return

        old_id = study_deck_note_watch_last_id
        study_deck_note_watch_last_id = max_id
        save_study_deck_note_watermark(max_id)

        config = types.SimpleNamespace(**config_util.get_config(mw))
        if not getattr(config, "updatestudydecksonnoteadd", True):
            return

        note_ids = mw.col.db.list("select id from notes where id > ? and id <= ? order by id", old_id, max_id)
        config.did = mw.col.conf["curDeck"]
        if getattr(config, "defaultdeck", ""):
            selected_deck = mw.col.decks.by_name(config.defaultdeck)
            if selected_deck:
                config.did = selected_deck["id"]
        config.fieldslist = shlex.split(getattr(config, "defaultfield", "").lower())
        config.timetravel_enabled = False
        config.timetravel_time = 0

        append_new_notes_to_study_decks(note_ids, config)
    except Exception:  # noqa: BLE001
        logger.error_log("Failed to poll added notes for Kanji Grid study decks", traceback.format_exc())

def start_study_deck_note_watcher(*args, **kwargs) -> None:
    global study_deck_note_watch_timer, study_deck_note_watch_last_id
    if mw is None or mw.col is None:
        return
    saved_config = types.SimpleNamespace(**config_util.get_config(mw))
    saved_last_id = getattr(saved_config, "studydecklastnoteid", 0)
    study_deck_note_watch_last_id = saved_last_id or current_max_note_id()
    if not saved_last_id:
        save_study_deck_note_watermark(study_deck_note_watch_last_id)
    if study_deck_note_watch_timer is not None:
        return

    study_deck_note_watch_timer = QTimer(mw)
    study_deck_note_watch_timer.setInterval(4000)
    study_deck_note_watch_timer.timeout.connect(poll_added_notes_for_study_decks)
    study_deck_note_watch_timer.start()

def group_index_by_kanji(config: types.SimpleNamespace) -> dict:
    if config.groupby <= 0:
        return {}
    grouping = data.groupings[config.groupby - 1]
    result = {}
    for index, group in enumerate(grouping.groups):
        for char in group.characters:
            result[char] = index
    return result

def note_kanji_for_saved_fields(note, config: types.SimpleNamespace) -> set:
    fields = shlex.split(getattr(config, "defaultfield", "").lower())
    if len(fields) == 0:
        return set()

    chars = set()
    for field_name in note.keys():
        if field_name.lower() not in fields:
            continue
        for char in note[field_name]:
            if util.is_kanji(char):
                chars.add(char)
    return chars

def update_study_decks_for_added_note(note) -> None:
    try:
        QTimer.singleShot(3000, poll_added_notes_for_study_decks)
    except Exception:  # noqa: BLE001
        logger.error_log("Failed to update Kanji Grid study decks for added note", traceback.format_exc())

def update_study_decks_for_added_note_hook(*args, **kwargs) -> None:
    note = kwargs.get("note")
    if note is None:
        for arg in args:
            if hasattr(arg, "keys") and hasattr(arg, "__getitem__"):
                note = arg
                break
    if note is not None:
        update_study_decks_for_added_note(note)

def on_search_cmd(char: str, wv: AnkiWebView, config: types.SimpleNamespace) -> None:
    open_search_link(wv, config, char)

def on_find_cmd(wv: AnkiWebView) -> None:
    char = QApplication.clipboard().text().strip()

    # limit searches to kanji to prevent js injection
    if not util.is_kanji(char):
        # truncate in case there's random garbage in the clipboard
        len_limit = 20
        tooltip_char = char if len(char) <= len_limit else char[:len_limit] + "..."
        tooltip(f"\"{tooltip_char}\" is not valid kanji.")
        return

    def find_text_callback(found: bool) -> None:
        if not found:
          tooltip(f"\"{char}\" not found in grid.")

    # qt's findText impl is bugged and inconvenient, so we use our own
    wv.evalWithCallback(f"findChar('{char}');", find_text_callback)

def add_webview_context_menu_items(wv: AnkiWebView, expected_wv: AnkiWebView, menu, config: types.SimpleNamespace, deckname: str, char: str) -> None:
    # hook is active while kanjigrid is open, and right clicking on the main window (deck list) will also trigger this, so check wv
    if wv is not expected_wv:
      return
    if char != "":
        menu.clear()
        copy_action = menu.addAction(f"Copy {char} to clipboard")
        qconnect(copy_action.triggered, lambda: on_copy_cmd(char))
        browse_action = menu.addAction(f"Browse deck for {char}")
        qconnect(browse_action.triggered, lambda: on_browse_cmd(char, config, deckname))
        search_action = menu.addAction(f"Search online for {char}")
        qconnect(search_action.triggered, lambda: on_search_cmd(char, wv, config))
    else:
        find_action = menu.addAction("Find copied kanji")
        qconnect(find_action.triggered, lambda: on_find_cmd(wv))
