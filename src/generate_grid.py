import datetime
import csv
import html
import json
import math
import operator
import os
import re
import sys
import types
import urllib.parse
import urllib.request
import urllib.error
import zipfile
from functools import reduce

from anki.utils import ids2str

from . import data, util


JITEN_INTERVAL_MARKER = 1000000
GSM_ENCOUNTER_MARKER = 2000000
GSM_FUTURE_EXPOSURE_MARKER = 3000000
EXTERNAL_SCORE_SCALE = 1000
JMDICT_CACHE_VERSION = 2
GSM_FUTURE_CACHE_VERSION = 1
GSM_FUTURE_MAX_GAMES = 10
GSM_FUTURE_MAX_VOCAB_PAGES_PER_DECK = 50
GSM_API_BASE_URL = "http://localhost:7275"
JITEN_API_BASE_URL = "https://api.jiten.moe/api"
GSM_FUTURE_EXPOSURE_DETAILS = {}
JITEN_SELECTED_WORK_GRADIENT = ["#fff4e0", "#d9791f"]
JITEN_MEDIA_TYPE_LABELS = {
    1: "Anime",
    2: "Drama",
    3: "Movie",
    4: "Novel",
    5: "Non-fiction",
    6: "Video game",
    7: "Visual novel",
    8: "Web novel",
    9: "Manga",
    10: "Audio",
}


def valid_unit_key(config: types.SimpleNamespace, unit_key: str) -> bool:
    return util.ignored_characters.find(unit_key) == -1 and (not config.kanjionly or util.is_kanji(unit_key))


def download_jiten_vocabulary_export(token: str) -> str:
    auth_token = token.strip()
    if not auth_token:
        raise ValueError("Missing Jiten API token")

    if ":" in auth_token and "\n" not in auth_token:
        header_name, header_value = auth_token.split(":", 1)
        auth_header = (header_name.strip(), header_value.strip())
    else:
        if auth_token.startswith("ak_"):
            auth_token = "ApiKey " + auth_token
        elif not auth_token.lower().startswith(("bearer ", "apikey ")):
            auth_token = "Bearer " + auth_token
        auth_header = ("Authorization", auth_token)

    request = urllib.request.Request(
        JITEN_API_BASE_URL + "/user/vocabulary/export",
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            auth_header[0]: auth_header[1],
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8")


def jiten_api_json(token: str, path: str):
    auth_token = token.strip()
    if not auth_token:
        raise ValueError("Missing Jiten API token")

    if ":" in auth_token and "\n" not in auth_token:
        header_name, header_value = auth_token.split(":", 1)
        auth_header = (header_name.strip(), header_value.strip())
    else:
        if auth_token.startswith("ak_"):
            auth_token = "ApiKey " + auth_token
        elif not auth_token.lower().startswith(("bearer ", "apikey ")):
            auth_token = "Bearer " + auth_token
        auth_header = ("Authorization", auth_token)

    request = urllib.request.Request(
        JITEN_API_BASE_URL + path,
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            auth_header[0]: auth_header[1],
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def load_jiten_vocabulary_export(config: types.SimpleNamespace) -> dict:
    if getattr(config, "usejitenapi", False):
        token = getattr(config, "jitenapikey", "").strip()
        if not token:
            return {}
        return {"cards": jiten_api_json(token, "/user/vocabulary/cards"), "has_word_text": True}

    source_path = getattr(config, "textsourcepath", "")
    if not source_path or not os.path.isfile(source_path):
        return {}

    with open(source_path, "r", encoding="utf-8-sig") as backup_in:
        return json.load(backup_in)


def external_unit_score(unit, config: types.SimpleNamespace) -> float:
    if unit.avg_interval <= -GSM_FUTURE_EXPOSURE_MARKER:
        return min((abs(unit.avg_interval) - GSM_FUTURE_EXPOSURE_MARKER) / EXTERNAL_SCORE_SCALE, 1)
    if unit.avg_interval <= -GSM_ENCOUNTER_MARKER:
        return min((abs(unit.avg_interval) - GSM_ENCOUNTER_MARKER) / EXTERNAL_SCORE_SCALE, 1)
    if unit.avg_interval <= -JITEN_INTERVAL_MARKER:
        interval = abs(unit.avg_interval) - JITEN_INTERVAL_MARKER
        return util.score_adjust(interval / config.interval)
    if unit.avg_interval < 0:
        return min(abs(unit.avg_interval) / 5, 1)
    return util.score_adjust(unit.avg_interval / config.interval)


def add_textfile_units(units: dict, config: types.SimpleNamespace, anki_priority: bool = False) -> dict:
    source_path = getattr(config, "textsourcepath", "")
    if not source_path or not os.path.isfile(source_path):
        return units

    counts = {}
    first_seen = {}
    with open(source_path, "r", encoding="utf-8-sig", errors="replace") as file_in:
        for idx, line in enumerate(file_in, start=1):
            word = line.strip()
            if not word:
                continue
            for ch in set(word):
                if not valid_unit_key(config, ch):
                    continue
                counts[ch] = counts.get(ch, 0) + 1
                first_seen[ch] = min(first_seen.get(ch, idx), idx)

    for ch, count in counts.items():
        if anki_priority and ch in units:
            continue
        units[ch] = util.unit_tuple(first_seen[ch], ch, -float(count), count, 0, ())

    return units


def add_gsm_csv_units(units: dict, config: types.SimpleNamespace, anki_priority: bool = False) -> dict:
    source_path = getattr(config, "gsmsourcepath", "")
    if not source_path or not os.path.isfile(source_path):
        return units

    field_limit = sys.maxsize
    while True:
        try:
            csv.field_size_limit(field_limit)
            break
        except OverflowError:
            field_limit = int(field_limit / 10)

    aggregate = {}
    with open(source_path, "r", encoding="utf-8-sig", newline="", errors="replace") as csv_in:
        reader = csv.DictReader(csv_in)
        for idx, row in enumerate(reader, start=1):
            word = (row.get("word") or "").strip()
            if not word:
                continue
            try:
                encounters = int(float(row.get("frequency") or 1))
            except ValueError:
                encounters = 1
            encounters = max(encounters, 1)

            for ch in set(word):
                if not valid_unit_key(config, ch):
                    continue
                data = aggregate.setdefault(ch, {"idx": idx, "count": 0})
                data["idx"] = min(data["idx"], idx)
                data["count"] += encounters

    return add_gsm_aggregate_units(units, aggregate, anki_priority)


def add_gsm_aggregate_units(units: dict, aggregate: dict, anki_priority: bool = False) -> dict:
    if not aggregate:
        return units
    max_count = max(data["count"] for data in aggregate.values())
    for ch, data in aggregate.items():
        if anki_priority and ch in units:
            continue
        score = 1 if max_count <= 1 else (math.log1p(data["count"]) / math.log1p(max_count))
        units[ch] = util.unit_tuple(
            data["idx"],
            ch,
            -(GSM_ENCOUNTER_MARKER + (score * EXTERNAL_SCORE_SCALE)),
            data["count"],
            0,
            (),
        )

    return units


def gsm_api_json(path: str, timeout: float = 1.5):
    url = GSM_API_BASE_URL + path
    request = urllib.request.Request(url, headers={"Accept": "application/json", "Accept-Encoding": "identity"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def gsm_api_available() -> bool:
    try:
        status = gsm_api_json("/api/tokenization/status", timeout=0.75)
        return bool(status.get("enabled", True))
    except (OSError, ValueError, urllib.error.URLError):
        return False


def jiten_public_api_json(path: str, timeout: float = 8):
    url = JITEN_API_BASE_URL + path
    request = urllib.request.Request(url, headers={"Accept": "application/json", "Accept-Encoding": "identity"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def jiten_media_result_from_deck(deck: dict) -> dict:
    deck_id = int(deck.get("deckId", deck.get("DeckId")))
    media_type = deck.get("mediaType", deck.get("MediaType", ""))
    try:
        media_type_label = JITEN_MEDIA_TYPE_LABELS.get(int(media_type), str(media_type))
    except (TypeError, ValueError):
        media_type_label = str(media_type or "")
    subdeck_count = deck.get("childrenDeckCount", deck.get("ChildrenDeckCount", 0)) or 0
    word_count = deck.get("wordCount", deck.get("WordCount", 0)) or 0
    unique_kanji_count = deck.get("uniqueKanjiCount", deck.get("UniqueKanjiCount", 0)) or 0
    title = deck.get("originalTitle") or deck.get("romajiTitle") or deck.get("englishTitle") or ("Jiten deck " + str(deck_id))
    subtitle = deck.get("romajiTitle") or deck.get("englishTitle") or ""
    return {
        "deckId": deck_id,
        "title": title,
        "subtitle": subtitle if subtitle != title else "",
        "mediaType": media_type,
        "mediaTypeLabel": media_type_label,
        "subdeckCount": int(subdeck_count),
        "wordCount": int(word_count),
        "uniqueKanjiCount": int(unique_kanji_count),
    }


def jiten_media_search_suggestions(query: str, limit: int = 8) -> list:
    cleaned = query.strip()
    if len(cleaned) < 2:
        return []
    encoded = urllib.parse.urlencode({"query": cleaned, "limit": max(1, min(limit, 10))})
    payload = jiten_public_api_json(f"/media-deck/search-suggestions?{encoded}", timeout=8)
    suggestions = payload.get("suggestions", payload.get("Suggestions", [])) or []
    details_by_deck_id = {}
    try:
        details_payload = jiten_public_api_json(
            "/media-deck/get-media-decks?" + urllib.parse.urlencode({"titleFilter": cleaned, "offset": 0}),
            timeout=8,
        )
        for deck in details_payload.get("data", details_payload.get("Data", [])) or []:
            details_by_deck_id[int(deck.get("deckId", deck.get("DeckId")))] = deck
    except Exception:  # noqa: BLE001
        details_by_deck_id = {}

    results = []
    for item in suggestions:
        deck_id = item.get("deckId", item.get("DeckId"))
        if not deck_id:
            continue
        deck_id = int(deck_id)
        details = details_by_deck_id.get(deck_id, {})
        merged = dict(item)
        merged.update({key: value for key, value in details.items() if value not in (None, "")})
        merged["deckId"] = deck_id
        results.append(jiten_media_result_from_deck(merged))
    return results


def jiten_media_child_suggestions(deck_id: int) -> dict:
    offset = 0
    page_size = 25
    total_items = 0
    children = []
    parent_title = "Jiten deck " + str(deck_id)

    for _ in range(80):
        payload = jiten_public_api_json(f"/media-deck/{deck_id}/detail?" + urllib.parse.urlencode({"offset": offset}), timeout=10)
        total_items = int(payload.get("totalItems", payload.get("TotalItems", total_items or 0)) or 0)
        page_size = int(payload.get("pageSize", payload.get("PageSize", page_size)) or page_size)
        data_payload = payload.get("data", payload.get("Data", {})) or {}
        main_deck = data_payload.get("mainDeck", data_payload.get("MainDeck", {})) or {}
        if main_deck:
            parent_title = main_deck.get("originalTitle") or main_deck.get("romajiTitle") or main_deck.get("englishTitle") or parent_title
        subdecks = data_payload.get("subDecks", data_payload.get("SubDecks", [])) or []
        for subdeck in subdecks:
            children.append(jiten_media_result_from_deck(subdeck))

        offset += page_size
        if not subdecks or offset >= total_items:
            break

    return {
        "parentDeckId": deck_id,
        "parentTitle": parent_title,
        "children": children,
        "totalItems": total_items,
        "complete": len(children) >= total_items,
    }


def gsm_future_cache_path() -> str:
    return os.path.join(os.path.dirname(__file__), "user_files", "gsm_future_exposure_cache.json")


def load_gsm_future_cache() -> dict:
    try:
        with open(gsm_future_cache_path(), "r", encoding="utf-8") as cache_in:
            cache = json.load(cache_in)
        if cache.get("version") == GSM_FUTURE_CACHE_VERSION:
            return cache
    except Exception:  # noqa: BLE001
        pass
    return {"version": GSM_FUTURE_CACHE_VERSION, "decks": {}}


def save_gsm_future_cache(cache: dict) -> None:
    try:
        os.makedirs(os.path.dirname(gsm_future_cache_path()), exist_ok=True)
        with open(gsm_future_cache_path(), "w", encoding="utf-8") as cache_out:
            json.dump(cache, cache_out, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        pass


def gsm_unfinished_jiten_games() -> list:
    payload = gsm_api_json("/api/games-management", timeout=5)
    games = payload.get("games", [])
    linked_unfinished = []
    for game in games:
        deck_id = game.get("deck_id")
        if not deck_id or game.get("completed"):
            continue
        try:
            deck_id = int(deck_id)
        except (TypeError, ValueError):
            continue
        title = (
            game.get("title_original")
            or game.get("title_romaji")
            or game.get("title_english")
            or game.get("obs_scene_name")
            or ("Jiten deck " + str(deck_id))
        )
        linked_unfinished.append({
            "deck_id": deck_id,
            "title": title,
            "last_played": game.get("last_played") or 0,
            "mined_character_count": game.get("mined_character_count") or 0,
        })

    linked_unfinished.sort(key=lambda game: (game["last_played"] or 0, game["mined_character_count"] or 0), reverse=True)
    return linked_unfinished[:GSM_FUTURE_MAX_GAMES]


def word_text_from_jiten_word(word: dict) -> str:
    main = word.get("mainReading") or word.get("MainReading") or {}
    return main.get("text") or main.get("Text") or ""


def load_jiten_deck_kanji_counts(deck_id: int, config: types.SimpleNamespace, cache: dict) -> dict:
    deck_key = str(deck_id)
    cached = cache.get("decks", {}).get(deck_key)
    if cached and isinstance(cached.get("kanji_counts"), dict):
        cached["_cache_hit"] = True
        return cached

    kanji_counts = {}
    total_items = None
    page_size = 100
    fetched_items = 0
    complete = False
    for page in range(GSM_FUTURE_MAX_VOCAB_PAGES_PER_DECK):
        offset = page * page_size
        query = urllib.parse.urlencode({"offset": offset, "sortBy": "deckFreq"})
        payload = jiten_public_api_json(f"/media-deck/{deck_id}/vocabulary?{query}", timeout=12)
        total_items = payload.get("totalItems", payload.get("TotalItems", total_items or 0))
        page_size = payload.get("pageSize", payload.get("PageSize", page_size)) or page_size
        data_payload = payload.get("data", payload.get("Data", {})) or {}
        words = data_payload.get("words", data_payload.get("Words", [])) or []
        if not words:
            complete = fetched_items >= int(total_items or 0)
            break

        for word in words:
            text = word_text_from_jiten_word(word)
            if not text:
                continue
            try:
                occurrences = int(float(word.get("occurrences", word.get("Occurrences", 1)) or 1))
            except (TypeError, ValueError):
                occurrences = 1
            for ch in set(text):
                if valid_unit_key(config, ch):
                    kanji_counts[ch] = kanji_counts.get(ch, 0) + max(occurrences, 1)

        fetched_items += len(words)
        if fetched_items >= int(total_items or 0):
            complete = True
            break

    cached_deck = {
        "kanji_counts": kanji_counts,
        "total_items": total_items or fetched_items,
        "fetched_items": fetched_items,
        "complete": complete,
        "cached_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "_cache_hit": False,
    }
    cache.setdefault("decks", {})[deck_key] = cached_deck
    return cached_deck


def jiten_selected_work_exposure_payload(deck_id: int, title: str, config: types.SimpleNamespace) -> dict:
    cache = load_gsm_future_cache()
    cached_before = str(deck_id) in cache.get("decks", {})
    deck_data = load_jiten_deck_kanji_counts(deck_id, config, cache)
    if not cached_before:
        save_gsm_future_cache(cache)
    counts = {ch: int(count) for ch, count in deck_data.get("kanji_counts", {}).items() if int(count) > 0}
    max_count = max(counts.values()) if counts else 1
    colors = {
        ch: util.get_gradient_color_hex(1 if max_count <= 1 else (math.log1p(count) / math.log1p(max_count)), JITEN_SELECTED_WORK_GRADIENT)
        for ch, count in counts.items()
    }
    return {
        "deckId": deck_id,
        "title": title,
        "counts": counts,
        "colors": colors,
        "complete": bool(deck_data.get("complete", False)),
        "cacheHit": bool(deck_data.get("_cache_hit", False)),
        "fetchedItems": int(deck_data.get("fetched_items", 0) or 0),
        "totalItems": int(deck_data.get("total_items", 0) or 0),
    }


def add_gsm_future_exposure_units(units: dict, config: types.SimpleNamespace, anki_priority: bool = True) -> dict:
    global GSM_FUTURE_EXPOSURE_DETAILS
    GSM_FUTURE_EXPOSURE_DETAILS = {}
    if not getattr(config, "usegsmapi", False) or not getattr(config, "usegsmfutureexposure", False):
        return units

    try:
        games = gsm_unfinished_jiten_games()
    except (OSError, ValueError, urllib.error.URLError):
        return units

    if not games:
        return units

    cache = load_gsm_future_cache()
    aggregate = {}
    changed_cache = False
    for game_idx, game in enumerate(games, start=1):
        before = json.dumps(cache.get("decks", {}).get(str(game["deck_id"]), {}), sort_keys=True)
        try:
            deck_data = load_jiten_deck_kanji_counts(game["deck_id"], config, cache)
        except (OSError, ValueError, urllib.error.URLError, TimeoutError):
            continue
        after = json.dumps(cache.get("decks", {}).get(str(game["deck_id"]), {}), sort_keys=True)
        changed_cache = changed_cache or before != after
        suffix = "" if deck_data.get("complete", False) else " (partial cache)"

        for ch, count in deck_data.get("kanji_counts", {}).items():
            if anki_priority and ch in units:
                continue
            entry = aggregate.setdefault(ch, {"idx": game_idx, "count": 0, "games": []})
            entry["idx"] = min(entry["idx"], game_idx)
            entry["count"] += int(count)
            entry["games"].append({"title": game["title"] + suffix, "count": int(count)})

    if changed_cache:
        save_gsm_future_cache(cache)
    if not aggregate:
        return units

    max_count = max(data["count"] for data in aggregate.values())
    for ch, data in aggregate.items():
        if anki_priority and ch in units:
            continue
        details = sorted(data["games"], key=lambda row: row["count"], reverse=True)
        GSM_FUTURE_EXPOSURE_DETAILS[ch] = {"count": data["count"], "max_count": max_count, "games": details}

    return units


def gsm_future_exposure_bgcolor(char: str):
    details = GSM_FUTURE_EXPOSURE_DETAILS.get(char)
    if not details:
        return None
    count = max(int(details.get("count", 0)), 0)
    max_count = max(int(details.get("max_count", 0)), 1)
    if count <= 0:
        return None
    score = 1 if max_count <= 1 else (math.log1p(count) / math.log1p(max_count))
    return util.get_gradient_color_hex(score, ["#edf7f5", "#289988"])


def add_gsm_api_units(units: dict, config: types.SimpleNamespace, anki_priority: bool = False) -> dict:
    try:
        payload = gsm_api_json("/api/stats/kanji-grid", timeout=5)
    except (OSError, ValueError, urllib.error.URLError):
        return units

    aggregate = {}
    for idx, row in enumerate(payload.get("kanji_data", []), start=1):
        ch = row.get("kanji", "")
        if not ch or not valid_unit_key(config, ch):
            continue
        try:
            encounters = int(float(row.get("frequency") or 1))
        except (TypeError, ValueError):
            encounters = 1
        aggregate[ch] = {"idx": idx, "count": max(encounters, 1)}

    return add_gsm_aggregate_units(units, aggregate, anki_priority)


def jmdict_cache_path() -> str:
    return os.path.join(os.path.dirname(__file__), "user_files", "jmdict_sequence_cache.json")


def load_jmdict_sequence_map(config: types.SimpleNamespace) -> dict:
    jmdict_path = getattr(config, "jmdictpath", "")
    if not jmdict_path or not os.path.isfile(jmdict_path):
        return {}

    stat = os.stat(jmdict_path)
    cache_path = jmdict_cache_path()
    try:
        with open(cache_path, "r", encoding="utf-8") as cache_in:
            cache = json.load(cache_in)
        if cache.get("version") == JMDICT_CACHE_VERSION and cache.get("source") == jmdict_path and cache.get("mtime") == stat.st_mtime and cache.get("size") == stat.st_size:
            return {int(k): v for k, v in cache.get("mapping", {}).items()}
    except Exception:  # noqa: BLE001
        pass

    mapping = {}
    mapping_scores = {}
    with zipfile.ZipFile(jmdict_path) as zip_in:
        term_banks = sorted(name for name in zip_in.namelist() if name.startswith("term_bank_") and name.endswith(".json"))
        for term_bank in term_banks:
            entries = json.loads(zip_in.read(term_bank).decode("utf-8"))
            for entry in entries:
                if len(entry) > 6 and isinstance(entry[6], int):
                    score = entry[4] if len(entry) > 4 and isinstance(entry[4], int) else 0
                    if entry[6] not in mapping_scores or score > mapping_scores[entry[6]]:
                        mapping[entry[6]] = entry[0]
                        mapping_scores[entry[6]] = score

    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    try:
        with open(cache_path, "w", encoding="utf-8") as cache_out:
            json.dump({"version": JMDICT_CACHE_VERSION, "source": jmdict_path, "mtime": stat.st_mtime, "size": stat.st_size, "mapping": mapping}, cache_out, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        pass
    return mapping


def card_field(card: dict, short_name: str, long_name: str, default=None):
    return card.get(short_name, card.get(long_name, default))


def jiten_timestamp(value):
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0
    return 0


def jiten_card_interval(card: dict, config: types.SimpleNamespace) -> float:
    state = card_field(card, "s", "state")
    if state == 5 or state == "Mastered":
        return float(config.interval)

    interval = card_field(card, "st", "stability")
    if interval is not None:
        return float(interval)

    due = jiten_timestamp(card_field(card, "du", "due", 0))
    last_review = jiten_timestamp(card_field(card, "lr", "lastReview", 0))
    return max((due - last_review) / 86400, 1) if due and last_review else 1


def jiten_card_word(card: dict, sequence_map=None):
    word_text = card_field(card, "wordText", "wordText")
    if word_text:
        return word_text
    if sequence_map is None:
        return None
    return sequence_map.get(card_field(card, "w", "wordId"))


def add_jiten_backup_units(units: dict, config: types.SimpleNamespace, anki_priority: bool = False) -> dict:
    backup = load_jiten_vocabulary_export(config)
    if not backup:
        return units

    sequence_map = None
    if not backup.get("has_word_text"):
        sequence_map = load_jmdict_sequence_map(config)
        if not sequence_map:
            return units

    aggregate = {}
    for idx, card in enumerate(backup.get("cards", []), start=1):
        word = jiten_card_word(card, sequence_map)
        if not word:
            continue

        interval = jiten_card_interval(card, config)

        for ch in set(word):
            if not valid_unit_key(config, ch):
                continue
            data = aggregate.setdefault(ch, {"idx": idx, "total": 0.0, "count": 0})
            data["idx"] = min(data["idx"], idx)
            data["total"] += float(interval)
            data["count"] += 1

    for ch, data in aggregate.items():
        if anki_priority and ch in units:
            continue
        avg_interval = data["total"] / data["count"]
        units[ch] = util.unit_tuple(data["idx"], ch, -(JITEN_INTERVAL_MARKER + avg_interval), data["count"], 0, ())

    return units


def textfile_grid(config: types.SimpleNamespace):
    if getattr(config, "textsourcekind", "txt") == "jiten":
        return add_jiten_backup_units({}, config)
    return add_textfile_units({}, config)


def get_grouping_overall_total(units_list: list, grouping: data.KanjiGrouping, config: types.SimpleNamespace) -> str:
    total_count = 0
    overall_count_known = 0
    grouping_count_known = 0
    grouping_unique_characters = set("".join(group.characters for group in grouping.groups))
    grouping_unique_characters_count = len(grouping_unique_characters)
    for unit in units_list:
        in_grouping = unit.value in grouping_unique_characters

        if unit.seen_cards_count != 0 or config.unseen:
            total_count += 1
            bgcolor = util.get_background_color(unit.avg_interval, config.interval, unit.seen_cards_count, config.gradientcolors, config.kanjitileunseencolor)
            if unit_counts_as_known(unit, bgcolor, config):
                overall_count_known += 1
                if in_grouping:
                    grouping_count_known += 1

    percent_known_overall = "{:.2f}".format(round(overall_count_known / (total_count if total_count > 0 else 1) * 100, 2)) + "%"
    percent_known_grouping = "{:.2f}".format(round(grouping_count_known / (grouping_unique_characters_count if grouping_unique_characters_count > 0 else 1) * 100, 2)) + "%"
    if overall_count_known == 0:
        percent_known_overall = "0%"
        percent_known_grouping = "0%"

    overall_total = "<h4>" + str(overall_count_known) + " of " + str(total_count) + " Known Overall - " + percent_known_overall + "<br>\n"
    within_grouping_total = str(grouping_count_known) + " of " + str(grouping_unique_characters_count) + " Known in Grouping - " + percent_known_grouping + "</h4>\n"
    return overall_total + within_grouping_total


def unit_counts_as_known(unit, bgcolor: str, config: types.SimpleNamespace) -> bool:
    if unit.avg_interval <= -GSM_FUTURE_EXPOSURE_MARKER:
        return False
    return unit.seen_cards_count != 0 or bgcolor not in [config.gradientcolors[0], config.kanjitileunseencolor]


def generate(mw, config: types.SimpleNamespace, units, export: bool = False) -> str:
    def unit_score(unit) -> float:
        return external_unit_score(unit, config)

    def kanjitile(char: str, bgcolor: str, seen_cards_count: int = 0, unseen_cards_count: int = 0, avg_interval: int = 0, extra_class: str = "") -> str:
        tile = ""

        context_menu_events = f" onmouseenter=\"bridgeCommand('h:{char}');\" onmouseleave=\"bridgeCommand('l:{char}');\"" if not export else ""
        escaped_char = html.escape(char, quote=True)
        class_name = "grid-item" + ((" " + extra_class) if extra_class else "")

        if config.tooltips:
            tooltip = "Character: %s" % util.safe_unicodedata_name(char)
            future_details = GSM_FUTURE_EXPOSURE_DETAILS.get(char)
            if avg_interval <= -GSM_FUTURE_EXPOSURE_MARKER or future_details:
                tooltip += " | GSM unfinished works exposure: " + str(future_details.get("count", seen_cards_count) if future_details else seen_cards_count)
                details = future_details.get("games", []) if future_details else []
                if details:
                    top_details = details[:5]
                    detail_text = "; ".join(str(row["title"]) + ": " + str(row["count"]) for row in top_details)
                    if len(details) > len(top_details):
                        detail_text += "; +" + str(len(details) - len(top_details)) + " more"
                    tooltip += " | " + detail_text
            elif avg_interval <= -GSM_ENCOUNTER_MARKER:
                tooltip += " | GSM Encounters: " + str(seen_cards_count)
            elif avg_interval <= -JITEN_INTERVAL_MARKER:
                interval = abs(avg_interval) - JITEN_INTERVAL_MARKER
                tooltip += " | Jiten Avg Interval: " + str("{:.2f}".format(interval))
            elif avg_interval < 0:
                tooltip += " | TXT Words: " + str(abs(int(avg_interval)))
            elif avg_interval:
                tooltip += " | Avg Interval: " + str("{:.2f}".format(avg_interval)) + " | Score: " + str("{:.2f}".format(util.score_adjust(avg_interval / config.interval)))
            tooltip += " | Unseen: " + str(unseen_cards_count) + " | Seen: " + str(seen_cards_count)
            tile += "\t<div class=\"%s\" data-char=\"%s\" style=\"background:%s;\" title=\"%s\"%s>" % (class_name, escaped_char, bgcolor, html.escape(tooltip, quote=True), context_menu_events)
        else:
            tile += "\t<div class=\"%s\" data-char=\"%s\" style=\"background:%s;\"%s>" % (class_name, escaped_char, bgcolor, context_menu_events)

        if config.onclickaction == "copy":
            tile += "<a style=\"cursor: pointer;\" class=\"kanji-tile\">" + char + "</a>"
        elif config.onclickaction == "browse" and not export:
            tile += "<a href=\"" + util.get_browse_command(char) + "\" \" class=\"kanji-tile\">" + char + "</a>"
        elif config.onclickaction == "search":
            tile += "<a href=\"" + util.get_search(config, char) + "\" class=\"kanji-tile\">" + char + "</a>"
        else:
            tile += "<span class=\"kanji-tile\">" + char + "</span>"

        tile += "</div>\n"

        return tile

    def study_card_ids(study_units: list) -> list:
        card_ids = []
        for unit in study_units:
            if unit.seen_cards_count == 0 and unit.unseen_cards_count > 0:
                card_ids.extend(unit.unseen_card_ids)
        return sorted(set(card_ids))

    def studybuttons(group_index: int, card_ids: list) -> str:
        if export or len(card_ids) == 0:
            return ""
        label = str(len(card_ids)) + " unseen card"
        if len(card_ids) != 1:
            label += "s"
        return (
            "<p class=\"study-actions\">"
            + "<span class=\"study-count\">" + label + "</span>"
            + "<a class=\"study-button\" href=\"" + util.get_create_study_deck_command(group_index) + "\">Create deck</a>"
            + "<a class=\"study-button\" href=\"" + util.get_study_command(group_index) + "\">Study now</a>"
            + "</p>\n"
        )

    deckname = "*"
    if getattr(config, "usetextsource", False):
        deckname = os.path.basename(getattr(config, "textsourcepath", "")) or "TXT word list"
    elif config.did != "*":
        deckname = mw.col.decks.name(config.did).rsplit("::", 1)[-1]

    result_html  = "<!doctype html><html lang=\"" + config.lang + "\"><head><meta charset=\"UTF-8\" /><title>Anki Kanji Grid</title>"
    result_html += "<style type=\"text/css\">" + HEADER_CSS_SNIPPET(config)
    result_html += "a, a:visited {color: " + config.kanjitextcolor + ";text-decoration: none;}"
    result_html += ".kanji-tile {color: " + config.kanjitextcolor + "}"
    result_html += "body {color: " + config.textcolor + "}"
    result_html += ".kanji"
    result_html += "</style>"
    if config.onclickaction == "copy":
        result_html += COPY_JS_SNIPPET
    if not export:
        result_html += "<style type=\"text/css\">" + SEARCH_CSS_SNIPPET + "</style>"
        result_html += "<script>" + SEARCH_JS_SNIPPET + "</script>"
        if getattr(config, "usejitenapi", False) and getattr(config, "jitenapikey", "").strip():
            result_html += "<script>" + JITEN_WORK_EXPOSURE_JS_SNIPPET + "</script>"
    result_html += "</head>\n"
    result_html += "<body>\n"
    result_html += "<div style=\"font-size: 3em;\">Kanji Grid - " + deckname + "</div>\n"
    if config.timetravel_enabled:
        date_time = datetime.datetime.fromtimestamp(config.timetravel_time / 1000, tz = datetime.timezone.utc)
        date_time_str = date_time.strftime("%d/%m/%Y %H:%M:%S")
        result_html += "<p style=\"text-align: center\">for " + date_time_str + "</p>"
    result_html += "<p style=\"text-align: center\">Key</p>"
    result_html += "<p style=\"text-align: center;\">Weak&nbsp;"

    key_css_gradient = "linear-gradient(90deg"
    gradient_key_step_count = 100
    for i in range(0, gradient_key_step_count + 1):
        key_css_gradient += "," + util.get_gradient_color_hex(i / gradient_key_step_count, config.gradientcolors)
    key_css_gradient += ")"
    result_html += "<span class=\"key\" style=\"background: " + key_css_gradient + "; width: 21em;\">&nbsp;</span>"
    result_html += "&nbsp;Strong</p></div>\n"
    if getattr(config, "usetextsource", False):
        external_key_css_gradient = "linear-gradient(90deg"
        for i in range(0, gradient_key_step_count + 1):
            external_key_css_gradient += "," + util.mute_hex_color(util.get_gradient_color_hex(i / gradient_key_step_count, config.gradientcolors))
        external_key_css_gradient += ")"
        result_html += "<p style=\"text-align: center;\">External&nbsp;<span class=\"key\" style=\"background: " + external_key_css_gradient + "; width: 21em;\">&nbsp;</span>&nbsp;Stronger</p>\n"
    if getattr(config, "usegsmapi", False) or getattr(config, "usegsmsource", False):
        gsm_key_css_gradient = "linear-gradient(90deg"
        for i in range(0, gradient_key_step_count + 1):
            gsm_key_css_gradient += "," + util.get_gradient_color_hex(i / gradient_key_step_count, ["#f0edf6", "#8a5fb5"])
        gsm_key_css_gradient += ")"
        result_html += "<p style=\"text-align: center;\">GSM encounters&nbsp;<span class=\"key\" style=\"background: " + gsm_key_css_gradient + "; width: 21em;\">&nbsp;</span>&nbsp;More encounters</p>\n"
    if getattr(config, "usegsmapi", False) and getattr(config, "usegsmfutureexposure", False):
        future_key_css_gradient = "linear-gradient(90deg"
        for i in range(0, gradient_key_step_count + 1):
            future_key_css_gradient += "," + util.get_gradient_color_hex(i / gradient_key_step_count, ["#edf7f5", "#289988"])
        future_key_css_gradient += ")"
        result_html += "<p style=\"text-align: center;\">GSM unfinished works&nbsp;<span class=\"key\" style=\"background: " + future_key_css_gradient + "; width: 21em;\">&nbsp;</span>&nbsp;More future exposure</p>\n"
    show_jiten_work_search = not export and getattr(config, "usejitenapi", False) and getattr(config, "jitenapikey", "").strip()
    show_jiten_attribution = show_jiten_work_search or (getattr(config, "usegsmapi", False) and getattr(config, "usegsmfutureexposure", False))
    if show_jiten_work_search:
        jiten_work_key_css_gradient = "linear-gradient(90deg"
        for i in range(0, gradient_key_step_count + 1):
            jiten_work_key_css_gradient += "," + util.get_gradient_color_hex(i / gradient_key_step_count, JITEN_SELECTED_WORK_GRADIENT)
        jiten_work_key_css_gradient += ")"
        result_html += "<p style=\"text-align: center;\">Selected Jiten work&nbsp;<span class=\"key\" style=\"background: " + jiten_work_key_css_gradient + "; width: 21em;\">&nbsp;</span>&nbsp;More occurrences</p>\n"
    result_html += "<hr style=\"border-style: dashed;border-color: #666;width: 100%;\">\n"
    if show_jiten_work_search:
        result_html += JITEN_WORK_EXPOSURE_HTML_SNIPPET
    elif show_jiten_attribution:
        result_html += JITEN_GSM_ATTRIBUTION_HTML_SNIPPET
    result_html += "<div style=\"text-align: center;\">\n"

    units_list = {
        util.SortOrder.NONE:      sorted(units.values(), key=lambda unit: (unit.idx, unit.seen_cards_count)),
        util.SortOrder.UNICODE:   sorted(units.values(), key=lambda unit: (util.safe_unicodedata_name(unit.value), unit.seen_cards_count)),
        util.SortOrder.SCORE:     sorted(units.values(), key=lambda unit: (unit_score(unit), unit.seen_cards_count), reverse=True),
        util.SortOrder.SEEN_CARDS_COUNT: sorted(units.values(), key=lambda unit: (unit.seen_cards_count, unit_score(unit)), reverse=True),
        util.SortOrder.UNSEEN_CARDS_COUNT:  sorted(units.values(), key=lambda unit: (unit.unseen_cards_count), reverse=True),
    }[util.SortOrder(config.sortby)]

    if config.groupby > 0:
        grouping = data.groupings[config.groupby - 1]
        kanji = [u.value for u in units_list]

        result_html += get_grouping_overall_total(units_list, grouping, config)

        for i in range(0, len(grouping.groups)):
            result_html += "<h2>" + grouping.groups[i].name + "</h2>\n"
            table = "<div class=\"grid-container\">\n"
            count_found = 0
            count_known = 0

            sorted_units = []
            if config.sortby == 0:
                sorted_units = [units[c] for c in grouping.groups[i].characters if c in kanji]
            else:
                sorted_units = [units[c] for c in kanji if c in grouping.groups[i].characters]

            for unit in sorted_units:
                if unit.seen_cards_count != 0 or config.unseen:
                    count_found += 1
                    bgcolor = util.get_background_color(unit.avg_interval, config.interval, unit.seen_cards_count, config.gradientcolors, config.kanjitileunseencolor)
                    if unit_counts_as_known(unit, bgcolor, config):
                        count_known += 1
                    table += kanjitile(unit.value, bgcolor, unit.seen_cards_count, unit.unseen_cards_count, unit.avg_interval)
            table += "</div>\n"
            block_study_card_ids = study_card_ids(sorted_units)
            total_count = len(grouping.groups[i].characters)
            if config.unseen:
                unseen_kanji = []
                count = 0
                missing_chars = [c for c in grouping.groups[i].characters if c not in kanji]
                missing_chars.sort(key=lambda c: GSM_FUTURE_EXPOSURE_DETAILS.get(c, {}).get("count", 0), reverse=True)
                for char in missing_chars:
                    count += 1
                    bgcolor = gsm_future_exposure_bgcolor(char) or config.kanjitilemissingcolor
                    unseen_kanji.append(kanjitile(char, bgcolor, extra_class="missing-kanji"))
                if count != 0:
                    table += "<details><summary>Missing kanji</summary><div class=\"grid-container\">\n"
                    for element in unseen_kanji:
                        table += element
                    table += "</div></details>\n"
            result_html += "<h4>" + str(count_found) + " of " + str(total_count) + " Found - " + "{:.2f}".format(round(count_found / (total_count if total_count > 0 else 1) * 100, 2)) + "%, " + str(count_known) + " of " + str(total_count) + " Known - " + "{:.2f}".format(round(count_known / (total_count if total_count > 0 else 1) * 100, 2)) + "%</h4>\n"
            result_html += studybuttons(i, block_study_card_ids)
            result_html += table

        chars = reduce(lambda x, y: x+y, dict(grouping.groups).values())
        result_html += "<h2>" + grouping.leftover_group + "</h2>" #label for "not in group" groups
        table = "<div class=\"grid-container\">\n"
        total_count = 0
        count_known = 0
        leftover_units = [u for u in units_list if u.value not in chars]
        for unit in leftover_units:
            if unit.seen_cards_count != 0 or config.unseen:
                total_count += 1
                bgcolor = util.get_background_color(unit.avg_interval, config.interval, unit.seen_cards_count, config.gradientcolors, config.kanjitileunseencolor)
                if unit_counts_as_known(unit, bgcolor, config):
                    count_known += 1
                table += kanjitile(unit.value, bgcolor, unit.seen_cards_count, unit.unseen_cards_count, unit.avg_interval)
        table += "</div>\n"
        result_html += "<h4>" + str(count_known) + " of " + str(total_count) + " Known - " + "{:.2f}".format(round(count_known / (total_count if total_count > 0 else 1) * 100, 2)) + "%</h4>\n"
        result_html += studybuttons(len(grouping.groups), study_card_ids(leftover_units))
        result_html += table
        result_html += "<style type=\"text/css\">.datasource{font-style:italic;font-size:0.75em;margin-top:1em;overflow-wrap:break-word;}.datasource a{color:#1034A6;}</style><span class=\"datasource\">Data source: " + ' '.join("<a href=\"{}\">{}</a>".format(w, urllib.parse.unquote(w)) if re.match("https?://", w) else w for w in grouping.source.split(' ')) + "</span>"
    else:
        table = "<div class=\"grid-container\">\n"
        total_count = 0
        count_known = 0
        for unit in units_list:
            if unit.seen_cards_count != 0 or config.unseen:
                total_count += 1
                bgcolor = util.get_background_color(unit.avg_interval, config.interval, unit.seen_cards_count, config.gradientcolors, config.kanjitileunseencolor)
                if unit_counts_as_known(unit, bgcolor, config):
                    count_known += 1
                table += kanjitile(unit.value, bgcolor, unit.seen_cards_count, unit.unseen_cards_count, unit.avg_interval)
        table += "</div>\n"

        known_percent = "{:.2f}".format(round(count_known / (total_count if total_count > 0 else 1) * 100, 2)) + "%"
        if count_known == 0:
            known_percent = "0%"
        result_html += "<h4>" + str(count_known) + " of " + str(total_count) + " Known - " + known_percent + "</h4>\n"
        result_html += studybuttons(0, study_card_ids(units_list))
        result_html += table
    result_html += "</div></body></html>\n"
    return result_html

def get_revlog(mw, cids, timetravel_time):
    # SQLITE_MAX_SQL_LENGTH is 1GB by default
    # so it could handle ~70 million ids
    # but chunk just in case
    CIDS_CHUNK = 50000
    revlog = {}
    for i in range(0, len(cids), CIDS_CHUNK):
        chunked_cids = cids[i:i+CIDS_CHUNK]
        revlog_rows = mw.col.db.all(f"""
            select cid, max(id), ivl
            from revlog 
            where id <= {timetravel_time}
            and cid in {ids2str(chunked_cids)}
            group by cid
        """)
        revlog |= {row[0]: row[2] for row in revlog_rows} # cid -> ivl

    return revlog

def timetravel(card, revlog, timetravel_time):
    if card.id not in revlog:
        # card was not reviewed during the timeframe...
        if card.id > timetravel_time:
            # ...and wasn't in deck either, so it shouldn't be counted
            return False
        # ...but was still present in deck,
        # so patch it to be of type "new" (to emulate existing behaviour)
        card.ivl = 0
        card.type = 0
    else:
        # a negative revlog ivl is in seconds, positive is in days
        revlog_ivl = revlog[card.id]
        card.ivl = revlog_ivl if revlog_ivl >= 0 else (-revlog_ivl) // (60 * 60 * 24)

    return True

def kanjigrid(mw, config: types.SimpleNamespace):
    if getattr(config, "usetextsource", False) and not getattr(config, "mixtextsource", False):
        units = textfile_grid(config)
        if getattr(config, "usegsmapi", False):
            add_gsm_api_units(units, config, anki_priority=True)
            add_gsm_future_exposure_units(units, config, anki_priority=True)
        elif getattr(config, "usegsmsource", False):
            add_gsm_csv_units(units, config, anki_priority=True)
        return units

    dids = [config.did]
    if config.did == "*":
        dids = mw.col.decks.all_ids()
    for deck_id in dids:
        for _, id_ in mw.col.decks.children(int(deck_id)):
            dids.append(id_)
    cids = []
    #mw.col.find_cards and mw.col.db.list sort differently
    #mw.col.db.list is kept due to some users being very picky about the order of kanji when using `Sort by: None`
    if len(config.searchfilter) > 0 and len(config.fieldslist) > 0 and len(dids) > 0:
        cids = mw.col.find_cards("(" + util.make_query(dids, config.fieldslist) + ") " + config.searchfilter)
    else:
        cids = mw.col.db.list("select id from cards where did in %s or odid in %s" % (ids2str(dids), ids2str(dids)))

    timetravel_enabled = config.timetravel_enabled
    timetravel_time = config.timetravel_time
    revlog = get_revlog(mw, cids, timetravel_time) if timetravel_enabled else {}

    units = {}
    notes = {}
    for i in cids:
        card = mw.col.get_card(i)
        # tradeoff between branching and mutating here vs collecting all the cards and then filtermapping 
        if timetravel_enabled and not timetravel(card, revlog, timetravel_time):
            continue # ignore card
        if card.nid not in notes:
            keys = card.note().keys()
            unit_key = set()
            matches = operator.eq
            for keyword in config.fieldslist:
                for key in keys:
                    if matches(key.lower(), keyword):
                        unit_key.update(set(card.note()[key]))
                        break
            notes[card.nid] = unit_key
        else:
            unit_key = notes[card.nid]
        if unit_key is not None:
            for ch in unit_key:
                util.add_unit_data(units, ch, i, card, config.kanjionly)
    if getattr(config, "usetextsource", False) and getattr(config, "mixtextsource", False):
        if getattr(config, "textsourcekind", "txt") == "jiten":
            add_jiten_backup_units(units, config, anki_priority=True)
        else:
            add_textfile_units(units, config, anki_priority=True)
    if getattr(config, "usegsmapi", False):
        add_gsm_api_units(units, config, anki_priority=True)
        add_gsm_future_exposure_units(units, config, anki_priority=True)
    elif getattr(config, "usegsmsource", False):
        add_gsm_csv_units(units, config, anki_priority=True)
    return units

HEADER_CSS_SNIPPET = lambda config: ("""
body {
  text-align: center;
}

.grid-container {
  display: grid;
  grid-gap: 2px;
  grid-template-columns: repeat(auto-fit, 23px);
  justify-content: center;
  """ + util.get_font_css(config) + """
}

.key {
  display: inline-block;
  width: 3em
}

.study-actions {
  margin: 0.2em 0 0.7em;
}

.study-count {
  display: inline-block;
  font-weight: 600;
  margin: 0.15em 0.4em 0.15em 0;
}

.study-button {
  border: 1px solid currentColor;
  border-radius: 4px;
  display: inline-block;
  margin: 0.15em 0.25em;
  padding: 0.25em 0.7em;
}

.jiten-work-panel {
  border: 1px solid #cfcfcf;
  border-radius: 6px;
  display: block;
  margin: 0.5em auto 1em;
  max-width: 54em;
  padding: 0.6em;
  text-align: left;
  width: calc(100% - 2em);
}

.jiten-work-panel * {
  box-sizing: border-box;
}

.jiten-work-row {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4em;
}

.jiten-work-input {
  flex: 1;
  min-width: 12em;
}

.jiten-work-results {
  margin-top: 0.45em;
}

.jiten-work-results.child-view {
  display: grid;
  gap: 0.35em 0.45em;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, var(--kg-jiten-result-width, 14em)), var(--kg-jiten-result-width, max-content)));
  justify-content: start;
}

.jiten-work-results.loading {
  opacity: 0.55;
  pointer-events: none;
}

.jiten-work-result {
  align-items: stretch;
  border: 1px solid #ddd;
  border-radius: 4px;
  cursor: pointer;
  display: flex;
  gap: 0.4em;
  margin: 0.25em 0;
  max-width: 100%;
  overflow: hidden;
  padding: 0.35em 0.5em;
  text-align: left;
  width: 100%;
}

.jiten-work-results.child-view .jiten-work-result {
  height: 100%;
  margin: 0;
  min-width: 0;
  width: var(--kg-jiten-result-width, auto);
}

.jiten-work-result:hover {
  background: #f7f7f7;
}

.jiten-work-result-title {
  display: block;
  font-weight: 600;
  overflow-wrap: anywhere;
}

.jiten-work-result-main {
  flex: 1;
  min-width: 0;
}

.jiten-work-result-meta {
  color: #666;
  display: block;
  font-size: 0.85em;
  margin-top: 0.15em;
  overflow-wrap: anywhere;
}

.jiten-work-folder {
  align-items: center;
  border: 1px solid #bbb;
  border-radius: 4px;
  cursor: pointer;
  display: flex;
  flex: 0 0 auto;
  font-size: 1.25em;
  justify-content: center;
  line-height: 1;
  min-width: 2.4em;
  padding: 0.2em 0.65em;
}

.jiten-work-toggle {
  align-items: center;
  display: inline-flex;
  gap: 0.25em;
  white-space: nowrap;
}

.jiten-work-back {
  grid-column: 1 / -1;
  margin-bottom: 0.35em;
}

.jiten-work-status {
  color: #666;
  font-size: 0.85em;
  margin-top: 0.45em;
}

.jiten-attribution {
  color: #666;
  font-size: 0.8em;
  margin-top: 0.35em;
}

.jiten-attribution a {
  color: #1034A6;
}
""").strip()

SEARCH_CSS_SNIPPET = """
.grid-item.highlight {
  background: black !important; /* override item's inline interval colour */
}

.grid-item.highlight > * {
  color: white !important; /* override inline style */
}

.blink {
  animation: blink 0.2s ease-in-out;
  animation-iteration-count: 2;
}

@keyframes blink {
  0% { opacity: 1; }
  50% { opacity: 0; }
  100% { opacity: 1; }
}
""".strip()

SEARCH_JS_SNIPPET = """
function findChar(char) {
  const GRID_ITEM_CLASS = "grid-item";
  const HIGHLIGHT_CLASS = "highlight";
  const ANIM_CLASS = "blink";

  /* clear the previous match's highlight (if any) */
  const prevMatchingElem = document.querySelector(`.${HIGHLIGHT_CLASS}`);
  if (prevMatchingElem !== null) {
    prevMatchingElem.classList.remove(HIGHLIGHT_CLASS);
  }

  /* selects the first matching grid item, so it assumes the grid kanji are unique */
  /* according to mdn, more specific xpath exprs are faster, esp. on larger grids */
  const xpath = `.//div[contains(@class, '${GRID_ITEM_CLASS}')][*[.='${char}']]`;
  const matchingElement = document.evaluate(xpath, document.body, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;

  if (matchingElement === null) {
    return false;
  }

  /* we need to open the enclosing <details> block first (if any), or scrollIntoView won't work */
  const parentDetailsElem = matchingElement.closest('details');
  if (parentDetailsElem !== null) {
    parentDetailsElem.open = true;
  }

  /* add our own highlight style to the current match */
  matchingElement.classList.add(HIGHLIGHT_CLASS);

  /* scroll to match */
  matchingElement.scrollIntoView({ behavior: "smooth", block: "center" });

  /* blink anim with cleanup */
  matchingElement.classList.add(ANIM_CLASS);
  matchingElement.addEventListener("animationend", function() {
    matchingElement.classList.remove(ANIM_CLASS);
  }, { once: true });

  /* ret value indicates whether a match was found */
  return true;
}
""".strip()

JITEN_WORK_EXPOSURE_HTML_SNIPPET = """
<div class="jiten-work-panel">
  <div class="jiten-work-row">
    <input id="kg-jiten-query" class="jiten-work-input" type="search" placeholder="Search Jiten work" />
    <button type="button" onclick="kgSearchJitenWork()">Search</button>
    <label class="jiten-work-toggle" title="Keep multiple selected works. For each kanji, show the selected work with the most occurrences.">
      <input id="kg-jiten-additive" type="checkbox" />
      Additive
    </label>
    <button type="button" onclick="kgClearJitenExposure()">Clear</button>
  </div>
  <div id="kg-jiten-results" class="jiten-work-results"></div>
  <div id="kg-jiten-status" class="jiten-work-status">Search a Jiten work to preview its occurrences on missing kanji. First use for a work can be slow while its vocabulary is cached.</div>
  <div class="jiten-attribution">Search, deck, and media vocabulary data are fetched from <a href="https://jiten.moe/">jiten.moe</a> and licensed under <a href="https://creativecommons.org/licenses/by-sa/4.0/">CC BY-SA 4.0</a>.</div>
</div>
""".strip()

JITEN_ATTRIBUTION_HTML_SNIPPET = """
<div class="jiten-attribution">Jiten media vocabulary data are fetched from <a href="https://jiten.moe/">jiten.moe</a> and licensed under <a href="https://creativecommons.org/licenses/by-sa/4.0/">CC BY-SA 4.0</a>.</div>
""".strip()

JITEN_GSM_ATTRIBUTION_HTML_SNIPPET = """
<div class="jiten-attribution">GSM unfinished-work media data are matched to Jiten media decks; the media vocabulary data are fetched from <a href="https://jiten.moe/">jiten.moe</a> and licensed under <a href="https://creativecommons.org/licenses/by-sa/4.0/">CC BY-SA 4.0</a>.</div>
""".strip()

JITEN_WORK_EXPOSURE_JS_SNIPPET = """
let kgJitenExposureOriginals = new Map();
let kgJitenLastSearchResults = null;
let kgJitenLastSearchStatus = '';
let kgJitenSearchTimer = null;
let kgJitenSearchRequestId = 0;
let kgJitenExposureTimer = null;
let kgJitenAdditiveSources = new Map();
const KG_JITEN_SEARCH_CACHE_PREFIX = 'kanjigrid.jiten.search.';

function kgSetJitenStatus(text) {
  const status = document.getElementById('kg-jiten-status');
  if (status) status.textContent = text || '';
}

function kgSetJitenResultsLoading(isLoading) {
  const container = document.getElementById('kg-jiten-results');
  if (!container) return;
  container.classList.toggle('loading', !!isLoading);
  container.setAttribute('aria-busy', isLoading ? 'true' : 'false');
}

function kgJitenSearchCacheKey(query) {
  return KG_JITEN_SEARCH_CACHE_PREFIX + query.trim().toLowerCase();
}

function kgLoadCachedJitenSearch(query) {
  try {
    const raw = window.localStorage.getItem(kgJitenSearchCacheKey(query));
    if (!raw) return false;
    const payload = JSON.parse(raw);
    if (!payload || !Array.isArray(payload.results)) return false;
    kgJitenLastSearchResults = payload.results;
    kgRenderJitenResults(payload.results, false);
    kgJitenLastSearchStatus = 'Showing cached Jiten results while refreshing...';
    kgSetJitenStatus(kgJitenLastSearchStatus);
    return true;
  } catch (error) {
    return false;
  }
}

function kgSaveCachedJitenSearch(query, results) {
  try {
    if (!query || !results || results.length === 0) return;
    window.localStorage.setItem(kgJitenSearchCacheKey(query), JSON.stringify({
      cachedAt: Date.now(),
      results,
    }));
  } catch (error) {
    /* localStorage can be disabled/full; search still works without it. */
  }
}

function kgTextMatchesJitenQuery(result, query) {
  const haystack = [
    result.title || '',
    result.subtitle || '',
    result.mediaTypeLabel || '',
  ].join(' ').toLowerCase();
  return haystack.includes(query.trim().toLowerCase());
}

function kgCachedJitenMediaMatches(query) {
  const seen = new Set();
  const matches = [];
  const normalized = query.trim().toLowerCase();
  if (normalized.length < 2) return matches;
  try {
    for (let index = 0; index < window.localStorage.length; index += 1) {
      const key = window.localStorage.key(index);
      if (!key || !key.startsWith(KG_JITEN_SEARCH_CACHE_PREFIX)) continue;
      const payload = JSON.parse(window.localStorage.getItem(key) || '{}');
      const results = Array.isArray(payload.results) ? payload.results : [];
      results.forEach((result) => {
        if (!result || !result.deckId || seen.has(result.deckId)) return;
        if (!kgTextMatchesJitenQuery(result, normalized)) return;
        seen.add(result.deckId);
        matches.push(result);
      });
    }
  } catch (error) {
    return matches;
  }
  return matches.slice(0, 20);
}

function kgScheduleJitenSearch() {
  window.clearTimeout(kgJitenSearchTimer);
  const input = document.getElementById('kg-jiten-query');
  const query = input ? input.value.trim() : '';
  if (query.length < 2) {
    kgSetJitenResultsLoading(false);
    kgSetJitenStatus('Type at least 2 characters.');
    return;
  }
  const localMatches = kgCachedJitenMediaMatches(query);
  if (localMatches.length > 0) {
    kgRenderJitenResults(localMatches, false);
    kgSetJitenStatus('Showing cached media matches while refreshing Jiten...');
  } else {
    const usedCache = kgLoadCachedJitenSearch(query);
    if (!usedCache) kgSetJitenStatus('Waiting for typing to pause...');
  }
  kgJitenSearchTimer = window.setTimeout(() => kgSearchJitenWork(), 650);
}

function kgSearchJitenWork() {
  const input = document.getElementById('kg-jiten-query');
  const query = input ? input.value.trim() : '';
  if (query.length < 2) {
    kgSetJitenStatus('Type at least 2 characters.');
    return;
  }
  kgJitenSearchRequestId += 1;
  const requestId = kgJitenSearchRequestId;
  kgSetJitenStatus('Searching Jiten...');
  kgSetJitenResultsLoading(true);
  bridgeCommand('jitensearch:' + encodeURIComponent(JSON.stringify({query, requestId})));
}

function kgJitenSearchResults(payload) {
  const requestId = payload && !Array.isArray(payload) ? payload.requestId || 0 : 0;
  if (requestId && requestId !== kgJitenSearchRequestId) return;
  const results = Array.isArray(payload) ? payload : payload.results || [];
  const query = payload && !Array.isArray(payload) ? payload.query || '' : '';
  kgSetJitenResultsLoading(false);
  kgJitenLastSearchResults = results || [];
  kgRenderJitenResults(kgJitenLastSearchResults, false);
  kgSaveCachedJitenSearch(query, results);
  kgJitenLastSearchStatus = results && results.length > 0 ? 'Choose a work to color matching missing kanji. First scan can be slow; cached works are reused.' : 'No Jiten matches found.';
  kgSetJitenStatus(kgJitenLastSearchStatus);
}

function kgResizeJitenChildGrid() {
  const container = document.getElementById('kg-jiten-results');
  if (!container || !container.classList.contains('child-view')) return;
  const buttons = Array.from(container.querySelectorAll('.jiten-work-result'));
  if (buttons.length === 0) return;
  container.style.removeProperty('--kg-jiten-result-width');
  let widest = 0;
  buttons.forEach((button) => {
    widest = Math.max(widest, button.scrollWidth);
  });
  const maxWidth = Math.max(180, container.clientWidth);
  const target = Math.min(Math.ceil(widest) + 2, maxWidth);
  container.style.setProperty('--kg-jiten-result-width', target + 'px');
}

function kgRenderJitenResults(results, showBack) {
  const container = document.getElementById('kg-jiten-results');
  if (!container) return;
  container.innerHTML = '';
  container.classList.toggle('child-view', !!showBack);
  if (!results || results.length === 0) {
    return;
  }
  if (showBack) {
    const back = document.createElement('button');
    back.type = 'button';
    back.className = 'jiten-work-back';
    back.textContent = '< Back';
    back.onclick = () => {
      kgRenderJitenResults(kgJitenLastSearchResults || [], false);
      kgSetJitenStatus(kgJitenLastSearchStatus || 'Back to previous Jiten search.');
    };
    container.appendChild(back);
  }
  results.forEach((result) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'jiten-work-result';
    const main = document.createElement('span');
    main.className = 'jiten-work-result-main';
    const title = document.createElement('span');
    title.className = 'jiten-work-result-title';
    const subtitle = result.subtitle ? ' - ' + result.subtitle : '';
    title.textContent = result.title + subtitle;
    const meta = document.createElement('span');
    meta.className = 'jiten-work-result-meta';
    const parts = [];
    if (result.mediaTypeLabel) parts.push(result.mediaTypeLabel);
    if (result.subdeckCount && result.subdeckCount > 0) parts.push(result.subdeckCount + ' sub-works');
    if (result.wordCount && result.wordCount > 0) parts.push(result.wordCount.toLocaleString() + ' words');
    if (result.uniqueKanjiCount && result.uniqueKanjiCount > 0) parts.push(result.uniqueKanjiCount.toLocaleString() + ' unique kanji');
    meta.textContent = parts.join(' - ');
    main.appendChild(title);
    if (parts.length > 0) main.appendChild(meta);
    button.appendChild(main);
    if (result.subdeckCount && result.subdeckCount > 0) {
      const folder = document.createElement('span');
      folder.className = 'jiten-work-folder';
      folder.textContent = '>';
      folder.title = 'Show sub-works';
      folder.onclick = (event) => {
        event.stopPropagation();
        kgLoadJitenChildren(result.deckId);
      };
      button.appendChild(folder);
    }
    button.onclick = () => kgLoadJitenExposure(result.deckId, result.title);
    container.appendChild(button);
  });
  if (showBack) window.setTimeout(kgResizeJitenChildGrid, 0);
}

function kgJitenSearchError(payload) {
  const requestId = payload && typeof payload === 'object' ? payload.requestId || 0 : 0;
  if (requestId && requestId !== kgJitenSearchRequestId) return;
  const message = payload && typeof payload === 'object' ? payload.error : payload;
  kgSetJitenResultsLoading(false);
  kgSetJitenStatus('Jiten search failed: ' + message);
}

function kgLoadJitenExposure(deckId, title) {
  kgSetJitenStatus('Loading Jiten vocabulary exposure in the background...');
  window.clearInterval(kgJitenExposureTimer);
  const startedAt = Date.now();
  kgJitenExposureTimer = window.setInterval(() => {
    const seconds = Math.floor((Date.now() - startedAt) / 1000);
    kgSetJitenStatus('Loading Jiten vocabulary exposure in the background... ' + seconds + 's');
  }, 1000);
  bridgeCommand('jitenexposure:' + encodeURIComponent(JSON.stringify({deckId, title})));
}

function kgLoadJitenChildren(deckId) {
  kgSetJitenStatus('Loading sub-works from Jiten...');
  bridgeCommand('jitenchildren:' + encodeURIComponent(JSON.stringify({deckId})));
}

function kgJitenChildResults(payload) {
  const children = payload.children || [];
  kgSaveCachedJitenSearch('children:' + payload.parentDeckId, children);
  kgRenderJitenResults(children, true);
  const completeness = payload.complete ? '' : ' Showing partial sub-work list.';
  kgSetJitenStatus('Sub-works for "' + payload.parentTitle + '": ' + children.length + ' of ' + payload.totalItems + '.' + completeness);
}

function kgJitenExposureStatus(message) {
  kgSetJitenStatus(message);
}

function kgJitenExposureError(message) {
  window.clearInterval(kgJitenExposureTimer);
  kgSetJitenStatus('Jiten exposure failed: ' + message);
}

function kgClearJitenExposure() {
  kgJitenAdditiveSources = new Map();
  document.querySelectorAll('.missing-kanji').forEach((tile) => {
    const original = kgJitenExposureOriginals.get(tile);
    if (original) {
      tile.style.background = original.background;
      tile.title = original.title;
    }
  });
  kgSortMissingKanjiByCounts({});
  kgSetJitenStatus('Selected Jiten work exposure cleared.');
}

function kgApplyJitenSourceToTiles(counts, colors, title) {
  let applied = 0;
  document.querySelectorAll('.missing-kanji').forEach((tile) => {
    const char = tile.dataset.char;
    if (!char || !counts[char]) return;
    if (!kgJitenExposureOriginals.has(tile)) {
      kgJitenExposureOriginals.set(tile, {background: tile.style.background, title: tile.title || ''});
    }
    tile.style.background = colors[char] || '#d9791f';
    const original = kgJitenExposureOriginals.get(tile);
    const baseTitle = original.title || ('Character: ' + char);
    tile.title = baseTitle + ' | Best Jiten work: ' + title + ' | Occurrences: ' + counts[char];
    applied += 1;
  });
  return applied;
}

function kgRecomputeAdditiveJitenExposure() {
  const bestCounts = {};
  const bestColors = {};
  const bestTitles = {};
  kgJitenAdditiveSources.forEach((source) => {
    Object.entries(source.counts || {}).forEach(([char, count]) => {
      if (!bestCounts[char] || count > bestCounts[char]) {
        bestCounts[char] = count;
        bestColors[char] = source.colors[char];
        bestTitles[char] = source.title;
      }
    });
  });
  let applied = 0;
  document.querySelectorAll('.missing-kanji').forEach((tile) => {
    const original = kgJitenExposureOriginals.get(tile);
    if (original) {
      tile.style.background = original.background;
      tile.title = original.title;
    }
  });
  document.querySelectorAll('.missing-kanji').forEach((tile) => {
    const char = tile.dataset.char;
    if (!char || !bestCounts[char]) return;
    if (!kgJitenExposureOriginals.has(tile)) {
      kgJitenExposureOriginals.set(tile, {background: tile.style.background, title: tile.title || ''});
    }
    const original = kgJitenExposureOriginals.get(tile);
    const baseTitle = original.title || ('Character: ' + char);
    tile.style.background = bestColors[char] || '#d9791f';
    tile.title = baseTitle + ' | Best Jiten work: ' + bestTitles[char] + ' | Occurrences: ' + bestCounts[char];
    applied += 1;
  });
  kgSortMissingKanjiByCounts(bestCounts);
  return applied;
}

function kgSortMissingKanjiByCounts(counts) {
  document.querySelectorAll('details .grid-container').forEach((container) => {
    const tiles = Array.from(container.children).filter((tile) => tile.classList && tile.classList.contains('missing-kanji'));
    if (tiles.length === 0) return;
    tiles.forEach((tile, index) => {
      if (!tile.dataset.kgOriginalOrder) tile.dataset.kgOriginalOrder = String(index);
    });
    tiles.sort((left, right) => {
      const leftCount = counts[left.dataset.char] || 0;
      const rightCount = counts[right.dataset.char] || 0;
      if (leftCount !== rightCount) return rightCount - leftCount;
      return Number(left.dataset.kgOriginalOrder || 0) - Number(right.dataset.kgOriginalOrder || 0);
    });
    tiles.forEach((tile) => container.appendChild(tile));
  });
}

function kgApplyJitenExposure(payload) {
  window.clearInterval(kgJitenExposureTimer);
  const counts = payload.counts || {};
  const colors = payload.colors || {};
  const additive = !!document.getElementById('kg-jiten-additive')?.checked;
  let applied = 0;
  if (additive) {
    kgJitenAdditiveSources.set(String(payload.deckId), {title: payload.title, counts, colors});
    applied = kgRecomputeAdditiveJitenExposure();
  } else {
    kgClearJitenExposure();
    applied = kgApplyJitenSourceToTiles(counts, colors, payload.title);
    kgSortMissingKanjiByCounts(counts);
  }
  const completeness = payload.complete ? '' : ' Partial cache.';
  const cacheText = payload.cacheHit ? ' Used cache.' : ' Cached for next time.';
  const additiveText = additive ? ' Additive selection: ' + kgJitenAdditiveSources.size + ' works.' : '';
  kgSetJitenStatus('Applied "' + payload.title + '" to ' + applied + ' missing kanji.' + completeness + cacheText + additiveText);
}

document.addEventListener('DOMContentLoaded', () => {
  const input = document.getElementById('kg-jiten-query');
  if (input) {
    input.addEventListener('input', () => kgScheduleJitenSearch());
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        window.clearTimeout(kgJitenSearchTimer);
        kgSearchJitenWork();
      }
    });
  }
  window.addEventListener('resize', () => kgResizeJitenChildGrid());
});
""".strip()

COPY_JS_SNIPPET = """
<script>
    function copyText(text) {
        const range = document.createRange();
        const tempElem = document.createElement('div');
        tempElem.textContent = text;
        document.body.appendChild(tempElem);
        range.selectNode(tempElem);
        const selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
        document.execCommand('copy');
        document.body.removeChild(tempElem);
    }
    document.addEventListener('click', function(e) {
        if (e.srcElement.tagName == 'A') {
            e.preventDefault();
            copyText(e.srcElement.textContent);
        }
    }, false);
</script>
""".strip()
