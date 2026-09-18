"""Acceptance tests for recall.

The property worth guarding hardest is that closing an item never touches the
item: the captured corpus is the owner's record of his own thinking, and a tool
that rewrites it in place would be the opposite of what this repository is for.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hai_mcp.config import Config
from hai_mcp.recall import (
    RecallError,
    collect,
    context_terms,
    load_resolutions,
    resolve,
    resolutions_dir,
    review,
)

DAY = 86400.0


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


@pytest.fixture()
def cfg(tmp_path: Path) -> Config:
    """An isolated HAI_HOME shaped like the owner's real one."""
    home = tmp_path / "hai"
    for sub in ("intake", "parking", "distillations", "resolutions"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    return Config(hai_home=home)


def put(cfg: Config, folder: str, name: str, data: dict) -> Path:
    path = cfg.hai_home / folder / f"{name}.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def intake(cfg: Config, ident: str, raw: str, *, days_ago: float = 10.0) -> str:
    put(cfg, "intake", ident, {"intake_id": ident, "raw": raw, "captured_at": iso(time.time() - days_ago * DAY)})
    return ident


def distillation(cfg: Config, ident: str, *, intake_id: str, next_step: str, days_ago: float = 10.0) -> str:
    put(
        cfg,
        "distillations",
        ident,
        {
            "distill_id": ident,
            "intake_id": intake_id,
            "decision": "irgendein Beschluss",
            "next_step": next_step,
            "distilled_at": iso(time.time() - days_ago * DAY),
        },
    )
    return ident


def parked(cfg: Config, ident: str, idea: str, *, days_ago: float = 40.0, status: str = "parked") -> str:
    put(
        cfg,
        "parking",
        ident,
        {
            "parking_id": ident,
            "idea": idea,
            "rationale": "weil es sonst verloren geht",
            "status": status,
            "parked_at": iso(time.time() - days_ago * DAY),
        },
    )
    return ident


I1 = "I-20260807T224318-b7371814"
I2 = "I-20260917T025017-dbb90c02"
A1 = "A-20260809T124122-0860c7c2"
P1 = "P-20260807T230047-44c72f6d"
M1 = "M-20260917T204141-b89b6f11"


# --- the three backlogs ------------------------------------------------------


def test_intake_without_distillation_is_untriaged(cfg: Config) -> None:
    intake(cfg, I1, "ein nie angeschauter Gedanke")
    items = collect(cfg)
    assert [i.backlog for i in items] == ["intake_untriaged"]
    assert items[0].item_id == I1


def test_distilled_intake_drops_out_of_the_backlog(cfg: Config) -> None:
    intake(cfg, I1, "wurde triagiert")
    distillation(cfg, A1, intake_id=I1, next_step="etwas tun")
    backlogs = {i.backlog for i in collect(cfg)}
    assert "intake_untriaged" not in backlogs
    assert "next_step_open" in backlogs


def test_next_step_becomes_its_own_open_item(cfg: Config) -> None:
    intake(cfg, I1, "x")
    distillation(cfg, A1, intake_id=I1, next_step="Regressionsevidenz liefern")
    item = next(i for i in collect(cfg) if i.backlog == "next_step_open")
    assert "Regressionsevidenz" in item.summary


def test_distillation_without_next_step_is_not_an_item(cfg: Config) -> None:
    put(cfg, "distillations", A1, {"distill_id": A1, "intake_id": I1, "next_step": "  "})
    assert collect(cfg) == []


def test_parked_item_is_open(cfg: Config) -> None:
    parked(cfg, P1, "Lease-Durchsetzung fail-closed bauen")
    assert [i.backlog for i in collect(cfg)] == ["parking_open"]


def test_non_parked_status_is_not_collected(cfg: Config) -> None:
    parked(cfg, P1, "schon erledigt", status="done")
    assert collect(cfg) == []


# --- ranking -----------------------------------------------------------------


def test_relevance_to_the_active_context_wins_over_age(cfg: Config) -> None:
    (cfg.hai_home / "ACTIVE_CONTEXT.json").write_text(
        json.dumps(
            {
                "focus_id": "brueggen-v1",
                "active": [{"id": "brueggen-v1", "label": "Brueggen Fragebogen", "project_path": "/x/BRUEGGEN"}],
            }
        ),
        encoding="utf-8",
    )
    parked(cfg, P1, "etwas ganz anderes ueber Vektoren", days_ago=90)
    parked(cfg, "P-20260828T212924-8728a80e", "Brueggen Fragebogen nachfassen", days_ago=2)
    items = collect(cfg)
    assert items[0].summary.startswith("Brueggen"), "Wiedervorlage, nicht Archiv"
    assert items[0].relevance > 0


def test_oldest_first_when_nothing_matches_the_context(cfg: Config) -> None:
    parked(cfg, P1, "alt", days_ago=90)
    parked(cfg, "P-20260828T212924-8728a80e", "neu", days_ago=2)
    assert collect(cfg)[0].item_id == P1


def test_context_terms_are_empty_without_active_context(cfg: Config) -> None:
    assert context_terms(cfg) == set()


# --- closing never edits the item -------------------------------------------


def test_resolution_does_not_touch_the_captured_item(cfg: Config) -> None:
    path = cfg.hai_home / "parking" / f"{P1}.json"
    parked(cfg, P1, "unveraendert bleiben")
    before = path.read_bytes()
    resolve(cfg, item_id=P1, outcome="dropped", rationale="nicht mehr relevant")
    assert path.read_bytes() == before, "der Bestand darf nie umgeschrieben werden"


def test_resolved_item_leaves_the_backlog(cfg: Config) -> None:
    parked(cfg, P1, "wird geschlossen")
    resolve(cfg, item_id=P1, outcome="resolved", rationale="erledigt")
    assert collect(cfg) == []


def test_deleting_the_resolution_reopens_the_item(cfg: Config) -> None:
    parked(cfg, P1, "wird versehentlich geschlossen")
    record = resolve(cfg, item_id=P1, outcome="dropped", rationale="Irrtum")
    (resolutions_dir(cfg) / f"{record['resolution_id']}.json").unlink()
    assert [i.item_id for i in collect(cfg)] == [P1], "eine Fehlentscheidung muss rueckholbar sein"


# --- fail-closed -------------------------------------------------------------


def test_rationale_is_required(cfg: Config) -> None:
    parked(cfg, P1, "x")
    with pytest.raises(RecallError) as excinfo:
        resolve(cfg, item_id=P1, outcome="dropped", rationale="   ")
    assert excinfo.value.code == "invalid_args"


def test_unknown_outcome_is_refused(cfg: Config) -> None:
    parked(cfg, P1, "x")
    with pytest.raises(RecallError):
        resolve(cfg, item_id=P1, outcome="erledigt-irgendwie", rationale="weil")


def test_unknown_item_is_refused(cfg: Config) -> None:
    with pytest.raises(RecallError) as excinfo:
        resolve(cfg, item_id=P1, outcome="resolved", rationale="weil")
    assert excinfo.value.code == "unknown_item"


def test_malformed_id_is_refused(cfg: Config) -> None:
    with pytest.raises(RecallError) as excinfo:
        resolve(cfg, item_id="../../etc/passwd", outcome="resolved", rationale="weil")
    assert excinfo.value.code == "invalid_args"


def test_second_resolution_is_refused(cfg: Config) -> None:
    parked(cfg, P1, "x")
    resolve(cfg, item_id=P1, outcome="resolved", rationale="erst")
    with pytest.raises(RecallError) as excinfo:
        resolve(cfg, item_id=P1, outcome="dropped", rationale="dann")
    assert excinfo.value.code == "already_resolved"


def test_promoted_requires_the_mission_it_became(cfg: Config) -> None:
    parked(cfg, P1, "x")
    with pytest.raises(RecallError):
        resolve(cfg, item_id=P1, outcome="promoted", rationale="wurde eine Mission")
    record = resolve(cfg, item_id=P1, outcome="promoted", rationale="wurde eine Mission", mission_id=M1)
    assert record["mission_id"] == M1


# --- the chain ---------------------------------------------------------------


def test_resolutions_are_chained(cfg: Config) -> None:
    parked(cfg, P1, "eins")
    parked(cfg, "P-20260828T212924-8728a80e", "zwei")
    first = resolve(cfg, item_id=P1, outcome="dropped", rationale="a")
    second = resolve(cfg, item_id="P-20260828T212924-8728a80e", outcome="dropped", rationale="b")
    assert first["seq"] == 1 and first["prev_hash"] is None
    assert second["seq"] == 2
    assert second["prev_hash"] == first["record_hash"]
    assert second["prev_resolution_id"] == first["resolution_id"]


def test_head_tracks_the_latest_record(cfg: Config) -> None:
    parked(cfg, P1, "x")
    record = resolve(cfg, item_id=P1, outcome="resolved", rationale="fertig")
    head = json.loads((resolutions_dir(cfg) / "HEAD.json").read_text(encoding="utf-8"))
    assert head["seq"] == record["seq"]
    assert head["record_hash"] == record["record_hash"]


# --- report ------------------------------------------------------------------


def test_review_caps_what_it_shows(cfg: Config) -> None:
    for n in range(7):
        parked(cfg, f"P-2026080{n}T230047-44c72f6{n}", f"posten {n}", days_ago=10 + n)
    report = review(cfg, limit=3)
    backlog = next(b for b in report["backlogs"] if b["backlog"] == "parking_open")
    assert backlog["open"] == 7
    assert len(backlog["shown"]) == 3, "ein Befehl, der 67 Posten vorlegt, wird ignoriert"


def test_review_is_json_serialisable_and_empty_is_fine(cfg: Config) -> None:
    report = review(cfg)
    json.dumps(report)
    assert report["open_total"] == 0
    assert [b["backlog"] for b in report["backlogs"]] == [
        "next_step_open",
        "parking_open",
        "intake_untriaged",
    ]
